"""
PDF to DXF — PyMuPDF version
Dùng page.get_drawings() để lấy đầy đủ: color, fill, dashes, linewidth, path type
"""
import os, uuid, re, tempfile, math
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

OUTPUT_DIR = Path("tmp/outputs")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
job_results = {}

app = FastAPI(title="PDF to DXF PyMuPDF")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

VERSION_MAP = {
    "R12":"AC1009","R2000":"AC1015","R2004":"AC1018",
    "R2007":"AC1021","R2010":"AC1024","R2013":"AC1027","R2018":"AC1032",
}

# ── Color → Layer mapping ─────────────────────────────────
# PDF color là tuple RGB float (0.0–1.0)
# Map sang AutoCAD color index + layer name

def rgb_to_aci(r, g, b):
    """Chuyển RGB float → AutoCAD Color Index (ACI) gần nhất"""
    # Một số màu chuẩn hay gặp trong bản vẽ kết cấu
    r8, g8, b8 = int(r*255), int(g*255), int(b*255)

    # Black / near-black → white in CAD (color 7)
    if r8 < 30 and g8 < 30 and b8 < 30:
        return 7
    # White → white (7)
    if r8 > 225 and g8 > 225 and b8 > 225:
        return 7
    # Red family → 1
    if r8 > 180 and g8 < 80 and b8 < 80:
        return 1
    # Yellow → 2
    if r8 > 180 and g8 > 180 and b8 < 80:
        return 2
    # Green → 3
    if r8 < 80 and g8 > 150 and b8 < 80:
        return 3
    # Cyan → 4
    if r8 < 80 and g8 > 150 and b8 > 150:
        return 4
    # Blue → 5
    if r8 < 80 and g8 < 80 and b8 > 150:
        return 5
    # Magenta/Purple → 6
    if r8 > 150 and g8 < 80 and b8 > 150:
        return 6
    # Gray → 8
    if abs(r8-g8) < 30 and abs(g8-b8) < 30 and r8 > 30:
        return 8
    return 7

def color_to_layer(stroke_rgb, fill_rgb, lw_mm, is_dashed):
    """
    Tạo tên layer từ màu + linewidth.
    Ưu tiên: màu gốc → linewidth → dash
    """
    if is_dashed:
        if stroke_rgb:
            r,g,b = stroke_rgb
            return f"DASHED_{int(r*255):02X}{int(g*255):02X}{int(b*255):02X}"
        return "DASHED"

    if stroke_rgb:
        r,g,b = stroke_rgb
        # Snap về màu chuẩn
        if r < 0.1 and g < 0.1 and b < 0.1:
            color_name = "BLACK"
        elif r > 0.9 and g > 0.9 and b > 0.9:
            color_name = "WHITE"
        elif r > 0.7 and g < 0.3 and b < 0.3:
            color_name = "RED"
        elif r < 0.3 and g > 0.7 and b < 0.3:
            color_name = "GREEN"
        elif r < 0.3 and g < 0.3 and b > 0.7:
            color_name = "BLUE"
        elif r > 0.7 and g > 0.7 and b < 0.3:
            color_name = "YELLOW"
        elif r < 0.3 and g > 0.7 and b > 0.7:
            color_name = "CYAN"
        elif r > 0.7 and g < 0.3 and b > 0.7:
            color_name = "MAGENTA"
        elif abs(r-g) < 0.1 and abs(g-b) < 0.1:
            color_name = "GRAY"
        else:
            color_name = f"C{int(r*255):02X}{int(g*255):02X}{int(b*255):02X}"
        return color_name

    return "GEOMETRY"

def ensure_layer(doc, name, layer_set, aci_color=7, is_dashed=False):
    safe = re.sub(r'[<>/\\:?"*|=;,\s]', "_", str(name))[:255] or "0"
    if safe not in layer_set:
        try:
            lyr = doc.layers.new(name=safe)
            lyr.color = aci_color
            if is_dashed:
                try:
                    if "DASHED" not in doc.linetypes:
                        doc.linetypes.new("DASHED", dxfattribs={
                            "description": "Dashed",
                            "pattern_length": 0.75,
                            "pattern": [0.5, -0.25]
                        })
                    lyr.linetype = "DASHED"
                except Exception:
                    pass
        except Exception:
            safe = "0"
    layer_set.add(safe)
    return safe


def pts_are_circle(pts_2d):
    """Detect circle từ list điểm 2D"""
    if len(pts_2d) < 6:
        return False, 0, 0, 0
    cx = sum(p[0] for p in pts_2d) / len(pts_2d)
    cy = sum(p[1] for p in pts_2d) / len(pts_2d)
    radii = [math.sqrt((p[0]-cx)**2 + (p[1]-cy)**2) for p in pts_2d]
    r_avg = sum(radii) / len(radii)
    if r_avg < 0.001:
        return False, 0, 0, 0
    variance = sum((r-r_avg)**2 for r in radii) / len(radii)
    cv = math.sqrt(variance) / r_avg
    return (cv < 0.04), cx, cy, r_avg


def do_convert(pdf_bytes, filename, version, scale, units, opts=None):
    import fitz  # PyMuPDF
    import ezdxf

    if opts is None:
        opts = {}
    include_text  = opts.get("include_text", False)
    include_hatch = opts.get("include_hatch", True)

    dxf_version = VERSION_MAP.get(version, "AC1024")
    doc = ezdxf.new(dxf_version)
    msp = doc.modelspace()

    # Set DXF units header (4=mm, 1=inch, 6=m)
    unit_code = {"mm": 4, "cm": 5, "m": 6, "inch": 1}.get(units, 4)
    doc.header["$INSUNITS"] = unit_code
    doc.header["$LUNITS"] = 2  # decimal

    # PyMuPDF unit = points (1/72 inch)
    UNIT_MULT = {"mm":25.4/72, "cm":2.54/72, "m":0.0254/72, "inch":1.0/72}
    mult = UNIT_MULT.get(units, 25.4/72) / scale

    entities = 0
    layers = {"0"}

    # Pre-create standard layers
    standard = {
        "BLACK":   7,  "WHITE":  7,  "RED":    1,
        "GREEN":   3,  "BLUE":   5,  "YELLOW": 2,
        "CYAN":    4,  "MAGENTA":6,  "GRAY":   8,
        "DASHED":  4,  "TEXT":   2,  "HATCH":  8,
        "GEOMETRY":7,
    }
    for lname, aci in standard.items():
        ensure_layer(doc, lname, layers, aci, lname.startswith("DASHED"))

    # Thêm linetype DASHDOT cho center line
    try:
        if "DASHDOT" not in doc.linetypes:
            doc.linetypes.new("DASHDOT", dxfattribs={
                "description": "Dash dot",
                "pattern_length": 1.4,
                "pattern": [1.0, -0.2, 0.0, -0.2]
            })
    except Exception:
        pass

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        pdf_doc = fitz.open(tmp_path)
        y_offset = 0.0
        PAGE_GAP = 50.0

        for page_num in range(len(pdf_doc)):
            page = pdf_doc[page_num]
            # Drawings và text luôn dùng mediabox coordinates
            # rect đã apply rotation nhưng coordinates bên trong vẫn theo mediabox gốc
            rotation = page.rotation
            mb = page.mediabox  # kích thước gốc, chưa rotate
            mb_w = mb.width     # 1684
            mb_h = mb.height    # 2384

            # Output size theo rotation
            if rotation in (90, 270):
                pw = mb_h  # landscape width
                ph = mb_w  # landscape height
            else:
                pw = mb_w
                ph = mb_h

            ph_u = ph * mult
            pw_u = pw * mult
            cur_offset = y_offset

            # Transform coordinate tùy rotation
            # rotation=90: x_dxf = py_pdf, y_dxf = mb_w - px_pdf
            # rotation=270: x_dxf = mb_h - py_pdf, y_dxf = px_pdf
            # rotation=180: x_dxf = mb_w - px_pdf, y_dxf = mb_h - py_pdf
            # rotation=0: x_dxf = px_pdf, y_dxf = mb_h - py_pdf (flip Y only)
            def to_dxf(px, py, _o=cur_offset, _rot=rotation, _mw=mb_w, _mh=mb_h):
                # Drawings/text dùng mediabox coordinates (không phải page.rect)
                # mb_w=1684, mb_h=2384 với rotation=90
                # Flip Y: y_dxf = mb_h - py
                # rotation=90: swap x↔y để output landscape
                if _rot == 90:
                    x = _mw - py
                    y = _mh - px
                elif _rot == 270:
                    x = py
                    y = px
                elif _rot == 180:
                    x = _mw - px
                    y = py
                else:
                    x = px
                    y = _mh - py
                return (x * mult, _o + y * mult)

            # Border page
            msp.add_lwpolyline([
                (0, cur_offset), (pw_u, cur_offset),
                (pw_u, cur_offset + ph_u), (0, cur_offset + ph_u),
                (0, cur_offset)
            ], dxfattribs={"layer": "BLACK", "closed": True})

            # ── Extract drawings (geometry) ───────────────
            drawings = page.get_drawings()

            for path in drawings:
                stroke_color = path.get("color")    # RGB tuple hoặc None
                fill_color   = path.get("fill")     # RGB tuple hoặc None
                lw_pt        = path.get("width", 0) or 0
                lw_mm        = lw_pt * 0.3528
                dashes       = path.get("dashes")   # string dash pattern

                is_dashed = bool(dashes and dashes.strip() not in ("", "[]", "[0]"))

                layer_name = color_to_layer(stroke_color, fill_color, lw_mm, is_dashed)
                aci = rgb_to_aci(*(stroke_color if stroke_color else (0,0,0)))
                layer_name = ensure_layer(doc, layer_name, layers, aci, is_dashed)

                # ── Collect tất cả điểm trong path để detect circle ──
                items = path.get("items", [])

                # Thử detect circle từ toàn bộ path trước
                all_pts = []
                for item in items:
                    if item[0] == "c":
                        p1,p2,p3,p4 = item[1],item[2],item[3],item[4]
                        for t_i in range(5):
                            t = t_i/4
                            mt = 1-t
                            x = mt**3*p1.x+3*mt**2*t*p2.x+3*mt*t**2*p3.x+t**3*p4.x
                            y = mt**3*p1.y+3*mt**2*t*p2.y+3*mt*t**2*p3.y+t**3*p4.y
                            all_pts.append(to_dxf(x,y))
                    elif item[0] == "l":
                        all_pts.append(to_dxf(item[1].x, item[1].y))
                        all_pts.append(to_dxf(item[2].x, item[2].y))

                if all_pts:
                    is_circ, cx, cy, r = pts_are_circle(all_pts)
                    if is_circ and r > 0.3:
                        msp.add_circle(center=(cx,cy), radius=r,
                                       dxfattribs={"layer": layer_name})
                        entities += 1
                        continue  # skip item-by-item processing

                # ── Process items từng cái ────────────────
                for item in items:
                    itype = item[0]

                    if itype == "l":
                        p1, p2 = item[1], item[2]
                        msp.add_line(
                            to_dxf(p1.x, p1.y),
                            to_dxf(p2.x, p2.y),
                            dxfattribs={"layer": layer_name}
                        )
                        entities += 1

                    elif itype == "re":
                        rect = item[1]
                        # Chỉ vẽ nếu có stroke (color và width)
                        # Fill-only paths (color=None) bỏ qua hoàn toàn
                        if stroke_color is None:
                            continue
                        corners = [
                            to_dxf(rect.x0, rect.y0),
                            to_dxf(rect.x1, rect.y0),
                            to_dxf(rect.x1, rect.y1),
                            to_dxf(rect.x0, rect.y1),
                        ]
                        xs = [c[0] for c in corners]
                        ys = [c[1] for c in corners]
                        xmin,xmax = min(xs),max(xs)
                        ymin,ymax = min(ys),max(ys)
                        msp.add_lwpolyline(
                            [(xmin,ymin),(xmax,ymin),(xmax,ymax),(xmin,ymax)],
                            dxfattribs={"layer": layer_name, "closed": True}
                        )
                        entities += 1

                    elif itype == "c":
                        # Bezier đơn lẻ → append vào curve_pts để build polyline sau
                        pass  # handled below

                    elif itype == "qu":
                        # Quad object: dùng index [0..3] thay vì .x/.y
                        quad = item[1]
                        try:
                            pts = [to_dxf(quad[i].x, quad[i].y) for i in range(4)]
                        except (AttributeError, TypeError):
                            # Fallback: item[1..4] là Points trực tiếp
                            pts = [to_dxf(item[i].x, item[i].y) for i in range(1,5)]
                        msp.add_lwpolyline(pts, dxfattribs={"layer": layer_name, "closed": True})
                        entities += 1

                # Build polylines chỉ từ bezier curve (c) segments
                # Lines (l) đã được vẽ riêng lẻ ở trên
                curve_pts = []
                prev_c_end = None
                for item in items:
                    if item[0] != "c":
                        # Non-curve item → kết thúc segment hiện tại
                        if len(curve_pts) >= 2:
                            msp.add_lwpolyline(curve_pts, dxfattribs={"layer": layer_name})
                            entities += 1
                        curve_pts = []
                        prev_c_end = None
                        continue
                    p1,p2,p3,p4 = item[1],item[2],item[3],item[4]
                    # Nếu điểm đầu không nối tiếp → segment mới
                    if prev_c_end and (abs(p1.x-prev_c_end[0])>1 or abs(p1.y-prev_c_end[1])>1):
                        if len(curve_pts) >= 2:
                            msp.add_lwpolyline(curve_pts, dxfattribs={"layer": layer_name})
                            entities += 1
                        curve_pts = []
                    n = 8
                    start_i = 0 if not curve_pts else 1
                    for t_i in range(start_i, n+1):
                        t = t_i/n
                        mt = 1-t
                        x = mt**3*p1.x+3*mt**2*t*p2.x+3*mt*t**2*p3.x+t**3*p4.x
                        y = mt**3*p1.y+3*mt**2*t*p2.y+3*mt*t**2*p3.y+t**3*p4.y
                        curve_pts.append(to_dxf(x,y))
                    prev_c_end = (p4.x, p4.y)
                if len(curve_pts) >= 2:
                    msp.add_lwpolyline(curve_pts, dxfattribs={"layer": layer_name})
                    entities += 1

            # Text extraction disabled by default — geometry only
            # Enable bằng cách tick "Include text layer" trong UI
            if include_text:
                try:
                    _written = set()
                    for _block in page.get_text("dict").get("blocks", []):
                        if _block.get("type") != 0: continue
                        for _line in _block.get("lines", []):
                            _dir = _line.get("dir", (1,0))
                            _ang = __import__("math").degrees(
                                __import__("math").atan2(_dir[1], _dir[0]))
                            _dxf_ang = (_ang + rotation) % 360
                            # Normalize: DXF text angle >180° đọc ngược
                            if 180 < _dxf_ang <= 360:
                                _dxf_ang -= 180
                            for _span in _line.get("spans", []):
                                _txt = _span.get("text","").strip()
                                if not _txt or len(_txt) < 1: continue
                                _sz = _span.get("size", 8)
                                if _sz < 2: continue
                                _bbox = _span.get("bbox")
                                if not _bbox: continue
                                # Dùng origin (baseline start) nếu có, fallback bbox
                                _ox = _span.get("origin")
                                if _ox:
                                    _ix, _iy = float(_ox[0]), float(_ox[1])
                                else:
                                    _ix, _iy = float(_bbox[0]), float(_bbox[3])
                                # Dedup bằng bbox chính xác - loại duplicate hoàn toàn
                                _bbox_key = tuple(round(v, 1) for v in _bbox)
                                if _bbox_key in _written: continue
                                _written.add(_bbox_key)
                                _tx, _ty = to_dxf(_ix, _iy)
                                # Normalize angle: DXF text > 180° bị đọc ngược
                                # → trừ 180° để giữ hướng đọc tự nhiên
                                if _dxf_ang > 180.5:
                                    _dxf_ang = _dxf_ang - 180
                                _fh = max(0.5, _sz * mult)
                                # Layer theo màu text
                                _color_int = _span.get("color", 0)
                                _r = (_color_int >> 16) & 0xFF
                                _g = (_color_int >> 8) & 0xFF
                                _b = _color_int & 0xFF
                                _txt_layer = ensure_layer(doc, "TEXT", layers, 7)
                                if _r == 0 and _g == 0 and _b == 0:
                                    _txt_layer = ensure_layer(doc, "TEXT_BLACK", layers, 7)
                                elif _b > 200 and _r < 50:
                                    _txt_layer = ensure_layer(doc, "TEXT_BLUE", layers, 5)
                                elif _r > 200 and _g < 50:
                                    _txt_layer = ensure_layer(doc, "TEXT_RED", layers, 1)
                                _attribs = {"layer": _txt_layer, "height": _fh, "insert": (_tx, _ty)}
                                if _dxf_ang > 0.5:
                                    _attribs["rotation"] = _dxf_ang
                                msp.add_text(_txt, dxfattribs=_attribs)
                                entities += 1
                except Exception:
                    pass

            y_offset += ph_u + PAGE_GAP

        pdf_doc.close()

    finally:
        os.unlink(tmp_path)

    stem = Path(filename).stem
    job_id = str(uuid.uuid4())
    out_name = f"{job_id}_{stem}.dxf"
    doc.saveas(str(OUTPUT_DIR / out_name))

    return {
        "job_id":   job_id,
        "filename": f"{stem}.dxf",
        "out_name": out_name,
        "entities": entities,
        "layers":   len(layers),
    }


@app.get("/health")
def health():
    return {"status": "ok", "version": "pymupdf", "time": datetime.utcnow().isoformat()}

@app.post("/convert")
async def convert(
    files: list[UploadFile] = File(...),
    version: str = Form(default="R2010"),
    scale:   str = Form(default="1"),
    units:   str = Form(default="mm"),
    include_text:  str = Form(default="0"),
    include_hatch: str = Form(default="1"),
):
    if not files:
        raise HTTPException(400, "No files")
    try:
        scale_f = max(0.001, float(scale))
    except:
        scale_f = 1.0
    f = files[0]
    if not f.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Not a PDF")
    file_content = await f.read()
    if len(file_content) > 200 * 1024 * 1024:
        raise HTTPException(413, "File too large")
    opts = {
        "include_text":  include_text == "1",
        "include_hatch": include_hatch == "1",
    }
    try:
        result = do_convert(file_content, f.filename, version, scale_f, units, opts)
    except Exception as e:
        import traceback
        raise HTTPException(500, f"Conversion failed: {e}\n{traceback.format_exc()}")
    job_results[result["job_id"]] = result
    return {"job_id": result["job_id"], "files": 1}

@app.post("/detect-scale")
async def detect_scale(files: list[UploadFile] = File(...)):
    """Auto-detect scale từ dimension text dùng PyMuPDF"""
    import fitz, re as _re, math as _math
    if not files:
        raise HTTPException(400, "No files")
    content = await files[0].read()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        pdf = fitz.open(tmp_path)
        page = pdf[0]
        ph = page.rect.height
        PT_TO_MM = 25.4 / 72

        # Extract lines và text từ page đầu
        lines_data = []
        for path in page.get_drawings():
            for item in path.get("items", []):
                if item[0] == "l":
                    p1, p2 = item[1], item[2]
                    length_mm = _math.sqrt((p2.x-p1.x)**2 + (p2.y-p1.y)**2) * PT_TO_MM
                    if length_mm > 2:
                        mx = (p1.x + p2.x) / 2
                        my = (p1.y + p2.y) / 2
                        lines_data.append({"mx": mx, "my": my, "length_mm": length_mm})

        DIM_RE = _re.compile(r'^(\d{2,5}(?:[.,]\d{1,2})?)$')
        texts_data = []
        blocks = page.get_text("dict")
        for block in blocks.get("blocks", []):
            if block.get("type") != 0: continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    txt = span.get("text","").strip()
                    m = DIM_RE.match(txt)
                    if m:
                        val = float(m.group(1).replace(",","."))
                        if 50 <= val <= 50000:
                            bbox = span.get("bbox", [0,0,0,0])
                            texts_data.append({
                                "val": val,
                                "x": (bbox[0]+bbox[2])/2,
                                "y": (bbox[1]+bbox[3])/2,
                            })

        candidates = []
        for t in texts_data:
            for l in lines_data:
                dist = _math.sqrt((l["mx"]-t["x"])**2 + (l["my"]-t["y"])**2)
                if dist < 50 and l["length_mm"] > 0.5:
                    ratio = t["val"] / l["length_mm"]
                    if 0.5 <= ratio <= 5000:
                        candidates.append(ratio)

        pdf.close()
        if not candidates:
            return {"scale": 1.0, "detected": False, "confidence": "none"}

        candidates.sort()
        median = candidates[len(candidates)//2]
        STANDARD = [1,2,5,10,20,25,50,100,200,250,500,1000,2500,5000]
        snapped = min(STANDARD, key=lambda s: abs(s-median))
        if abs(snapped-median)/median < 0.30:
            final = float(snapped)
            conf = "high"
        else:
            final = median
            conf = "medium"

        return {
            "scale": final,
            "detected": True,
            "confidence": conf,
            "label": f"1:{int(final)}" if final >= 1 else f"{1/final:.0f}:1"
        }
    except Exception as e:
        return {"scale": 1.0, "detected": False, "confidence": "none", "error": str(e)}
    finally:
        os.unlink(tmp_path)

@app.get("/status/{job_id}")
def status(job_id: str):
    if job_id in job_results:
        r = job_results[job_id]
        return {"status":"done","job_id":job_id,
                "filename":r["filename"],"entities":r["entities"],"layers":r["layers"]}
    return {"status":"error","message":"Job not found"}

@app.get("/download/{job_id}")
def download(job_id: str):
    if job_id not in job_results:
        raise HTTPException(404, "Job not found")
    r = job_results[job_id]
    out_path = OUTPUT_DIR / r["out_name"]
    if not out_path.exists():
        raise HTTPException(404, "File expired")
    return FileResponse(str(out_path), media_type="application/dxf", filename=r["filename"])

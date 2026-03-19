"""
PDF to DXF — PyMuPDF version
Dùng page.get_drawings() để lấy đầy đủ: color, fill, dashes, linewidth, path type
"""
import os, uuid, re, tempfile, math, sqlite3, secrets, string, json, requests
from pathlib import Path
from datetime import datetime, date
import vtracer, cv2, numpy as np
from fastapi import FastAPI, File, UploadFile, HTTPException, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
OUTPUT_DIR = Path("tmp/outputs")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
job_results = {}
# ── Database setup ─────────────────────────────────────────
DATA_DIR = os.environ.get("DATA_DIR", "data")
DB_PATH = Path(DATA_DIR) / "usage.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            ip TEXT NOT NULL,
            day TEXT NOT NULL,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (ip, day)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pro_keys (
            key TEXT PRIMARY KEY,
            email TEXT,
            label TEXT,
            plan TEXT DEFAULT 'monthly',
            created TEXT,
            expires_at TEXT,
            active INTEGER DEFAULT 1
        )
    """)
    # Migration: add missing columns if upgrading from old schema
    try: conn.execute("ALTER TABLE pro_keys ADD COLUMN email TEXT")
    except: pass
    try: conn.execute("ALTER TABLE pro_keys ADD COLUMN plan TEXT DEFAULT 'monthly'")
    except: pass
    try: conn.execute("ALTER TABLE pro_keys ADD COLUMN expires_at TEXT")
    except: pass
    conn.commit()
    return conn
FREE_LIMIT = 5  # lượt free mỗi ngày
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "changeme123")  # set trong Railway env
def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host
def check_usage(ip: str, pro_key: str = "") -> dict:
    """Kiểm tra usage. Return {allowed, remaining, is_pro}"""
    # Check pro key trước
    if pro_key:
        conn = get_db()
        row = conn.execute(
            "SELECT active, expires_at FROM pro_keys WHERE key=?", (pro_key.strip(),)
        ).fetchone()
        conn.close()
        if row and row[0] == 1:
            # Check expiry
            if row[1]:  # có expires_at
                from datetime import date as _date
                if row[1] < _date.today().isoformat():
                    return {"allowed": False, "remaining": 0, "is_pro": False, "error": "Pro key expired. Please renew."}
            return {"allowed": True, "remaining": 999, "is_pro": True}
        else:
            return {"allowed": False, "remaining": 0, "is_pro": False, "error": "Invalid or inactive pro key"}
    # Check IP limit
    today = date.today().isoformat()
    conn = get_db()
    row = conn.execute(
        "SELECT count FROM usage WHERE ip=? AND day=?", (ip, today)
    ).fetchone()
    count = row[0] if row else 0
    conn.close()
    remaining = max(0, FREE_LIMIT - count)
    return {
        "allowed": count < FREE_LIMIT,
        "remaining": remaining,
        "is_pro": False,
        "used": count
    }
def increment_usage(ip: str):
    today = date.today().isoformat()
    conn = get_db()
    conn.execute("""
        INSERT INTO usage (ip, day, count) VALUES (?, ?, 1)
        ON CONFLICT(ip, day) DO UPDATE SET count = count + 1
    """, (ip, today))
    conn.commit()
    conn.close()
def generate_pro_key(email: str = "", label: str = "", plan: str = "monthly") -> dict:
    from datetime import date, timedelta
    chars = string.ascii_uppercase + string.digits
    key = "PRO-" + "".join(secrets.choice(chars) for _ in range(20))
    
    # Tính expiry
    today = date.today()
    if plan == "yearly":
        expires_at = (today.replace(year=today.year + 1)).isoformat()
    elif plan == "lifetime":
        expires_at = None  # không hết hạn
    else:  # monthly
        expires_at = (today + timedelta(days=30)).isoformat()
    
    # Revoke key cũ của cùng email (nếu có)
    conn = get_db()
    if email:
        conn.execute("UPDATE pro_keys SET active=0 WHERE email=? AND active=1", (email,))
    conn.execute(
        "INSERT INTO pro_keys (key, email, label, plan, created, expires_at) VALUES (?,?,?,?,?,?)",
        (key, email, label, plan, datetime.utcnow().isoformat(), expires_at)
    )
    conn.commit()
    conn.close()
    return {"key": key, "email": email, "plan": plan, "expires_at": expires_at}
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
    cv = math.sqrt(variance) / r_avg
    return (cv < 0.04), cx, cy, r_avg

def get_dxf_lineweight(lw_pts: float) -> int:
    """Chuyển đổi lineweight từ points (PDF) sang DXF lineweight (1/100 mm)"""
    lw_mm = lw_pts * 0.3528
    buckets = [0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211]
    lw_100mm = round(lw_mm * 100)
    return min(buckets, key=lambda x: abs(x - lw_100mm))

def do_convert(pdf_bytes, filename, version, scale, units, opts=None):
    import fitz  # PyMuPDF
    import ezdxf
    if opts is None:
        opts = {}
    include_text  = opts.get("include_text", False)
    include_hatch = opts.get("include_hatch", True)
    dxf_version = VERSION_MAP.get(version, "AC1024")
    doc = ezdxf.new(dxf_version)
    if "Arial" not in doc.styles:
        try:
            doc.styles.new("Arial", dxfattribs={"font": "arial.ttf"})
        except Exception:
            pass
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
                dxf_lw       = get_dxf_lineweight(lw_pt)
                dashes       = path.get("dashes")   # string dash pattern
                is_dashed = bool(dashes and dashes.strip() not in ("", "[]", "[0]"))
                layer_name = color_to_layer(stroke_color, fill_color, lw_mm, is_dashed)
                aci = rgb_to_aci(*(stroke_color if stroke_color else (0,0,0)))
                layer_name = ensure_layer(doc, layer_name, layers, aci, is_dashed)
                # ── Collect tất cả điểm trong path để detect circle ──
                items = path.get("items", [])
                # Detect circle: chỉ từ bezier curves (c), không dùng lines (l)
                # Rectangle có 4 corners đều cách đều center → false positive nếu dùng l
                all_pts = []
                has_curves = any(it[0] == "c" for it in items)
                has_lines  = any(it[0] == "l" for it in items)
                if has_curves and not has_lines:
                    # Pure bezier path → có thể là circle
                    for item in items:
                        if item[0] == "c":
                            p1,p2,p3,p4 = item[1],item[2],item[3],item[4]
                            for t_i in range(5):
                                t = t_i/4
                                mt = 1-t
                                x = mt**3*p1.x+3*mt**2*t*p2.x+3*mt*t**2*p3.x+t**3*p4.x
                                y = mt**3*p1.y+3*mt**2*t*p2.y+3*mt*t**2*p3.y+t**3*p4.y
                                all_pts.append(to_dxf(x,y))
                if all_pts:
                    is_circ, cx, cy, r = pts_are_circle(all_pts)
                    if is_circ and r > 0.3:
                        msp.add_circle(center=(cx,cy), radius=r,
                                       dxfattribs={"layer": layer_name, "lineweight": dxf_lw})
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
                            dxfattribs={"layer": layer_name, "lineweight": dxf_lw}
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
                            dxfattribs={"layer": layer_name, "closed": True, "lineweight": dxf_lw}
                        )
                        entities += 1
                    elif itype == "c":
                        # Bezier đơn lẻ → append vào curve_pts để build polyline sau
                        pass  # handled below
                    elif itype == "qu":
                        # Quad object: dùng index [0..3] thay vì .x/.y
                        # Check stroke_color in PyMuPDF
                        if stroke_color is None:
                            continue
                        quad = item[1]
                        try:
                            # PyMuPDF quad points order: ul, ur, ll, lr -> cần theo thứ tự viền 0, 1, 3, 2 để tránh 2 đường chéo X
                            pts = [to_dxf(quad[i].x, quad[i].y) for i in (0, 1, 3, 2)]
                        except (AttributeError, TypeError):
                            # Fallback: item[1..4] là Points trực tiếp
                            pts = [to_dxf(item[i].x, item[i].y) for i in (1, 2, 4, 3)]
                        msp.add_lwpolyline(pts, dxfattribs={"layer": layer_name, "closed": True, "lineweight": dxf_lw})
                        entities += 1
                # Build polylines chỉ từ bezier curve (c) segments
                # Lines (l) đã được vẽ riêng lẻ ở trên
                curve_pts = []
                prev_c_end = None
                for item in items:
                    if item[0] != "c":
                        # Non-curve item → kết thúc segment hiện tại
                        if len(curve_pts) >= 2:
                            msp.add_lwpolyline(curve_pts, dxfattribs={"layer": layer_name, "lineweight": dxf_lw})
                            entities += 1
                        curve_pts = []
                        prev_c_end = None
                        continue
                    p1,p2,p3,p4 = item[1],item[2],item[3],item[4]
                    # Nếu điểm đầu không nối tiếp → segment mới
                    if prev_c_end and (abs(p1.x-prev_c_end[0])>1 or abs(p1.y-prev_c_end[1])>1):
                        if len(curve_pts) >= 2:
                            msp.add_lwpolyline(curve_pts, dxfattribs={"layer": layer_name, "lineweight": dxf_lw})
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
                    msp.add_lwpolyline(curve_pts, dxfattribs={"layer": layer_name, "lineweight": dxf_lw})
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
                            _ang = math.degrees(
                                math.atan2(_dir[1], _dir[0]))
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
                                _attribs = {"layer": _txt_layer, "height": _fh, "insert": (_tx, _ty), "style": "Arial", "width": 0.85}
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
# ── CNC-grade Image Processing Helpers ──────────────────────
COLOR_PRESETS = {
    'red':     [(np.array([0,50,50]),   np.array([15,255,255])),
                (np.array([160,50,50]), np.array([180,255,255]))],
    'black':   [(np.array([0,0,0]),     np.array([180,255,50]))],
    'white':   [(np.array([0,0,200]),   np.array([180,30,255]))],
    'blue':    [(np.array([100,80,80]), np.array([130,255,255]))],
    'green':   [(np.array([40,80,80]),  np.array([85,255,255]))],
    'nonwhite': None,
}

def build_mask_cnc(img, color='nonwhite', min_component=300):
    """Xử lý ảnh bằng OpenCV: Lọc màu, khử nhiễu (CNC v2 logic)"""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    
    if color == 'nonwhite':
        _, mask = cv2.threshold(gray, 210, 255, cv2.THRESH_BINARY_INV)
    elif color in COLOR_PRESETS and COLOR_PRESETS[color]:
        ranges = COLOR_PRESETS[color]
        mask = cv2.inRange(hsv, ranges[0][0], ranges[0][1])
        for lo, hi in ranges[1:]:
            mask = cv2.bitwise_or(mask, cv2.inRange(hsv, lo, hi))
    else:
        # Fallback to simple threshold
        _, mask = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY_INV)

    # Khử nhiễu morphology và loại bỏ các đốm nhỏ
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3,3), np.uint8))
    num, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    clean = np.zeros_like(mask)
    for i in range(1, num):
        if stats[i, cv2.CC_STAT_AREA] >= min_component:
            clean[labels == i] = 255
    return clean

def bezier_flatten(p0, p1, p2, p3, n=16):
    """Làm phẳng đường cong Bézier thành chuỗi điểm (CNC v2 logic)"""
    res = []
    for k in range(0, n + 1):
        t = k / n; mt = 1 - t
        x = mt**3*p0[0] + 3*mt**2*t*p1[0] + 3*mt*t**2*p2[0] + t**3*p3[0]
        y = mt**3*p0[1] + 3*mt**2*t*p1[1] + 3*mt*t**2*p2[1] + t**3*p3[1]
        res.append((x, y))
    return res

def vectorize_opencv(mask, mult, img_h):
    """Vectorize binary mask using OpenCV contours (Robust fallback)"""
    contours, _ = cv2.findContours(mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    all_paths = []
    for cnt in contours:
        # Làm mượt đường nét (epsilon tùy chỉnh độ chi tiết)
        epsilon = 0.001 * cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, epsilon, True)
        pts = []
        for p in approx:
            x_px, y_px = p[0]
            pts.append((x_px * mult, (img_h - y_px) * mult))
        if len(pts) >= 2:
            all_paths.append((pts, True)) # Contours are closed
    return all_paths

def parse_vector_svg(svg_content, mult, img_h=1000):
    """Bộ giải mã SVG nâng cao hỗ trợ Bézier và lặp lệnh (CNC v2 logic)"""
    import re as _re
    # Tìm transform từ potrace/vtracer nếu có (mặc định vtracer ít dùng transform phức tạp)
    all_paths = []
    path_data_list = _re.findall(r'\bd="([^"]+)"', svg_content)
    
    for pd in path_data_list:
        tokens = _re.findall(r'[MmCcLlHhVvSsZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', pd)
        pts = []; cx, cy = 0.0, 0.0; sx_start, sy_start = 0.0, 0.0
        cmd = None; i = 0
        
        while i < len(tokens):
            tok = tokens[i]
            if tok[0].isalpha():
                cmd = tok; i += 1; continue
            
            # Xử lý các lệnh M, L, C, S, H, V...
            if cmd in ('M', 'm'):
                x, y = float(tokens[i]), float(tokens[i+1]); i += 2
                if cmd == 'm': x += cx; y += cy
                cx, cy = x, y; sx_start, sy_start = cx, cy
                pts.append((cx * mult, (img_h - cy) * mult))
                cmd = 'L' if cmd == 'M' else 'l' # Implicit repeat
            elif cmd in ('L', 'l'):
                x, y = float(tokens[i]), float(tokens[i+1]); i += 2
                if cmd == 'l': x += cx; y += cy
                cx, cy = x, y; pts.append((cx * mult, (img_h - cy) * mult))
            elif cmd in ('C', 'c'):
                v = [float(tokens[i+j]) for j in range(6)]; i += 6
                if cmd == 'c':
                    p1 = (cx+v[0], cy+v[1]); p2 = (cx+v[2], cy+v[3]); p3 = (cx+v[4], cy+v[5])
                else:
                    p1 = (v[0], v[1]); p2 = (v[2], v[3]); p3 = (v[4], v[5])
                curve_pts = bezier_flatten((cx, cy), p1, p2, p3)
                for cpt in curve_pts[1:]:
                    pts.append((cpt[0] * mult, (img_h - cpt[1]) * mult))
                cx, cy = p3
            elif cmd in ('Z', 'z'):
                if len(pts) >= 2: all_paths.append((pts[:], True))
                pts = []; cx, cy = sx_start, sy_start; i += 1; cmd = None
            else:
                i += 1 # Bỏ qua các tokens không hiểu
        if len(pts) >= 2: all_paths.append((pts, False))
    return all_paths

def do_convert_image(img_bytes, filename, version, scale, units):
    import ezdxf
    import vtracer
    import tempfile
    import os
    import xml.dom.minidom as minidom

    # 1. Decode image and apply CNC preprocessing
    nparr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Cannot decode image")
    
    H, W = img.shape[:2]
    mask = build_mask_cnc(img, color='nonwhite')
    
    # 2. Setup DXF
    dxf_version = VERSION_MAP.get(version, "AC1024")
    doc = ezdxf.new(dxf_version)
    msp = doc.modelspace()
    
    # Scaling factor
    UNIT_MULT = {"mm": 1.0, "cm": 0.1, "m": 0.001, "inch": 1/25.4}
    mult = (UNIT_MULT.get(units, 1.0) / scale) # pixel-to-unit scale

    # 3. Vectorize with OpenCV (Robust & Dependency-free)
    try:
        paths = vectorize_opencv(mask, mult, img_h=H)
        entities = 0
        for pts, closed in paths:
            msp.add_lwpolyline(pts, close=closed, dxfattribs={"layer": "IMAGE_VECTORS"})
            entities += 1

        stem = Path(filename).stem
        job_id = str(uuid.uuid4())
        out_name = f"{job_id}_{stem}.dxf"
        doc.saveas(str(OUTPUT_DIR / out_name))
        
        return {
            "job_id":   job_id,
            "filename": f"{stem}.dxf",
            "out_name": out_name,
            "entities": entities,
            "layers":   1,
        }
    except Exception as e:
        print(f"DEBUG: OpenCV Vectorization FAILED: {e}")
        raise
    finally:
        pass

@app.get("/health")
def health():
    return {"status": "ok", "version": "pymupdf", "time": datetime.utcnow().isoformat()}
@app.get("/usage")
def check_usage_endpoint(request: Request, pro_key: str = ""):
    ip = get_client_ip(request)
    return check_usage(ip, pro_key)
@app.post("/convert")
async def convert(
    request: Request,
    files: list[UploadFile] = File(...),
    version: str = Form(default="R2010"),
    scale:   str = Form(default="1"),
    units:   str = Form(default="mm"),
    include_text:  str = Form(default="0"),
    include_hatch: str = Form(default="1"),
    pro_key: str = Form(default=""),
):
    if not files:
        raise HTTPException(400, "No files")
    # Check rate limit
    ip = get_client_ip(request)
    usage = check_usage(ip, pro_key)
    if not usage["allowed"]:
        if usage.get("error"):
            raise HTTPException(403, usage["error"])
        raise HTTPException(429, f"Daily limit reached ({FREE_LIMIT} conversions/day). Upgrade to Pro for unlimited access.")
    try:
        scale_f = max(0.001, float(scale))
    except:
        scale_f = 1.0
    f = files[0]
    ext = f.filename.lower().split('.')[-1]
    if ext not in ["pdf", "jpg", "jpeg", "png", "webp"]:
        raise HTTPException(400, "Only PDF and Image files (JPG, PNG, WEBP) are supported.")
    file_content = await f.read()
    if len(file_content) > 200 * 1024 * 1024:
        raise HTTPException(413, "File too large")
    opts = {
        "include_text":  include_text == "1",
        "include_hatch": include_hatch == "1",
    }
    try:
        if ext == "pdf":
            result = do_convert(file_content, f.filename, version, scale_f, units, opts)
        else:
            result = do_convert_image(file_content, f.filename, version, scale_f, units)
    except Exception as e:
        import traceback
        raise HTTPException(500, f"Conversion failed: {e}\n{traceback.format_exc()}")
    # Tính usage sau khi convert thành công
    if not usage["is_pro"]:
        increment_usage(ip)
    job_results[result["job_id"]] = result
    remaining_after = max(0, usage.get("remaining", FREE_LIMIT) - 1)
    return {
        "job_id": result["job_id"],
        "files": 1,
        "remaining": remaining_after if not usage["is_pro"] else 999,
        "is_pro": usage["is_pro"]
    }
# ── Admin endpoints ────────────────────────────────────────
# Email config (set trong Railway env vars)
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "noreply@pdf2dxf.io")
# ── PayPal config ──────────────────────────────────────────
PAYPAL_CLIENT_ID     = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_MODE          = os.environ.get("PAYPAL_MODE", "sandbox") # 'sandbox' or 'live'

PAYPAL_API_BASE = "https://api-m.sandbox.paypal.com" if PAYPAL_MODE == "sandbox" else "https://api-m.paypal.com"

PRICES = {
    "monthly":  {"amount": "9.00",   "currency": "USD", "desc": "pdf2dxf Pro - 1 Month"},
    "yearly":   {"amount": "89.00",  "currency": "USD", "desc": "pdf2dxf Pro - 1 Year"},
    "lifetime": {"amount": "249.00", "currency": "USD", "desc": "pdf2dxf Pro - Lifetime"},
}

def get_paypal_access_token():
    """Lấy Access Token từ PayPal API"""
    res = requests.post(
        f"{PAYPAL_API_BASE}/v1/oauth2/token",
        auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        timeout=10
    )
    if not res.ok:
        raise Exception(f"Failed to get PayPal token: {res.text}")
    return res.json()["access_token"]
# Removed PayOS helper functions
def send_key_email(to_email: str, key: str, plan: str, expires_at: str):
    """Gửi email chứa pro key cho khách"""
    if not SMTP_HOST or not SMTP_USER:
        return False  # Email chưa config
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        plan_text = {"monthly": "1 Month", "yearly": "1 Year", "lifetime": "Lifetime"}.get(plan, plan)
        expire_text = f"Expires: {expires_at}" if expires_at else "Never expires (Lifetime)"
        
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Your pdf2dxf Pro Key"
        msg["From"] = SMTP_FROM
        msg["To"] = to_email
        
        body = f"""Hi,
Thank you for your purchase!
Your Pro Key: {key}
Plan: {plan_text}
{expire_text}
How to use:
1. Go to https://pdf2dxf.io (or our site URL)
2. Enter your key in the "Pro key" field
3. Enjoy unlimited conversions!
If you have any questions, reply to this email.
— pdf2dxf Team
"""
        msg.attach(MIMEText(body, "plain"))
        
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False
def send_contact_email(user_name: str, user_email: str, message: str) -> bool:
    """Gửi email contact form đến admin."""
    if not SMTP_HOST or not SMTP_USER:
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"New Contact Request from {user_name}"
        msg["From"] = SMTP_FROM
        # Email này gửi đến admin, nên "To" là SMTP_USER (hoặc email của chủ app)
        msg["To"] = "letusform@gmail.com"
        
        body = f"""You have a new contact request from pdf2dxf.io:

Name: {user_name}
Email: {user_email}

Message:
{message}
"""
        msg.attach(MIMEText(body, "plain"))
        
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, "letusform@gmail.com", msg.as_string())
        return True
    except Exception as e:
        print(f"Contact Email error: {e}")
        return False

CONTACT_DAILY_LIMIT = 3  # Tối đa 3 lần liên hệ mỗi IP/ngày

def check_contact_rate(ip: str) -> bool:
    """Trả True nếu IP còn quota, False nếu đã vượt giới hạn."""
    today = date.today().isoformat()
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contact_rate (
            ip TEXT NOT NULL,
            day TEXT NOT NULL,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (ip, day)
        )
    """)
    conn.commit()
    row = conn.execute(
        "SELECT count FROM contact_rate WHERE ip=? AND day=?", (ip, today)
    ).fetchone()
    count = row[0] if row else 0
    conn.close()
    return count < CONTACT_DAILY_LIMIT

def increment_contact_rate(ip: str):
    today = date.today().isoformat()
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contact_rate (
            ip TEXT NOT NULL,
            day TEXT NOT NULL,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (ip, day)
        )
    """)
    conn.execute("""
        INSERT INTO contact_rate (ip, day, count) VALUES (?, ?, 1)
        ON CONFLICT(ip, day) DO UPDATE SET count = count + 1
    """, (ip, today))
    conn.commit()
    conn.close()

@app.post("/contact")
def submit_contact(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    message: str = Form(...)
):
    if not name or not email or not message:
        raise HTTPException(400, "All fields are required")
    # Rate limiting: tối đa 3 lần/IP/ngày
    ip = get_client_ip(request)
    if not check_contact_rate(ip):
        raise HTTPException(429, f"Too many contact requests. Maximum {CONTACT_DAILY_LIMIT} per day per IP.")
    success = send_contact_email(name, email, message)
    # Tăng counter dù email có được gửi hay không (tránh brute force)
    increment_contact_rate(ip)
    if not success:
        # Trong môi trường dev có thể SMTP chưa config, vẫn trả success ở đây để test UI
        return {"status": "ok", "warning": "Email config missing, but request received"}
    return {"status": "ok"}

@app.post("/admin/create-key")
def create_key(
    secret: str = Form(...),
    email: str = Form(default=""),
    label: str = Form(default=""),
    plan: str = Form(default="monthly"),
    send_email: str = Form(default="1"),
):
    if secret != ADMIN_SECRET:
        raise HTTPException(403, "Forbidden")
    result = generate_pro_key(email=email, label=label, plan=plan)
    # Tự động gửi email nếu có email và SMTP config
    email_sent = False
    if email and send_email == "1":
        email_sent = send_key_email(email, result["key"], plan, result.get("expires_at", ""))
    return {**result, "email_sent": email_sent}
@app.get("/admin/list-keys")
def list_keys(secret: str = ""):
    if secret != ADMIN_SECRET:
        raise HTTPException(403, "Forbidden")
    conn = get_db()
    rows = conn.execute(
        "SELECT key, email, label, plan, created, expires_at, active FROM pro_keys ORDER BY created DESC"
    ).fetchall()
    conn.close()
    today = date.today().isoformat()
    result = []
    for r in rows:
        expired = bool(r[5] and r[5] < today)
        result.append({
            "key": r[0], "email": r[1] or "", "label": r[2] or "",
            "plan": r[3] or "monthly", "created": r[4],
            "expires_at": r[5], "active": r[6], "expired": expired
        })
    return {"keys": result}
@app.post("/admin/revoke-key")
def revoke_key(secret: str = Form(...), key: str = Form(...)):
    if secret != ADMIN_SECRET:
        raise HTTPException(403, "Forbidden")
    conn = get_db()
    conn.execute("UPDATE pro_keys SET active=0 WHERE key=?", (key,))
    conn.commit()
    conn.close()
    return {"revoked": key}
# ── PayPal Endpoints ───────────────────────────────────────
@app.post("/paypal/create-order")
async def paypal_create_order_endpoint(
    email: str = Form(...),
    plan: str = Form(default="monthly"),
):
    """Tạo PayPal order trên server"""
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        raise HTTPException(503, "PayPal chưa được cấu hình")
    
    if plan not in PRICES:
        raise HTTPException(400, "Invalid plan")
    
    price = PRICES[plan]
    access_token = get_paypal_access_token()
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}"
    }
    
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "amount": {
                "currency_code": price["currency"],
                "value": price["amount"]
            },
            "description": price["desc"]
        }]
    }
    
    res = requests.post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders",
        headers=headers,
        json=body,
        timeout=10
    )
    
    if not res.ok:
        raise HTTPException(500, f"PayPal Error: {res.text}")
    
    order = res.json()
    
    # Lưu metadata tạm vào memory (cho demo) hoặc Database để dùng khi capture
    # Ở đây chúng ta sẽ pass email/plan vào frontend để gửi lại khi capture
    return {"id": order["id"]}

@app.post("/paypal/capture-order/{order_id}")
async def paypal_capture_order_endpoint(
    order_id: str,
    email: str = Form(...),
    plan: str = Form(default="monthly"),
):
    """Capture PayPal order sau khi khách approve"""
    access_token = get_paypal_access_token()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}"
    }
    
    res = requests.post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}/capture",
        headers=headers,
        timeout=15
    )
    
    if not res.ok:
        raise HTTPException(500, f"Capture Error: {res.text}")
    
    capture_data = res.json()
    if capture_data.get("status") == "COMPLETED":
        # Thanh toán thành công -> tạo key và gửi email
        result = generate_pro_key(email=email, label=f"PayPal {plan}", plan=plan)
        send_key_email(email, result["key"], plan, result.get("expires_at", ""))
        return {"status": "success", "key": result["key"]}
    
    return {"status": "failed", "detail": capture_data.get("status")}

# Removed Stripe endpoints
@app.post("/webhook/paypal")
async def paypal_webhook(request: Request):
    """PayPal IPN/Webhook — tự động tạo key khi có payment"""
    try:
        body = await request.body()
        data = {}
        # Parse form data từ PayPal IPN
        for item in body.decode().split("&"):
            if "=" in item:
                k, v = item.split("=", 1)
                from urllib.parse import unquote_plus
                data[unquote_plus(k)] = unquote_plus(v)
        
        # Verify payment status
        payment_status = data.get("payment_status", "")
        if payment_status != "Completed":
            return {"status": "ignored", "reason": payment_status}
        
        # Lấy thông tin
        payer_email = data.get("payer_email", "")
        item_name   = data.get("item_name", "monthly")  # tên sản phẩm = plan
        amount      = float(data.get("mc_gross", "0"))
        
        # Xác định plan từ amount hoặc item_name
        if "year" in item_name.lower() or amount >= 20:
            plan = "yearly"
        elif "lifetime" in item_name.lower() or amount >= 49:
            plan = "lifetime"
        else:
            plan = "monthly"
        
        # Tạo key và gửi email
        result = generate_pro_key(email=payer_email, label=f"PayPal {amount}", plan=plan)
        send_key_email(payer_email, result["key"], plan, result.get("expires_at", ""))
        
        print(f"PayPal payment: {payer_email} {plan} ${amount} → {result['key']}")
        return {"status": "ok", "key_created": True}
    except Exception as e:
        print(f"PayPal webhook error: {e}")
        return {"status": "error"}
@app.get("/admin/stats")
def stats(secret: str = ""):
    if secret != ADMIN_SECRET:
        raise HTTPException(403, "Forbidden")
    conn = get_db()
    today = date.today().isoformat()
    today_total = conn.execute("SELECT SUM(count) FROM usage WHERE day=?", (today,)).fetchone()[0] or 0
    week_total  = conn.execute("SELECT SUM(count) FROM usage WHERE day >= date('now', '-7 days')").fetchone()[0] or 0
    total_keys  = conn.execute("SELECT COUNT(*) FROM pro_keys WHERE active=1").fetchone()[0]
    conn.close()
    return {"today": today_total, "week": week_total, "active_pro_keys": total_keys}
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

"""
Celery Worker — PDF to DXF Conversion Engine
Libraries: pdfminer.six (MIT license), ezdxf (MIT license), opencv-python, pytesseract
"""
import os, re, math, struct
from pathlib import Path
from celery import Celery

# ── Celery setup ──────────────────────────────────────────
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery("pdf2dxf", broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=7200,  # results expire after 2 hours
    worker_prefetch_multiplier=1,
    task_acks_late=True,
)

import os
from pathlib import Path

# Lấy từ biến môi trường, fallback sang /data/uploads và /data/output
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/data/uploads"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/data/output"))

# Tạo thư mục nếu chưa tồn tại
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── DXF version map ───────────────────────────────────────
VERSION_MAP = {
    "R12":   "AC1009",
    "R2000": "AC1015",
    "R2004": "AC1018",
    "R2007": "AC1021",
    "R2010": "AC1024",
    "R2013": "AC1027",
    "R2018": "AC1032",
}

# Lineweight buckets (mm → AutoCAD lwindex)
LW_BUCKETS = [0.09, 0.13, 0.18, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.53, 0.60, 0.70, 0.80, 0.90, 1.00]


def snap_lineweight(w_pts: float) -> float:
    """Convert PDF linewidth (pts) to nearest ISO lineweight (mm)."""
    w_mm = w_pts * 0.3528  # 1pt = 0.3528mm
    return min(LW_BUCKETS, key=lambda lw: abs(lw - w_mm))


def is_vector_pdf(pdf_path: str) -> bool:
    """Quick check: does this PDF contain vector path operators?"""
    try:
        with open(pdf_path, "rb") as f:
            chunk = f.read(65536)  # read first 64KB
        # Vector PDF will have path operators
        return b" m\n" in chunk or b" l\n" in chunk or b" re\n" in chunk or b"BT\n" in chunk
    except Exception:
        return False


# ── Main task ─────────────────────────────────────────────
@celery_app.task(bind=True, name="convert_task")
def convert_task(self, job_id: str, file_paths: list, version: str, scale: float, units: str):
    """Main conversion task. Dispatches to vector or raster pipeline."""
    import ezdxf

    dxf_version = VERSION_MAP.get(version, "AC1024")
    doc = ezdxf.new(dxf_version)
    msp = doc.modelspace()

    total_entities = 0
    layer_names = set()
    layer_names.add("0")

    for file_path in file_paths:
        self.update_state(state="PROGRESS", meta={"progress": 10, "step": f"Reading {Path(file_path).name}…"})

        if is_vector_pdf(file_path):
            entities, layers = process_vector_pdf(file_path, doc, msp, scale, units, self)
        else:
            entities, layers = process_raster_pdf(file_path, doc, msp, scale, units, self)

        total_entities += entities
        layer_names.update(layers)

    # Save DXF
    self.update_state(state="PROGRESS", meta={"progress": 92, "step": "Writing DXF file…"})
    out_name = f"{job_id}_{Path(file_paths[0]).stem}.dxf"
    out_path = OUTPUT_DIR / out_name
    doc.saveas(str(out_path))

    # Cleanup input files
    for p in file_paths:
        try:
            Path(p).unlink()
        except Exception:
            pass

    return {
        "filename": out_name.split("_", 1)[1],
        "entities": total_entities,
        "layers":   len(layer_names),
        "job_id":   job_id,
    }


# ── Vector PDF Pipeline ───────────────────────────────────
def process_vector_pdf(pdf_path: str, doc, msp, scale: float, units: str, task) -> tuple[int, set]:
    """
    Extract vector paths from PDF using pdfminer.six.
    Returns (entity_count, set_of_layer_names).
    """
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import (
        LTPage, LTLine, LTRect, LTCurve, LTChar, LTAnon,
        LTTextBox, LTTextLine, LTFigure, LTLayoutContainer
    )
    import ezdxf
    from ezdxf.enums import TextEntityAlignment

    entities = 0
    layers = set()
    task.update_state(state="PROGRESS", meta={"progress": 30, "step": "Extracting vector paths…"})

    # Unit multiplier (PDF coordinates are in points; 1pt = 1/72 inch)
    # Convert to target units
    UNIT_MULT = {"mm": 25.4/72, "cm": 2.54/72, "m": 0.0254/72, "inch": 1.0/72}
    mult = UNIT_MULT.get(units, 25.4/72) / scale

    for page_num, page_layout in enumerate(extract_pages(pdf_path)):
        page_height = page_layout.height  # for Y-flip (PDF Y is bottom-up, DXF top-down)

        def process_element(element, layer_name="0"):
            nonlocal entities

            if isinstance(element, LTLine):
                layer = ensure_layer(doc, layer_name, layers)
                lw = snap_lineweight(element.linewidth if hasattr(element, "linewidth") else 0.25)
                x1 = element.x0 * mult
                y1 = (page_height - element.y0) * mult
                x2 = element.x1 * mult
                y2 = (page_height - element.y1) * mult
                line = msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": layer})
                entities += 1

            elif isinstance(element, LTRect):
                layer = ensure_layer(doc, layer_name, layers)
                x0, y0 = element.x0 * mult, (page_height - element.y0) * mult
                x1, y1 = element.x1 * mult, (page_height - element.y1) * mult
                pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
                msp.add_lwpolyline(pts, dxfattribs={"layer": layer, "closed": True})
                entities += 1

            elif isinstance(element, LTCurve):
                # Approximate Bézier curves as polyline segments
                layer = ensure_layer(doc, layer_name, layers)
                pts = []
                prev = None
                for seg in element.pts:
                    px = seg[0] * mult
                    py = (page_height - seg[1]) * mult
                    pts.append((px, py))
                if len(pts) >= 2:
                    msp.add_lwpolyline(pts, dxfattribs={"layer": layer})
                    entities += 1

            elif isinstance(element, (LTTextBox, LTTextLine)):
                layer = ensure_layer(doc, "TEXT", layers)
                txt = element.get_text().strip()
                if not txt:
                    return
                x = element.x0 * mult
                y = (page_height - element.y0) * mult
                # Approximate font height
                font_h = (element.height * mult) if hasattr(element, "height") else 2.5
                msp.add_text(
                    txt,
                    dxfattribs={
                        "layer": layer,
                        "height": max(0.5, font_h * 0.7),
                        "insert": (x, y),
                    }
                )
                entities += 1

            elif isinstance(element, LTFigure):
                for child in element:
                    process_element(child, "FIGURE")

            elif isinstance(element, LTLayoutContainer):
                for child in element:
                    process_element(child, layer_name)

        for element in page_layout:
            process_element(element)

    task.update_state(state="PROGRESS", meta={"progress": 80, "step": f"Extracted {entities} entities…"})
    return entities, layers


# ── Raster PDF Pipeline ───────────────────────────────────
def process_raster_pdf(pdf_path: str, doc, msp, scale: float, units: str, task) -> tuple[int, set]:
    """
    Convert scanned/raster PDF to DXF via image processing.
    Pipeline: render page → grayscale → threshold → Hough lines → ezdxf lines
    Requires: Pillow, opencv-python, pytesseract (optional)
    """
    try:
        from PIL import Image
        import io
    except ImportError:
        raise RuntimeError("Pillow not installed. Run: pip install Pillow")

    try:
        import fitz  # PyMuPDF for rendering — fallback to Pillow if not available
        HAS_PYMUPDF = True
    except ImportError:
        HAS_PYMUPDF = False

    try:
        import cv2
        import numpy as np
        HAS_CV2 = True
    except ImportError:
        HAS_CV2 = False

    entities = 0
    layers = set()
    UNIT_MULT = {"mm": 25.4/72, "cm": 2.54/72, "m": 0.0254/72, "inch": 1.0/72}
    mult = UNIT_MULT.get(units, 25.4/72) / scale

    task.update_state(state="PROGRESS", meta={"progress": 20, "step": "Rendering scanned PDF…"})

    if not HAS_PYMUPDF:
        # Fallback: embed raster as a note, no conversion possible without renderer
        ensure_layer(doc, "RASTER_NOTE", layers)
        msp.add_text(
            "Raster PDF detected. Install pymupdf for full raster support.",
            dxfattribs={"layer": "RASTER_NOTE", "height": 5, "insert": (0, 0)}
        )
        return 1, layers

    pdf_doc = fitz.open(pdf_path)

    for page_num in range(len(pdf_doc)):
        page = pdf_doc[page_num]
        # Render at 300 DPI for good line detection
        mat = fitz.Matrix(300/72, 300/72)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
        img_array = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)

        task.update_state(state="PROGRESS", meta={"progress": 40, "step": f"Processing page {page_num+1}…"})

        if not HAS_CV2:
            continue

        # Preprocessing
        _, binary = cv2.threshold(img_array, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        # Denoise
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)

        task.update_state(state="PROGRESS", meta={"progress": 60, "step": "Detecting lines…"})

        # Probabilistic Hough Transform for line detection
        lines = cv2.HoughLinesP(
            binary,
            rho=1,
            theta=math.pi/180,
            threshold=50,
            minLineLength=20,
            maxLineGap=5
        )

        page_h = pix.height
        # Scale: pixels → PDF points → target units
        px_to_unit = (72.0 / 300.0) * mult

        layer = ensure_layer(doc, f"RASTER_P{page_num+1}", layers)

        if lines is not None:
            for line in lines:
                x1, y1, x2, y2 = line[0]
                ux1, uy1 = x1 * px_to_unit, (page_h - y1) * px_to_unit
                ux2, uy2 = x2 * px_to_unit, (page_h - y2) * px_to_unit
                msp.add_line((ux1, uy1), (ux2, uy2), dxfattribs={"layer": layer})
                entities += 1

        # Optional: OCR for text
        try:
            import pytesseract
            page_img = Image.fromarray(img_array)
            ocr_data = pytesseract.image_to_data(page_img, output_type=pytesseract.Output.DICT)
            text_layer = ensure_layer(doc, "OCR_TEXT", layers)
            for i, word in enumerate(ocr_data["text"]):
                word = word.strip()
                if not word or int(ocr_data["conf"][i]) < 60:
                    continue
                wx = ocr_data["left"][i] * px_to_unit
                wy = (page_h - ocr_data["top"][i]) * px_to_unit
                msp.add_text(
                    word,
                    dxfattribs={"layer": text_layer, "height": 2.5, "insert": (wx, wy)}
                )
                entities += 1
        except Exception:
            pass  # OCR is optional

    task.update_state(state="PROGRESS", meta={"progress": 88, "step": f"Raster conversion: {entities} lines…"})
    return entities, layers


# ── DXF layer helper ──────────────────────────────────────
def ensure_layer(doc, name: str, layer_set: set) -> str:
    """Create layer in DXF document if not already existing."""
    safe_name = re.sub(r'[<>/\\:?"*|=;]', "_", str(name))[:255] or "0"
    if safe_name not in layer_set:
        try:
            doc.layers.new(name=safe_name)
        except Exception:
            safe_name = "0"
    layer_set.add(safe_name)
    return safe_name

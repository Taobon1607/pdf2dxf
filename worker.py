"""
Celery Worker — PDF to DXF Conversion Engine
Fix: nhận file content qua base64 thay vì file path
"""
import os, re, math, base64, tempfile
from pathlib import Path
from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
celery_app = Celery("pdf2dxf", broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=7200,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
)

OUTPUT_DIR = Path("tmp/outputs")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

VERSION_MAP = {
    "R12":"AC1009","R2000":"AC1015","R2004":"AC1018",
    "R2007":"AC1021","R2010":"AC1024","R2013":"AC1027","R2018":"AC1032",
}

LW_BUCKETS = [0.09,0.13,0.18,0.20,0.25,0.30,0.35,0.40,0.50,0.53,0.60,0.70,0.80,0.90,1.00]

def snap_lineweight(w_pts: float) -> float:
    w_mm = w_pts * 0.3528
    return min(LW_BUCKETS, key=lambda lw: abs(lw - w_mm))

@celery_app.task(bind=True, name="convert_task")
def convert_task(self, job_id: str, file_data_list: list, version: str, scale: float, units: str):
    import ezdxf

    dxf_version = VERSION_MAP.get(version, "AC1024")
    doc = ezdxf.new(dxf_version)
    msp = doc.modelspace()

    total_entities = 0
    layer_names = set()
    layer_names.add("0")
    first_filename = "output"

    for file_data in file_data_list:
        filename = file_data["filename"]
        content = base64.b64decode(file_data["content_b64"])
        first_filename = Path(filename).stem

        self.update_state(state="PROGRESS", meta={"progress": 10, "step": f"Reading {filename}…"})

        # Ghi file tạm vào /tmp (mỗi container có /tmp riêng, OK)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            entities, layers = process_vector_pdf(tmp_path, doc, msp, scale, units, self)
            total_entities += entities
            layer_names.update(layers)
        finally:
            os.unlink(tmp_path)

    self.update_state(state="PROGRESS", meta={"progress": 92, "step": "Writing DXF…"})
    out_name = f"{job_id}_{first_filename}.dxf"
    out_path = OUTPUT_DIR / out_name
    doc.saveas(str(out_path))

    return {
        "filename": f"{first_filename}.dxf",
        "entities": total_entities,
        "layers":   len(layer_names),
        "job_id":   job_id,
    }


def process_vector_pdf(pdf_path: str, doc, msp, scale: float, units: str, task) -> tuple:
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import (LTLine, LTRect, LTCurve, LTTextBox,
                                  LTTextLine, LTFigure, LTLayoutContainer)

    entities = 0
    layers = set()
    task.update_state(state="PROGRESS", meta={"progress": 30, "step": "Extracting vector paths…"})

    UNIT_MULT = {"mm": 25.4/72, "cm": 2.54/72, "m": 0.0254/72, "inch": 1.0/72}
    mult = UNIT_MULT.get(units, 25.4/72) / scale

    for page_layout in extract_pages(pdf_path):
        page_height = page_layout.height

        def process_element(element, layer_name="0"):
            nonlocal entities

            if isinstance(element, LTLine):
                layer = ensure_layer(doc, layer_name, layers)
                x1 = element.x0 * mult
                y1 = (page_height - element.y0) * mult
                x2 = element.x1 * mult
                y2 = (page_height - element.y1) * mult
                msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": layer})
                entities += 1

            elif isinstance(element, LTRect):
                layer = ensure_layer(doc, layer_name, layers)
                x0 = element.x0 * mult
                y0 = (page_height - element.y0) * mult
                x1 = element.x1 * mult
                y1 = (page_height - element.y1) * mult
                pts = [(x0,y0),(x1,y0),(x1,y1),(x0,y1),(x0,y0)]
                msp.add_lwpolyline(pts, dxfattribs={"layer": layer, "closed": True})
                entities += 1

            elif isinstance(element, LTCurve):
                layer = ensure_layer(doc, layer_name, layers)
                pts = [(seg[0]*mult, (page_height-seg[1])*mult) for seg in element.pts]
                if len(pts) >= 2:
                    msp.add_lwpolyline(pts, dxfattribs={"layer": layer})
                    entities += 1

            elif isinstance(element, (LTTextBox, LTTextLine)):
                txt = element.get_text().strip()
                if not txt:
                    return
                layer = ensure_layer(doc, "TEXT", layers)
                x = element.x0 * mult
                y = (page_height - element.y0) * mult
                font_h = max(0.5, (element.height * mult * 0.7) if hasattr(element, "height") else 2.5)
                msp.add_text(txt, dxfattribs={"layer": layer, "height": font_h, "insert": (x, y)})
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


def ensure_layer(doc, name: str, layer_set: set) -> str:
    safe_name = re.sub(r'[<>/\\:?"*|=;]', "_", str(name))[:255] or "0"
    if safe_name not in layer_set:
        try:
            doc.layers.new(name=safe_name)
        except Exception:
            safe_name = "0"
    layer_set.add(safe_name)
    return safe_name

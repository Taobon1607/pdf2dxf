import os
import time
from pathlib import Path
from celery import Celery, current_task

# Celery config — adapt broker/backend from env
CELERY_BROKER = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

celery_app = Celery("worker", broker=CELERY_BROKER, backend=CELERY_BACKEND)
celery_app.conf.task_track_started = True

# Directories from env with sensible defaults
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/data/uploads"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/data/output"))

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def _update_progress(progress: int, step: str = ""):
    try:
        current_task.update_state(state="PROGRESS", meta={"progress": progress, "step": step})
    except Exception:
        pass

@celery_app.task(bind=True)
def convert_task(self, job_id: str, saved_paths: list, version: str, scale: float, units: str):
    """
    Placeholder conversion task.
    Replace conversion logic with actual pdf->dxf conversion using pdfminer/ezdxf.
    Must write output file to OUTPUT_DIR and return dict with filename and optional metadata.
    """
    _update_progress(5, "starting")
    time.sleep(0.5)

    # Example: iterate input PDFs and produce one DXF per input (or combine as needed)
    produced_filename = None
    try:
        for idx, input_path in enumerate(saved_paths):
            _update_progress(10 + int(40 * idx / max(1, len(saved_paths))), "processing")
            # Simulate conversion work (replace with real conversion)
            input_path = Path(input_path)
            if not input_path.exists():
                raise FileNotFoundError(f"Input file not found: {input_path}")

            # Create a dummy DXF file for demonstration; replace with real output bytes
            original_name = input_path.name
            out_name = f"{job_id}_{Path(original_name).stem}.dxf"
            out_path = OUTPUT_DIR / out_name

            # Simulate conversion output
            with open(out_path, "wb") as f:
                f.write(b"0\nSECTION\n0\nENDSEC\n0\nEOF\n")  # minimal placeholder content

            produced_filename = out_name
            _update_progress(80, "finalizing")
            time.sleep(0.5)

        _update_progress(100, "done")
        # Return metadata expected by main.status
        return {"filename": produced_filename, "entities": 0, "layers": 0}
    except Exception as e:
        # Ensure exceptions are raised so Celery marks task as FAILURE
        raise e

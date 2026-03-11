"""
PDF to DXF Converter — FastAPI Backend
Stack: FastAPI + Celery + Redis + pdfminer.six + ezdxf
"""
import os
import uuid
import time
from pathlib import Path
from typing import Optional
from datetime import datetime, date

from fastapi import FastAPI, File, UploadFile, HTTPException, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from celery.result import AsyncResult

from worker import celery_app, convert_task
from database import init_db, get_usage, increment_usage, is_pro_user

# ── Config ────────────────────────────────────────────────
# Use environment variables with sensible defaults for local dev
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/data/uploads"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/data/output"))
MAX_FREE_MB = int(os.environ.get("MAX_FREE_MB", 10))
MAX_PRO_MB = int(os.environ.get("MAX_PRO_MB", 100))
FREE_DAILY = int(os.environ.get("FREE_DAILY", 3))

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── App ───────────────────────────────────────────────────
app = FastAPI(title="PDF to DXF API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to your domain in production
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    init_db()


# ── Helpers ───────────────────────────────────────────────
def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    return forwarded.split(",")[0].strip() if forwarded else request.client.host


def check_rate_limit(ip: str, is_pro: bool) -> int:
    """Returns remaining conversions. Raises 429 if exceeded."""
    if is_pro:
        return 999
    used = get_usage(ip, str(date.today()))
    remaining = FREE_DAILY - used
    if remaining <= 0:
        raise HTTPException(
            status_code=429,
            detail=f"Free limit reached ({FREE_DAILY}/day). Upgrade to Pro for unlimited conversions."
        )
    return remaining


# ── Routes ───────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.post("/convert")
async def convert(
    request: Request,
    files: list[UploadFile] = File(...),
    version: str  = Form(default="R2010"),
    scale:   str  = Form(default="1"),
    units:   str  = Form(default="mm"),
    pro_key: Optional[str] = Form(default=None),
):
    ip = get_client_ip(request)
    is_pro = is_pro_user(pro_key) if pro_key else False

    # Validate params
    valid_versions = {"R12", "R2000", "R2004", "R2007", "R2010", "R2013", "R2018"}
    if version not in valid_versions:
        version = "R2010"

    try:
        scale_f = float(scale)
        if scale_f <= 0 or scale_f > 10000:
            scale_f = 1.0
    except (ValueError, TypeError):
        scale_f = 1.0

    valid_units = {"mm", "cm", "m", "inch"}
    if units not in valid_units:
        units = "mm"

    max_mb = MAX_PRO_MB if is_pro else MAX_FREE_MB
    check_rate_limit(ip, is_pro)

    # Accept only first file for free tier (batch for pro)
    if not is_pro:
        files = files[:1]

    job_id = str(uuid.uuid4())
    saved_paths = []

    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            raise HTTPException(400, detail=f"File '{f.filename}' is not a PDF.")
        content = await f.read()
        size_mb = len(content) / 1024 / 1024
        if size_mb > max_mb:
            raise HTTPException(
                413,
                detail=f"'{f.filename}' is {size_mb:.1f}MB. Limit is {max_mb}MB ({'Pro' if is_pro else 'Free'})."
            )
        # sanitize filename: keep basename only
        safe_name = Path(f.filename).name
        file_path = UPLOAD_DIR / f"{job_id}_{safe_name}"
        file_path.write_bytes(content)
        saved_paths.append(str(file_path))

    # Queue conversion task
    convert_task.apply_async(
        args=[job_id, saved_paths, version, scale_f, units],
        task_id=job_id
    )

    # Increment usage
    if not is_pro:
        increment_usage(ip, str(date.today()))

    return {"job_id": job_id, "files": len(saved_paths)}


@app.get("/status/{job_id}")
def status(job_id: str):
    result = AsyncResult(job_id, app=celery_app)
    state = result.state

    if state == "PENDING":
        return {"status": "pending"}
    elif state == "PROGRESS":
        info = result.info or {}
        return {"status": "processing", "progress": info.get("progress", 0), "step": info.get("step", "")}
    elif state == "SUCCESS":
        data = result.result or {}
        return {
            "status": "done",
            "job_id": job_id,
            "filename": data.get("filename"),
            "entities": data.get("entities", 0),
            "layers": data.get("layers", 0),
        }
    elif state == "FAILURE":
        return {"status": "error", "message": str(result.result)}
    else:
        return {"status": state.lower()}


@app.get("/download/{job_id}")
def download(job_id: str):
    # Security: only allow safe characters in job_id
    if not all(c in "0123456789abcdef-" for c in job_id):
        raise HTTPException(400, "Invalid job ID")

    # Find output file
    matches = list(OUTPUT_DIR.glob(f"{job_id}_*.dxf"))
    if not matches:
        raise HTTPException(404, "File not found or expired")

    file_path = matches[0]
    # Return file with original filename (strip job_id prefix)
    original_name = file_path.name.split("_", 1)[1] if "_" in file_path.name else file_path.name
    return FileResponse(
        path=str(file_path),
        media_type="application/dxf",
        filename=original_name,
    )


@app.post("/create-checkout")
async def create_checkout():
    """Stripe checkout session. Fill in STRIPE_SECRET_KEY in env."""
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe.api_key:
        raise HTTPException(500, "Stripe not configured")

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "pdf2dxf.io Pro"},
                "unit_amount": 900,
                "recurring": {"interval": "month"},
            },
            "quantity": 1,
        }],
        mode="subscription",
        success_url=os.environ.get("FRONTEND_URL", "http://localhost") + "?pro=1",
        cancel_url=os.environ.get("FRONTEND_URL", "http://localhost"),
    )
    return {"url": session.url}


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except Exception:
        raise HTTPException(400, "Invalid signature")

    if event["type"] == "customer.subscription.deleted":
        # Handle cancellation — revoke pro access
        customer_id = event["data"]["object"]["customer"]
        # TODO: update database to mark user as free
        pass

    return {"received": True}


# ── Cleanup old files (run as scheduled Celery beat task) ─
@app.delete("/internal/cleanup")
def cleanup():
    """Remove files older than 2 hours."""
    now = time.time()
    removed = 0
    for d in [UPLOAD_DIR, OUTPUT_DIR]:
        for f in d.iterdir():
            if f.is_file() and (now - f.stat().st_mtime) > 7200:
                try:
                    f.unlink()
                    removed += 1
                except Exception:
                    pass
    return {"removed": removed}

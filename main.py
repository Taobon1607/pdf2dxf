"""
PDF to DXF Converter — FastAPI Backend
Fix: truyền file content qua Celery task thay vì filesystem
"""
import os, uuid, base64
from pathlib import Path
from typing import Optional
from datetime import datetime, date

from fastapi import FastAPI, File, UploadFile, HTTPException, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from celery.result import AsyncResult
from worker import celery_app, convert_task
from database import init_db, get_usage, increment_usage, is_pro_user

OUTPUT_DIR = Path("tmp/outputs")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MAX_FREE_MB = 10
MAX_PRO_MB  = 100
FREE_DAILY  = 3

app = FastAPI(title="PDF to DXF API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    init_db()

def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    return forwarded.split(",")[0].strip() if forwarded else request.client.host

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

    valid_versions = {"R12","R2000","R2004","R2007","R2010","R2013","R2018"}
    if version not in valid_versions:
        version = "R2010"

    try:
        scale_f = float(scale)
        if scale_f <= 0 or scale_f > 10000:
            scale_f = 1.0
    except:
        scale_f = 1.0

    if units not in {"mm","cm","m","inch"}:
        units = "mm"

    max_mb = MAX_PRO_MB if is_pro else MAX_FREE_MB

    if not is_pro:
        used = get_usage(ip, str(date.today()))
        if used >= FREE_DAILY:
            raise HTTPException(429, detail=f"Free limit {FREE_DAILY}/day reached. Upgrade to Pro.")

    if not is_pro:
        files = files[:1]

    job_id = str(uuid.uuid4())
    file_data_list = []

    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            raise HTTPException(400, detail=f"'{f.filename}' is not a PDF.")
        content = await f.read()
        size_mb = len(content) / 1024 / 1024
        if size_mb > max_mb:
            raise HTTPException(413, detail=f"'{f.filename}' is {size_mb:.1f}MB. Limit: {max_mb}MB.")
        file_data_list.append({
            "filename": f.filename,
            "content_b64": base64.b64encode(content).decode("utf-8")
        })

    convert_task.apply_async(
        args=[job_id, file_data_list, version, scale_f, units],
        task_id=job_id
    )

    if not is_pro:
        increment_usage(ip, str(date.today()))

    return {"job_id": job_id, "files": len(file_data_list)}

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
        data = result.result
        return {"status": "done", "job_id": job_id,
                "filename": data.get("filename"),
                "entities": data.get("entities", 0),
                "layers": data.get("layers", 0)}
    elif state == "FAILURE":
        return {"status": "error", "message": str(result.result)}
    else:
        return {"status": state.lower()}

@app.get("/download/{job_id}")
def download(job_id: str):
    if not all(c in "0123456789abcdef-" for c in job_id):
        raise HTTPException(400, "Invalid job ID")
    matches = list(OUTPUT_DIR.glob(f"{job_id}_*.dxf"))
    if not matches:
        raise HTTPException(404, "File not found or expired")
    return FileResponse(
        path=str(matches[0]),
        media_type="application/dxf",
        filename=matches[0].name.split("_", 1)[1],
    )

@app.post("/create-checkout")
async def create_checkout():
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe.api_key:
        raise HTTPException(500, "Stripe not configured")
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price_data": {"currency": "usd",
            "product_data": {"name": "pdf2dxf.io Pro"},
            "unit_amount": 900,
            "recurring": {"interval": "month"}}, "quantity": 1}],
        mode="subscription",
        success_url=os.environ.get("FRONTEND_URL", "http://localhost") + "?pro=1",
        cancel_url=os.environ.get("FRONTEND_URL", "http://localhost"),
    )
    return {"url": session.url}

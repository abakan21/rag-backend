import os
from contextlib import asynccontextmanager
from typing import Optional, List

from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models import get_db, Source, IngestJob, StatusEnum, Evidence
from ingestion import ingest_url
from rag import query_rag, delete_job_vectors
from auth import get_current_user, UserInfo, oauth2_scheme, decode_token
from datetime import datetime, timedelta
from scheduler import start_scheduler, stop_scheduler

def _ts(dt) -> str | None:
    """Serialize datetime with Z suffix for JS compatibility."""
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"

@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    stop_scheduler()

app = FastAPI(title="Web Data Ingestion API", lifespan=lifespan)

origins = os.getenv("CORS_ORIGINS", "https://195.113.167.83,http://localhost").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_admin_user(user: UserInfo = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can perform this action")
    return user

# Опциональная авторизация — для поиска без логина
async def get_optional_user(token: str = Depends(oauth2_scheme)) -> Optional[UserInfo]:
    if not token:
        return None
    try:
        payload = decode_token(token)
        roles = payload.get("realm_access", {}).get("roles", [])
        return UserInfo(
            id=payload.get("sub", ""),
            username=payload.get("preferred_username", ""),
            role="admin" if "admin" in roles else "user",
        )
    except Exception:
        return None

# ── Auth ──────────────────────────────────────────────────────

@app.get("/api/auth/me")
def get_me(user: UserInfo = Depends(get_current_user)):
    return {"id": user.id, "username": user.username, "role": user.role}

# ── Ingestion ─────────────────────────────────────────────────

VALID_SCHEDULES = {None, "hourly", "daily", "weekly", "monthly"}

class IngestRequest(BaseModel):
    url: str
    source_name: str = "DefaultSource"
    deep_crawl: bool = False
    max_depth: int = 1
    schedule: Optional[str] = None
    permission_type: str = "public"
    strategy: Optional[str] = None

@app.post("/api/ingest")
def trigger_ingestion(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: UserInfo = Depends(get_admin_user),
):
    # Фикс дубликатов — проверяем активный job для этого URL
    # Validace schedule
    if request.schedule not in VALID_SCHEDULES:
        raise HTTPException(status_code=400, detail=f"Invalid schedule: {request.schedule}. Valid: hourly, daily, weekly, monthly")

    # Check duplicate: RUNNING job for this URL
    existing_job = db.query(IngestJob).filter(
        IngestJob.url == request.url,
        IngestJob.status == StatusEnum.RUNNING,
    ).order_by(IngestJob.id.desc()).first()
    if existing_job:
        return {
            "message": f"Ingestion already running for {request.url}",
            "source_id": existing_job.source_id,
            "status": "skipped"
        }

    source = db.query(Source).filter(Source.name == request.source_name).first()
    if not source:
        source = Source(
            name=request.source_name,
            base_url=request.url,
            permission_type=request.permission_type
        )
        db.add(source)
        db.commit()
        db.refresh(source)
    else:
        # Обновляем permission_type если изменился
        source.permission_type = request.permission_type
        db.commit()

    # Создаём job заранее чтобы ingestion мог его найти
    from models import StrategyEnum
    job = IngestJob(
        source_id=source.id,
        url=request.url,
        strategy=StrategyEnum.HTML,
        status=StatusEnum.RUNNING,
        max_depth=request.max_depth,
    )
    db.add(job)
    db.commit()

    background_tasks.add_task(
        ingest_url, request.url, source.id,
        request.deep_crawl, request.max_depth, request.strategy
    )
    return {"message": f"Ingestion started for {request.url}", "source_id": source.id, "status": "started"}

# ── Jobs ──────────────────────────────────────────────────────

def _job_to_dict(j: IngestJob, db: Session) -> dict:
    evidence_list = db.query(Evidence).filter(Evidence.job_id == j.id).all()
    screenshot_count = sum(1 for e in evidence_list if e.evidence_type == "screenshot")
    return {
        "id": j.id,
        "url": j.url,
        "status": j.status.value,
        "strategy": j.strategy.value if j.strategy else None,
        "error_code": j.error_code,
        "started_ts": _ts(j.started_ts),
        "completed_ts": _ts(j.completed_ts),
        "max_depth": j.max_depth if hasattr(j, 'max_depth') else 1,
        "has_evidence": len(evidence_list) > 0,
        "screenshot_count": screenshot_count,
    }

@app.get("/api/jobs")
def get_jobs(limit: int = 50, db: Session = Depends(get_db), admin: UserInfo = Depends(get_admin_user)):
    jobs = db.query(IngestJob).order_by(IngestJob.started_ts.desc()).limit(limit).all()
    return {"jobs": [_job_to_dict(j, db) for j in jobs]}

@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: int, db: Session = Depends(get_db), admin: UserInfo = Depends(get_admin_user)):
    job = db.query(IngestJob).filter(IngestJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    delete_job_vectors(job_id)
    db.delete(job)
    db.commit()
    return {"message": f"Job {job_id} deleted"}

@app.get("/api/jobs/{job_id}/detail")
def get_job_detail(job_id: int, db: Session = Depends(get_db), user: UserInfo = Depends(get_current_user)):
    job = db.query(IngestJob).filter(IngestJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    source = db.query(Source).filter(Source.id == job.source_id).first()
    evidences = db.query(Evidence).filter(Evidence.job_id == job_id).all()
    return {
        "id": job.id,
        "url": job.url,
        "status": job.status.value,
        "strategy": job.strategy.value if job.strategy else None,
        "error_code": job.error_code,
        "started_ts": str(job.started_ts),
        "completed_ts": str(job.completed_ts) if job.completed_ts else None,
        "max_depth": job.max_depth if hasattr(job, 'max_depth') else 1,
        "source_name": source.name if source else None,
        "evidences": [
            {"id": e.id, "type": e.evidence_type, "storage_uri": e.storage_uri,
             "file_hash": e.file_hash, "created_ts": _ts(e.created_ts)}
            for e in evidences
        ]
    }

@app.get("/api/jobs/{job_id}/files")
def get_job_files(job_id: int, db: Session = Depends(get_db), user: UserInfo = Depends(get_current_user)):
    evidences = db.query(Evidence).filter(
        Evidence.job_id == job_id,
        Evidence.evidence_type == "markdown"
    ).all()
    files = [os.path.basename(e.storage_uri) for e in evidences]
    return {"files": files}

@app.put("/api/jobs/{job_id}/resolve")
def resolve_job(job_id: int, db: Session = Depends(get_db), admin: UserInfo = Depends(get_admin_user)):
    job = db.query(IngestJob).filter(IngestJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.status = StatusEnum.COMPLETED
    job.error_code = None
    db.commit()
    return {"message": f"Job {job_id} resolved"}

# ── Sources ───────────────────────────────────────────────────

@app.get("/api/sources")
def get_sources(db: Session = Depends(get_db), user: UserInfo = Depends(get_current_user)):
    sources = db.query(Source).all()
    return {"sources": [{"id": s.id, "name": s.name, "base_url": s.base_url,
                          "permission_type": s.permission_type} for s in sources]}

@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int, db: Session = Depends(get_db), admin: UserInfo = Depends(get_admin_user)):
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    db.delete(source)
    db.commit()
    return {"message": f"Source {source_id} deleted"}

# ── Analytics ─────────────────────────────────────────────────

@app.get("/api/analytics")
def get_analytics(db: Session = Depends(get_db), user: UserInfo = Depends(get_current_user)):
    from sqlalchemy import func
    total_jobs = db.query(IngestJob).count()
    completed = db.query(IngestJob).filter(IngestJob.status == StatusEnum.COMPLETED).count()
    failed = db.query(IngestJob).filter(IngestJob.status == StatusEnum.FAILED).count()
    running = db.query(IngestJob).filter(IngestJob.status == StatusEnum.RUNNING).count()
    captcha = db.query(IngestJob).filter(IngestJob.status == StatusEnum.CAPTCHA_DETECTED).count()
    total_sources = db.query(Source).count()
    screenshots = db.query(Evidence).filter(Evidence.evidence_type == "screenshot").count()
    markdowns = db.query(Evidence).filter(Evidence.evidence_type == "markdown").count()

    strategy_rows = db.query(IngestJob.strategy, func.count(IngestJob.id)).group_by(IngestJob.strategy).all()
    strategies = {str(row[0].value if row[0] else "unknown"): row[1] for row in strategy_rows}

    recent_jobs = db.query(IngestJob).order_by(IngestJob.started_ts.desc()).limit(10).all()
    return {
        "jobs": {"total": total_jobs, "completed": completed, "failed": failed,
                 "running": running, "captcha": captcha},
        "sources": {"total": total_sources, "scheduled": 0},
        "evidences": {"total": screenshots + markdowns, "screenshots": screenshots, "markdowns": markdowns},
        "strategies": strategies,
        "recent_jobs": [
            {"id": j.id, "url": j.url, "status": j.status.value,
             "strategy": j.strategy.value if j.strategy else None,
             "started_ts": _ts(j.started_ts)}
            for j in recent_jobs
        ]
    }

# ── Files ─────────────────────────────────────────────────────

@app.get("/api/files/{filename}")
def get_file(filename: str, user: Optional[UserInfo] = Depends(get_optional_user)):
    data_dir = os.getenv("DATA_DIR", "data")
    file_path = os.path.join(data_dir, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    return {"content": content}

@app.get("/api/evidence/{evidence_id}/file")
def get_evidence_file(evidence_id: int, db: Session = Depends(get_db), user: UserInfo = Depends(get_current_user)):
    ev = db.query(Evidence).filter(Evidence.id == evidence_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evidence not found")
    from fastapi.responses import FileResponse
    if not os.path.exists(ev.storage_uri):
        raise HTTPException(status_code=404, detail="File not found on disk")
    return FileResponse(ev.storage_uri, media_type="image/png")

# ── Search ────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str
    k: int = 3

@app.post("/api/search")
async def search_rag(
    request: SearchRequest,
    user: Optional[UserInfo] = Depends(get_optional_user)  # Работает и без логина
):
    try:
        result = query_rag(request.query, k=request.k)
        return {
            "answer": result.get("answer") or "Database is empty or no relevant info found.",
            "sources": result.get("sources") or []
        }
    except Exception as e:
        return {"answer": f"Error: {str(e)}", "sources": []}

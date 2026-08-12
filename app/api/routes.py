"""API routes for immigration document translation service."""

import os
import json
import uuid
import asyncio
import re
import io
import time
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, Response
from openai import OpenAI
import httpx

from app.config import settings
from app.services.glossary import GlossaryService
from app.services.document_parser import DocumentParser
from app.services.translator import TranslatorService
from app.services import dedup_guard
from app.services import numbering_check
from app.models.schemas import (
    CustomAPIConfig,
    GlossaryUploadResponse,
    TranslateRequest,
    JobResponse,
    RevisionRequest,
)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level service singletons
# ---------------------------------------------------------------------------
glossary_service = GlossaryService()
translator_service = TranslatorService()
doc_parser = DocumentParser()

# ---------------------------------------------------------------------------
# Storage directories
# ---------------------------------------------------------------------------
UPLOAD_DIR = settings.UPLOAD_DIR
JOBS_DIR = settings.JOBS_DIR
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(JOBS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# File-backed job tracking
# ---------------------------------------------------------------------------
# Every mutation to jobs or original_filenames is written to disk so state
# survives a process restart as long as the filesystem remains intact.
jobs: dict[str, dict] = {}
original_filenames: dict[str, str] = {}


def _load_all_jobs() -> None:
    """Populate *jobs* and *original_filenames* from disk on startup."""
    fnames_path = os.path.join(JOBS_DIR, "_filenames.json")
    if os.path.exists(fnames_path):
        try:
            with open(fnames_path, "r", encoding="utf-8") as f:
                original_filenames.update(json.load(f))
        except Exception:
            pass

    for name in os.listdir(JOBS_DIR):
        if not name.endswith(".json") or name == "_filenames.json":
            continue
        job_id = name[:-5]  # strip ".json"
        try:
            with open(os.path.join(JOBS_DIR, name), "r", encoding="utf-8") as f:
                jobs[job_id] = json.load(f)
        except Exception:
            pass


def _save_job(job_id: str, data: dict) -> None:
    """Write a single job to disk (thread-safe)."""
    path = os.path.join(JOBS_DIR, f"{job_id}.json")
    with _JOB_PROGRESS_LOCK:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str)


def _delete_job(job_id: str) -> None:
    """Remove a job file from disk."""
    path = os.path.join(JOBS_DIR, f"{job_id}.json")
    if os.path.exists(path):
        os.remove(path)


def _save_filenames() -> None:
    """Persist the original_filenames mapping to disk."""
    path = os.path.join(JOBS_DIR, "_filenames.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(original_filenames, f, ensure_ascii=False, default=str)


# Load existing jobs on startup
_load_all_jobs()

# Regex for Chinese punctuation detection (used for term-only heuristic)
_CN_PUNCT_RE = re.compile(
    r"["
    r"　-〿"   # CJK symbols and punctuation
    r"＀-￯"   # Fullwidth forms
    r"‘-‟"   # Curly quotes / general punctuation
    r"　-〿"   # CJK symbols and punctuation (duplicate for clarity)
    r"＀-￯"   # Fullwidth forms (duplicate)
    r"一-鿿"   # Catch CJK Unified ideographs as well (any Chinese char means translate)
    r"]"
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/upload/glossary", response_model=GlossaryUploadResponse)
async def upload_glossary(file: UploadFile = File(...)):
    """Upload a glossary CSV or XLSX file.

    Returns a ``GlossaryUploadResponse`` with a generated glossary ID,
    term count, and original filename.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    ext = os.path.splitext(file.filename)[-1].lower()
    if ext not in (".csv", ".xlsx"):
        raise HTTPException(
            status_code=400,
            detail="Only .csv and .xlsx files are supported",
        )

    # Save the uploaded file to a temporary path
    temp_path = os.path.join(UPLOAD_DIR, f"glossary_{uuid.uuid4()}{ext}")
    content = await file.read()
    with open(temp_path, "wb") as f:
        f.write(content)

    try:
        glossary_id = glossary_service.load_glossary(temp_path, file.filename)
        term_count = glossary_service.get_term_count(glossary_id)
    except HTTPException:
        raise
    except Exception as e:
        import traceback, logging
        logging.getLogger(__name__).error(f"Glossary upload error: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=400, detail=f"术语表解析失败：{e}")
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                # Temp-file cleanup failure must not mask a successful upload.
                pass

    return GlossaryUploadResponse(
        glossary_id=glossary_id,
        term_count=term_count,
        filename=file.filename,
    )


@router.post("/upload/files")
async def upload_files(files: list[UploadFile] = File(...)):
    """Upload one or more .docx files for translation.

    Returns a plain JSON dict with a ``file_ids`` list (UUIDs assigned to
    each uploaded file).  The FileUploadResponse schema is *not* used here
    because the response shape differs (multiple file IDs).
    """
    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required")

    file_ids: list[str] = []
    for f in files:
        if not f.filename or not f.filename.lower().endswith(".docx"):
            raise HTTPException(
                status_code=400,
                detail=f"Only .docx files are supported, got: {f.filename}",
            )
        file_id = str(uuid.uuid4())
        dest = os.path.join(UPLOAD_DIR, f"{file_id}.docx")
        content = await f.read()
        with open(dest, "wb") as out:
            out.write(content)
        original_filenames[file_id] = os.path.splitext(f.filename)[0]
        file_ids.append(file_id)

    _save_filenames()
    return {"file_ids": file_ids}


@router.post("/translate", response_model=JobResponse)
async def translate(req: TranslateRequest):
    """Start an async translation job for the given files and glossary.

    Returns immediately with a ``job_id`` and ``status="processing"``.
    The actual translation runs in a background ``asyncio.Task``.
    """
    custom_api = req.custom_api.model_dump() if req.custom_api else None
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "processing",
        "file_ids": req.file_ids,
        "glossary_id": req.glossary_id,
        "custom_api": custom_api,
        "progress": {
            "total": len(req.file_ids),
            "completed": 0,
            "current_file": None,
            "stage": "starting",
            "detail": "准备就绪",
        },
    }
    _save_job(job_id, jobs[job_id])
    asyncio.create_task(
        run_translation(job_id, req.file_ids, req.glossary_id, custom_api=custom_api)
    )
    return JobResponse(job_id=job_id, status="processing")


@router.get("/status/{job_id}")
async def get_status(job_id: str):
    """Poll the status of a translation job."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job_id,
        "status": job["status"],
        "progress": job.get("progress"),
        "results": job.get("results", []),
        "error": job.get("error"),
    }


@router.get("/result/{job_id}")
async def get_result(job_id: str, file_id: str | None = None):
    """Download a translated .docx file for a job.

    If *file_id* is provided, only that file is downloaded.
    Otherwise the first completed result is returned.
    """
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail="Job not yet completed")

    results = job.get("results", [])
    if not results:
        raise HTTPException(status_code=404, detail="No results found")

    if file_id:
        results = [r for r in results if r["file_id"] == file_id]

    for entry in results:
        if entry.get("status") == "completed":
            file_path = os.path.join(UPLOAD_DIR, f"{entry['file_id']}_EN.docx")
            if os.path.exists(file_path):
                base_name = original_filenames.get(
                    entry["file_id"], entry["file_id"]
                )
                return FileResponse(
                    file_path,
                    media_type=(
                        "application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document"
                    ),
                    filename=f"{base_name}-EN.docx",
                )

    raise HTTPException(status_code=404, detail="No completed result files found")


@router.get("/download-all/{job_id}")
async def download_all(job_id: str):
    """Download all translated files as a single ZIP archive."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail="Job not yet completed")

    results = job.get("results", [])
    completed = [r for r in results if r.get("status") == "completed"]
    if not completed:
        raise HTTPException(status_code=404, detail="No completed files found")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in completed:
            file_path = os.path.join(UPLOAD_DIR, f"{entry['file_id']}_EN.docx")
            if os.path.exists(file_path):
                base_name = original_filenames.get(
                    entry["file_id"], entry["file_id"]
                )
                zf.write(file_path, f"{base_name}-EN.docx")

    buffer.seek(0)
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f"attachment; filename=translations_{job_id[:8]}.zip"
            )
        },
    )


@router.get("/quality-report/{job_id}")
async def get_quality_report(job_id: str):
    """Return a visual job-level quality report (HTML) for the front-end modal.

    Summarizes every file's translation & QA state: completion, warning
    categories, and unresolved items requiring human attention.
    """
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail="Job not yet completed")
    html = _build_job_report_html(job_id, job.get("results", []))
    return Response(content=html, media_type="text/html; charset=utf-8")


@router.post("/fix-issues")
async def fix_issues(req: dict):
    """Batch-fix issues checked in the quality report.

    Body: {"job_id": str, "file_id": str, "fix_types": ["term", "numbering",
    "fidelity", "chinese", "duplicate"]}
    Applies deterministic fixes to the translated docx, refreshes the job's
    warnings, and returns what changed. The front-end then reloads the report.
    """
    job_id = req.get("job_id")
    file_id = req.get("file_id")
    fix_types = [t for t in (req.get("fix_types") or []) if t in _CATEGORY_FIX_MAP.values()]
    if not job_id or not file_id or not fix_types:
        raise HTTPException(status_code=400, detail="job_id / file_id / fix_types 均必填")
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    glossary: dict[str, str] = {}
    gid = job.get("glossary_id") or ""
    if gid:
        try:
            glossary = glossary_service.get_glossary(gid)
        except ValueError:
            glossary = {}

    result = _apply_fixes(file_id, glossary, fix_types)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    # Refresh the job result (warnings + quality summary) so the next report
    # render reflects the fixes.
    for r in job.get("results", []):
        if r.get("file_id") == file_id:
            new_warnings = result.get("cn_warnings", [])
            r["cn_warnings"] = new_warnings
            r.pop("warning_contexts", None)
            cats: dict[str, int] = {}
            for w in new_warnings:
                cat = _categorize_warning(w)
                cats[cat] = cats.get(cat, 0) + 1
            summary = r.get("quality_summary") or {}
            summary["warning_count"] = len(new_warnings)
            summary["categories"] = cats
            r["quality_summary"] = summary
            break
    _save_job(job_id, job)

    return {"changed": result.get("changed", []), "warning_count": len(result.get("cn_warnings", []))}


@router.get("/preview/{job_id}")
async def preview_result(job_id: str, file_id: str | None = None):
    """Return the translated content with run-level formatting."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail="Job not yet completed")

    results = job.get("results", [])
    previews = []
    for entry in results:
        if entry.get("status") == "completed":
            file_path = os.path.join(UPLOAD_DIR, f"{entry['file_id']}_EN.docx")
            if os.path.exists(file_path):
                doc = doc_parser.read_document(file_path)
                paragraphs = doc_parser.extract_paragraphs(doc)
                # Remove non-serialisable 'paragraph' objects from table cells
                previews.append({
                    "file_id": entry["file_id"],
                    "paragraphs": paragraphs,
                })
            else:
                previews.append({
                    "file_id": entry["file_id"],
                    "paragraphs": None,
                })

    if file_id:
        previews = [p for p in previews if p["file_id"] == file_id]

    return {"job_id": job_id, "previews": previews}


@router.post("/revise", response_model=JobResponse)
async def revise(req: RevisionRequest):
    """Re-translate an existing job with user feedback.

    Creates a *new* job based on the original job's file list and glossary,
    and launches a background translation that includes the feedback text
    in every DeepSeek API call.
    """
    original = jobs.get(req.job_id)
    if original is None:
        raise HTTPException(status_code=404, detail="Original job not found")

    new_job_id = str(uuid.uuid4())
    file_ids = original.get("file_ids", [])
    glossary_id = original.get("glossary_id", "")
    custom_api = (
        req.custom_api.model_dump()
        if req.custom_api
        else original.get("custom_api")
    )

    jobs[new_job_id] = {
        "status": "processing",
        "file_ids": file_ids,
        "glossary_id": glossary_id,
        "custom_api": custom_api,
        "progress": {
            "total": len(file_ids),
            "completed": 0,
            "current_file": None,
            "stage": "starting",
            "detail": "准备就绪",
        },
    }

    _save_job(new_job_id, jobs[new_job_id])

    asyncio.create_task(
        run_translation_with_feedback(
            new_job_id, file_ids, req.feedback, glossary_id=glossary_id, custom_api=custom_api
        )
    )

    return JobResponse(job_id=new_job_id, status="processing")


@router.get("/glossary/{glossary_id}")
async def get_glossary(glossary_id: str):
    """Return a glossary's metadata and term mapping."""
    try:
        terms = glossary_service.get_glossary(glossary_id)
        metadata = glossary_service.get_metadata(glossary_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {"glossary_id": glossary_id, "terms": terms, **metadata}


@router.post("/test-api")
async def test_api(req: CustomAPIConfig):
    """Test whether the provided API credentials work with a minimal call."""
    try:
        client = OpenAI(
            api_key=req.api_key,
            base_url=req.base_url,
            http_client=httpx.Client(),
        )
        response = client.chat.completions.create(
            model=req.model,
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=5,
        )
        if response.choices and response.choices[0].message.content:
            return {"status": "ok", "message": "连接成功"}
        return {"status": "error", "message": "API 返回为空"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Quality report generator
# ---------------------------------------------------------------------------
_REPORT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "测试文件用",
)
os.makedirs(_REPORT_DIR, exist_ok=True)


# Shared report design system — mirrors static/index.html (warm amber editorial,
# brass accents, DM Serif Display for ceremony, Inter for work, 45° grid +
# vignette). Plain string (NOT an f-string) so CSS braces need no escaping.
_REPORT_CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
:root {
  --bg:            oklch(96% 0.008 70);
  --bg-grid:       oklch(0% 0 0 / 0.018);
  --surface:       oklch(100% 0.004 75);
  --surface-raised:oklch(99% 0.003 75);
  --brass:         oklch(55% 0.095 65);
  --brass-dark:    oklch(42% 0.10 60);
  --brass-glow:    oklch(55% 0.095 65 / 0.18);
  --brass-subtle:  oklch(55% 0.095 65 / 0.07);
  --border:        oklch(88% 0.006 70);
  --border-strong: oklch(78% 0.008 70);
  --border-accent: oklch(55% 0.095 65 / 0.25);
  --fg:            oklch(18% 0.015 60);
  --fg-secondary:  oklch(46% 0.015 65);
  --fg-tertiary:   oklch(62% 0.01 70);
  --error-bg:      oklch(92% 0.02 30);
  --error-fg:      oklch(42% 0.07 30);
  --success-bg:    oklch(92% 0.018 140);
  --success-fg:    oklch(38% 0.065 140);
  --warn-bg:       oklch(94% 0.04 85);
  --warn-fg:       oklch(45% 0.08 75);
  --shadow-sm: 0 1px 3px oklch(0% 0 0 / 0.04), 0 1px 2px oklch(0% 0 0 / 0.03);
  --shadow-md: 0 4px 16px oklch(0% 0 0 / 0.04), 0 2px 4px oklch(0% 0 0 / 0.03);
  --radius:    12px;
  --radius-lg: 18px;
  --ease-out:  cubic-bezier(0.22, 1, 0.36, 1);
  --font-serif:'DM Serif Display', 'Playfair Display', Georgia, serif;
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
}
::selection { background: var(--brass-glow); color: var(--brass-dark); }
body {
  background: var(--bg);
  background-image:
    repeating-linear-gradient(45deg, transparent 0, transparent 39px,
      var(--bg-grid) 39px, var(--bg-grid) 40px, transparent 40px, transparent 79px),
    repeating-linear-gradient(-45deg, transparent 0, transparent 39px,
      var(--bg-grid) 39px, var(--bg-grid) 40px, transparent 40px, transparent 79px);
  color: var(--fg);
  font-family: var(--font-sans);
  font-size: 15px; line-height: 1.6;
  min-height: 100vh;
  padding: 44px 24px 80px;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}
body::after {
  content: '';
  position: fixed; inset: 0;
  background: radial-gradient(ellipse at 50% -10%, transparent 40%, oklch(0% 0 0 / 0.035) 100%);
  pointer-events: none; z-index: 0;
}
.container { max-width: 960px; margin: 0 auto; position: relative; z-index: 1; }
@keyframes fadeSlide {
  from { opacity: 0; transform: translateY(12px); }
  to   { opacity: 1; transform: none; }
}
.report-header { text-align: center; margin-bottom: 34px; animation: fadeSlide .55s var(--ease-out) both; }
.title-line { display: flex; align-items: center; justify-content: center; gap: 22px; }
.title-line .rule { flex: 0 0 64px; height: 1px; }
.title-line .rule:first-child { background: linear-gradient(90deg, var(--border), var(--border-strong)); }
.title-line .rule:last-child  { background: linear-gradient(270deg, var(--border), var(--border-strong)); }
.title-line h1 {
  font-family: var(--font-serif); font-weight: 500;
  font-size: clamp(28px, 5vw, 42px); letter-spacing: 0.015em; color: var(--fg);
}
.title-en {
  font-family: var(--font-serif); font-style: italic;
  font-size: 13px; letter-spacing: 0.32em; text-transform: uppercase;
  color: var(--fg-tertiary); margin-top: 6px;
}
.sub { color: var(--fg-secondary); font-size: 13px; margin-top: 10px; }
.badge {
  display: inline-block; padding: 2px 12px; border-radius: 999px;
  font-size: 11px; font-weight: 600; margin-left: 8px; border: 1px solid transparent;
}
.badge-critical { background: var(--error-bg); color: var(--error-fg); border-color: oklch(42% 0.07 30 / 0.22); }
.badge-moderate { background: var(--warn-bg);   color: var(--warn-fg);   border-color: oklch(45% 0.08 75 / 0.28); }
.badge-minor    { background: var(--brass-subtle); color: var(--brass-dark); border-color: var(--border-accent); }
.badge-clean    { background: var(--success-bg); color: var(--success-fg); border-color: oklch(38% 0.065 140 / 0.22); }
.badge-failed   { background: var(--error-bg); color: var(--error-fg); font-weight: 600; border-color: oklch(42% 0.07 30 / 0.22); }
.overview {
  display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px;
  margin: 8px 0 22px;
}
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 16px 10px; text-align: center;
  box-shadow: var(--shadow-sm);
  transition: transform .3s var(--ease-out), box-shadow .3s var(--ease-out);
}
.card:hover { transform: translateY(-2px); box-shadow: var(--shadow-md); }
.card .n { font-family: var(--font-serif); font-size: 32px; font-weight: 500; color: var(--brass-dark); }
.card .l { font-size: 11px; color: var(--fg-secondary); margin-top: 3px; letter-spacing: 0.04em; }
.card.small {
  background: var(--surface-raised); border: 1px solid var(--border);
  border-radius: 10px; padding: 8px 12px; min-width: 64px; text-align: center; box-shadow: none;
}
.card.small .n { font-family: var(--font-serif); font-size: 20px; color: var(--brass-dark); }
.card.small .l { font-size: 11px; color: var(--fg-secondary); margin-top: 2px; }
.file-block {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-lg); padding: 20px 24px; margin-bottom: 16px;
  box-shadow: var(--shadow-sm);
  animation: fadeSlide .5s var(--ease-out) both;
}
.file-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
.file-head b { font-size: 16px; font-weight: 600; color: var(--fg); letter-spacing: 0.01em; }
.file-head .sub { margin-left: auto; color: var(--fg-tertiary); font-size: 12px; margin-top: 0; }
.summary { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.ctx-block {
  background: var(--surface-raised);
  border-left: 3px solid var(--brass);
  border-radius: 10px; padding: 11px 16px; margin: 8px 0; font-size: 13.5px;
}
/* Fixable issue blocks: content left, large fix button right */
.ctx-block.fixable { display: flex; align-items: flex-start; gap: 14px; }
.fixable .ctx-main { flex: 1; min-width: 0; }
.issue-check {
  flex: 0 0 auto; margin-left: auto;
  display: inline-flex; align-items: center; justify-content: center; gap: 7px;
  padding: 9px 18px;
  border: 1px solid var(--border-accent); border-radius: 999px;
  background: var(--surface); cursor: pointer;
  font-size: 13px; font-weight: 600; color: var(--brass-dark);
  user-select: none;
  transition: background .18s var(--ease-out), border-color .18s, transform .18s var(--ease-out), box-shadow .18s;
}
.issue-check:hover { background: var(--brass-subtle); border-color: var(--brass); transform: translateY(-1px); }
.issue-check input {
  width: 16px; height: 16px; accent-color: var(--brass);
  cursor: pointer; flex: 0 0 auto;
}
.issue-check:has(input:checked) {
  background: linear-gradient(135deg, var(--brass-light), var(--brass));
  color: var(--fg-on-brass); border-color: var(--brass-dark);
  box-shadow: 0 2px 10px var(--brass-glow);
}
.issue-check:has(input:checked) .fix-tick::after { content: '已勾选'; }
.issue-check .fix-tick::after { content: '修复'; }
.tag {
  display: inline-block; background: var(--brass); color: var(--fg-on-brass);
  border-radius: 999px;
  padding: 2px 12px; font-size: 11px; font-weight: 600; margin-right: 8px;
}
.hl { color: var(--fg); font-weight: 500; }
.loc { color: var(--fg-tertiary); font-size: 11px; margin-top: 3px; }
.ctx-row { display: flex; gap: 8px; align-items: baseline; margin-top: 6px; font-size: 12px; }
.ctx-label {
  flex: 0 0 auto; background: var(--brass-subtle); color: var(--brass-dark);
  border: 1px solid var(--border-accent); border-radius: 999px;
  padding: 0 8px; font-size: 10px; font-weight: 600; line-height: 18px; height: 18px;
}
.ctx-cn { color: var(--fg); line-height: 1.5; }
.ctx-en { color: var(--fg-secondary); font-style: italic; line-height: 1.5; }
.ok { color: var(--success-fg); font-size: 13px; margin-top: 6px; }
.all-clean {
  background: var(--success-bg); color: var(--success-fg);
  border: 1px solid oklch(38% 0.065 140 / 0.22);
  border-radius: var(--radius); padding: 14px 16px; text-align: center; font-weight: 600;
  margin-bottom: 16px;
  animation: fadeSlide .5s var(--ease-out) both;
}
.foot { margin-top: 22px; font-size: 12px; color: var(--fg-tertiary); text-align: center; }
.fix-bar {
  display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap;
  margin: 26px 0 8px; padding: 20px 24px;
  background: linear-gradient(135deg, var(--surface) 0%, var(--brass-subtle) 130%);
  border: 1px solid var(--border-accent);
  border-radius: var(--radius-lg);
  box-shadow: 0 8px 28px oklch(55% 0.095 65 / 0.14);
  position: sticky; bottom: 14px; z-index: 5;
}
.fix-info { min-width: 0; }
.fix-title {
  font-family: var(--font-serif); font-size: 18px; font-weight: 500;
  color: var(--brass-dark); letter-spacing: 0.02em;
}
.fix-desc { font-size: 12px; color: var(--fg-secondary); margin-top: 3px; }
.fix-actions { display: flex; gap: 10px; flex-wrap: wrap; margin-left: auto; }
.fix-btn {
  font-family: var(--font-sans); font-size: 14px; font-weight: 600; letter-spacing: 0.01em;
  color: var(--fg-on-brass);
  background: linear-gradient(135deg, var(--brass-light) 0%, var(--brass) 100%);
  border: 1px solid var(--brass-dark);
  border-radius: 999px; padding: 11px 26px; cursor: pointer;
  box-shadow: 0 3px 14px var(--brass-glow);
  transition: transform .2s var(--ease-out), box-shadow .2s var(--ease-out), opacity .2s;
}
.fix-btn:hover { transform: translateY(-2px); box-shadow: 0 6px 20px oklch(55% 0.095 65 / 0.30); }
.fix-btn:active { transform: translateY(0); }
.fix-btn:disabled { opacity: .55; cursor: wait; transform: none; }
.fix-btn.ghost {
  background: var(--surface); color: var(--brass-dark);
  border: 1px solid var(--border-accent); box-shadow: none;
  padding: 11px 20px; font-size: 13px;
}
.fix-btn.ghost:hover { background: var(--brass-subtle); box-shadow: none; transform: translateY(-1px); }
"""


# Batch-fix + export JS embedded in the job report. Plain string so braces
# need no escaping inside the f-string template; __JOB_ID__ is substituted.
_REPORT_JS = """
<script>
window.JOB_ID = "__JOB_ID__";
async function fixChecked() {
  const groups = {};
  document.querySelectorAll('.issue-check input:checked').forEach(cb => {
    const file = cb.dataset.file;
    if (!groups[file]) groups[file] = new Set();
    groups[file].add(cb.value);
  });
  const entries = Object.entries(groups);
  if (!entries.length) { alert('请先勾选需要修复的问题'); return; }
  const btn = document.getElementById('btn-fix');
  btn.disabled = true; btn.textContent = '修复中…';
  const lines = [];
  for (const [file, types] of entries) {
    try {
      const r = await fetch('/api/fix-issues', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({job_id: window.JOB_ID, file_id: file, fix_types: [...types]})
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || '修复失败');
      lines.push(file.slice(0,8) + ' → ' + (data.changed.length ? data.changed.join('、') : '无需修改'));
    } catch (e) {
      lines.push(file.slice(0,8) + ' → 失败: ' + e.message);
    }
  }
  alert('修复完成\\n' + lines.join('\\n'));
  location.reload();
}
function downloadHtml() {
  const blob = new Blob([document.documentElement.outerHTML], {type: 'text/html;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = '翻译报告_' + window.JOB_ID.slice(0,8) + '.html';
  a.click();
  URL.revokeObjectURL(a.href);
}
</script>
"""


# Export-only JS for the per-file quality report (no job context for fixes).
_REPORT_JS_SIMPLE = """
<script>
function downloadHtml() {
  const blob = new Blob([document.documentElement.outerHTML], {type: 'text/html;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = '质检报告.html';
  a.click();
  URL.revokeObjectURL(a.href);
}
</script>
"""


def _generate_quality_report(
    file_id: str, docx_path: str, warnings: list[str],
    contexts: dict | None = None,
) -> str | None:
    """Generate an HTML quality report if issues remain after auto-fix.

    *contexts* maps warning index → {"cn": ..., "en": ...} snippets so each
    issue shows its source paragraph and translation for quick lookup.
    Returns the report file path, or ``None`` if no issues."""
    if not warnings:
        return None

    severity = "critical" if len(warnings) > 5 else ("moderate" if len(warnings) > 2 else "minor")
    now_str = time.strftime("%Y-%m-%d %H:%M", time.localtime())

    rows = ""
    for wi, w in enumerate(warnings):
        loc, _, preview = w.partition(": ")
        ctx = (contexts or {}).get(wi)
        ctx_html = ""
        if ctx:
            ctx_html = (
                f'<div class="ctx-row"><span class="ctx-label">原文</span>'
                f'<span class="ctx-cn">{_html_escape(ctx.get("cn", ""))}</span></div>'
                f'<div class="ctx-row"><span class="ctx-label">译文</span>'
                f'<span class="ctx-en">{_html_escape(ctx.get("en", ""))}</span></div>'
            )
        rows += (
            '<div class="ctx-block">'
            f'<span class="tag">{_categorize_warning(w)}</span>'
            f'<span class="hl">{_html_escape(preview[:110])}</span>'
            f'<div class="loc">{_html_escape(loc)}</div>'
            f'{ctx_html}'
            '</div>\n'
        )

    badge_class = (
        "badge-critical" if severity == "critical"
        else "badge-moderate" if severity == "moderate"
        else "badge-minor"
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>翻译质检报告 — {_html_escape(file_id[:8])}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=Inter:opsz,wght@14..32,300;14..32,400;14..32,500;14..32,600;14..32,700&display=swap" rel="stylesheet">
<style>{_REPORT_CSS}</style></head>
<body><div class="container">
<header class="report-header">
  <div class="title-line"><span class="rule"></span><h1>翻译质检报告</h1><span class="rule"></span></div>
  <div class="title-en">Quality Inspection Report</div>
  <p class="sub">文件 {_html_escape(file_id[:8])} · 生成时间 {now_str} · 状态 有残留问题 <span class="badge {badge_class}">{severity}</span></p>
</header>
<div class="overview" style="grid-template-columns:repeat(2,1fr);max-width:420px;margin:8px auto 22px">
<div class="card"><div class="n">{len(warnings)}</div><div class="l">残留问题数</div></div>
<div class="card"><div class="n" style="font-size:16px;line-height:1.4;padding-top:10px">{severity}</div><div class="l">严重程度</div></div>
</div>
<h3 style="font-size:14px;font-weight:600;margin:4px 0 10px;color:var(--fg)">问题详情</h3>
{rows}
<div class="fix-bar">
  <button class="fix-btn ghost" onclick="window.print()">导出 PDF</button>
  <button class="fix-btn ghost" onclick="downloadHtml()">下载 HTML</button>
</div>
<p class="foot">以上问题未能自动修复，请人工处理 · {_html_escape(docx_path)}</p>
</div>{_REPORT_JS_SIMPLE}</body></html>"""

    report_path = os.path.join(_REPORT_DIR, f"quality_{file_id}.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    return report_path


def _html_escape(text: str) -> str:
    """Minimal HTML escape."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _snippet(text: str, limit: int = 80) -> str:
    """Compact snippet for report display: collapse whitespace, truncate."""
    if not text:
        return ""
    compact = re.sub(r'\s+', ' ', str(text)).strip()
    if len(compact) > limit:
        return compact[:limit].rstrip() + "…"
    return compact


# Category → deterministic fix action (used by the report's batch-fix button).
_CATEGORY_FIX_MAP = {
    '数字保真': 'fidelity',
    '术语': 'term',
    '中文残留': 'chinese',
    '重复': 'duplicate',
    '编号': 'numbering',
}


def _fix_paragraph_text(paragraph, new_text: str) -> None:
    """Replace a paragraph's text, keeping the first run's formatting."""
    if not paragraph.runs:
        paragraph.add_run(new_text)
        return
    paragraph.runs[0].text = new_text
    for r in paragraph.runs[1:]:
        r.text = ""


def _apply_fixes(file_id: str, glossary: dict[str, str], fix_types: list[str]) -> dict:
    """Apply deterministic fixes to an already-translated document.

    Called from POST /api/fix-issues (report batch-fix). Each fix type scans
    the translated docx and rewrites matching paragraphs in place, then
    re-runs the final Chinese-residue scan. Returns changed actions + the
    refreshed warnings; does NOT retranslate whole files.
    """
    en_path = os.path.join(UPLOAD_DIR, f"{file_id}_EN.docx")
    if not os.path.exists(en_path):
        return {"error": "译文文件不存在", "changed": [], "cn_warnings": []}
    doc = doc_parser.read_document(en_path)
    src_path = os.path.join(UPLOAD_DIR, f"{file_id}.docx")
    src_doc = doc_parser.read_document(src_path) if os.path.exists(src_path) else None

    paras = list(doc.paragraphs)
    table_paras: list = []
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                table_paras.extend(cell.paragraphs)
    all_paras = paras + table_paras

    changed: list[str] = []

    if 'term' in fix_types and glossary:
        n = 0
        for p in all_paras:
            if not p.text.strip():
                continue
            new_t, _ = dedup_guard.complete_glossary_terms(p.text, glossary)
            if new_t != p.text:
                _fix_paragraph_text(p, new_t)
                n += 1
        if n:
            changed.append(f'术语补全 {n} 处')

    if 'numbering' in fix_types:
        texts = [p.text for p in all_paras]
        new_texts, _ = numbering_check.normalize_numbering(texts)
        n = 0
        for p, nt in zip(all_paras, new_texts):
            if nt != p.text:
                _fix_paragraph_text(p, nt)
                n += 1
        if n:
            changed.append(f'编号归一化 {n} 段')

    if 'duplicate' in fix_types:
        n = 0
        for p in all_paras:
            if not p.text.strip():
                continue
            if dedup_guard.audit_duplicates(p.text):
                new_t, _ = dedup_guard.dedup_text(p.text)
                if new_t != p.text:
                    _fix_paragraph_text(p, new_t)
                    n += 1
        if n:
            changed.append(f'重复清理 {n} 段')

    if 'fidelity' in fix_types:
        n = 0
        if src_doc:
            src_paras = [p.text for p in src_doc.paragraphs]
            for p, cn in zip(paras, src_paras):
                if not p.text.strip() or not cn.strip():
                    continue
                if dedup_guard.verify_fidelity(cn, p.text):
                    retried = _fidelity_retranslate(cn, glossary, set())
                    if retried and not dedup_guard.verify_fidelity(cn, retried):
                        _fix_paragraph_text(p, retried)
                        n += 1
        if n:
            changed.append(f'数字缺失重译 {n} 段')

    if 'chinese' in fix_types:
        n = 0
        for p in all_paras:
            if not p.text.strip():
                continue
            if TranslatorService.detect_chinese_residue(p.text):
                prompt = (
                    "Please fully translate the following Chinese to English "
                    "(no Chinese characters should remain):\n" + p.text
                )
                retried = translator_service.translate_text(prompt, glossary)
                if retried and not retried.startswith("[Translation Error]"):
                    retried = translator_service._clean_mechanical_errors(retried)
                    if not TranslatorService.detect_chinese_residue(retried):
                        _fix_paragraph_text(p, retried)
                        n += 1
        if n:
            changed.append(f'中文残留重译 {n} 段')

    if not changed:
        return {"changed": [], "cn_warnings": []}

    doc_parser.save_document(doc, en_path)
    cn_warnings = doc_parser.verify_no_cn(en_path)
    return {"changed": changed, "cn_warnings": list(cn_warnings)}


# ---------------------------------------------------------------------------
# Job-level visual quality report (前端"翻译报告"弹窗)
# ---------------------------------------------------------------------------

def _categorize_warning(w: str) -> str:
    """Bucket a warning into a category for the report."""
    if "金额/数量缺失" in w or "数字缺失" in w:
        return "数字保真"
    if "术语缺失" in w or "术语补全" in w:
        return "术语"
    if "残留" in w or "Para " in w or "Table " in w or "TextBox" in w:
        return "中文残留"
    if "重复检测" in w:
        return "重复"
    if "编号" in w:
        return "编号"
    return "其他"


def _build_job_report_html(job_id: str, results: list[dict]) -> str:
    """Build a visual job-level quality report (HTML string).

    Summarizes every file's translation & QA state: completion status, warning
    categories (数字保真/术语/中文残留/重复/编号), and the unresolved items
    that need human attention.
    """
    now_str = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    done = [r for r in results if r.get("status") == "completed"]
    total_warns = sum(len(r.get("cn_warnings") or []) for r in done)
    clean_files = sum(1 for r in done if not r.get("cn_warnings"))
    failed = [r for r in results if r.get("status") != "completed"]

    severity = ("critical" if total_warns > 10 else
                "moderate" if total_warns > 4 else
                "minor" if total_warns > 0 else "clean")
    badge_class = {"critical": "badge-critical", "moderate": "badge-moderate",
                   "minor": "badge-minor", "clean": "badge-clean"}[severity]
    sev_label = {"critical": "严重", "moderate": "中度", "minor": "轻微",
                 "clean": "无问题"}[severity]

    file_blocks = ""
    for idx, r in enumerate(results):
        fid = r.get("file_id", "")
        fname = original_filenames.get(fid, fid)
        st = r.get("status")
        warns = r.get("cn_warnings") or []
        summary = r.get("quality_summary") or {}

        if st != "completed":
            err = _html_escape(str(r.get("error", "未知错误"))[:120])
            file_blocks += (
                f'<div class="file-block" style="animation-delay:{0.06 + idx * 0.05:.2f}s">'
                f'<div class="file-head"><b>{_html_escape(fname)}</b>'
                f'<span class="badge badge-failed">失败 · {err}</span></div></div>'
            )
            continue

        cats = summary.get("categories") or {}
        cat_cards = "".join(
            f'<div class="card small"><div class="n">{v}</div><div class="l">{_html_escape(k)}</div></div>'
            for k, v in cats.items() if v)
        if not cat_cards:
            cat_cards = '<div class="card small"><div class="n">0</div><div class="l">告警</div></div>'

        rows = ""
        warn_ctx = r.get("warning_contexts") or []
        ctx_by_index = {c["index"]: c for c in warn_ctx}
        for wi, w in enumerate(warns):
            cat = _categorize_warning(w)
            loc, _, preview = w.partition(": ")
            ctx = ctx_by_index.get(wi)
            ctx_html = ""
            if ctx:
                ctx_html = (
                    f'<div class="ctx-row"><span class="ctx-label">原文</span>'
                    f'<span class="ctx-cn">{_html_escape(ctx.get("cn", ""))}</span></div>'
                    f'<div class="ctx-row"><span class="ctx-label">译文</span>'
                    f'<span class="ctx-en">{_html_escape(ctx.get("en", ""))}</span></div>'
                )
            fix_type = _CATEGORY_FIX_MAP.get(cat)
            check_html = ""
            if fix_type:
                check_html = (
                    f'<label class="issue-check" title="勾选后点击底部「修复勾选项」批量修复">'
                    f'<input type="checkbox" value="{fix_type}" data-file="{_html_escape(fid)}">'
                    f'<span class="fix-tick"></span></label>'
                )
            rows += (
                f'<div class="ctx-block fixable">'
                f'<div class="ctx-main"><span class="tag">{cat}</span>'
                f'<span class="hl">{_html_escape(preview[:110])}</span>'
                f'<div class="loc">{_html_escape(loc)}</div>{ctx_html}</div>'
                f'{check_html}</div>\n'
            )

        clean_badge = '<span class="badge badge-clean">无问题</span>' if not warns else ""
        paras = summary.get("paragraphs", "-")
        file_blocks += (
            f'<div class="file-block" style="animation-delay:{0.06 + idx * 0.05:.2f}s">'
            f'<div class="file-head"><b>{_html_escape(fname)}</b>{clean_badge}'
            f'<span class="sub">段落 {paras}</span></div>'
            f'<div class="summary">{cat_cards}</div>'
            f'{rows}'
            + ('' if warns else '<p class="ok">✓ 本文件无遗留问题，无需人工处理</p>')
            + '</div>'
        )

    noissue_note = (
        '<div class="all-clean">全部文件无遗留问题，无需人工处理</div>'
        if total_warns == 0 and not failed else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8">
<title>翻译报告 — {_html_escape(job_id[:8])}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=Inter:opsz,wght@14..32,300;14..32,400;14..32,500;14..32,600;14..32,700&display=swap" rel="stylesheet">
<style>{_REPORT_CSS}</style></head>
<body><div class="container">
<header class="report-header">
  <div class="title-line"><span class="rule"></span><h1>翻译报告</h1><span class="rule"></span></div>
  <div class="title-en">Quality Report</div>
  <p class="sub">任务 {_html_escape(job_id[:8])} · 生成时间 {now_str} · 整体状态 <span class="badge {badge_class}">{sev_label}</span></p>
</header>
{noissue_note}
<div class="overview">
<div class="card"><div class="n">{len(results)}</div><div class="l">文件总数</div></div>
<div class="card"><div class="n">{len(done)}</div><div class="l">完成</div></div>
<div class="card"><div class="n">{len(failed)}</div><div class="l">失败</div></div>
<div class="card"><div class="n">{total_warns}</div><div class="l">遗留问题</div></div>
<div class="card"><div class="n">{clean_files}</div><div class="l">无问题文件</div></div>
</div>
{file_blocks}
<div class="fix-bar">
  <div class="fix-info">
    <div class="fix-title">问题批量修复</div>
    <div class="fix-desc">勾选问题后一键自动修复；导出 PDF / HTML 可保存归档</div>
  </div>
  <div class="fix-actions">
    <button class="fix-btn ghost" onclick="downloadHtml()">下载 HTML</button>
    <button class="fix-btn ghost" onclick="window.print()">导出 PDF</button>
    <button class="fix-btn" id="btn-fix" onclick="fixChecked()">修复勾选项</button>
  </div>
</div>
<p class="foot">勾选问题后点击「修复勾选项」自动修复；未确定项可在「重新翻译」输入反馈后处理。自动修复项（去重、占位符还原、编号归一化、术语补全、格式规范化）不在此处重复展示。</p>
</div>{_REPORT_JS.replace('__JOB_ID__', _html_escape(job_id))}</body></html>"""
    return html


# ---------------------------------------------------------------------------
# Background translation tasks
# ---------------------------------------------------------------------------

# Whole-paragraph translation concurrency & polish gate.
# Per-run translation was removed (slow, produced run-boundary duplicates);
# polish is reserved for large/complex text so short/structured paragraphs
# don't pay a second API call.
_PARALLEL_WORKERS = 6
_POLISH_MIN_LEN = 120  # only large/complex text gets the polish pass

# File-level parallelism: multiple files translate concurrently. The total API
# concurrency (file_workers * paragraph_workers) is capped so the provider
# rate limit is not hit even on large batch jobs.
_FILE_WORKERS = 2
_API_CONCURRENCY_LIMIT = 6  # hard ceiling for concurrent API calls

# Guards shared job progress / job file writes across file threads.
_JOB_PROGRESS_LOCK = threading.Lock()

def _translate_paragraphs_sync(
    paragraphs: list[dict],
    glossary: dict[str, str],
    feedback: str | None = None,
    custom_api: dict | None = None,
    job_id: str | None = None,
    frac_start: float = 0.0,
    frac_end: float = 1.0,
    para_workers: int | None = None,
    update_progress: bool = True,
    progress_key: str | None = None,
) -> list[dict]:
    """Translate extracted paragraphs (sync, runs in thread).

    Whole-paragraph translation only. Per-run translation was removed — it was
    slow (one API call per run), produced run-boundary duplicates (badcase
    type 2), and degraded English word order. The full translation is written
    into the first non-empty run; run formats are merged (union) onto that
    carrier run by document_parser.apply_per_run_formatting.

    Paragraphs are translated concurrently (ThreadPoolExecutor). Updates job
    progress within [frac_start, frac_end].
    """
    api_kw = (
        {
            "api_key": custom_api["api_key"],
            "base_url": custom_api.get("base_url"),
            "model": custom_api.get("model"),
        }
        if custom_api
        else {}
    )
    total = len(paragraphs)
    # ── Protected-token set for conservative dedup (rare / proper nouns) ──
    protected = dedup_guard.build_protected_tokens([p["text"] for p in paragraphs])
    progress_lock = threading.Lock()
    done_count = 0

    def _full_text_runs(runs_data: list[dict], full_text: str) -> list[str]:
        """translated_runs with the full translation in the first non-empty slot."""
        translated_runs: list[str] = []
        first_done = False
        for rd in runs_data:
            if rd.get("text", "").strip():
                if not first_done:
                    translated_runs.append(full_text)
                    first_done = True
                else:
                    translated_runs.append("")
            else:
                translated_runs.append(rd.get("text", ""))
        return translated_runs

    def _translate_one(idx: int, para_data: dict) -> dict:
        text = para_data["text"]
        runs_data = para_data["runs"]

        if not text.strip():
            return {
                **para_data,
                "translated_text": text,
                "translated_runs": [rd["text"] for rd in runs_data],
                "fidelity_warnings": [],
            }

        # ── URL-aware shortcut (URL-only lines) ──
        if TranslatorService.contains_url(text):
            cn_count = len(re.findall(r'[一-鿿]', text))
            if cn_count < 20:
                translated = TranslatorService.translate_url_label_line(text, glossary)
                translated = TranslatorService.fix_cn_labels(translated)
                translated = translator_service._clean_mechanical_errors(translated, protected)
                return {
                    **para_data,
                    "translated_text": translated,
                    "translated_runs": _full_text_runs(runs_data, translated),
                    "fidelity_warnings": [],
                }
            # URL present but lots of Chinese — fall through to full translation

        # ── Full paragraph translation ──
        if len(text) < 100 and not _CN_PUNCT_RE.search(text):
            translated = translator_service.replace_with_glossary(text, glossary)
            went_through_api = False
        else:
            prompt = text
            if feedback:
                prompt = (
                    f"{text}\n\n"
                    f"[REVISION_INSTRUCTION]\n{feedback}\n[/REVISION_INSTRUCTION]\n"
                    f"Apply the above revision instruction when translating."
                )
            translated = translator_service.translate_text(prompt, glossary, **api_kw)

            # API failure fallback: glossary replacement (no error leak)
            if translated.startswith("[Translation Error]"):
                translated = translator_service.replace_with_glossary(text, glossary)

            # Chinese-residue retry (up to two passes; second pass is stronger
            # so the model does not leave any CJK chars).
            residue = TranslatorService.detect_chinese_residue(translated)
            if residue:
                retry = text
                if feedback:
                    retry = f"{text}\n\n[REVISION_INSTRUCTION]\n{feedback}\n[/REVISION_INSTRUCTION]"
                translated = translator_service.translate_text(
                    "Please fully translate the following Chinese to English "
                    "(no Chinese characters should remain):\n" + retry,
                    glossary, **api_kw,
                )
                residue2 = TranslatorService.detect_chinese_residue(translated)
                if residue2 and len(text) < 300:
                    translated = translator_service.translate_text(
                        "Translate the text below to English ONLY. "
                        "Your output MUST contain NO Chinese characters whatsoever — "
                        "if any Chinese remains, retranslate the whole text.\n\n" + retry,
                        glossary, **api_kw,
                    )
                    if TranslatorService.detect_chinese_residue(translated):
                        # Last resort: glossary replacement of any CN terms.
                        replaced = translator_service.replace_with_glossary(text, glossary)
                        if replaced and not TranslatorService.detect_chinese_residue(replaced):
                            translated = replaced
            went_through_api = True

        translated = TranslatorService.fix_cn_labels(translated)
        translated = re.sub(
            r'\s*\[/?REVISION_INSTRUCTION\].*?\[/REVISION_INSTRUCTION\]\s*',
            '', translated, flags=re.DOTALL,
        ).strip()
        translated = re.sub(
            r'\s*\[/?REVISION_INSTRUCTION\].*',
            '', translated, flags=re.DOTALL,
        ).strip()
        translated = re.sub(r'\*\*\*(.+?)\*\*\*', r'\1', translated, flags=re.DOTALL)
        translated = re.sub(r'\*\*(.+?)\*\*', r'\1', translated, flags=re.DOTALL)
        translated = re.sub(r'\*(.+?)\*', r'\1', translated, flags=re.DOTALL)

        # Always clean mechanical errors (spacing, duplicated words) for ALL paths
        translated = translator_service._clean_mechanical_errors(translated, protected)

        # ── Polish gate ──
        # Whole-paragraph translation already produces natural English; polish
        # is reserved for large/complex text (threshold _POLISH_MIN_LEN) so we
        # do not spend a second API call on short/structured paragraphs.
        if went_through_api and len(translated) > _POLISH_MIN_LEN:
            polished = translator_service.polish_text(translated, **api_kw)
            if polished and polished != translated:
                translated = polished

        # ── Glossary term compliance: restricted prefix completion ──
        # Completes truncated standard translations (e.g. missing ", Ltd.")
        # via word-aligned prefix completion; nothing else is rewritten.
        translated, term_audit = dedup_guard.complete_glossary_terms(translated, glossary)

        # ── CN↔EN fidelity check (API-translated paragraphs) ──
        fidelity_warnings: list[str] = []
        if went_through_api:
            fid_warnings = dedup_guard.verify_fidelity(text, translated)
            term_warnings = dedup_guard.verify_terms(text, translated, glossary)
            fidelity_warnings.extend(fid_warnings)
            fidelity_warnings.extend(term_warnings)

            # ── Fidelity auto-recovery: retry once if numbers were dropped ──
            # The model occasionally drops a digit (e.g. "November 25, 2024" →
            # "November 25,..", losing the year). Retry the paragraph ONCE with
            # every number/amount/date spelled out, and adopt the retry only if
            # it clears the fidelity warnings. Never loops, never blocks.
            if fid_warnings:
                retried = _fidelity_retranslate(
                    text, glossary, protected, **api_kw)
                if retried:
                    retry_fid = dedup_guard.verify_fidelity(text, retried)
                    if len(retry_fid) < len(fid_warnings):
                        translated = retried
                        retried, retry_audit = dedup_guard.complete_glossary_terms(
                            retried, glossary)
                        translated = retried
                        term_warnings = dedup_guard.verify_terms(
                            text, translated, glossary)
                        # Rebuild warnings from the adopted retry.
                        fidelity_warnings = list(retry_fid)
                        fidelity_warnings.extend(term_warnings)
                        fidelity_warnings.extend(
                            f"术语补全: 「{a['from']}」→「{a['to']}」"
                            for a in retry_audit)
                        if retry_fid:
                            fidelity_warnings.append(
                                "保真重译后仍有数字缺失，请人工核对")
                        else:
                            fidelity_warnings.append(
                                "保真重译: 已自动恢复缺失的数字/金额")

            fidelity_warnings.extend(
                f"术语补全: 「{a['from']}」→「{a['to']}」" for a in term_audit
            )

        return {
            **para_data,
            "translated_text": translated,
            "translated_runs": _full_text_runs(runs_data, translated),
            "fidelity_warnings": fidelity_warnings,
        }

    # ── Concurrent translation ──
    result: list[dict] = [None] * total
    with ThreadPoolExecutor(max_workers=para_workers or _PARALLEL_WORKERS) as ex:
        futures = {ex.submit(_translate_one, i, p): i for i, p in enumerate(paragraphs)}
        for fut in as_completed(futures):
            idx = futures[fut]
            result[idx] = fut.result()
            # File-level parallelism: per-paragraph progress is disabled
            # (multiple files would race on the same completed counter);
            # the file completion event drives progress instead. Paragraph
            # counts still update the per-file slot so the UI can show
            # "contract.docx 12/40 段" while files translate concurrently.
            if update_progress and job_id and job_id in jobs and total > 0:
                with progress_lock:
                    done_count += 1
                    p = jobs[job_id]["progress"]
                    frac = frac_start + (done_count / total) * (frac_end - frac_start)
                    base = int(p.get("completed", 0))
                    p["completed"] = base + min(frac, 0.99)
            elif progress_key and job_id and job_id in jobs and total > 0:
                with _JOB_PROGRESS_LOCK:
                    files = jobs[job_id]["progress"].setdefault("files", {})
                    slot = files.setdefault(progress_key, {})
                    slot["done"] = slot.get("done", 0) + 1
                    slot["total"] = total

    # ── Document-level numbering consistency (badcase type 4) ──
    texts = [e.get("translated_text", "") for e in result]
    normalized, num_warnings = numbering_check.normalize_numbering(texts)
    for entry, new_text in zip(result, normalized):
        if new_text != entry.get("translated_text"):
            entry["translated_text"] = new_text
            non_empty = [i for i, r in enumerate(entry.get("translated_runs", [])) if r.strip()]
            if len(non_empty) == 1:
                entry["translated_runs"][non_empty[0]] = new_text
    # Document-level numbering warnings are attached once (to the first entry).
    if num_warnings and result:
        result[0].setdefault("fidelity_warnings", []).extend(num_warnings)

    return result


def _process_file_sync(
    file_id: str,
    glossary: dict[str, str],
    feedback: str | None = None,
    custom_api: dict | None = None,
    job_id: str | None = None,
    para_workers: int | None = None,
    file_progress: bool = True,
) -> dict:
    """Translate a single .docx file (sync, runs in thread)."""
    file_path = os.path.join(UPLOAD_DIR, f"{file_id}.docx")
    translated_path = os.path.join(UPLOAD_DIR, f"{file_id}_EN.docx")
    if not os.path.exists(file_path):
        return {"file_id": file_id, "status": "failed", "error": "Source file not found"}

    def _set_detail(detail: str, fraction: float | None = None) -> None:
        # Thread-safe: multiple file threads may update the shared job dict.
        if job_id and job_id in jobs:
            with _JOB_PROGRESS_LOCK:
                p = jobs[job_id]["progress"]
                # File-level parallelism: write per-file phase into the file
                # slot (parallel mode) so the UI can show what each file is
                # doing; global detail stays file-level ("已完成 x/y 个文件").
                if not file_progress:
                    files = p.setdefault("files", {})
                    slot = files.setdefault(file_id, {})
                    slot["phase"] = detail
                    return
                p["detail"] = detail
                # Per-file fractions would race on the shared completed
                # counter — only the file completion event advances it in
                # parallel mode (file_progress=False).
                if fraction is not None:
                    base = int(p.get("completed", 0))
                    p["completed"] = base + min(fraction, 0.99)

    # Register this file's progress slot up front (parallel mode) so the UI
    # sees it immediately as "preparing".
    if job_id and job_id in jobs and not file_progress:
        with _JOB_PROGRESS_LOCK:
            files = jobs[job_id]["progress"].setdefault("files", {})
            files[file_id] = {
                "name": original_filenames.get(file_id, file_id),
                "done": 0, "total": 0, "phase": "准备中",
            }

    try:
        fmt_actions: list[dict] = []
        needs_retranslation = True
        translated: list[dict] = []
        translated_cells: list[dict] = []
        translated_tb: list[dict] = []

        if feedback:
            src_doc = doc_parser.read_document(file_path)
            body_paras = doc_parser.extract_paragraphs(src_doc)
            all_texts = [p["text"] for p in body_paras]
            table_cells = doc_parser.extract_table_cells(src_doc)
            all_texts.extend(c["text"] for c in table_cells)

            fmt_result = translator_service.interpret_formatting_feedback(
                feedback, all_texts, **(custom_api or {}),
            )
            fmt_actions = fmt_result.get("actions", [])
            needs_retranslation = fmt_result.get("needs_retranslation", True)

        # ── Translation (unified — a single translate-or-load path) ──
        if needs_retranslation or not os.path.exists(translated_path):
            _set_detail("正在解析文档结构...", 0.05)
            doc = doc_parser.read_document(file_path)
            paragraphs = doc_parser.extract_paragraphs(doc)
            _set_detail("正在翻译正文段落...", 0.3)
            translated = _translate_paragraphs_sync(paragraphs, glossary, feedback, custom_api, job_id, 0.3, 0.6, para_workers, file_progress, file_id)

            for idx, entry in enumerate(translated):
                if idx < len(doc.paragraphs):
                    doc_parser.apply_per_run_formatting(doc.paragraphs[idx], entry["runs"], entry.get("translated_runs", []))
                    doc_parser.set_line_spacing(doc.paragraphs[idx], False)

            table_cells = doc_parser.extract_table_cells(doc)
            if table_cells:
                _set_detail("正在翻译表格内容...", 0.6)
                translated_cells = _translate_paragraphs_sync(table_cells, glossary, feedback, None, job_id, 0.6, 0.8, para_workers, file_progress)
                for entry in translated_cells:
                    doc_parser.apply_per_run_formatting(entry["paragraph"], entry["runs"], entry.get("translated_runs", []))
                    doc_parser.set_line_spacing(entry["paragraph"], False)

            textbox_paras = doc_parser.extract_textbox_paragraphs(doc)
            if textbox_paras:
                _set_detail("正在翻译文本框...", 0.8)
                translated_tb = _translate_paragraphs_sync(textbox_paras, glossary, feedback, None, job_id, 0.8, 0.9, para_workers, file_progress)
                for entry in translated_tb:
                    doc_parser.apply_textbox_formatting(entry["element"], entry["runs"], entry["translated_text"])
                    doc_parser.set_textbox_line_spacing(entry["element"])

            _set_detail("正在清理背景色...", 0.9)
            doc_parser.clear_background_shading(doc)
        else:
            doc = doc_parser.read_document(translated_path)

        applied: list[str] = []
        if fmt_actions:
            _set_detail("正在应用格式调整...", 0.93)
            applied = doc_parser.apply_targeted_formatting(doc, fmt_actions)
        elif feedback:
            _set_detail("正在应用格式调整...", 0.93)
            applied = doc_parser.apply_formatting_instructions(doc, feedback)

        _set_detail("正在保存文件...", 0.98)
        output_path = translated_path
        doc_parser.save_document(doc, output_path)

        # ── Quality gate: final scan ──
        cn_warnings = doc_parser.verify_no_cn(output_path)
        warnings = list(cn_warnings)
        # ── Fidelity warnings with CN↔EN context (for locating issues) ──
        # Each fidelity warning records the source paragraph (CN) and its
        # translation (EN), so the report can show both snippets for lookup.
        warn_ctx: list[dict] = []
        for entry in translated + translated_cells + translated_tb:
            ws = entry.get("fidelity_warnings", [])
            if ws:
                cn_snip = _snippet(entry.get("text", ""))
                en_snip = _snippet(entry.get("translated_text", ""))
                for w in ws:
                    warnings.append(w)
                    warn_ctx.append({
                        "index": len(warnings) - 1,
                        "cn": cn_snip,
                        "en": en_snip,
                    })
        # ── Read-only duplicate audit on the final document ──
        # Reports any residual exact-adjacent duplicates so every deletion (or
        # kept-with-warning case) is traceable in the QA report.
        final_doc = doc_parser.read_document(output_path)
        final_texts = [p.text for p in final_doc.paragraphs]
        for table in final_doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    final_texts.extend(para.text for para in cell.paragraphs)
        for tb_para in doc_parser.extract_textbox_paragraphs(final_doc):
            final_texts.append(tb_para["text"])
        for t in final_texts:
            warnings.extend(dedup_guard.audit_duplicates(t))

        result = {"file_id": file_id, "status": "completed"}
        if warnings:
            result["cn_warnings"] = warnings
            if warn_ctx:
                result["warning_contexts"] = warn_ctx
            report_path = _generate_quality_report(
                file_id, output_path, warnings,
                contexts={c["index"]: c for c in warn_ctx},
            )
            if report_path:
                result["quality_report"] = report_path

        # ── Quality summary for the job-level visual report ──
        categories: dict[str, int] = {}
        for w in warnings:
            cat = _categorize_warning(w)
            categories[cat] = categories.get(cat, 0) + 1
        result["quality_summary"] = {
            "paragraphs": len(translated) + len(translated_cells) + len(translated_tb),
            "warning_count": len(warnings),
            "categories": categories,
        }

        if applied:
            result["formatting_applied"] = applied
        return result
    except Exception as e:
        return {"file_id": file_id, "status": "failed", "error": str(e)}


def _fidelity_retranslate(
    cn_text: str,
    glossary: dict[str, str],
    protected: set,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> str | None:
    """Retranslate *cn_text* once, spelling out every number/amount/date that
    must survive, so the model cannot drop them again (fidelity auto-recovery
    for badcase type 1).

    Only numbers, amounts, dates and identifiers are enumerated — nothing else
    is constrained, so the retry is a normal translation plus a hard
    must-preserve list. Returns the cleaned EN text, or None on failure.

    *api_key* / *base_url* / *model* are optional — when omitted the default
    server configuration is used (same fallback as translate_text), so the
    helper can be called standalone for manual testing.
    """
    expected: list[str] = []
    for m in dedup_guard._CN_NUM_UNIT_RE.finditer(cn_text):
        seg = m.group(0).strip()
        if seg and seg not in expected:
            expected.append(seg)
    for m in re.finditer(r'(?<!\d)\d{4,}(?!\d)', cn_text):
        if m.group(0) not in expected:
            expected.append(m.group(0))
    if not expected:
        return None
    prompt = (
        "Translate the following Chinese into English.\n"
        "CRITICAL: every number, amount, date and identifier listed below must "
        "appear in your translation verbatim. Do not drop, truncate, round or "
        "reformat any of them.\n"
        "Must-preserve values:\n"
        + "\n".join(f"- {e}" for e in expected)
        + "\n\nChinese text:\n"
        + cn_text
    )
    t = translator_service.translate_text(
        prompt, glossary, api_key=api_key, base_url=base_url, model=model)
    if not t or t.startswith("[Translation Error]"):
        return None
    t = TranslatorService.fix_cn_labels(t)
    t = translator_service._clean_mechanical_errors(t, protected)
    return t


def _run_files_parallel(
    file_ids: list[str],
    glossary: dict[str, str],
    feedback: str | None,
    custom_api: dict | None,
    job_id: str,
    total: int,
) -> list[dict]:
    """Translate several files concurrently (sync; runs in a worker thread).

    Called via run_in_executor from the async job loops so the asyncio event
    loop is never blocked by the as_completed iteration. Per-file progress
    fractions are disabled (they would race on the shared counter); progress
    is driven by file completion events instead. Total API concurrency stays
    under _API_CONCURRENCY_LIMIT (file_workers * paragraph_workers).
    """
    para_workers = max(1, _API_CONCURRENCY_LIMIT // _FILE_WORKERS)
    results: list[dict] = [None] * total
    done = 0
    with ThreadPoolExecutor(max_workers=_FILE_WORKERS) as ex:
        futs: dict = {}
        for i, file_id in enumerate(file_ids):
            fname = original_filenames.get(file_id, file_id)
            futs[ex.submit(
                _process_file_sync, file_id, glossary, feedback, custom_api,
                job_id, para_workers, False,
            )] = (i, fname)
        for fut in as_completed(futs):
            i, fname = futs[fut]
            try:
                results[i] = fut.result()
            except Exception as e:  # pragma: no cover - defensive
                results[i] = {"file_id": file_ids[i], "status": "failed", "error": str(e)}
            with _JOB_PROGRESS_LOCK:
                done += 1
                jobs[job_id]["progress"].update(
                    completed=done,
                    current_file=fname,
                    stage="translating",
                    detail=f"已完成 {done}/{total} 个文件，正在翻译其余文件...",
                )
                # Drop this file's live-progress slot (it has finished).
                jobs[job_id]["progress"].get("files", {}).pop(file_ids[i], None)
            _save_job(job_id, jobs[job_id])
    return results


async def run_translation(
    job_id: str,
    file_ids: list[str],
    glossary_id: str | None = None,
    custom_api: dict | None = None,
) -> None:
    """Background task: translate all files in a job."""
    try:
        if glossary_id:
            glossary = glossary_service.get_glossary(glossary_id)
        else:
            glossary = {}
    except ValueError as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        _save_job(job_id, jobs[job_id])
        return

    total = len(file_ids)
    # Preserve any existing progress fields (e.g. detail set at job creation)
    cur = jobs[job_id].get("progress", {})
    cur.update(total=total, completed=0, current_file=None, stage="starting", detail=cur.get("detail", "准备就绪"))
    jobs[job_id]["progress"] = cur
    _save_job(job_id, jobs[job_id])

    loop = asyncio.get_event_loop()
    results: list[dict] = []

    # ── File-level parallelism ──
    # Several files translate concurrently inside a worker thread (so the
    # asyncio event loop keeps serving /status requests). Single-file jobs
    # keep the original simple path.
    if total <= 1:
        for file_id in file_ids:
            fname = original_filenames.get(file_id, file_id)
            jobs[job_id]["progress"].update(
                completed=0, current_file=fname, stage="translating",
                detail="正在解析文档...",
            )
            _save_job(job_id, jobs[job_id])
            result = await loop.run_in_executor(
                None, _process_file_sync, file_id, glossary, None, custom_api, job_id
            )
            results.append(result)
    else:
        results = await loop.run_in_executor(
            None, _run_files_parallel, file_ids, glossary, None, custom_api,
            job_id, total,
        )

    jobs[job_id]["progress"].update(
        completed=total,
        current_file=None,
        stage="done",
        detail="全部翻译完成",
    )
    jobs[job_id]["status"] = "completed"
    jobs[job_id]["results"] = results
    _save_job(job_id, jobs[job_id])


async def run_translation_with_feedback(
    job_id: str,
    file_ids: list[str],
    feedback: str,
    glossary_id: str | None = None,
    custom_api: dict | None = None,
) -> None:
    """Background task: re-translate all files with user feedback."""
    try:
        if glossary_id:
            glossary = glossary_service.get_glossary(glossary_id)
        else:
            glossary = {}
    except ValueError as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        _save_job(job_id, jobs[job_id])
        return

    total = len(file_ids)
    cur = jobs[job_id].get("progress", {})
    cur.update(total=total, completed=0, current_file=None, stage="starting", detail=cur.get("detail", "准备就绪"))
    jobs[job_id]["progress"] = cur
    _save_job(job_id, jobs[job_id])

    loop = asyncio.get_event_loop()
    results: list[dict] = []

    # File-level parallelism (same pattern as run_translation).
    if total <= 1:
        for file_id in file_ids:
            fname = original_filenames.get(file_id, file_id)
            jobs[job_id]["progress"].update(
                completed=0, current_file=fname, stage="translating",
                detail="正在解析文档...",
            )
            _save_job(job_id, jobs[job_id])
            result = await loop.run_in_executor(
                None, _process_file_sync, file_id, glossary, feedback, custom_api, job_id
            )
            results.append(result)
    else:
        results = await loop.run_in_executor(
            None, _run_files_parallel, file_ids, glossary, feedback, custom_api,
            job_id, total,
        )

    jobs[job_id]["progress"].update(
        completed=total,
        current_file=None,
        stage="done",
        detail="全部翻译完成",
    )
    jobs[job_id]["status"] = "completed"
    jobs[job_id]["results"] = results
    _save_job(job_id, jobs[job_id])

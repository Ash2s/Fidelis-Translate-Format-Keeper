"""API routes for immigration document translation service."""

import os
import json
import uuid
import asyncio
import re
import io
import time
import zipfile
from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, Response
from openai import OpenAI
import httpx

from app.config import settings
from app.services.glossary import GlossaryService
from app.services.document_parser import DocumentParser
from app.services.translator import TranslatorService
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
    """Write a single job to disk."""
    path = os.path.join(JOBS_DIR, f"{job_id}.json")
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
            os.remove(temp_path)

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


def _generate_quality_report(
    file_id: str, docx_path: str, warnings: list[str]
) -> str | None:
    """Generate an HTML quality report if issues remain after auto-fix.

    Returns the report file path, or ``None`` if no issues."""
    if not warnings:
        return None

    severity = "critical" if len(warnings) > 5 else ("moderate" if len(warnings) > 2 else "minor")
    now_str = time.strftime("%Y-%m-%d %H:%M", time.localtime())

    rows = ""
    for w in warnings:
        loc, _, preview = w.partition(": ")
        rows += (
            '<div class="ctx-block">'
            f'<span class="marker">↳</span>'
            f'<span class="hl">{_html_escape(preview[:80])}</span>'
            f'<div class="loc">{_html_escape(loc)}</div>'
            '</div>\n'
        )

    badge_class = (
        "badge-critical" if severity == "critical"
        else "badge-moderate" if severity == "moderate"
        else "badge-minor"
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>翻译质检报告 — {_html_escape(file_id)}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f5f7fa;color:#1a1a2e;padding:24px}}
.container{{max-width:900px;margin:0 auto}}
h1{{font-size:22px;margin-bottom:4px}}
.sub{{color:#666;font-size:13px;margin-bottom:18px}}
.badge{{display:inline-block;padding:2px 10px;border-radius:8px;font-size:11px;font-weight:600}}
.badge-critical{{background:#fde8e8;color:#c0392b}}
.badge-moderate{{background:#fef3cd;color:#856404}}
.badge-minor{{background:#dbeafe;color:#1e40af}}
.ctx-block{{background:#fff;border-radius:8px;padding:10px 14px;margin:6px 0;box-shadow:0 1px 2px rgba(0,0,0,.05);font-size:13px}}
.hl{{background:#ffeaa7;padding:0 2px;border-radius:2px}}
.loc{{color:#888;font-size:11px;margin-top:3px}}
.marker{{color:#999;margin-right:6px;font-size:11px}}
.summary{{display:flex;gap:16px;margin-bottom:18px}}
.card{{background:#fff;border-radius:10px;padding:14px 18px;box-shadow:0 1px 3px rgba(0,0,0,.06);text-align:center;min-width:100px}}
.card .n{{font-size:28px;font-weight:700;color:#e74c3c}}
.card .l{{font-size:11px;color:#666;margin-top:2px}}
</style></head>
<body><div class="container">
<h1>翻译质检报告</h1>
<p class="sub">文件: {_html_escape(file_id)} | 生成时间: {now_str} | 状态: 有残留问题</p>
<div class="summary">
<div class="card"><div class="n">{len(warnings)}</div><div class="l">残留问题数</div></div>
<div class="card"><span class="badge {badge_class}">{severity}</span></div>
</div>
<h3 style="font-size:14px;margin-bottom:8px">问题详情</h3>
{rows}
<p style="margin-top:16px;font-size:12px;color:#888">
⚠ 以上问题未能自动修复，请人工处理。文件路径: {_html_escape(docx_path)}
</p>
</div></body></html>"""

    report_path = os.path.join(_REPORT_DIR, f"quality_{file_id}.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    return report_path


def _html_escape(text: str) -> str:
    """Minimal HTML escape."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Background translation tasks
# ---------------------------------------------------------------------------

def _translate_paragraphs_sync(
    paragraphs: list[dict],
    glossary: dict[str, str],
    feedback: str | None = None,
    custom_api: dict | None = None,
    job_id: str | None = None,
    frac_start: float = 0.0,
    frac_end: float = 1.0,
) -> list[dict]:
    """Translate extracted paragraphs (sync, runs in thread).

    Each paragraph result includes both a ``translated_text`` (full paragraph)
    and ``translated_runs`` (per-run list) so that downstream formatting can
    preserve per-run formatting (highlight, underline, colour, etc.).

    Updates job progress per-paragraph within [frac_start, frac_end].
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
    result = []
    for idx, para_data in enumerate(paragraphs):
        text = para_data["text"]
        runs_data = para_data["runs"]

        if not text.strip():
            result.append({
                **para_data,
                "translated_text": text,
                "translated_runs": [rd["text"] for rd in runs_data],
            })
            continue

        # ── 0. URL-aware translation shortcut ──
        # Only applies to lines that are PRIMARILY a URL + short Chinese label
        # (e.g. "链接：https://...").  For paragraphs that contain a URL
        # embedded in substantial Chinese text, use the full translation path
        # so the Chinese content actually gets translated.
        if TranslatorService.contains_url(text):
            # Count Chinese characters in the text
            cn_count = len(re.findall(r'[一-鿿]', text))
            if cn_count < 20:
                translated = TranslatorService.translate_url_label_line(text, glossary)
                translated = TranslatorService.fix_cn_labels(translated)
                translated = translator_service._clean_mechanical_errors(translated)
                # For URL lines, put the full translation in the first non-empty run
                translated_runs = []
                first_done = False
                for rd in runs_data:
                    if rd.get("text", "").strip():
                        if not first_done:
                            translated_runs.append(translated)
                            first_done = True
                        else:
                            translated_runs.append("")
                    else:
                        translated_runs.append(rd.get("text", ""))
                result.append({
                    **para_data,
                    "translated_text": translated,
                    "translated_runs": translated_runs,
                })
                if job_id and job_id in jobs and total > 0:
                    p = jobs[job_id]["progress"]
                    frac = frac_start + (idx + 1) / total * (frac_end - frac_start)
                    base = int(p.get("completed", 0))
                    p["completed"] = base + min(frac, 0.99)
                continue
            # else: URL present but lots of Chinese — fall through to full translation

        # ── 1. Translate full paragraph (for quality / fallback) ──
        if len(text) < 100 and not _CN_PUNCT_RE.search(text):
            translated = translator_service.replace_with_glossary(text, glossary)
        else:
            prompt = text
            if feedback:
                prompt = (
                    f"{text}\n\n"
                    f"[REVISION_INSTRUCTION]\n{feedback}\n[/REVISION_INSTRUCTION]\n"
                    f"Apply the above revision instruction when translating."
                )
            translated = translator_service.translate_text(prompt, glossary, **api_kw)

            # If API call failed, fall back to glossary replacement
            # so the output doesn't contain "[Translation Error]: ..."
            if translated.startswith("[Translation Error]"):
                translated = translator_service.replace_with_glossary(text, glossary)

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
        translated = translator_service._clean_mechanical_errors(translated)

        # ── 1b. Polish / proofread the translated English (quality pass) ──
        # Only polish text that went through the API (not glossary replacement)
        # and is long enough to benefit from editing.
        went_through_api = not (len(text) < 100 and not _CN_PUNCT_RE.search(text))
        text_was_polished = False
        if went_through_api and len(translated) > 30:
            polished = translator_service.polish_text(translated, **api_kw)
            if polished and polished != translated:
                translated = polished
                text_was_polished = True

        # ── 2. Translate each non-empty run (preserve per-run formatting) ──
        # First, detect whether runs are Chinese-character fragments (e.g. a
        # word like "例如" split across runs as "例" + "如").  When runs are
        # very short (average ≤ 2 Chinese chars), or when the full text was
        # polished, per-run translation would produce text inconsistent with
        # the polished full text — skip it and put the full translation in
        # the first run instead.
        non_empty_runs = [rd for rd in runs_data if rd.get("text", "").strip()]
        avg_run_len = (
            sum(len(rd["text"]) for rd in non_empty_runs) / len(non_empty_runs)
            if non_empty_runs else 0
        )
        runs_are_fragments = (
            len(non_empty_runs) > 1 and (avg_run_len <= 2.5 or text_was_polished)
        )

        translated_runs: list[str] = []
        if runs_are_fragments:
            # Put full paragraph translation in the first non-empty run,
            # empty in the rest (formatting union applied separately).
            first_done = False
            for rd in runs_data:
                if rd.get("text", "").strip():
                    if not first_done:
                        translated_runs.append(translated)
                        first_done = True
                    else:
                        translated_runs.append("")
                else:
                    translated_runs.append(rd.get("text", ""))
        else:
            for rd in runs_data:
                run_text = rd["text"]
                if not run_text.strip():
                    translated_runs.append(run_text)
                    continue

                # Term-only heuristic for short runs
                if len(run_text) < 100 and not _CN_PUNCT_RE.search(run_text):
                    rt = translator_service.replace_with_glossary(run_text, glossary)
                else:
                    # Provide full paragraph as context for per-run translation
                    ctx_prompt = (
                        f"Full paragraph context (do NOT translate this):\n{text}\n\n"
                        f"Now translate ONLY the following segment from that paragraph:\n{run_text}"
                    )
                    if feedback:
                        ctx_prompt += (
                            f"\n\n[REVISION_INSTRUCTION]\n{feedback}\n[/REVISION_INSTRUCTION]\n"
                            f"Apply the revision instruction."
                        )
                    rt = translator_service.translate_text(ctx_prompt, glossary, **api_kw)

                    residue = TranslatorService.detect_chinese_residue(rt)
                    if residue:
                        retry_run = (
                            f"Full paragraph context:\n{text}\n\n"
                            f"Translate ONLY this segment and output ONLY English:\n{run_text}"
                        )
                        if feedback:
                            retry_run += (
                                f"\n\n[REVISION_INSTRUCTION]\n{feedback}\n[/REVISION_INSTRUCTION]"
                            )
                        rt = translator_service.translate_text(
                            "Please fully translate the following Chinese to English "
                            "(no Chinese characters should remain):\n" + retry_run,
                            glossary, **api_kw,
                        )

                rt = TranslatorService.fix_cn_labels(rt)
                rt = re.sub(r'\*\*\*(.+?)\*\*\*', r'\1', rt, flags=re.DOTALL)
                rt = re.sub(r'\*\*(.+?)\*\*', r'\1', rt, flags=re.DOTALL)
                rt = re.sub(r'\*(.+?)\*', r'\1', rt, flags=re.DOTALL)
                # Clean mechanical errors on per-run text too
                rt = translator_service._clean_mechanical_errors(rt)
                # Clean revision instruction residue from run translation
                rt = re.sub(
                    r'\s*\[/?REVISION_INSTRUCTION\].*?\[/REVISION_INSTRUCTION\]\s*',
                    '', rt, flags=re.DOTALL,
                ).strip()
                rt = re.sub(
                    r'\s*\[/?REVISION_INSTRUCTION\].*',
                    '', rt, flags=re.DOTALL,
                ).strip()
                # If per-run translation failed, use the original run text
                # instead of leaking the error + context prompt into output
                if rt.startswith("[Translation Error]"):
                    rt = run_text
                translated_runs.append(rt)

        result.append({
            **para_data,
            "translated_text": translated,
            "translated_runs": translated_runs,
        })

        # Update progress per paragraph
        if job_id and job_id in jobs and total > 0:
            p = jobs[job_id]["progress"]
            frac = frac_start + (idx + 1) / total * (frac_end - frac_start)
            base = int(p.get("completed", 0))
            p["completed"] = base + min(frac, 0.99)

    return result


def _process_file_sync(
    file_id: str,
    glossary: dict[str, str],
    feedback: str | None = None,
    custom_api: dict | None = None,
    job_id: str | None = None,
) -> dict:
    """Translate a single .docx file (sync, runs in thread)."""
    file_path = os.path.join(UPLOAD_DIR, f"{file_id}.docx")
    translated_path = os.path.join(UPLOAD_DIR, f"{file_id}_EN.docx")
    if not os.path.exists(file_path):
        return {"file_id": file_id, "status": "failed", "error": "Source file not found"}

    def _set_detail(detail: str, fraction: float | None = None) -> None:
        if job_id and job_id in jobs:
            p = jobs[job_id]["progress"]
            p["detail"] = detail
            if fraction is not None:
                base = int(p.get("completed", 0))
                p["completed"] = base + min(fraction, 0.99)

    try:
        fmt_actions: list[dict] = []
        needs_retranslation = True

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
            translated = _translate_paragraphs_sync(paragraphs, glossary, feedback, custom_api, job_id, 0.3, 0.6)

            for idx, entry in enumerate(translated):
                if idx < len(doc.paragraphs):
                    doc_parser.apply_per_run_formatting(doc.paragraphs[idx], entry["runs"], entry.get("translated_runs", []))
                    doc_parser.set_line_spacing(doc.paragraphs[idx], False)

            table_cells = doc_parser.extract_table_cells(doc)
            if table_cells:
                _set_detail("正在翻译表格内容...", 0.6)
                translated_cells = _translate_paragraphs_sync(table_cells, glossary, feedback, None, job_id, 0.6, 0.8)
                for entry in translated_cells:
                    doc_parser.apply_per_run_formatting(entry["paragraph"], entry["runs"], entry.get("translated_runs", []))
                    doc_parser.set_line_spacing(entry["paragraph"], False)

            textbox_paras = doc_parser.extract_textbox_paragraphs(doc)
            if textbox_paras:
                _set_detail("正在翻译文本框...", 0.8)
                translated_tb = _translate_paragraphs_sync(textbox_paras, glossary, feedback, None, job_id, 0.8, 0.9)
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
        result = {"file_id": file_id, "status": "completed"}
        if cn_warnings:
            result["cn_warnings"] = cn_warnings
            report_path = _generate_quality_report(
                file_id, output_path, cn_warnings
            )
            if report_path:
                result["quality_report"] = report_path
        if applied:
            result["formatting_applied"] = applied
        return result
    except Exception as e:
        return {"file_id": file_id, "status": "failed", "error": str(e)}


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
    results = []
    for i, file_id in enumerate(file_ids):
        fname = original_filenames.get(file_id, file_id)
        jobs[job_id]["progress"].update(
            completed=i,
            current_file=fname,
            stage="translating",
            detail="正在解析文档...",
        )
        _save_job(job_id, jobs[job_id])

        result = await loop.run_in_executor(
            None, _process_file_sync, file_id, glossary, None, custom_api, job_id
        )
        results.append(result)

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
    results = []
    for i, file_id in enumerate(file_ids):
        fname = original_filenames.get(file_id, file_id)
        jobs[job_id]["progress"].update(
            completed=i,
            current_file=fname,
            stage="translating",
            detail="正在解析文档...",
        )
        _save_job(job_id, jobs[job_id])

        result = await loop.run_in_executor(
            None, _process_file_sync, file_id, glossary, feedback, custom_api, job_id
        )
        results.append(result)

    jobs[job_id]["progress"].update(
        completed=total,
        current_file=None,
        stage="done",
        detail="全部翻译完成",
    )
    jobs[job_id]["status"] = "completed"
    jobs[job_id]["results"] = results
    _save_job(job_id, jobs[job_id])

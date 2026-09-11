import sqlite3
import threading
import time
import random
import os
import re
import uuid
import asyncio
import logging
import shutil
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib
import secrets
from flask import Flask, Response, jsonify, request, send_from_directory, send_file
from werkzeug.utils import secure_filename
from scraper import fetch_all_papers, filter_and_translate
import config
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# translation job state: arxiv_id -> {"status": "pending"|"running"|"done"|"error", "pdf": path, "error": msg}
_translate_jobs: dict = {}
# Upload state is a cache only; persisted upload_translations is authoritative.
_upload_jobs: dict = {}
_upload_job_locks = defaultdict(threading.RLock)
_upload_jobs_state_lock = threading.RLock()
_manual_jobs_semaphore = threading.BoundedSemaphore(2)

MANUAL_SOURCE = "manual"
RSS_SOURCE = "rss"
ARCHIVED_LABEL = "归档"
MANUAL_LABEL_PREFIX = "manual:"
MAX_UPLOAD_BYTES = int(getattr(config, "MAX_UPLOAD_BYTES", 50 * 1024 * 1024))

BASE = Path(__file__).parent
app = Flask(__name__, static_folder=str(BASE / "static"), static_url_path="/static")
PAPERS_DB = BASE / "data" / "papers.db"
LABELS_DB = BASE / "data" / "labels.db"
VALID_LABELS = {"相关", "感兴趣", "可做", "组会报告", "不相关", ARCHIVED_LABEL}
UPLOAD_DIR = BASE / "data" / "uploads"


@app.before_request
def require_password():
    """Protect the entire site with browser-native HTTP Basic Auth."""
    auth = request.authorization
    username = auth.username if auth else ""
    password = auth.password if auth else ""
    username_ok = secrets.compare_digest(username, config.ACCESS_USERNAME)
    password_ok = bool(config.ACCESS_PASSWORD) and secrets.compare_digest(
        password, config.ACCESS_PASSWORD
    )
    if username_ok and password_ok:
        return None
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="Paper Web", charset="UTF-8"'},
    )

async def _run_sync_pipeline():
    print("Fetching all papers via standalone scraper...")
    all_papers = await fetch_all_papers()
    if not all_papers:
        print(
            "PIPELINE_SUMMARY status=no_data fetched=0 new=0 evaluated=0 "
            "related=0 successful_batches=0 failed_batches=0 retries=0 "
            "retry_pending=0 errors=0"
        )
        return

    with papers_conn() as pc:
        evaluated_urls = {
            r[0] for r in pc.execute("SELECT url FROM paper_evaluations").fetchall()
        }

    papers_to_process = [p for p in all_papers if p["url"] not in evaluated_urls]
    print(f"Total: {len(all_papers)}, New to process: {len(papers_to_process)}")
    if not papers_to_process:
        print(
            f"PIPELINE_SUMMARY status=noop fetched={len(all_papers)} new=0 evaluated=0 "
            "related=0 successful_batches=0 failed_batches=0 retries=0 "
            "retry_pending=0 errors=0"
        )
        return

    print(f"Running AI loop against {len(papers_to_process)} papers...")
    evaluated_papers, errors, stats = await filter_and_translate(
        papers_to_process,
        config.FILTER_BASE_URL,
        config.FILTER_API_KEY,
        config.FILTER_MODEL,
    )

    now_str = datetime.now(tz=timezone.utc).isoformat()
    transport_papers = [p for p in evaluated_papers if p.get("is_transport") == 1]
    with papers_conn() as pc:
        pc.executemany(
            """INSERT OR REPLACE INTO paper_evaluations
            (url, is_transport, title_zh, abstract_zh, evaluated_at, summary_zh, abstract_en)
            VALUES (?,?,?,?,?,?,?)""",
            [(
                p["url"], p.get("is_transport", 0), p.get("title_zh", ""),
                p.get("abstract_zh", ""), now_str, p.get("summary_zh", ""),
                p.get("abstract_en", ""),
            ) for p in evaluated_papers],
        )
        pc.executemany(
            "INSERT OR IGNORE INTO pushed_papers (url, journal, title, pushed_at) VALUES (?,?,?,?)",
            [(p["url"], p["journal"], p["title"], now_str) for p in transport_papers],
        )
        pc.commit()

    status = "success" if not errors and stats["retry_pending_papers"] == 0 else "partial"
    print(
        f"PIPELINE_SUMMARY status={status} fetched={len(all_papers)} "
        f"new={len(papers_to_process)} evaluated={len(evaluated_papers)} "
        f"related={len(transport_papers)} "
        f"successful_batches={stats['successful_batches']} "
        f"failed_batches={stats['failed_batches']} "
        f"retries={stats['retry_attempts']} "
        f"retry_pending={stats['retry_pending_papers']} errors={len(errors)}"
    )
    for number, error in enumerate(errors, 1):
        print(f"PIPELINE_ERROR {number}/{len(errors)} {error}")

def _daily_fetch_job():
    tz_cn = timezone(timedelta(hours=8))
    last_run_day = None

    while True:
        now = datetime.now(tz_cn)
        if now.hour == config.DAILY_FETCH_HOUR and now.day != last_run_day:
            last_run_day = now.day
            print(f"[{now}] Starting daily {config.DAILY_FETCH_HOUR}:00 fetch task...")
            try:
                # Flask may not run true async loops cleanly natively without patching, so we just run blockingly for the thread
                asyncio.run(_run_sync_pipeline())
            except Exception as e:
                print(f"Daily fetch failed: {e}")
        time.sleep(60)  # Check every minute

def papers_conn():
    conn = sqlite3.connect(PAPERS_DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def labels_conn():
    conn = sqlite3.connect(LABELS_DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _utc_now():
    return datetime.now(tz=timezone.utc).isoformat()


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _legacy_original_path(job_id: str) -> Path:
    return UPLOAD_DIR / f"{job_id}.pdf"


def _manual_label_key(job_id: str) -> str:
    return f"{MANUAL_LABEL_PREFIX}{job_id}"


def _ensure_column(conn, table: str, column: str, definition: str):
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _backfill_upload_rows(conn):
    """Fill ownership metadata for legacy URL-keyed uploads without deleting data."""
    url_by_job = {
        hashlib.md5(url.encode()).hexdigest(): url
        for (url,) in conn.execute("SELECT url FROM pushed_papers").fetchall()
    }
    rows = conn.execute(
        "SELECT job_id, source_type, paper_url, original_pdf_path, pdf_sha256, metadata_status "
        "FROM upload_translations"
    ).fetchall()
    for job_id, source_type, paper_url, original_pdf_path, pdf_sha256, metadata_status in rows:
        if source_type not in (RSS_SOURCE, MANUAL_SOURCE):
            source_type = RSS_SOURCE
        path = Path(original_pdf_path) if original_pdf_path else _legacy_original_path(job_id)
        digest = pdf_sha256 or _sha256_file(path)
        paper_url = paper_url or url_by_job.get(job_id)
        if source_type == RSS_SOURCE and metadata_status in (None, ""):
            metadata_status = "not_applicable"
        conn.execute(
            "UPDATE upload_translations SET source_type=?, paper_url=?, original_pdf_path=?, "
            "pdf_sha256=?, metadata_status=? WHERE job_id=?",
            (source_type, paper_url, str(path), digest, metadata_status or "not_applicable", job_id),
        )


def init_dbs():
    PAPERS_DB.parent.mkdir(parents=True, exist_ok=True)
    with papers_conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS pushed_papers (
                url TEXT PRIMARY KEY, journal TEXT, title TEXT, pushed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pushed_at ON pushed_papers(pushed_at);
            CREATE TABLE IF NOT EXISTS paper_evaluations (
                url TEXT PRIMARY KEY, is_transport INTEGER NOT NULL,
                title_zh TEXT, abstract_zh TEXT, evaluated_at TEXT NOT NULL,
                summary_zh TEXT, abstract_en TEXT
            );
            CREATE TABLE IF NOT EXISTS pdf_translations (
                arxiv_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                pdf_path TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS upload_translations (
                job_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                status TEXT NOT NULL,
                pdf_path TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'rss',
                paper_url TEXT,
                original_pdf_path TEXT,
                pdf_sha256 TEXT,
                metadata_status TEXT NOT NULL DEFAULT 'not_applicable',
                metadata_stage TEXT,
                metadata_error TEXT,
                tex_dir TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS manual_papers (
                job_id TEXT PRIMARY KEY,
                original_title TEXT NOT NULL DEFAULT '',
                title_zh TEXT NOT NULL DEFAULT '',
                abstract_original TEXT NOT NULL DEFAULT '',
                abstract_zh TEXT NOT NULL DEFAULT '',
                summary_zh TEXT NOT NULL DEFAULT '',
                source_language TEXT NOT NULL DEFAULT '',
                metadata_incomplete INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        _ensure_column(c, "paper_evaluations", "summary_zh", "TEXT")
        _ensure_column(c, "paper_evaluations", "abstract_en", "TEXT")
        _ensure_column(c, "upload_translations", "source_type", "TEXT NOT NULL DEFAULT 'rss'")
        _ensure_column(c, "upload_translations", "paper_url", "TEXT")
        _ensure_column(c, "upload_translations", "original_pdf_path", "TEXT")
        _ensure_column(c, "upload_translations", "pdf_sha256", "TEXT")
        _ensure_column(c, "upload_translations", "metadata_status", "TEXT NOT NULL DEFAULT 'not_applicable'")
        _ensure_column(c, "upload_translations", "metadata_stage", "TEXT")
        _ensure_column(c, "upload_translations", "metadata_error", "TEXT")
        _ensure_column(c, "upload_translations", "tex_dir", "TEXT")
        _ensure_column(c, "upload_translations", "updated_at", "TEXT")
        _backfill_upload_rows(c)
    with labels_conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS paper_labels (
                url TEXT PRIMARY KEY,
                label TEXT NOT NULL DEFAULT '不相关',
                labeled_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS date_reads (
                date TEXT PRIMARY KEY,
                read_at TEXT NOT NULL
            );
        """)


def _upload_index():
    with papers_conn() as c:
        rows = c.execute(
            "SELECT job_id, paper_url, status, source_type FROM upload_translations "
            "WHERE paper_url IS NOT NULL"
        ).fetchall()
    return {
        paper_url: {
            "job_id": job_id,
            "status": status,
            "source_type": source_type or RSS_SOURCE,
        }
        for job_id, paper_url, status, source_type in rows
    }


@app.route("/")
def index():
    return send_from_directory(BASE, "index.html")


@app.route("/dates")
def dates():
    with papers_conn() as c:
        rows = c.execute(
            "SELECT DISTINCT DATE(datetime(pushed_at), '+8 hours') d "
            "FROM pushed_papers ORDER BY d DESC"
        ).fetchall()
    return jsonify({"dates": [r[0] for r in rows]})


@app.route("/papers")
def papers():
    date = request.args.get("date", "")
    with papers_conn() as pc, labels_conn() as lc:
        rows = pc.execute(
            """
            SELECT p.url, p.journal, p.title, p.pushed_at,
                   e.title_zh, e.abstract_zh, e.summary_zh, e.abstract_en
            FROM pushed_papers p
            LEFT JOIN paper_evaluations e ON p.url = e.url
            WHERE DATE(datetime(p.pushed_at), '+8 hours') = ?
            ORDER BY (p.journal LIKE 'arXiv:%'), p.pushed_at
            """,
            (date,),
        ).fetchall()
        labels = {
            r[0]: r[1]
            for r in lc.execute("SELECT url, label FROM paper_labels").fetchall()
        }
        translations = {k: v["status"] for k, v in _translate_jobs.items()}
    uploads = _upload_index()
    result = []
    for url, journal, title, pushed_at, title_zh, abstract_zh, summary_zh, abstract_en in rows:
        arxiv_match = re.search(r'abs/([^/?v]+)', url)
        arxiv_id = arxiv_match.group(1) if arxiv_match else None
        upload = uploads.get(url)
        paper_label = labels.get(url, "不相关")
        if ARCHIVED_LABEL in paper_label.split(","):
            continue
        result.append({
            "url": url,
            "label_key": url,
            "source_type": RSS_SOURCE,
            "journal": journal or "",
            "title": title or "",
            "pushed_at": pushed_at,
            "title_zh": title_zh or "",
            "abstract_zh": abstract_zh or "",
            "summary_zh": summary_zh or "",
            "abstract_en": abstract_en or "",
            "abstract_original": abstract_en or "",
            "label": paper_label,
            "is_arxiv": (journal or "").startswith("arXiv:"),
            "translation_status": translations.get(arxiv_id) if arxiv_id else None,
            "upload_translation_status": upload["status"] if upload else None,
            "upload_job_id": upload["job_id"] if upload else None,
        })
    return jsonify({"date": date, "papers": result})


@app.route("/filter")
def filter_papers():
    label = request.args.get("label", "")
    page = int(request.args.get("page", 1))
    limit = 20
    offset = (page - 1) * limit
    
    if not label or label not in VALID_LABELS:
        return jsonify({"label": label, "papers": [], "has_next": False})

    with papers_conn() as pc:
        pc.execute("ATTACH DATABASE ? AS ldb", (str(LABELS_DB),))
        label_patterns = (label, f"{label},%", f"%,{label},%", f"%,{label}")
        archive_patterns = (
            ARCHIVED_LABEL,
            f"{ARCHIVED_LABEL},%",
            f"%,{ARCHIVED_LABEL},%",
            f"%,{ARCHIVED_LABEL}",
        )
        label_expr = "(l.label = ? OR l.label LIKE ? OR l.label LIKE ? OR l.label LIKE ?)"
        archive_expr = "(l.label = ? OR l.label LIKE ? OR l.label LIKE ? OR l.label LIKE ?)"
        if label == ARCHIVED_LABEL:
            label_condition = archive_expr
            label_params = archive_patterns
        else:
            label_condition = f"{label_expr} AND NOT {archive_expr}"
            label_params = label_patterns + archive_patterns
        type_filter = ""
        type_params = ()
        paper_type = request.args.get("paper_type", "")
        if paper_type == "arxiv":
            type_filter = " AND p.journal LIKE 'arXiv:%'"
        elif paper_type == "journal":
            type_filter = " AND p.journal NOT LIKE 'arXiv:%'"

        q = f"""
            SELECT p.url, p.journal, p.title, p.pushed_at,
                   e.title_zh, e.abstract_zh, e.summary_zh, e.abstract_en,
                   l.label
            FROM pushed_papers p
            JOIN ldb.paper_labels l ON p.url = l.url
            LEFT JOIN paper_evaluations e ON p.url = e.url
            WHERE {label_condition}
            {type_filter}
            ORDER BY p.pushed_at DESC
            LIMIT ? OFFSET ?
        """
        rows = pc.execute(q, (*label_params, limit, offset)).fetchall()

        q_count = f"SELECT COUNT(*) FROM ldb.paper_labels l JOIN pushed_papers p ON p.url = l.url WHERE {label_condition} {type_filter}"
        total = pc.execute(q_count, label_params).fetchone()[0]
        has_next = (offset + limit) < total
        translations = {k: v["status"] for k, v in _translate_jobs.items()}
        pc.execute("DETACH DATABASE ldb")

    uploads = _upload_index()
    result = []
    for url, journal, title, pushed_at, title_zh, abstract_zh, summary_zh, abstract_en, paper_label in rows:
        arxiv_match = re.search(r'abs/([^/?v]+)', url)
        arxiv_id = arxiv_match.group(1) if arxiv_match else None
        upload = uploads.get(url)
        result.append({
            "url": url,
            "label_key": url,
            "source_type": RSS_SOURCE,
            "journal": journal or "",
            "title": title or "",
            "pushed_at": pushed_at,
            "title_zh": title_zh or "",
            "abstract_zh": abstract_zh or "",
            "summary_zh": summary_zh or "",
            "abstract_en": abstract_en or "",
            "abstract_original": abstract_en or "",
            "label": paper_label or label,
            "is_arxiv": (journal or "").startswith("arXiv:"),
            "translation_status": translations.get(arxiv_id) if arxiv_id else None,
            "upload_translation_status": upload["status"] if upload else None,
            "upload_job_id": upload["job_id"] if upload else None,
        })
    return jsonify({"label": label, "papers": result, "has_next": has_next})


@app.route("/search")
def search_papers():
    q_str = request.args.get("q", "").strip()
    page = int(request.args.get("page", 1))
    limit = 20
    offset = (page - 1) * limit

    if not q_str:
        return jsonify({"query": q_str, "papers": [], "has_next": False})

    with papers_conn() as pc:
        pc.execute("ATTACH DATABASE ? AS ldb", (str(LABELS_DB),))
        
        type_filter = ""
        paper_type = request.args.get("paper_type", "")
        if paper_type == "arxiv":
            type_filter = " AND p.journal LIKE 'arXiv:%'"
        elif paper_type == "journal":
            type_filter = " AND p.journal NOT LIKE 'arXiv:%'"

        like_str = f"%{q_str}%"
        archive_patterns = (
            ARCHIVED_LABEL,
            f"{ARCHIVED_LABEL},%",
            f"%,{ARCHIVED_LABEL},%",
            f"%,{ARCHIVED_LABEL}",
        )
        archive_condition = "NOT (l.label = ? OR l.label LIKE ? OR l.label LIKE ? OR l.label LIKE ?)"
        q_sql = f"""
            SELECT p.url, p.journal, p.title, p.pushed_at,
                   e.title_zh, e.abstract_zh, e.summary_zh, e.abstract_en,
                   IFNULL(l.label, '不相关') as label
            FROM pushed_papers p
            LEFT JOIN paper_evaluations e ON p.url = e.url
            LEFT JOIN ldb.paper_labels l ON p.url = l.url
            WHERE (p.title LIKE ?
               OR p.journal LIKE ?
               OR e.title_zh LIKE ?
               OR e.abstract_zh LIKE ?
               OR e.summary_zh LIKE ?
               OR e.abstract_en LIKE ?)
               AND {archive_condition}
               {type_filter}
            ORDER BY p.pushed_at DESC
            LIMIT ? OFFSET ?
        """
        params = (like_str, like_str, like_str, like_str, like_str, like_str, *archive_patterns, limit, offset)
        rows = pc.execute(q_sql, params).fetchall()
        
        q_count = f"""
            SELECT COUNT(*)
            FROM pushed_papers p
            LEFT JOIN paper_evaluations e ON p.url = e.url
            LEFT JOIN ldb.paper_labels l ON p.url = l.url
            WHERE (p.title LIKE ? OR p.journal LIKE ? OR e.title_zh LIKE ? OR e.abstract_zh LIKE ? OR e.summary_zh LIKE ? OR e.abstract_en LIKE ?)
              AND {archive_condition}
              {type_filter}
        """
        total = pc.execute(q_count, (like_str, like_str, like_str, like_str, like_str, like_str, *archive_patterns)).fetchone()[0]
        has_next = (offset + limit) < total
        translations = {k: v["status"] for k, v in _translate_jobs.items()}
        pc.execute("DETACH DATABASE ldb")

    uploads = _upload_index()
    result = []
    for url, journal, title, pushed_at, title_zh, abstract_zh, summary_zh, abstract_en, label in rows:
        arxiv_match = re.search(r'abs/([^/?v]+)', url)
        arxiv_id = arxiv_match.group(1) if arxiv_match else None
        upload = uploads.get(url)
        result.append({
            "url": url,
            "label_key": url,
            "source_type": RSS_SOURCE,
            "journal": journal or "",
            "title": title or "",
            "pushed_at": pushed_at,
            "title_zh": title_zh or "",
            "abstract_zh": abstract_zh or "",
            "summary_zh": summary_zh or "",
            "abstract_en": abstract_en or "",
            "abstract_original": abstract_en or "",
            "label": label,
            "is_arxiv": (journal or "").startswith("arXiv:"),
            "translation_status": translations.get(arxiv_id) if arxiv_id else None,
            "upload_translation_status": upload["status"] if upload else None,
            "upload_job_id": upload["job_id"] if upload else None,
        })
    return jsonify({"query": q_str, "papers": result, "has_next": has_next})


@app.route("/label", methods=["POST"])
def label():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    
    labels = data.get("labels", [])
    if isinstance(labels, str):
        labels = [labels]
        
    valid_selected = [l for l in labels if l in VALID_LABELS]
    if not url or not valid_selected:
        return jsonify({"ok": False, "error": "invalid"}), 400
        
    lbl_str = ",".join(valid_selected)
    now = datetime.now(tz=timezone.utc).isoformat()
    with labels_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO paper_labels (url, label, labeled_at) VALUES (?,?,?)",
            (url, lbl_str, now),
        )
    return jsonify({"ok": True})


TRANSLATE_OUTPUT_DIR = BASE / "data" / "translations"


def _save_translation_to_db(arxiv_id: str, status: str, pdf_path: str = None, error: str = None):
    now = datetime.now(tz=timezone.utc).isoformat()
    with papers_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO pdf_translations (arxiv_id, status, pdf_path, error, created_at) VALUES (?,?,?,?,?)",
            (arxiv_id, status, pdf_path, error, now),
        )


def _load_cached_translations():
    """Restore completed translation jobs from DB on startup."""
    with papers_conn() as c:
        rows = c.execute("SELECT arxiv_id, status, pdf_path, error FROM pdf_translations").fetchall()
    for arxiv_id, status, pdf_path, error in rows:
        if status == "done" and pdf_path and os.path.exists(pdf_path):
            _translate_jobs[arxiv_id] = {"status": "done", "pdf": pdf_path}
        elif status == "error":
            _translate_jobs[arxiv_id] = {"status": "error", "error": error or "unknown"}
        # skip 'running' from previous crash — treat as not started
    logging.getLogger(__name__).info(f"Restored {len(_translate_jobs)} cached translation jobs from DB")


def _run_translate_job(arxiv_id: str, arxiv_abs_url: str):
    from pdf_translate import translate_arxiv_pdf
    _translate_jobs[arxiv_id] = {"status": "running"}
    result = translate_arxiv_pdf(arxiv_abs_url, str(TRANSLATE_OUTPUT_DIR))
    if result["ok"]:
        _translate_jobs[arxiv_id] = {"status": "done", "pdf": result["pdf"]}
        _save_translation_to_db(arxiv_id, "done", pdf_path=result["pdf"])
    else:
        _translate_jobs[arxiv_id] = {"status": "error", "error": result["error"]}
        _save_translation_to_db(arxiv_id, "error", error=result["error"])


@app.route("/translate_pdf", methods=["POST"])
def translate_pdf():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url or "/abs/" not in url:
        return jsonify({"ok": False, "error": "invalid arxiv url"}), 400
    arxiv_id = url.rstrip("/").split("/abs/")[-1].split("v")[0]
    job = _translate_jobs.get(arxiv_id)
    if job and job["status"] in ("running", "done"):
        return jsonify({"ok": True, "status": job["status"], "arxiv_id": arxiv_id})
    TRANSLATE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    t = threading.Thread(target=_run_translate_job, args=(arxiv_id, url), daemon=True)
    t.start()
    return jsonify({"ok": True, "status": "pending", "arxiv_id": arxiv_id})


@app.route("/translate_status/<arxiv_id>")
def translate_status(arxiv_id):
    job = _translate_jobs.get(arxiv_id)
    if not job:
        return jsonify({"status": "not_found"})
    if job["status"] == "done":
        return jsonify({"status": "done", "arxiv_id": arxiv_id})
    if job["status"] == "error":
        return jsonify({"status": "error", "error": job.get("error", "")})
    return jsonify({"status": job["status"]})


@app.route("/download_translated_pdf/<arxiv_id>")
def download_translated_pdf(arxiv_id):
    job = _translate_jobs.get(arxiv_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "not ready"}), 404
    return send_file(job["pdf"], as_attachment=False,
                     download_name=f"{arxiv_id}_zh.pdf", mimetype="application/pdf")


# ── Upload Paper Translation ───────────────────────────────────────────────

UPLOAD_TRANSLATE_DIR = BASE / "data" / "upload_translations"

_UPLOAD_METADATA_ERROR_UNSET = object()


def _save_upload_translation_to_db(
    job_id: str,
    filename: str,
    status: str,
    pdf_path: str = None,
    error: str = None,
    *,
    source_type: str | None = None,
    paper_url: str | None = None,
    original_pdf_path: str | None = None,
    pdf_sha256: str | None = None,
    metadata_status: str | None = None,
    metadata_stage: str | None = None,
    metadata_error: str | None | object = _UPLOAD_METADATA_ERROR_UNSET,
    tex_dir: str | None = None,
):
    now = _utc_now()
    with papers_conn() as c:
        existing = c.execute(
            "SELECT source_type, paper_url, original_pdf_path, pdf_sha256, metadata_status, "
            "metadata_stage, metadata_error, tex_dir FROM upload_translations WHERE job_id=?",
            (job_id,),
        ).fetchone()
        old = existing or (RSS_SOURCE, None, None, None, "not_applicable", None, None, None)
        values = (
            source_type or old[0] or RSS_SOURCE,
            paper_url if paper_url is not None else old[1],
            original_pdf_path if original_pdf_path is not None else old[2],
            pdf_sha256 if pdf_sha256 is not None else old[3],
            metadata_status if metadata_status is not None else old[4] or "not_applicable",
            metadata_stage if metadata_stage is not None else old[5],
            metadata_error if metadata_error is not _UPLOAD_METADATA_ERROR_UNSET else old[6],
            tex_dir if tex_dir is not None else old[7],
        )
        c.execute(
            """INSERT INTO upload_translations
            (job_id, filename, status, pdf_path, error, created_at, source_type,
             paper_url, original_pdf_path, pdf_sha256, metadata_status, metadata_stage,
             metadata_error, tex_dir, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
              filename=excluded.filename, status=excluded.status, pdf_path=excluded.pdf_path,
              error=excluded.error, source_type=excluded.source_type, paper_url=excluded.paper_url,
              original_pdf_path=excluded.original_pdf_path, pdf_sha256=excluded.pdf_sha256,
              metadata_status=excluded.metadata_status, metadata_stage=excluded.metadata_stage,
              metadata_error=excluded.metadata_error, tex_dir=excluded.tex_dir,
              updated_at=excluded.updated_at""",
            (job_id, filename, status, pdf_path, error, now, *values, now),
        )


def _load_cached_upload_translations():
    """Restore persisted upload state and mark interrupted work as recoverable."""
    with papers_conn() as c:
        rows = c.execute(
            "SELECT job_id, filename, status, pdf_path, error, source_type, paper_url, "
            "original_pdf_path, metadata_status, metadata_stage, metadata_error, tex_dir, pdf_sha256 "
            "FROM upload_translations"
        ).fetchall()
    restored = 0
    for (
        job_id, filename, status, pdf_path, error, source_type, paper_url,
        original_pdf_path, metadata_status, metadata_stage, metadata_error, tex_dir, pdf_sha256,
    ) in rows:
        metadata_status = metadata_status or "not_applicable"
        if metadata_status in {"pending", "parsing", "identifying"}:
            metadata_status = "interrupted"
            metadata_stage = "interrupted"
            metadata_error = "服务重启时任务中断，可重试"
            _save_upload_translation_to_db(
                job_id, filename, status, pdf_path=pdf_path, error=error,
                source_type=source_type, paper_url=paper_url,
                original_pdf_path=original_pdf_path, metadata_status=metadata_status,
                metadata_stage=metadata_stage, metadata_error=metadata_error, tex_dir=tex_dir,
            )
        if status in {"pending", "running"}:
            status = "interrupted"
            error = error or "服务重启时全文翻译任务中断，可重试"
            _save_upload_translation_to_db(
                job_id, filename, status, pdf_path=pdf_path, error=error,
                source_type=source_type, paper_url=paper_url,
                original_pdf_path=original_pdf_path, metadata_status=metadata_status,
                metadata_stage=metadata_stage, metadata_error=metadata_error, tex_dir=tex_dir,
            )
        state = {
            "status": status,
            "filename": filename,
            "source_type": source_type or RSS_SOURCE,
            "paper_url": paper_url,
            "original_pdf_path": original_pdf_path or str(_legacy_original_path(job_id)),
            "metadata_status": metadata_status,
            "metadata_stage": metadata_stage,
            "metadata_error": metadata_error,
            "tex_dir": tex_dir,
            "pdf_sha256": pdf_sha256,
        }
        if status == "done" and pdf_path and os.path.exists(pdf_path):
            state["pdf"] = pdf_path
        elif pdf_path:
            state["pdf"] = pdf_path
        if error:
            state["error"] = error
        with _upload_jobs_state_lock:
            _upload_jobs[job_id] = state
        restored += 1
    logging.getLogger(__name__).info(f"Restored {restored} cached upload translation jobs from DB")


def _set_upload_memory_state(job_id: str, **updates):
    with _upload_jobs_state_lock:
        state = dict(_upload_jobs.get(job_id, {}))
        state.update(updates)
        _upload_jobs[job_id] = state
        return dict(state)


def _run_upload_translate_job(job_id: str, pdf_path: str, filename: str):
    from pdf_translate import translate_uploaded_pdf
    row = _upload_record(job_id)
    if not row:
        return
    source_type = row[5] or RSS_SOURCE
    paper_url = row[6]
    original_pdf_path = row[7] or str(_legacy_original_path(job_id))
    metadata_status = row[8] or "not_applicable"
    with _upload_job_locks[job_id]:
        current = _upload_record(job_id)
        if not current or current[2] == "deleting":
            return
        _set_upload_memory_state(
            job_id, status="running", filename=filename, source_type=source_type,
            paper_url=paper_url, original_pdf_path=original_pdf_path,
            metadata_status=metadata_status,
        )
        _save_upload_translation_to_db(
            job_id, filename, "running", pdf_path=current[3], error=None,
            source_type=source_type, paper_url=paper_url,
            original_pdf_path=original_pdf_path, metadata_status=metadata_status,
        )
    result = translate_uploaded_pdf(pdf_path, str(UPLOAD_TRANSLATE_DIR), job_id)
    with _upload_job_locks[job_id]:
        current = _upload_record(job_id)
        if not current or current[2] == "deleting":
            return
        if result["ok"]:
            _set_upload_memory_state(
                job_id, status="done", pdf=result["pdf"], filename=filename,
                source_type=source_type, paper_url=paper_url,
                original_pdf_path=original_pdf_path, metadata_status=metadata_status,
            )
            _save_upload_translation_to_db(
                job_id, filename, "done", pdf_path=result["pdf"], error=None,
                source_type=source_type, paper_url=paper_url,
                original_pdf_path=original_pdf_path, metadata_status=metadata_status,
            )
        else:
            _set_upload_memory_state(
                job_id, status="error", error=result["error"], filename=filename,
                source_type=source_type, paper_url=paper_url,
                original_pdf_path=original_pdf_path, metadata_status=metadata_status,
            )
            _save_upload_translation_to_db(
                job_id, filename, "error", pdf_path=current[3], error=result["error"],
                source_type=source_type, paper_url=paper_url,
                original_pdf_path=original_pdf_path, metadata_status=metadata_status,
            )


def _validate_upload_file(file_storage):
    filename = secure_filename(file_storage.filename or "")
    if not filename or not filename.lower().endswith(".pdf"):
        return None, "only PDF files are accepted"
    stream = file_storage.stream
    position = stream.tell()
    header = stream.read(5)
    stream.seek(position)
    if header != b"%PDF-":
        return None, "file is not a valid PDF"
    return filename, None


@app.route("/upload_paper", methods=["POST"])
def upload_paper():
    url = (request.form.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "no url provided"}), 400
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no file provided"}), 400
    file_storage = request.files["file"]
    filename, error = _validate_upload_file(file_storage)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    job_id = hashlib.md5(url.encode()).hexdigest()
    pdf_path = _legacy_original_path(job_id)
    with _upload_job_locks[job_id]:
        existing = _upload_record(job_id)
        if existing:
            return jsonify({"ok": False, "error": "existing upload must be deleted before replacement"}), 409
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = UPLOAD_DIR / f".{job_id}.{uuid.uuid4().hex}.upload"
        try:
            file_storage.save(tmp_path)
            if tmp_path.stat().st_size > MAX_UPLOAD_BYTES:
                return jsonify({"ok": False, "error": "file is too large"}), 413
            os.replace(tmp_path, pdf_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        digest = _sha256_file(pdf_path)
        _save_upload_translation_to_db(
            job_id, filename, "uploaded", pdf_path=None, error=None,
            source_type=RSS_SOURCE, paper_url=url, original_pdf_path=str(pdf_path),
            pdf_sha256=digest, metadata_status="not_applicable", metadata_stage="uploaded",
        )
        _set_upload_memory_state(
            job_id, status="uploaded", filename=filename, source_type=RSS_SOURCE,
            paper_url=url, original_pdf_path=str(pdf_path), metadata_status="not_applicable",
            metadata_stage="uploaded",
        )
    return jsonify({"ok": True, "job_id": job_id, "filename": filename, "status": "uploaded"})


def _manual_tex_dir_from_record(job_id: str, stored_tex_dir: str | None) -> str | None:
    if not stored_tex_dir:
        return None
    expected_root = (_owned_work_path(job_id) / "tex_src").resolve()
    candidate = Path(stored_tex_dir)
    try:
        if candidate.resolve() != expected_root:
            return None
    except OSError:
        return None
    if not candidate.is_dir() or not any(candidate.rglob("*.tex")):
        return None
    return str(candidate)


def _run_manual_metadata_job(job_id: str):
    """Convert one manual PDF and persist a card without running full translation."""
    import paper_metadata
    import pdf_translate

    with _manual_jobs_semaphore:
        row = _upload_record(job_id)
        if not row or row[5] != MANUAL_SOURCE:
            return
        current_stage = "parsing"
        with _upload_job_locks[job_id]:
            row = _upload_record(job_id)
            if not row or row[2] == "deleting":
                return
            if row[8] in {"parsing", "identifying", "deleting"}:
                return
            _save_upload_translation_to_db(
                job_id, row[1], row[2], pdf_path=row[3], error=None,
                source_type=MANUAL_SOURCE, paper_url=None,
                original_pdf_path=row[7], pdf_sha256=row[12],
                metadata_status="parsing", metadata_stage="parsing",
                metadata_error=None, tex_dir=row[11],
            )
            _set_upload_memory_state(
                job_id, status=row[2], filename=row[1], source_type=MANUAL_SOURCE,
                original_pdf_path=row[7], error=None, metadata_status="parsing", metadata_stage="parsing",
            )
        try:
            pdf_path = row[7]
            if not pdf_path or not Path(pdf_path).is_file():
                raise paper_metadata.MetadataError("original PDF is missing")
            pdf_sha256 = row[12] or _sha256_file(Path(pdf_path))
            tex_dir = _manual_tex_dir_from_record(job_id, row[11])
            if tex_dir is None:
                current_stage = "parsing"
                work_dir = _owned_work_path(job_id)
                work_dir.mkdir(parents=True, exist_ok=True)
                tex_dir = pdf_translate.pdf_to_latex_dir(
                    pdf_path, str(work_dir), source_sha256=pdf_sha256
                )
            with _upload_job_locks[job_id]:
                current = _upload_record(job_id)
                if not current or current[2] == "deleting":
                    return
                current_stage = "identifying"
                _save_upload_translation_to_db(
                    job_id, current[1], current[2], pdf_path=current[3], error=None,
                    source_type=MANUAL_SOURCE, original_pdf_path=current[7],
                    pdf_sha256=pdf_sha256, metadata_status="identifying",
                    metadata_stage="identifying", metadata_error=None, tex_dir=tex_dir,
                )
                _set_upload_memory_state(
                    job_id, status=current[2], filename=current[1], source_type=MANUAL_SOURCE,
                    original_pdf_path=current[7], error=None, metadata_status="identifying",
                    metadata_stage="identifying",
                )
            metadata = paper_metadata.recognize_metadata(tex_dir)
            now = _utc_now()
            with _upload_job_locks[job_id]:
                current = _upload_record(job_id)
                if not current or current[2] == "deleting":
                    return
                with papers_conn() as c:
                    c.execute(
                        """INSERT INTO manual_papers
                        (job_id, original_title, title_zh, abstract_original, abstract_zh,
                         summary_zh, source_language, metadata_incomplete, created_at, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(job_id) DO UPDATE SET
                          original_title=excluded.original_title, title_zh=excluded.title_zh,
                          abstract_original=excluded.abstract_original, abstract_zh=excluded.abstract_zh,
                          summary_zh=excluded.summary_zh, source_language=excluded.source_language,
                          metadata_incomplete=excluded.metadata_incomplete, updated_at=excluded.updated_at""",
                        (
                            job_id, metadata["original_title"], metadata["title_zh"],
                            metadata["abstract_original"], metadata["abstract_zh"],
                            metadata["summary_zh"], metadata["source_language"],
                            int(metadata["metadata_incomplete"]), now, now,
                        ),
                    )
                _save_upload_translation_to_db(
                    job_id, current[1], current[2], pdf_path=current[3], error=None,
                    source_type=MANUAL_SOURCE, original_pdf_path=current[7],
                    pdf_sha256=pdf_sha256, metadata_status="done",
                    metadata_stage="card_ready", metadata_error=None, tex_dir=tex_dir,
                )
                _set_upload_memory_state(
                    job_id, status=current[2], filename=current[1], source_type=MANUAL_SOURCE,
                    original_pdf_path=current[7], error=None, metadata_status="done",
                    metadata_stage="card_ready",
                )
        except Exception as exc:
            logging.getLogger(__name__).exception("manual metadata job failed: %s", job_id)
            with _upload_job_locks[job_id]:
                current = _upload_record(job_id)
                if current and current[2] != "deleting":
                    error = str(exc)[:2000]
                    _save_upload_translation_to_db(
                        job_id, current[1], current[2], pdf_path=current[3], error=current[4],
                        source_type=MANUAL_SOURCE, original_pdf_path=current[7],
                        pdf_sha256=current[12], metadata_status="error",
                        metadata_stage=current_stage, metadata_error=error, tex_dir=current[11],
                    )
                    _set_upload_memory_state(
                        job_id, status=current[2], filename=current[1], source_type=MANUAL_SOURCE,
                        original_pdf_path=current[7], metadata_status="error",
                        metadata_stage=current_stage, metadata_error=error,
                    )


def _start_manual_metadata_job(job_id: str) -> bool:
    with _upload_job_locks[job_id]:
        row = _upload_record(job_id)
        if not row or row[5] != MANUAL_SOURCE:
            return False
        if row[2] in {"pending", "running"} and row[8] in {"parsing", "identifying"}:
            return False
        _save_upload_translation_to_db(
            job_id, row[1], row[2], pdf_path=row[3], error=row[4],
            source_type=MANUAL_SOURCE, original_pdf_path=row[7], pdf_sha256=row[12],
            metadata_status="pending", metadata_stage="queued", metadata_error=None,
            tex_dir=row[11],
        )
        _set_upload_memory_state(
            job_id, status=row[2], filename=row[1], source_type=MANUAL_SOURCE,
            original_pdf_path=row[7], metadata_status="pending", metadata_stage="queued",
        )
    threading.Thread(target=_run_manual_metadata_job, args=(job_id,), daemon=True).start()
    return True


@app.route("/api/admin/uploads", methods=["POST"])
def admin_manual_upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no file provided"}), 400
    file_storage = request.files["file"]
    filename, error = _validate_upload_file(file_storage)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    job_id = uuid.uuid4().hex
    pdf_path = _legacy_original_path(job_id)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = UPLOAD_DIR / f".{job_id}.{uuid.uuid4().hex}.upload"
    try:
        file_storage.save(tmp_path)
        if tmp_path.stat().st_size > MAX_UPLOAD_BYTES:
            return jsonify({"ok": False, "error": "file is too large"}), 413
        os.replace(tmp_path, pdf_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    digest = _sha256_file(pdf_path)
    _save_upload_translation_to_db(
        job_id, filename, "uploaded", pdf_path=None, error=None,
        source_type=MANUAL_SOURCE, paper_url=None, original_pdf_path=str(pdf_path),
        pdf_sha256=digest, metadata_status="pending", metadata_stage="queued",
    )
    _set_upload_memory_state(
        job_id, status="uploaded", filename=filename, source_type=MANUAL_SOURCE,
        original_pdf_path=str(pdf_path), metadata_status="pending", metadata_stage="queued",
    )
    if not _start_manual_metadata_job(job_id):
        return jsonify({"ok": False, "error": "could not start metadata job", "job_id": job_id}), 500
    return jsonify({"ok": True, "job_id": job_id, "filename": filename, "status": "pending"}), 202


@app.route("/api/admin/uploads/<job_id>/process", methods=["POST"])
def retry_manual_metadata(job_id):
    if not _valid_upload_job_id(job_id):
        return jsonify({"ok": False, "error": "invalid job id"}), 400
    row = _upload_record(job_id)
    if not row:
        return jsonify({"ok": False, "error": "upload not found"}), 404
    if row[5] != MANUAL_SOURCE:
        return jsonify({"ok": False, "error": "only manual uploads have metadata processing"}), 400
    if row[8] in {"parsing", "identifying", "pending"}:
        return jsonify({"ok": True, "status": row[8]}), 200
    if not _start_manual_metadata_job(job_id):
        return jsonify({"ok": False, "error": "could not start metadata job"}), 409
    return jsonify({"ok": True, "status": "pending"}), 202


@app.route("/api/manual-papers")
def manual_papers_list():
    with papers_conn() as c:
        rows = c.execute(
            """SELECT m.job_id, m.original_title, m.title_zh, m.abstract_original,
                      m.abstract_zh, m.summary_zh, m.source_language, m.metadata_incomplete,
                      m.created_at, u.filename, u.status, u.pdf_path
               FROM manual_papers m JOIN upload_translations u ON u.job_id=m.job_id
               ORDER BY m.created_at DESC"""
        ).fetchall()
    with labels_conn() as c:
        labels = dict(c.execute(
            "SELECT url,label FROM paper_labels WHERE url LIKE ?", (MANUAL_LABEL_PREFIX + "%",)
        ).fetchall())
    papers = []
    for (
        job_id, original_title, title_zh, abstract_original, abstract_zh, summary_zh,
        source_language, metadata_incomplete, created_at, filename, full_status, pdf_path,
    ) in rows:
        url = f"/download_original_pdf?job_id={job_id}"
        papers.append({
            "job_id": job_id,
            "url": url,
            "label_key": _manual_label_key(job_id),
            "label": labels.get(_manual_label_key(job_id), "不相关"),
            "source_type": MANUAL_SOURCE,
            "title": original_title,
            "title_zh": title_zh,
            "abstract_original": abstract_original,
            "abstract_en": abstract_original,
            "abstract_zh": abstract_zh,
            "summary_zh": summary_zh,
            "source_language": source_language,
            "metadata_incomplete": bool(metadata_incomplete),
            "journal": "手动上传",
            "pushed_at": created_at,
            "is_arxiv": False,
            "upload_job_id": job_id,
            "upload_translation_status": full_status if pdf_path else None,
        })
    return jsonify({"papers": papers})


@app.route("/translate_uploaded_pdf", methods=["POST"])
def translate_uploaded_pdf_route():
    data = request.get_json(silent=True) or {}
    requested_job_id = (data.get("job_id") or "").strip()
    url = (data.get("url") or "").strip()
    if requested_job_id:
        if not _valid_upload_job_id(requested_job_id):
            return jsonify({"ok": False, "error": "invalid job id"}), 400
        job_id = requested_job_id
        row = _upload_record(job_id)
        if not row:
            return jsonify({"ok": False, "error": "file not uploaded"}), 404
    elif url:
        job_id = hashlib.md5(url.encode()).hexdigest()
        row = _upload_record(job_id)
        if not row:
            return jsonify({"ok": False, "error": "file not uploaded"}), 400
    else:
        return jsonify({"ok": False, "error": "invalid upload reference"}), 400
    with _upload_job_locks[job_id]:
        row = _upload_record(job_id)
        if not row:
            return jsonify({"ok": False, "error": "file not uploaded"}), 404
        full_status = row[2]
        if full_status in {"pending", "running"}:
            return jsonify({"ok": True, "status": full_status, "job_id": job_id})
        if full_status == "done" and row[3] and Path(row[3]).is_file():
            return jsonify({"ok": True, "status": "done", "job_id": job_id})
        if full_status not in {"uploaded", "error", "interrupted", "done"}:
            return jsonify({"ok": False, "error": "file not ready for translation"}), 409
        if not row[7] or not Path(row[7]).is_file():
            return jsonify({"ok": False, "error": "original PDF is missing"}), 404
        _save_upload_translation_to_db(
            job_id, row[1], "pending", pdf_path=row[3], error=None,
            source_type=row[5], paper_url=row[6], original_pdf_path=row[7],
            pdf_sha256=row[12], metadata_status=row[8], metadata_stage=row[9],
            metadata_error=row[10], tex_dir=row[11],
        )
        _set_upload_memory_state(
            job_id, status="pending", filename=row[1], source_type=row[5],
            paper_url=row[6], original_pdf_path=row[7], metadata_status=row[8],
            metadata_stage=row[9],
        )
        threading.Thread(
            target=_run_upload_translate_job,
            args=(job_id, row[7], row[1]), daemon=True,
        ).start()
    return jsonify({"ok": True, "status": "pending", "job_id": job_id})


@app.route("/upload_translate_status")
def upload_translate_status():
    requested_job_id = (request.args.get("job_id") or "").strip()
    url = (request.args.get("url") or "").strip()
    if requested_job_id:
        if not _valid_upload_job_id(requested_job_id):
            return jsonify({"status": "not_found"})
        job_id = requested_job_id
    elif url:
        job_id = hashlib.md5(url.encode()).hexdigest()
    else:
        return jsonify({"status": "not_found"})
    row = _upload_record(job_id)
    if not row:
        return jsonify({"status": "not_found"})
    return jsonify({
        "status": row[2], "job_id": job_id, "filename": row[1],
        "error": row[4] or "", "source_type": row[5] or RSS_SOURCE,
    })


def _translated_pdf_path(row) -> Path | None:
    job_id, _filename, _status, pdf_path = row[0], row[1], row[2], row[3]
    if not pdf_path:
        return None
    candidate = Path(pdf_path)
    try:
        candidate.resolve().relative_to(_owned_work_path(job_id).resolve())
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


@app.route("/download_uploaded_pdf")
def download_uploaded_pdf():
    requested_job_id = (request.args.get("job_id") or "").strip()
    url = (request.args.get("url") or "").strip()
    if requested_job_id:
        if not _valid_upload_job_id(requested_job_id):
            return jsonify({"error": "not ready"}), 404
        job_id = requested_job_id
    elif url:
        job_id = hashlib.md5(url.encode()).hexdigest()
    else:
        return jsonify({"error": "not ready"}), 404
    row = _upload_record(job_id)
    pdf_path = _translated_pdf_path(row) if row else None
    if not row or row[2] != "done" or pdf_path is None:
        return jsonify({"error": "not ready"}), 404
    base = os.path.splitext(row[1] or "paper")[0]
    return send_file(pdf_path, as_attachment=False,
                     download_name=f"{base}_zh.pdf", mimetype="application/pdf")


@app.route("/download_original_pdf")
def download_original_pdf():
    requested_job_id = (request.args.get("job_id") or "").strip()
    url = (request.args.get("url") or "").strip()
    if requested_job_id:
        if not _valid_upload_job_id(requested_job_id):
            return jsonify({"error": "not found"}), 404
        job_id = requested_job_id
    elif url:
        job_id = hashlib.md5(url.encode()).hexdigest()
    else:
        return jsonify({"error": "not found"}), 404
    row = _upload_record(job_id)
    if not row:
        return jsonify({"error": "file not found"}), 404
    try:
        pdf_path = _owned_original_path(job_id, row[7])
    except ValueError:
        return jsonify({"error": "file not found"}), 404
    if not pdf_path.is_file():
        return jsonify({"error": "file not found"}), 404
    return send_file(pdf_path, as_attachment=False,
                     download_name=row[1] or f"{job_id}.pdf", mimetype="application/pdf")


def _valid_upload_job_id(job_id: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{32}", job_id or ""))


def _upload_record(job_id: str):
    with papers_conn() as c:
        return c.execute(
            "SELECT job_id, filename, status, pdf_path, error, source_type, paper_url, "
            "original_pdf_path, metadata_status, metadata_stage, metadata_error, tex_dir, pdf_sha256 "
            "FROM upload_translations WHERE job_id=?",
            (job_id,),
        ).fetchone()


def _owned_original_path(job_id: str, stored_path: str | None) -> Path:
    root = UPLOAD_DIR.resolve()
    path = Path(stored_path) if stored_path else _legacy_original_path(job_id)
    if path.parent.resolve() != root or path.name != f"{job_id}.pdf":
        raise ValueError("upload record points outside the owned upload directory")
    return path


def _owned_work_path(job_id: str) -> Path:
    root = UPLOAD_TRANSLATE_DIR.resolve()
    path = UPLOAD_TRANSLATE_DIR / job_id
    if path.parent.resolve() != root or path.name != job_id:
        raise ValueError("upload record points outside the owned translation directory")
    return path


def _remove_owned_path(path: Path):
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _delete_upload_record(job_id: str):
    """Delete one upload and only its owned artifacts; return a result dict."""
    if not _valid_upload_job_id(job_id):
        return {"ok": False, "status": 400, "error": "invalid job id"}
    lock = _upload_job_locks[job_id]
    with lock:
        row = _upload_record(job_id)
        if not row:
            with _upload_jobs_state_lock:
                _upload_jobs.pop(job_id, None)
            return {"ok": True, "already_deleted": True}
        (
            _row_id, filename, full_status, pdf_path, full_error, source_type, paper_url,
            original_pdf_path, metadata_status, metadata_stage, metadata_error, _tex_dir, _pdf_sha256,
        ) = row
        active = full_status in {"pending", "running"} or metadata_status in {
            "pending", "parsing", "identifying", "deleting"
        }
        if active:
            return {"ok": False, "status": 409, "error": "upload is still processing"}
        previous = (full_status, full_error, metadata_status, metadata_stage, metadata_error)
        with papers_conn() as c:
            c.execute(
                "UPDATE upload_translations SET status='deleting', metadata_status=?, "
                "metadata_stage='deleting', metadata_error=NULL, updated_at=? WHERE job_id=?",
                ("deleting", _utc_now(), job_id),
            )
        try:
            _remove_owned_path(_owned_original_path(job_id, original_pdf_path))
            _remove_owned_path(_owned_work_path(job_id))
        except (OSError, ValueError) as exc:
            with papers_conn() as c:
                c.execute(
                    "UPDATE upload_translations SET status=?, error=?, metadata_status=?, "
                    "metadata_stage=?, metadata_error=?, updated_at=? WHERE job_id=?",
                    (*previous, _utc_now(), job_id),
                )
            return {"ok": False, "status": 500, "error": f"cleanup failed: {exc}"}
        try:
            if source_type == MANUAL_SOURCE:
                with labels_conn() as c:
                    c.execute("DELETE FROM paper_labels WHERE url=?", (_manual_label_key(job_id),))
                with papers_conn() as c:
                    c.execute("DELETE FROM manual_papers WHERE job_id=?", (job_id,))
            with papers_conn() as c:
                c.execute("DELETE FROM upload_translations WHERE job_id=?", (job_id,))
        except sqlite3.Error as exc:
            with papers_conn() as c:
                c.execute(
                    "UPDATE upload_translations SET status=?, error=?, metadata_status=?, "
                    "metadata_stage=?, metadata_error=?, updated_at=? WHERE job_id=?",
                    (*previous, _utc_now(), job_id),
                )
            return {"ok": False, "status": 500, "error": f"database cleanup failed: {exc}"}
        with _upload_jobs_state_lock:
            _upload_jobs.pop(job_id, None)
        return {"ok": True, "job_id": job_id, "source_type": source_type or RSS_SOURCE}


@app.route("/api/admin/uploads/<job_id>", methods=["DELETE"])
def admin_delete_upload(job_id):
    result = _delete_upload_record(job_id)
    status = result.pop("status", 200)
    return jsonify(result), status


@app.route("/api/admin/uploads")
def admin_uploads_list():
    """Return persisted upload records for the authenticated management view."""
    with papers_conn() as c:
        rows = c.execute(
            """SELECT u.job_id, u.filename, u.status, u.error, u.created_at,
                      u.source_type, u.paper_url, u.original_pdf_path, u.pdf_path,
                      u.metadata_status, u.metadata_stage, u.metadata_error, u.updated_at,
                      m.original_title, m.title_zh, m.abstract_original, m.abstract_zh,
                      m.summary_zh, m.source_language, m.metadata_incomplete,
                      p.title, e.title_zh
               FROM upload_translations u
               LEFT JOIN manual_papers m ON m.job_id = u.job_id
               LEFT JOIN pushed_papers p ON p.url = u.paper_url
               LEFT JOIN paper_evaluations e ON e.url = u.paper_url
               ORDER BY u.created_at DESC, u.job_id DESC"""
        ).fetchall()
    uploads = []
    for row in rows:
        (
            job_id, filename, status, error, created_at, source_type, paper_url,
            original_pdf_path, translated_pdf_path, metadata_status, metadata_stage,
            metadata_error, updated_at, original_title, title_zh, abstract_original,
            abstract_zh, summary_zh, source_language, metadata_incomplete, rss_title,
            rss_title_zh,
        ) = row
        original_exists = bool(original_pdf_path and Path(original_pdf_path).is_file())
        translated_exists = bool(translated_pdf_path and Path(translated_pdf_path).is_file())
        uploads.append({
            "job_id": job_id,
            "filename": filename,
            "source_type": source_type or RSS_SOURCE,
            "paper_url": paper_url or "",
            "title": (original_title or rss_title or filename or "未命名论文"),
            "title_zh": title_zh or rss_title_zh or "",
            "status": status,
            "error": error or "",
            "created_at": created_at,
            "updated_at": updated_at or created_at,
            "metadata_status": metadata_status or "not_applicable",
            "metadata_stage": metadata_stage or "",
            "metadata_error": metadata_error or "",
            "metadata_incomplete": bool(metadata_incomplete),
            "source_language": source_language or "",
            "original_exists": original_exists,
            "translated_exists": translated_exists,
            "can_retry_metadata": (source_type == MANUAL_SOURCE and metadata_status in {
                "error", "interrupted", "uploaded"
            }),
            "can_delete": status not in {"pending", "running"} and metadata_status not in {
                "pending", "parsing", "identifying", "deleting"
            },
        })
    return jsonify({"uploads": uploads})


@app.route("/upload_jobs")
def upload_jobs_list():
    """Return all upload translation jobs for the legacy UI."""
    jobs = []
    with papers_conn() as c:
        rows = c.execute(
            "SELECT job_id, filename, status, error FROM upload_translations ORDER BY created_at DESC"
        ).fetchall()
    for jid, filename, status, error in rows:
        jobs.append({
            "job_id": jid,
            "filename": filename,
            "status": status,
            "error": error or "",
        })
    return jsonify({"jobs": jobs})


@app.route("/mark_read", methods=["POST"])
def mark_read():
    data = request.get_json(force=True)
    date = (data.get("date") or "").strip()
    if not date:
        return jsonify({"ok": False, "error": "invalid date"}), 400
    now = datetime.now(tz=timezone.utc).isoformat()
    with labels_conn() as c:
        # toggle: if already read, unmark; else mark
        existing = c.execute("SELECT date FROM date_reads WHERE date = ?", (date,)).fetchone()
        if existing:
            c.execute("DELETE FROM date_reads WHERE date = ?", (date,))
            return jsonify({"ok": True, "read": False})
        else:
            c.execute("INSERT OR REPLACE INTO date_reads (date, read_at) VALUES (?,?)", (date, now))
            return jsonify({"ok": True, "read": True})


@app.route("/read_dates")
def read_dates():
    with labels_conn() as c:
        rows = c.execute("SELECT date FROM date_reads").fetchall()
    return jsonify({"dates": [r[0] for r in rows]})


if __name__ == "__main__":
    init_dbs()
    _load_cached_translations()
    _load_cached_upload_translations()
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug:
        t = threading.Thread(target=_daily_fetch_job, daemon=True)
        t.start()
    app.run(debug=config.SERVER_DEBUG, port=config.SERVER_PORT)

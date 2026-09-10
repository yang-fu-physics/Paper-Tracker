import base64
import hashlib
import importlib.util
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

if "config" not in sys.modules:
    config_path = Path(__file__).with_name("config.example.py")
    spec = importlib.util.spec_from_file_location("config", config_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["config"] = module

import server


class UploadManagementPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.papers_db = root / "papers.db"
        self.labels_db = root / "labels.db"
        self.upload_dir = root / "uploads"
        self.translation_dir = root / "upload_translations"
        self.upload_dir.mkdir()
        self.translation_dir.mkdir()
        self.old_values = {
            "PAPERS_DB": server.PAPERS_DB,
            "LABELS_DB": server.LABELS_DB,
            "UPLOAD_DIR": server.UPLOAD_DIR,
            "UPLOAD_TRANSLATE_DIR": server.UPLOAD_TRANSLATE_DIR,
        }
        server.PAPERS_DB = self.papers_db
        server.LABELS_DB = self.labels_db
        server.UPLOAD_DIR = self.upload_dir
        server.UPLOAD_TRANSLATE_DIR = self.translation_dir
        server._upload_jobs.clear()
        server._translate_jobs.clear()

    def tearDown(self):
        for name, value in self.old_values.items():
            setattr(server, name, value)
        server._upload_jobs.clear()
        server._translate_jobs.clear()
        self.tmp.cleanup()

    def test_init_dbs_migrates_legacy_upload_rows_idempotently(self):
        with sqlite3.connect(self.papers_db) as db:
            db.execute(
                "CREATE TABLE upload_translations ("
                "job_id TEXT PRIMARY KEY, filename TEXT NOT NULL, status TEXT NOT NULL, "
                "pdf_path TEXT, error TEXT, created_at TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO upload_translations VALUES (?,?,?,?,?,?)",
                ("legacy-job", "mistake.pdf", "uploaded", None, None, "2026-01-01T00:00:00+00:00"),
            )
        (self.upload_dir / "legacy-job.pdf").write_bytes(b"%PDF-1.7\nlegacy")

        server.init_dbs()
        server.init_dbs()

        with sqlite3.connect(self.papers_db) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(upload_translations)")}
            row = db.execute(
                "SELECT source_type, original_pdf_path, metadata_status, pdf_sha256 "
                "FROM upload_translations WHERE job_id = ?",
                ("legacy-job",),
            ).fetchone()
            manual_table = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='manual_papers'"
            ).fetchone()
        self.assertTrue({"source_type", "paper_url", "original_pdf_path", "pdf_sha256", "metadata_status"} <= columns)
        self.assertEqual(row[0], "rss")
        self.assertEqual(row[1], str(self.upload_dir / "legacy-job.pdf"))
        self.assertEqual(row[2], "not_applicable")
        self.assertEqual(row[3], hashlib.sha256(b"%PDF-1.7\nlegacy").hexdigest())
        self.assertIsNotNone(manual_table)

        server._load_cached_upload_translations()
        self.assertEqual(server._upload_jobs["legacy-job"]["status"], "uploaded")
        self.assertEqual(server._upload_jobs["legacy-job"]["source_type"], "rss")

    def test_management_listing_reads_persisted_rows_not_memory_only(self):
        server.init_dbs()
        with sqlite3.connect(self.papers_db) as db:
            db.execute(
                "INSERT INTO upload_translations "
                "(job_id, filename, status, pdf_path, error, created_at, source_type, "
                "paper_url, original_pdf_path, metadata_status, metadata_stage) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "persisted-job", "wrong.pdf", "error", None, "compile failed",
                    "2026-01-01T00:00:00+00:00", "rss", "https://example.test/paper",
                    str(self.upload_dir / "persisted-job.pdf"), "not_applicable", "uploaded",
                ),
            )

        old_user, old_password = server.config.ACCESS_USERNAME, server.config.ACCESS_PASSWORD
        server.config.ACCESS_USERNAME = "test"
        server.config.ACCESS_PASSWORD = "secret"
        try:
            auth = base64.b64encode(b"test:secret").decode()
            with server.app.test_client() as client:
                response = client.get("/api/admin/uploads", headers={"Authorization": f"Basic {auth}"})
            self.assertEqual(response.status_code, 200)
            body = response.get_json()
            self.assertEqual(len(body["uploads"]), 1)
            self.assertEqual(body["uploads"][0]["job_id"], "persisted-job")
            self.assertEqual(body["uploads"][0]["status"], "error")
            self.assertEqual(body["uploads"][0]["source_type"], "rss")
        finally:
            server.config.ACCESS_USERNAME = old_user
            server.config.ACCESS_PASSWORD = old_password

    def test_paper_card_exposes_persisted_upload_identity(self):
        server.init_dbs()
        job_id = "e" * 32
        self._insert_upload(job_id, status="uploaded")
        response = self._authenticated_client().get("/papers?date=2026-01-01")
        self.assertEqual(response.status_code, 200)
        paper = response.get_json()["papers"][0]
        self.assertEqual(paper["upload_job_id"], job_id)
        self.assertEqual(paper["upload_translation_status"], "uploaded")

    def test_restart_marks_active_translation_and_metadata_as_interrupted(self):
        server.init_dbs()
        job_id = "d" * 32
        original = self.upload_dir / f"{job_id}.pdf"
        original.write_bytes(b"%PDF-1.7\ninterrupted")
        with sqlite3.connect(self.papers_db) as db:
            db.execute(
                "INSERT INTO upload_translations(job_id,filename,status,pdf_path,error,created_at,source_type,original_pdf_path,metadata_status,metadata_stage) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, "interrupted.pdf", "running", None, None, "2026-01-01T00:00:00+00:00", "manual", str(original), "parsing", "parsing"),
            )
        server._load_cached_upload_translations()
        with sqlite3.connect(self.papers_db) as db:
            row = db.execute(
                "SELECT status, metadata_status, metadata_stage, error, metadata_error FROM upload_translations WHERE job_id=?",
                (job_id,),
            ).fetchone()
        self.assertEqual(row[0], "interrupted")
        self.assertEqual(row[1], "interrupted")
        self.assertEqual(row[2], "interrupted")
        self.assertIn("中断", row[3])
        self.assertIn("中断", row[4])

    def _authenticated_client(self):
        old_user, old_password = server.config.ACCESS_USERNAME, server.config.ACCESS_PASSWORD
        server.config.ACCESS_USERNAME = "test"
        server.config.ACCESS_PASSWORD = "secret"
        self.addCleanup(setattr, server.config, "ACCESS_USERNAME", old_user)
        self.addCleanup(setattr, server.config, "ACCESS_PASSWORD", old_password)
        auth = base64.b64encode(b"test:secret").decode()
        client = server.app.test_client()
        client.environ_base["HTTP_AUTHORIZATION"] = f"Basic {auth}"
        return client

    def _insert_upload(self, job_id, source_type="rss", status="error", metadata_status="not_applicable"):
        original = self.upload_dir / f"{job_id}.pdf"
        original.write_bytes(b"%PDF-1.7\nowned test fixture")
        work = self.translation_dir / job_id
        work.mkdir()
        (work / "translated.pdf").write_bytes(b"translated")
        paper_url = "https://example.test/paper" if source_type == "rss" else None
        now = "2026-01-01T00:00:00+00:00"
        with sqlite3.connect(self.papers_db) as db:
            db.execute(
                "INSERT INTO upload_translations "
                "(job_id, filename, status, pdf_path, error, created_at, source_type, paper_url, "
                "original_pdf_path, pdf_sha256, metadata_status, metadata_stage) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id, "fixture.pdf", status, str(work / "translated.pdf"), None, now,
                    source_type, paper_url, str(original), hashlib.sha256(original.read_bytes()).hexdigest(),
                    metadata_status, "done" if metadata_status == "done" else "",
                ),
            )
        if source_type == "rss":
            with sqlite3.connect(self.papers_db) as db:
                db.execute(
                    "INSERT INTO pushed_papers(url,journal,title,pushed_at) VALUES (?,?,?,?)",
                    (paper_url, "Journal", "A kept paper", now),
                )
            with sqlite3.connect(self.labels_db) as db:
                db.execute(
                    "INSERT INTO paper_labels(url,label,labeled_at) VALUES (?,?,?)",
                    (paper_url, "感兴趣", now),
                )
        else:
            with sqlite3.connect(self.papers_db) as db:
                db.execute(
                    "INSERT INTO manual_papers(job_id,original_title,title_zh,abstract_original,abstract_zh,summary_zh,source_language,metadata_incomplete,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (job_id, "Manual paper", "手动论文", "abstract", "摘要", "总结", "en", 0, now, now),
                )
            with sqlite3.connect(self.labels_db) as db:
                db.execute(
                    "INSERT INTO paper_labels(url,label,labeled_at) VALUES (?,?,?)",
                    (server._manual_label_key(job_id), "可做", now),
                )

    def test_delete_rss_attachment_keeps_paper_and_label(self):
        server.init_dbs()
        self._insert_upload("a" * 32)
        client = self._authenticated_client()

        response = client.delete("/api/admin/uploads/" + "a" * 32)
        self.assertEqual(response.status_code, 200)
        self.assertFalse((self.upload_dir / ("a" * 32 + ".pdf")).exists())
        self.assertFalse((self.translation_dir / ("a" * 32)).exists())
        with sqlite3.connect(self.papers_db) as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM pushed_papers WHERE url=?", ("https://example.test/paper",)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM upload_translations WHERE job_id=?", ("a" * 32,)).fetchone())
        with sqlite3.connect(self.labels_db) as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM paper_labels WHERE url=?", ("https://example.test/paper",)).fetchone())
        self.assertEqual(client.delete("/api/admin/uploads/" + "a" * 32).status_code, 200)

    def test_delete_manual_upload_removes_card_and_label(self):
        server.init_dbs()
        job_id = "b" * 32
        self._insert_upload(job_id, source_type="manual", metadata_status="done")
        response = self._authenticated_client().delete("/api/admin/uploads/" + job_id)
        self.assertEqual(response.status_code, 200)
        with sqlite3.connect(self.papers_db) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM manual_papers WHERE job_id=?", (job_id,)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM upload_translations WHERE job_id=?", (job_id,)).fetchone())
        with sqlite3.connect(self.labels_db) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM paper_labels WHERE url=?", (server._manual_label_key(job_id),)).fetchone())

    def test_delete_rejects_active_job(self):
        server.init_dbs()
        job_id = "c" * 32
        self._insert_upload(job_id, status="running")
        response = self._authenticated_client().delete("/api/admin/uploads/" + job_id)
        self.assertEqual(response.status_code, 409)

    def test_legacy_upload_rejects_overwrite_until_old_attachment_is_deleted(self):
        server.init_dbs()
        url = "https://example.test/immutable-paper"
        job_id = hashlib.md5(url.encode()).hexdigest()
        self._insert_upload(job_id, status="done")
        client = self._authenticated_client()
        response = client.post(
            "/upload_paper",
            data={"url": url, "file": (io.BytesIO(b"%PDF-new"), "new.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual((self.upload_dir / f"{job_id}.pdf").read_bytes(), b"%PDF-1.7\nowned test fixture")

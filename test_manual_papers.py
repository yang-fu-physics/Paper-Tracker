import base64
import hashlib
import importlib.util
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

if "config" not in sys.modules:
    config_path = Path(__file__).with_name("config.example.py")
    spec = importlib.util.spec_from_file_location("config", config_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["config"] = module

import server


class ManualPaperTests(unittest.TestCase):
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
        server.init_dbs()
        old_user, old_password = server.config.ACCESS_USERNAME, server.config.ACCESS_PASSWORD
        server.config.ACCESS_USERNAME = "test"
        server.config.ACCESS_PASSWORD = "secret"
        self.addCleanup(setattr, server.config, "ACCESS_USERNAME", old_user)
        self.addCleanup(setattr, server.config, "ACCESS_PASSWORD", old_password)

    def tearDown(self):
        for name, value in self.old_values.items():
            setattr(server, name, value)
        server._upload_jobs.clear()
        self.tmp.cleanup()

    def client(self):
        auth = base64.b64encode(b"test:secret").decode()
        client = server.app.test_client()
        client.environ_base["HTTP_AUTHORIZATION"] = f"Basic {auth}"
        return client

    def upload(self, filename="fictional.pdf"):
        with patch.object(server, "_start_manual_metadata_job") as starter:
            response = self.client().post(
                "/api/admin/uploads",
                data={"file": (io.BytesIO(b"%PDF-1.7\nfictional"), filename)},
                content_type="multipart/form-data",
            )
        return response, starter

    def test_manual_upload_requires_real_pdf_and_creates_independent_record(self):
        fake = self.client().post(
            "/api/admin/uploads",
            data={"file": (io.BytesIO(b"not a pdf"), "fictional.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(fake.status_code, 400)

        response, starter = self.upload()
        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        job_id = body["job_id"]
        self.assertEqual(starter.call_args.args, (job_id,))
        with sqlite3.connect(self.papers_db) as db:
            row = db.execute(
                "SELECT source_type, metadata_status, original_pdf_path, pdf_sha256 FROM upload_translations WHERE job_id=?",
                (job_id,),
            ).fetchone()
        self.assertEqual(row[0], "manual")
        self.assertEqual(row[1], "pending")
        self.assertTrue(Path(row[2]).is_file())
        self.assertEqual(row[3], hashlib.sha256(b"%PDF-1.7\nfictional").hexdigest())

    def test_multiple_manual_uploads_create_independent_jobs(self):
        first, first_starter = self.upload("first.pdf")
        second, second_starter = self.upload("second.pdf")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        first_job = first.get_json()["job_id"]
        second_job = second.get_json()["job_id"]
        self.assertNotEqual(first_job, second_job)
        self.assertEqual(first_starter.call_args.args, (first_job,))
        self.assertEqual(second_starter.call_args.args, (second_job,))
        with sqlite3.connect(self.papers_db) as db:
            rows = db.execute(
                "SELECT job_id, filename FROM upload_translations ORDER BY filename"
            ).fetchall()
        self.assertEqual(rows, [(first_job, "first.pdf"), (second_job, "second.pdf")])

    def test_manual_metadata_worker_persists_card_and_reuses_cached_latex(self):
        response, _ = self.upload()
        job_id = response.get_json()["job_id"]
        work_dir = self.translation_dir / job_id
        tex_dir = work_dir / "tex_src"
        tex_dir.mkdir(parents=True)
        (tex_dir / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}fixture\\end{document}", encoding="utf-8"
        )
        original = self.upload_dir / f"{job_id}.pdf"
        metadata = {
            "original_title": "Fictional title",
            "title_zh": "虚构标题",
            "abstract_original": "Fictional abstract",
            "abstract_zh": "虚构摘要",
            "summary_zh": "虚构总结",
            "source_language": "en",
            "metadata_incomplete": False,
        }
        with patch("pdf_translate.pdf_to_latex_dir", return_value=str(tex_dir)) as parse_pdf, \
             patch("paper_metadata.recognize_metadata", return_value=metadata) as recognize:
            server._run_manual_metadata_job(job_id)
            server._run_manual_metadata_job(job_id)
        self.assertEqual(parse_pdf.call_count, 1)
        self.assertEqual(recognize.call_count, 2)
        with sqlite3.connect(self.papers_db) as db:
            row = db.execute(
                "SELECT metadata_status, tex_dir FROM upload_translations WHERE job_id=?", (job_id,)
            ).fetchone()
            card = db.execute(
                "SELECT original_title, title_zh, abstract_original, summary_zh FROM manual_papers WHERE job_id=?",
                (job_id,),
            ).fetchone()
        self.assertEqual(row[0], "done")
        self.assertEqual(row[1], str(tex_dir))
        self.assertEqual(card, ("Fictional title", "虚构标题", "Fictional abstract", "虚构总结"))
        self.assertTrue(original.exists())

    def test_manual_paper_endpoint_returns_card_fields_and_label_key(self):
        response, _ = self.upload()
        job_id = response.get_json()["job_id"]
        now = "2026-01-01T00:00:00+00:00"
        with sqlite3.connect(self.papers_db) as db:
            db.execute(
                "INSERT INTO manual_papers(job_id, original_title, title_zh, abstract_original, abstract_zh, summary_zh, source_language, metadata_incomplete, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, "Original", "中文", "Abstract", "摘要", "Summary", "en", 0, now, now),
            )
        with sqlite3.connect(self.labels_db) as db:
            db.execute(
                "INSERT INTO paper_labels(url,label,labeled_at) VALUES (?,?,?)",
                (server._manual_label_key(job_id), "感兴趣", now),
            )
        result = self.client().get("/api/manual-papers")
        self.assertEqual(result.status_code, 200)
        paper = result.get_json()["papers"][0]
        self.assertEqual(paper["source_type"], "manual")
        self.assertEqual(paper["title"], "Original")
        self.assertEqual(paper["title_zh"], "中文")
        self.assertEqual(paper["label_key"], server._manual_label_key(job_id))
        self.assertEqual(paper["label"], "感兴趣")
        self.assertIn("job_id=" + job_id, paper["url"])

    def test_manual_full_translation_route_accepts_job_id(self):
        response, _ = self.upload()
        job_id = response.get_json()["job_id"]
        original_path = self.upload_dir / f"{job_id}.pdf"
        with patch("server.threading.Thread") as thread:
            result = self.client().post(
                "/translate_uploaded_pdf", json={"job_id": job_id}
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["status"], "pending")
        self.assertTrue(thread.return_value.start.called)
        self.assertEqual(thread.call_args.kwargs["args"][0], job_id)
        self.assertEqual(thread.call_args.kwargs["args"][1], str(original_path))


if __name__ == "__main__":
    unittest.main()

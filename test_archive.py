import base64
import importlib.util
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


class ArchivePaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old_values = {"PAPERS_DB": server.PAPERS_DB, "LABELS_DB": server.LABELS_DB}
        server.PAPERS_DB = root / "papers.db"
        server.LABELS_DB = root / "labels.db"
        server.init_dbs()
        now = "2026-01-01T00:00:00+00:00"
        papers = [
            ("https://example.test/active", "Active", now),
            ("https://example.test/interest", "Interest", now),
            ("https://example.test/archive", "Archive", now),
            ("https://example.test/archive-interest", "Archive interest", now),
        ]
        with sqlite3.connect(server.PAPERS_DB) as db:
            db.executemany("INSERT INTO pushed_papers(url, journal, title, pushed_at) VALUES (?,?,?,?)", [
                (url, "Journal", title, pushed_at) for url, title, pushed_at in papers
            ])
        with sqlite3.connect(server.LABELS_DB) as db:
            db.executemany(
                "INSERT INTO paper_labels(url, label, labeled_at) VALUES (?,?,?)",
                [
                    (papers[0][0], "不相关", now),
                    (papers[1][0], "感兴趣", now),
                    (papers[2][0], "归档", now),
                    (papers[3][0], "归档,感兴趣", now),
                ],
            )
        self.old_auth = (server.config.ACCESS_USERNAME, server.config.ACCESS_PASSWORD)
        server.config.ACCESS_USERNAME = "test"
        server.config.ACCESS_PASSWORD = "secret"

    def tearDown(self):
        server.PAPERS_DB, server.LABELS_DB = self.old_values["PAPERS_DB"], self.old_values["LABELS_DB"]
        server.config.ACCESS_USERNAME, server.config.ACCESS_PASSWORD = self.old_auth
        self.tmp.cleanup()

    def client(self):
        token = base64.b64encode(b"test:secret").decode()
        client = server.app.test_client()
        client.environ_base["HTTP_AUTHORIZATION"] = f"Basic {token}"
        return client

    def urls(self, response):
        self.assertEqual(response.status_code, 200)
        return {paper["url"] for paper in response.get_json()["papers"]}

    def test_archive_is_excluded_from_date_filter_and_search(self):
        client = self.client()
        date_urls = self.urls(client.get("/papers?date=2026-01-01"))
        self.assertEqual(date_urls, {"https://example.test/active", "https://example.test/interest"})
        interest_urls = self.urls(client.get("/filter?label=感兴趣&paper_type=journal"))
        self.assertEqual(interest_urls, {"https://example.test/interest"})
        archive_urls = self.urls(client.get("/filter?label=归档&paper_type=journal"))
        self.assertEqual(
            archive_urls,
            {"https://example.test/archive", "https://example.test/archive-interest"},
        )
        search_urls = self.urls(client.get("/search?q=Archive&paper_type=journal"))
        self.assertEqual(search_urls, set())

    def test_label_endpoint_accepts_archive_and_unarchive(self):
        client = self.client()
        response = client.post(
            "/label", json={"url": "https://example.test/active", "labels": ["不相关", "归档"]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.urls(client.get("/filter?label=归档&paper_type=journal")), {
            "https://example.test/active",
            "https://example.test/archive",
            "https://example.test/archive-interest",
        })
        response = client.post(
            "/label", json={"url": "https://example.test/active", "labels": ["不相关"]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("https://example.test/active", self.urls(client.get("/papers?date=2026-01-01")))


if __name__ == "__main__":
    unittest.main()

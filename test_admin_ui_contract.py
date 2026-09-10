import unittest
from pathlib import Path

ROOT = Path(__file__).parent


class AdminUiContractTests(unittest.TestCase):
    def test_index_exposes_management_and_manual_upload_controls(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="btn-management"', html)
        self.assertIn('id="btn-manual-papers"', html)
        self.assertIn('id="manual-upload-input"', html)
        self.assertIn('data-filter="manual"', html)

    def test_javascript_has_persisted_management_and_manual_card_routes(self):
        js = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
        for route in ("/api/admin/uploads", "/api/manual-papers", "/api/admin/uploads/", "/translate_uploaded_pdf"):
            self.assertIn(route, js)
        self.assertIn("source_type", js)
        self.assertIn("仅删除这个PDF附件，保留原论文和标签", js)
        self.assertIn("删除手动论文及其全部相关文件", js)
        self.assertIn("label_key", js)
        self.assertIn("filter === 'manual'", js)
        self.assertIn("function toggleManageMode", js)
        self.assertIn("删除原PDF", js)
        self.assertIn("btnManagement.addEventListener('click', toggleManageMode)", js)

    def test_css_has_management_responsive_layout_hooks(self):
        css = (ROOT / "static/css/style.css").read_text(encoding="utf-8")
        self.assertIn(".management-panel", css)
        self.assertIn(".management-row", css)
        self.assertIn(".manual-upload-toolbar", css)


if __name__ == "__main__":
    unittest.main()

import unittest
from pathlib import Path

ROOT = Path(__file__).parent


class AdminUiContractTests(unittest.TestCase):
    def test_index_exposes_management_and_manual_upload_controls(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="btn-management"', html)
        self.assertIn('id="btn-manual-papers"', html)
        self.assertIn('id="manual-upload-input"', html)
        self.assertIn('id="manual-upload-input" accept=".pdf" multiple', html)
        self.assertIn('data-filter="manual"', html)
        self.assertIn('data-filter="归档"', html)

    def test_javascript_has_persisted_management_and_manual_card_routes(self):
        js = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
        for route in ("/api/admin/uploads", "/api/admin/uploads/", "/translate_uploaded_pdf"):
            self.assertIn(route, js)
        self.assertIn("source_type", js)
        self.assertIn("仅删除这个PDF附件，保留原论文和标签", js)
        self.assertIn("删除手动论文及其全部相关文件", js)
        self.assertIn("label_key", js)
        self.assertIn("filter === 'manual'", js)
        self.assertIn("function toggleManageMode", js)
        self.assertIn("删除原PDF", js)
        self.assertIn("btnManagement.addEventListener('click', toggleManageMode)", js)
        self.assertIn("archive-btn", js)
        self.assertIn("归档", js)
        self.assertIn("async function onArchive", js)
        self.assertIn("async function uploadManualPdfs", js)
        self.assertIn("Array.from(manualUploadInput.files || [])", js)
        self.assertIn("Promise.allSettled", js)
        self.assertIn("manual-upload-progress", js)
        self.assertIn('id="manual-return-btn"', js)
        self.assertIn("manualUploads = (data.uploads || []).filter", js)
        self.assertIn("_renderManualUploadRows(manualUploads)", js)
        self.assertIn("manual-upload-actions", js)
        self.assertIn("manual-translate-btn", js)
        self.assertIn("onManualFullTranslation", js)
        self.assertIn("async function loadManualUploadPage", js)
        self.assertIn("async function loadManualPapers", js)
        upload_view = js.split("async function loadManualUploadPage", 1)[1].split("function returnToRss", 1)[0]
        self.assertNotIn("makeCard(", upload_view)
        category_view = js.split("async function loadManualPapers", 1)[1].split("async function loadManualUploadPage", 1)[0]
        self.assertIn("makeCard(", category_view)

    def test_reload_actions_preserve_current_scroll_position(self):
        js = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
        self.assertIn("function _restoreScrollPosition", js)
        self.assertIn("window.scrollY", js)
        self.assertIn("window.scrollTo", js)
        self.assertIn("_reloadCurrentView({ preserveScroll: true })", js)

    def test_css_has_management_responsive_layout_hooks(self):
        css = (ROOT / "static/css/style.css").read_text(encoding="utf-8")
        self.assertIn(".management-panel", css)
        self.assertIn(".management-row", css)
        self.assertIn(".manual-upload-toolbar", css)
        self.assertIn(".manual-upload-progress", css)
        self.assertIn(".manual-upload-item", css)
        self.assertIn(".manual-upload-records", css)
        self.assertIn(".manual-upload-actions", css)
        self.assertIn(".manual-translate-btn", css)


if __name__ == "__main__":
    unittest.main()

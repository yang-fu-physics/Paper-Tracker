import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

if "config" not in sys.modules:
    config_path = Path(__file__).with_name("config.example.py")
    spec = importlib.util.spec_from_file_location("config", config_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["config"] = module

import paper_metadata


class PaperMetadataTests(unittest.TestCase):
    def test_collect_latex_source_expands_local_include_and_excludes_translated_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tex").write_text(
                "\\documentclass{article}\n\\title{Fictional Title}\n"
                "\\input{sections/abstract}\n\\begin{document}\nBody $E=mc^2$.\n\\end{document}\n",
                encoding="utf-8",
            )
            (root / "sections").mkdir()
            (root / "sections/abstract.tex").write_text(
                "\\begin{abstract}A fictional abstract.\\end{abstract}\n", encoding="utf-8"
            )
            (root / "translated_main.tex").write_text("DO NOT SEND THIS", encoding="utf-8")
            source = paper_metadata.collect_latex_source(root)
        self.assertIn("Fictional Title", source)
        self.assertIn("fictional abstract", source)
        self.assertIn("E=mc^2", source)
        self.assertNotIn("DO NOT SEND THIS", source)

    def test_validate_metadata_marks_missing_abstract_as_incomplete(self):
        result = paper_metadata.validate_metadata({
            "original_title": "Fictional title",
            "title_zh": "虚构标题",
            "source_language": "en",
        })
        self.assertEqual(result["original_title"], "Fictional title")
        self.assertTrue(result["metadata_incomplete"])
        self.assertEqual(result["abstract_original"], "")

    def test_recognize_metadata_calls_configured_translation_endpoint_and_parses_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tex").write_text(
                "\\documentclass{article}\n\\begin{document}fictional input\\end{document}",
                encoding="utf-8",
            )
            body = {
                "original_title": "Fictional title",
                "title_zh": "虚构标题",
                "original_abstract": "Fictional abstract",
                "abstract_zh": "虚构摘要",
                "summary_zh": "虚构总结",
                "source_language": "en",
            }
            response = Mock(status_code=200)
            response.raise_for_status.return_value = None
            response.json.return_value = {
                "choices": [{"message": {"content": json.dumps(body, ensure_ascii=False)}}]
            }
            with patch.object(paper_metadata.requests, "post", return_value=response) as post:
                result = paper_metadata.recognize_metadata(
                    root,
                    config_module=type("C", (), {
                        "TRANSLATE_BASE_URL": "https://example.invalid",
                        "TRANSLATE_API_KEY": "test-key",
                        "TRANSLATE_MODEL": "fictional-model",
                    }),
                    sleep=lambda _: None,
                )
        self.assertEqual(result["title_zh"], "虚构标题")
        self.assertEqual(post.call_args.args[0], "https://example.invalid/v1/chat/completions")
        request_json = post.call_args.kwargs["json"]
        self.assertEqual(request_json["model"], "fictional-model")
        self.assertIn("fictional input", request_json["messages"][-1]["content"])

    def test_recognize_metadata_retries_rate_limit_then_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tex").write_text(
                "\\documentclass{article}\n\\begin{document}fictional\\end{document}",
                encoding="utf-8",
            )
            body = {"original_title": "T", "title_zh": "题", "source_language": "en"}
            first = Mock(status_code=429)
            second = Mock(status_code=200)
            second.json.return_value = {
                "choices": [{"message": {"content": json.dumps(body)}}]
            }
            with patch.object(paper_metadata.requests, "post", side_effect=[first, second]) as post:
                result = paper_metadata.recognize_metadata(
                    root, config_module=type("C", (), {
                        "TRANSLATE_BASE_URL": "https://example.invalid",
                        "TRANSLATE_API_KEY": "test-key",
                        "TRANSLATE_MODEL": "fictional-model",
                    }),
                    sleep=lambda _: None,
                )
        self.assertEqual(result["original_title"], "T")
        self.assertEqual(post.call_count, 2)

    def test_content_parser_accepts_explanation_and_unescaped_latex_backslashes(self):
        response = Mock()
        response.json.return_value = {
            "choices": [{
                "message": {
                    "content": '结果如下：```json\n{"original_title":"Fictional","title_zh":"虚构","abstract_zh":"含有 \\alpha 和 \\begin{abstract}","source_language":"en"}\n```'
                }
            }]
        }
        result = paper_metadata._content_from_response(response)
        self.assertEqual(result["abstract_zh"], r"含有 \alpha 和 \begin{abstract}")

    def test_content_parser_ignores_braces_before_and_after_json(self):
        response = Mock()
        response.json.return_value = {
            "choices": [{
                "message": {
                    "content": "说明中的 LaTeX \\text{data}。" + '{"original_title":"Fictional","title_zh":"虚构","source_language":"en"}' + " 以上。"
                }
            }]
        }
        result = paper_metadata._content_from_response(response)
        self.assertEqual(result["original_title"], "Fictional")

        with self.assertRaises(paper_metadata.MetadataError):
            paper_metadata.validate_metadata({"title_zh": "only translated"})

    def test_doc2x_zip_rejects_path_traversal(self):
        import pdf_translate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("../outside.tex", "unsafe")
            with self.assertRaises(RuntimeError):
                pdf_translate._safe_extract_zip(str(archive), str(root / "tex_src"))
            self.assertFalse((root / "outside.tex").exists())

    def test_pdf_latex_cache_requires_matching_source_marker(self):
        import pdf_translate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-source")
            work = root / "work"
            tex = work / "tex_src"
            tex.mkdir(parents=True)
            (tex / "main.tex").write_text("old", encoding="utf-8")
            (work / ".source_sha256").write_text("old-hash", encoding="ascii")
            with patch.object(pdf_translate.requests, "post", side_effect=AssertionError("cache miss")):
                with self.assertRaises(AssertionError):
                    pdf_translate.pdf_to_latex_dir(str(pdf), str(work), source_sha256="new-hash")
            self.assertFalse(tex.exists())


if __name__ == "__main__":
    unittest.main()

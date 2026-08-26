import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

# config.py is intentionally gitignored because production deployments put
# credentials there. Load the tracked, secret-free example for clean-clone tests.
if "config" not in sys.modules:
    config_path = Path(__file__).with_name("config.example.py")
    spec = importlib.util.spec_from_file_location("config", config_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load test config from {config_path}")
    config_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config_module)
    sys.modules["config"] = config_module

import scraper
import server


class FilterConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_limits_concurrent_batches(self):
        active = 0
        max_active = 0

        async def fake_process(batch, start_idx, base_url, api_key, model, **kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            evaluated = []
            for paper in batch:
                item = paper.copy()
                item["is_transport"] = 0
                evaluated.append(item)
            return evaluated, [], 0

        papers = [
            {"url": f"https://example.test/{i}", "title": f"p{i}", "abstract": "a"}
            for i in range(8)
        ]
        with patch.object(scraper, "_process_batch", fake_process):
            evaluated, errors, stats = await scraper.filter_and_translate(
                papers, "http://example.test", "key", "model",
                batch_size=1, max_concurrency=2,
            )

        self.assertEqual(len(evaluated), 8)
        self.assertEqual(errors, [])
        self.assertLessEqual(max_active, 2)
        self.assertEqual(stats["successful_batches"], 8)
        self.assertEqual(stats["failed_batches"], 0)

    async def test_failed_batch_does_not_discard_successful_batches(self):
        async def fake_process(batch, start_idx, base_url, api_key, model, **kwargs):
            if start_idx == 1:
                return [], ["batch 1 failed"], 2
            item = batch[0].copy()
            item["is_transport"] = 0
            return [item], [], 1 if start_idx == 0 else 0

        papers = [
            {"url": f"https://example.test/{i}", "title": f"p{i}", "abstract": "a"}
            for i in range(3)
        ]
        with patch.object(scraper, "_process_batch", fake_process):
            evaluated, errors, stats = await scraper.filter_and_translate(
                papers, "http://example.test", "key", "model",
                batch_size=1, max_concurrency=2,
            )

        self.assertEqual([p["url"] for p in evaluated], [
            "https://example.test/0", "https://example.test/2"
        ])
        self.assertEqual(errors, ["batch 1 failed"])
        self.assertEqual(stats["successful_batches"], 2)
        self.assertEqual(stats["failed_batches"], 1)
        self.assertEqual(stats["retry_pending_papers"], 1)
        self.assertEqual(stats["retry_attempts"], 3)

    async def test_rejects_concurrency_outside_two_to_four(self):
        for value in (1, 5, 2.5, True, "2"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "between 2 and 4"):
                    await scraper.filter_and_translate(
                        [], "http://example.test", "key", "model",
                        max_concurrency=value,
                    )

        with patch.object(scraper.config, "AI_MAX_CONCURRENCY", 5):
            with self.assertRaisesRegex(ValueError, "between 2 and 4"):
                await scraper.filter_and_translate(
                    [], "http://example.test", "key", "model",
                )


class BatchRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_http_500_then_succeeds(self):
        request = httpx.Request("POST", "http://example.test/v1/chat/completions")
        good_content = json.dumps({
            "results": [{
                "index": 0,
                "is_transport": False,
                "title_zh": "",
                "summary_zh": "",
                "abstract_zh": "",
            }]
        })
        responses = [
            httpx.Response(500, request=request, json={"error": "busy"}),
            httpx.Response(200, request=request, json={
                "choices": [{"message": {"content": good_content}}]
            }),
        ]
        attempts = []
        sleeps = []

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, *args, **kwargs):
                attempts.append(1)
                return responses.pop(0)

        async def fake_sleep(delay):
            sleeps.append(delay)

        papers = [{
            "url": "https://example.test/0",
            "title": "paper",
            "abstract": "abstract",
        }]
        evaluated, errors, retries = await scraper._process_batch(
            papers, 0, "http://example.test", "key", "model",
            max_attempts=3, base_delay=1,
            client_factory=lambda **kwargs: FakeClient(),
            sleep_func=fake_sleep,
        )

        self.assertEqual(len(evaluated), 1)
        self.assertEqual(errors, [])
        self.assertEqual(retries, 1)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleeps, [1])

    async def test_retries_incomplete_model_result(self):
        request = httpx.Request("POST", "http://example.test/v1/chat/completions")
        incomplete = json.dumps({"results": []})
        complete = json.dumps({
            "results": [{
                "index": 0,
                "is_transport": False,
                "title_zh": "",
                "summary_zh": "",
                "abstract_zh": "",
            }]
        })
        responses = [
            httpx.Response(200, request=request, json={
                "choices": [{"message": {"content": incomplete}}]
            }),
            httpx.Response(200, request=request, json={
                "choices": [{"message": {"content": complete}}]
            }),
        ]
        attempts = []

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, *args, **kwargs):
                attempts.append(1)
                return responses.pop(0)

        async def no_sleep(delay):
            return None

        papers = [{
            "url": "https://example.test/0",
            "title": "paper",
            "abstract": "abstract",
        }]
        evaluated, errors, retries = await scraper._process_batch(
            papers, 0, "http://example.test", "key", "model",
            max_attempts=2, base_delay=0,
            client_factory=lambda **kwargs: FakeClient(),
            sleep_func=no_sleep,
        )

        self.assertEqual(len(evaluated), 1)
        self.assertEqual(errors, [])
        self.assertEqual(retries, 1)
        self.assertEqual(len(attempts), 2)

    async def test_retries_strict_schema_violations_instead_of_persisting(self):
        good_item = {
            "index": 0,
            "is_transport": True,
            "title_zh": "有效标题",
            "summary_zh": "有效总结",
            "abstract_zh": "有效摘要",
        }
        malformed_items = {
            "boolean_index": {**good_item, "index": False},
            "float_index": {**good_item, "index": 0.0},
            "string_boolean": {**good_item, "is_transport": "false"},
            "missing_related_translation": {
                "index": 0,
                "is_transport": True,
                "title_zh": "有效标题",
                "summary_zh": "有效总结",
            },
            "non_object_result": "not-an-object",
        }
        request = httpx.Request("POST", "http://example.test/v1/chat/completions")
        papers = [{
            "url": "https://example.test/0",
            "title": "paper",
            "abstract": "abstract",
        }]

        for case, malformed_item in malformed_items.items():
            with self.subTest(case=case):
                responses = [
                    httpx.Response(200, request=request, json={
                        "choices": [{"message": {"content": json.dumps({
                            "results": [malformed_item]
                        })}}]
                    }),
                    httpx.Response(200, request=request, json={
                        "choices": [{"message": {"content": json.dumps({
                            "results": [good_item]
                        })}}]
                    }),
                ]
                attempts = []

                class FakeClient:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *args):
                        return None

                    async def post(self, *args, **kwargs):
                        attempts.append(1)
                        return responses.pop(0)

                async def no_sleep(delay):
                    return None

                evaluated, errors, retries = await scraper._process_batch(
                    papers, 0, "http://example.test", "key", "model",
                    max_attempts=2, base_delay=0,
                    client_factory=lambda **kwargs: FakeClient(),
                    sleep_func=no_sleep,
                )

                self.assertEqual(errors, [])
                self.assertEqual(len(evaluated), 1)
                self.assertEqual(evaluated[0]["is_transport"], 1)
                self.assertEqual(retries, 1)
                self.assertEqual(len(attempts), 2)

    async def test_exhausted_retries_returns_batch_error(self):
        request = httpx.Request("POST", "http://example.test/v1/chat/completions")
        responses = [
            httpx.Response(429, request=request, json={"error": "rate limited"})
            for _ in range(3)
        ]
        attempts = []
        sleeps = []

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, *args, **kwargs):
                attempts.append(1)
                return responses.pop(0)

        async def fake_sleep(delay):
            sleeps.append(delay)

        papers = [{
            "url": "https://example.test/0",
            "title": "paper",
            "abstract": "abstract",
        }]
        evaluated, errors, retries = await scraper._process_batch(
            papers, 0, "http://example.test", "key", "model",
            max_attempts=3, base_delay=1,
            client_factory=lambda **kwargs: FakeClient(),
            sleep_func=fake_sleep,
        )

        self.assertEqual(evaluated, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("failed after 3 attempt(s)", errors[0])
        self.assertEqual(retries, 2)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleeps, [1, 2])


class ModelJsonRepairTests(unittest.TestCase):
    def test_repairs_invalid_latex_backslashes(self):
        content = r'''{
          "results": [{
            "index": 0,
            "is_transport": true,
            "title_zh": "拓扑 $\Delta$",
            "summary_zh": "动量 $\mathbf{k}$",
            "abstract_zh": "能量 $\epsilon_k$"
          }]
        }'''

        parsed = scraper._parse_model_json(content)

        item = parsed["results"][0]
        self.assertEqual(item["title_zh"], r"拓扑 $\Delta$")
        self.assertEqual(item["summary_zh"], r"动量 $\mathbf{k}$")
        self.assertEqual(item["abstract_zh"], r"能量 $\epsilon_k$")

    def test_preserves_latex_commands_that_look_like_json_escapes(self):
        content = r'''{
          "results": [{
            "index": 0,
            "is_transport": true,
            "title_zh": "$\beta$ 与 $\frac{a}{b}$",
            "summary_zh": "$\nu$, $\rho$, $\theta$",
            "abstract_zh": "$\nabla \times \mathbf{B}$"
          }]
        }'''

        parsed = scraper._parse_model_json(content)

        item = parsed["results"][0]
        self.assertEqual(item["title_zh"], r"$\beta$ 与 $\frac{a}{b}$")
        self.assertEqual(item["summary_zh"], r"$\nu$, $\rho$, $\theta$")
        self.assertEqual(item["abstract_zh"], r"$\nabla \times \mathbf{B}$")

    def test_repairs_unlisted_latex_commands_without_allowlist(self):
        content = r'''{
          "results": [{
            "index": 0,
            "is_transport": true,
            "title_zh": "$\therefore A \rightleftharpoons B$",
            "summary_zh": "$\forall k, \neg f(k)$",
            "abstract_zh": "$\futurecommand{x}$"
          }]
        }'''

        item = scraper._parse_model_json(content)["results"][0]

        self.assertEqual(item["title_zh"], r"$\therefore A \rightleftharpoons B$")
        self.assertEqual(item["summary_zh"], r"$\forall k, \neg f(k)$")
        self.assertEqual(item["abstract_zh"], r"$\futurecommand{x}$")

    def test_preserves_legitimate_json_escapes(self):
        cases = {
            "newline_lowercase": (r'{"text":"line\nnext"}', "line\nnext"),
            "tab_lowercase": (r'{"text":"line\ttext"}', "line\ttext"),
            "backspace_lowercase": (r'{"text":"line\bback"}', "line\bback"),
            "formfeed_lowercase": (r'{"text":"line\fform"}', "line\fform"),
            "carriage_lowercase": (r'{"text":"line\rreturn"}', "line\rreturn"),
            "mixed_escapes": (
                r'{"text":"第一行\nNext line\t  quote: \" slash: \\ alpha: \u03b1"}',
                "第一行\nNext line\t  quote: \" slash: \\ alpha: α",
            ),
        }

        for case, (content, expected) in cases.items():
            with self.subTest(case=case):
                self.assertEqual(scraper._parse_model_json(content)["text"], expected)


class PipelineSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_run_logs_accurate_summary_and_persists_successes(self):
        fetched = [
            {"url": "https://example.test/ok", "journal": "J", "title": "ok", "abstract": "a"},
            {"url": "https://example.test/retry", "journal": "J", "title": "retry", "abstract": "a"},
        ]
        evaluated = [{
            **fetched[0],
            "is_transport": 1,
            "title_zh": "相关论文",
            "summary_zh": "摘要",
            "abstract_zh": "全文",
            "abstract_en": "a",
        }]
        stats = {
            "total_batches": 2,
            "successful_batches": 1,
            "failed_batches": 1,
            "evaluated_papers": 1,
            "retry_pending_papers": 1,
            "retry_attempts": 2,
        }

        async def fake_fetch():
            return fetched

        async def fake_filter(*args, **kwargs):
            return evaluated, ["batch 1 failed"], stats

        with tempfile.TemporaryDirectory() as tmp:
            old_papers_db = server.PAPERS_DB
            old_labels_db = server.LABELS_DB
            server.PAPERS_DB = Path(tmp) / "papers.db"
            server.LABELS_DB = Path(tmp) / "labels.db"
            try:
                server.init_dbs()
                output = io.StringIO()
                with patch.object(server, "fetch_all_papers", fake_fetch), \
                     patch.object(server, "filter_and_translate", fake_filter), \
                     contextlib.redirect_stdout(output):
                    await server._run_sync_pipeline()
                with server.papers_conn() as con:
                    evaluated_count = con.execute(
                        "SELECT COUNT(*) FROM paper_evaluations"
                    ).fetchone()[0]
            finally:
                server.PAPERS_DB = old_papers_db
                server.LABELS_DB = old_labels_db

        text = output.getvalue()
        self.assertEqual(evaluated_count, 1)
        self.assertIn(
            "PIPELINE_SUMMARY status=partial fetched=2 new=2 evaluated=1 "
            "related=1 successful_batches=1 failed_batches=1 "
            "retries=2 retry_pending=1 errors=1",
            text,
        )
        self.assertNotIn("Daily pipeline completed successfully.", text)


if __name__ == "__main__":
    unittest.main()

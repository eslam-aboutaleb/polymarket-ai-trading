"""Contract tests: search-index X snippets cannot masquerade as an authenticated feed."""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch


def _load_research_server():
    path = Path(__file__).resolve().parents[2] / "services" / "mcps" / "research" / "server.py"
    spec = importlib.util.spec_from_file_location("_test_x_provenance_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    fake_ddgs = ModuleType("ddgs")
    fake_ddgs.DDGS = MagicMock()
    with patch.dict(sys.modules, {"ddgs": fake_ddgs}):
        spec.loader.exec_module(module)
    return module


class IndexedXProvenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _load_research_server()

    def test_real_status_links_are_accepted_only_from_real_x_hosts(self):
        checker = self.server._is_indexed_x_post
        self.assertTrue(checker("https://x.com/analyst/status/123456?ref=foo"))
        self.assertTrue(checker("https://twitter.com/analyst/status/1"))
        self.assertFalse(checker("https://x.com.attacker.invalid/analyst/status/123"))
        self.assertFalse(checker("https://attacker.invalid/path/x.com/analyst/status/123"))
        self.assertFalse(checker("https://x.com/analyst"))
        self.assertFalse(checker("javascript://x.com/analyst/status/123"))
        self.assertFalse(checker("https://x.com/analyst/status/not-an-id"))

    def test_indexed_result_has_provenance_but_no_social_metadata(self):
        items = [
            {
                "url": "https://x.com/analyst/status/123?utm_medium=spam",
                "title": "Example",
                "snippet": "Snippet only",
            }
        ]
        results = self.server._indexed_x_results(items, "2026-10-21T00:00:00+00:00")
        self.assertEqual(len(results), 1)
        row = results[0]
        self.assertEqual(row["source_type"], "indexed_web")
        self.assertEqual(row["provider"], "duckduckgo_web_index")
        self.assertEqual(row["quality"], "unverified_search_snippet")
        self.assertEqual(row["url"], "https://x.com/analyst/status/123")
        self.assertNotIn("posted_at", row)
        self.assertNotIn("like_count", row)
        self.assertNotIn("verified", row)

    def test_search_filters_spoofed_urls_and_deduplicates_query_strings(self):
        response = [
            {"url": "https://evil.example/x.com/a/status/23", "title": "Phish"},
            {"url": "https://x.com/a/status/23?source=one", "title": "Post"},
            {"url": "https://x.com/a/status/23?source=two", "title": "Duplicate"},
        ]
        with patch.object(self.server, "web_search", return_value=response) as search:
            result = self.server.x_posts_search("election")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], "https://x.com/a/status/23")
        self.assertIn("site:x.com", search.call_args.args[0])

    def test_fallback_does_not_infer_zero_social_activity(self):
        with patch.object(
            self.server,
            "web_search",
            side_effect=[
                [],
                [{"url": "https://x.com/b/status/889", "title": "Found"}],
            ],
        ) as search:
            rows = self.server.x_posts_search("test")
        self.assertEqual(search.call_count, 2)
        self.assertEqual(rows[0]["coverage"], "partial_non_comprehensive")
        self.assertEqual(rows[0]["source_type"], "indexed_web")

    def test_no_matches_and_blank_queries_are_empty(self):
        with patch.object(self.server, "web_search", return_value=[]) as search:
            self.assertEqual(self.server.x_posts_search(""), [])
            self.assertEqual(search.call_count, 0)
            self.assertEqual(self.server.x_posts_search("unknown"), [])
            self.assertEqual(search.call_count, 2)


if __name__ == "__main__":
    unittest.main()

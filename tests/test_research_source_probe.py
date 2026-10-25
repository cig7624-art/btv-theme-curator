"""Source diagnostics never serialize prose, secrets, or arbitrary errors."""
import unittest
from scripts.probe_research_sources_live import sanitized_diagnostics


class SourceProbeTests(unittest.TestCase):
    def test_counts_and_fixed_codes_survive_without_raw_errors(self):
        parsed = sanitized_diagnostics({"search_completed": "true", "citations_found": "5",
            "fetch_attempted": "4", "search_errors": "primary_timeout,secret-provider-body",
            "provider_code": "unsupported_parameter", "fetch_http_statuses": "200,403,credential", "fallback_used": "true"})
        self.assertTrue(parsed["search_completed"])
        self.assertEqual(parsed["citations_found"], 5)
        self.assertEqual(parsed["search_errors"], ["primary_timeout"])
        self.assertEqual(parsed["fetch_http_statuses"], [200, 403])
        self.assertNotIn("secret-provider", str(parsed))

    def test_unknown_and_invalid_values_are_never_forwarded(self):
        parsed = sanitized_diagnostics({"provider_code": "https://private/?key=secret",
            "fetch_attempted": "-2", "citations_found": "raw-message", "search_completed": "yes",
            "fetch_http_statuses": "999,0"})
        self.assertEqual(parsed["provider_code"], "unknown")
        self.assertEqual(parsed["fetch_attempted"], 0)
        self.assertFalse(parsed["search_completed"])
        self.assertEqual(parsed["fetch_http_statuses"], [])


if __name__ == "__main__":
    unittest.main()

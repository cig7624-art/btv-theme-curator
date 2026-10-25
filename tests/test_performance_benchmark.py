"""Baseline quality must not override successful optimized measurements."""
from copy import deepcopy
import unittest

from scripts.benchmark_app_performance import compare_baseline


def report(*, status="passed", version="1.4.2", elapsed=500):
    return {
        "status": status, "errors": [], "observed_version": version,
        "protocol": "streamlit-full-rerun-v1", "samples_per_flow": 3,
        "flow_order": ["tab_recommendation", "library_open"],
        "viewports": [{"name": "desktop", "width": 1366, "height": 1000},
                      {"name": "mobile", "width": 390, "height": 844}],
        "summary": [{"viewport": viewport, "flow": flow, "samples": 3,
                     "median_completion_ms": elapsed}
                    for viewport in ("desktop", "mobile")
                    for flow in ("tab_recommendation", "library_open")],
    }


class PerformanceBenchmarkTests(unittest.TestCase):
    def compare(self, optimized, baseline):
        original_optimized, original_baseline = deepcopy(optimized), deepcopy(baseline)
        compared = compare_baseline(optimized, baseline)
        self.assertEqual(optimized, original_optimized)
        self.assertEqual(baseline, original_baseline)
        merged = {**optimized, **compared}
        self.assertEqual(merged["status"], "passed")
        self.assertEqual(merged["errors"], [])
        return compared

    def test_complete_baseline_compares_each_equal_three_sample_flow(self):
        result = self.compare(report(), report(version="1.4.1", elapsed=2000))
        self.assertEqual(result["comparison_status"], "complete")
        self.assertEqual(result["baseline_version"], "1.4.1")
        self.assertEqual(len(result["comparison"]), 4)
        for row in result["comparison"]:
            self.assertEqual(row["change_percent"], -75.0)
            self.assertEqual(row["baseline_samples"], 3)
            self.assertEqual(row["optimized_samples"], 3)

    def test_missing_baseline_keeps_measurements_successful(self):
        result = self.compare(report(), None)
        self.assertEqual(result["comparison_status"], "unavailable")
        self.assertEqual(result["comparison_reason"], "baseline_unavailable")
        self.assertEqual(result["comparison"], [])

    def test_protocol_viewport_or_sample_mismatch_cannot_be_compared(self):
        for key, value in (("protocol", "other-protocol"), ("viewports", []),
                           ("samples_per_flow", 1)):
            with self.subTest(key=key):
                baseline = report()
                baseline[key] = value
                result = self.compare(report(), baseline)
                self.assertEqual(result["comparison_status"], "unavailable")
                self.assertEqual(result["comparison_reason"], "incompatible_baseline")

    def test_failed_baseline_only_compares_complete_matched_flows(self):
        baseline = report(status="failed", elapsed=2000)
        baseline["summary"] = baseline["summary"][:2]
        baseline["summary"][1]["samples"] = 1
        result = self.compare(report(), baseline)
        self.assertEqual(result["comparison_status"], "partial")
        self.assertEqual(result["comparison_reason"], "incomplete_baseline")
        self.assertEqual(len(result["comparison"]), 1)
        self.assertEqual(result["comparison"][0]["flow"], "tab_recommendation")

    def test_one_before_sample_is_never_compared_with_three_after(self):
        baseline = report(status="failed")
        for row in baseline["summary"]:
            row["samples"] = 1
        result = self.compare(report(), baseline)
        self.assertEqual(result["comparison_status"], "unavailable")
        self.assertEqual(result["comparison"], [])

    def test_incomplete_after_rows_are_also_excluded(self):
        optimized = report()
        optimized["summary"][0]["samples"] = 1
        result = self.compare(optimized, report(elapsed=2000))
        self.assertEqual(result["comparison_status"], "partial")
        self.assertEqual(len(result["comparison"]), 3)
        self.assertNotIn(("desktop", "tab_recommendation"),
                         {(row["viewport"], row["flow"]) for row in result["comparison"]})

    def test_changed_flow_set_compares_only_existing_complete_flows(self):
        baseline = report(elapsed=2000)
        baseline["flow_order"] = ["tab_recommendation", "old_flow"]
        for row in baseline["summary"]:
            if row["flow"] == "library_open":
                row["flow"] = "old_flow"
        result = self.compare(report(), baseline)
        self.assertEqual(result["comparison_status"], "partial")
        self.assertEqual(len(result["comparison"]), 2)
        self.assertEqual({row["flow"] for row in result["comparison"]}, {"tab_recommendation"})

    def test_invalid_or_zero_elapsed_rows_cannot_produce_misleading_comparisons(self):
        baseline = report()
        for row, value in zip(baseline["summary"], [0, float("inf"), "1000", -1]):
            row["median_completion_ms"] = value
        # NaN would prevent equality assertions even for unchanged inputs.
        result = self.compare(report(), baseline)
        self.assertEqual(result["comparison_status"], "unavailable")
        self.assertEqual(result["comparison"], [])
        baseline["summary"] = None
        self.assertEqual(self.compare(report(), baseline)["comparison_status"], "unavailable")


if __name__ == "__main__":
    unittest.main()

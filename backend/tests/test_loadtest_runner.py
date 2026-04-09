from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loadtest.k6_runner import run_k6_test  # noqa: E402
from models.schemas import HttpMethod, LoadTestScenario  # noqa: E402


def _sample_scenario() -> LoadTestScenario:
    return LoadTestScenario(
        id="runner-scenario",
        name="Runner scenario",
        target_url="https://api.example.com/ping",
        method=HttpMethod.GET,
        expected_statuses=[200],
    )


def _sample_summary_payload() -> dict:
    return {
        "metrics": {
            "http_req_duration": {
                "values": {
                    "avg": 10,
                    "min": 5,
                    "max": 20,
                    "med": 9,
                    "p(90)": 15,
                    "p(95)": 18,
                    "p(99)": 19,
                }
            },
            "http_reqs": {"values": {"count": 100, "rate": 10}},
            "http_req_failed": {"values": {"fails": 2, "rate": 0.02}},
            "data_received": {"values": {"count": 2048}},
            "data_sent": {"values": {"count": 1024}},
            "vus_max": {"values": {"max": 5}},
        }
    }


class LoadTestRunnerTests(TestCase):
    def test_non_zero_exit_with_summary_is_failed(self) -> None:
        scenario = _sample_scenario()

        def _run(cmd, capture_output, text, timeout):
            summary_path = cmd[3]
            with open(summary_path, "w", encoding="utf-8") as handle:
                json.dump(_sample_summary_payload(), handle)
            return subprocess.CompletedProcess(cmd, 1, stdout="k6 out", stderr="k6 err")

        with (
            patch("loadtest.k6_runner.save_k6_script", return_value="/tmp/fake-script.js"),
            patch("loadtest.k6_runner.subprocess.run", side_effect=_run),
        ):
            metrics = run_k6_test(scenario)

        self.assertEqual(metrics.runner_status, "failed")
        self.assertEqual(metrics.runner_exit_code, 1)
        self.assertEqual(metrics.total_requests, 100)
        self.assertEqual(metrics.failed_requests, 2)
        self.assertEqual(metrics.metric_shape, "values")
        self.assertEqual(metrics.request_count_source, "http_reqs.count")
        self.assertEqual(metrics.error_rate_source, "http_req_failed.rate")
        self.assertEqual(metrics.parse_warnings, [])

    def test_threshold_breach_with_summary_uses_failed_status_and_iteration_fallback(self) -> None:
        scenario = _sample_scenario()

        summary_without_http_reqs = {
            "metrics": {
                "http_req_duration": {
                    "values": {
                        "avg": 35,
                        "min": 8,
                        "max": 120,
                        "med": 22,
                        "p(90)": 60,
                        "p(95)": 80,
                        "p(99)": 100,
                    }
                },
                # Simulates k6 outputs where iterations are present but http_reqs is absent.
                "iterations": {"values": {"count": 1130, "rate": 37.4}},
                "http_req_failed": {"values": {"rate": 1.0}},
                "errors": {"values": {"value": 1}},
                "vus_max": {"values": {"max": 10}},
            }
        }

        def _run(cmd, capture_output, text, timeout):
            summary_path = cmd[3]
            with open(summary_path, "w", encoding="utf-8") as handle:
                json.dump(summary_without_http_reqs, handle)
            return subprocess.CompletedProcess(
                cmd,
                99,
                stdout="k6 out",
                stderr="time=\"...\" level=error msg=\"thresholds on metrics 'http_req_failed' have been crossed\"",
            )

        with (
            patch("loadtest.k6_runner.save_k6_script", return_value="/tmp/fake-script.js"),
            patch("loadtest.k6_runner.subprocess.run", side_effect=_run),
        ):
            metrics = run_k6_test(scenario)

        self.assertEqual(metrics.runner_status, "failed")
        self.assertIn("thresholds crossed", metrics.runner_message)
        self.assertEqual(metrics.runner_exit_code, 99)
        self.assertEqual(metrics.total_requests, 1130)
        self.assertEqual(metrics.failed_requests, 1130)
        self.assertEqual(metrics.requests_per_second, 37.4)
        self.assertEqual(metrics.request_count_source, "iterations.count")
        self.assertEqual(metrics.error_rate_source, "http_req_failed.rate")

    def test_flat_metric_shape_uses_value_rate_not_fails_counter(self) -> None:
        scenario = _sample_scenario()

        flat_summary = {
            "metrics": {
                "http_req_duration": {
                    "avg": 192.95,
                    "min": 157.39,
                    "max": 334.82,
                    "med": 181.24,
                    "p(90)": 234.55,
                    "p(95)": 258.67,
                    "p(99)": 300.12,
                },
                "iterations": {"count": 66, "rate": 3.279761086300602},
                "http_reqs": {"count": 66, "rate": 3.279761086300602},
                # In this shape, value/rate indicates error ratio and fails can
                # represent internal Rate counters, not direct failed requests.
                "http_req_failed": {"passes": 0, "fails": 66, "value": 0},
                "data_received": {"count": 2146624, "rate": 106672.93},
                "data_sent": {"count": 11341, "rate": 563.57},
                "vus_max": {"min": 1, "max": 1, "value": 1},
                "errors": {"passes": 0, "fails": 66, "value": 0},
            }
        }

        def _run(cmd, capture_output, text, timeout):
            summary_path = cmd[3]
            with open(summary_path, "w", encoding="utf-8") as handle:
                json.dump(flat_summary, handle)
            return subprocess.CompletedProcess(cmd, 0, stdout="k6 out", stderr="")

        with (
            patch("loadtest.k6_runner.save_k6_script", return_value="/tmp/fake-script.js"),
            patch("loadtest.k6_runner.subprocess.run", side_effect=_run),
        ):
            metrics = run_k6_test(scenario)

        self.assertEqual(metrics.runner_status, "passed")
        self.assertEqual(metrics.total_requests, 66)
        self.assertAlmostEqual(metrics.requests_per_second, 3.279761086300602)
        self.assertEqual(metrics.failed_requests, 0)
        self.assertEqual(metrics.error_rate, 0)
        self.assertEqual(metrics.metric_shape, "flat")
        self.assertEqual(metrics.request_count_source, "http_reqs.count")
        self.assertEqual(metrics.error_rate_source, "http_req_failed.value")
        self.assertEqual(metrics.parse_warnings, [])

    def test_timeout_returns_structured_error(self) -> None:
        scenario = _sample_scenario()
        timeout_error = subprocess.TimeoutExpired(
            cmd=["k6", "run"],
            timeout=600,
            output="timeout stdout",
            stderr="timeout stderr",
        )

        with (
            patch("loadtest.k6_runner.save_k6_script", return_value="/tmp/fake-script.js"),
            patch("loadtest.k6_runner.subprocess.run", side_effect=timeout_error),
        ):
            metrics = run_k6_test(scenario)

        self.assertEqual(metrics.runner_status, "error")
        self.assertIn("timed out", metrics.runner_message)
        self.assertIn("timeout stdout", metrics.raw_metrics.get("stdout", ""))
        self.assertEqual(metrics.request_count_source, "none")
        self.assertEqual(metrics.parse_warnings, ["timeout"])

    def test_missing_k6_binary_returns_structured_error(self) -> None:
        scenario = _sample_scenario()

        with (
            patch("loadtest.k6_runner.save_k6_script", return_value="/tmp/fake-script.js"),
            patch("loadtest.k6_runner.subprocess.run", side_effect=FileNotFoundError()),
        ):
            metrics = run_k6_test(scenario)

        self.assertEqual(metrics.runner_status, "error")
        self.assertIn("k6 is not installed", metrics.runner_message)
        self.assertEqual(metrics.error_rate_source, "none")
        self.assertEqual(metrics.parse_warnings, ["k6_not_installed"])

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

    def test_missing_k6_binary_returns_structured_error(self) -> None:
        scenario = _sample_scenario()

        with (
            patch("loadtest.k6_runner.save_k6_script", return_value="/tmp/fake-script.js"),
            patch("loadtest.k6_runner.subprocess.run", side_effect=FileNotFoundError()),
        ):
            metrics = run_k6_test(scenario)

        self.assertEqual(metrics.runner_status, "error")
        self.assertIn("k6 is not installed", metrics.runner_message)

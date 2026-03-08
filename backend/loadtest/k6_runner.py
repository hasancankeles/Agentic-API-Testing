from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from datetime import datetime

from models.schemas import LoadTestMetrics, LoadTestScenario
from loadtest.k6_generator import save_k6_script


def _parse_k6_summary(summary_path: str) -> dict:
    """Parse the k6 JSON summary output file."""
    with open(summary_path, "r") as f:
        data = json.load(f)

    metrics = data.get("metrics", {})

    http_req_duration = metrics.get("http_req_duration", {}).get("values", {})
    http_reqs = metrics.get("http_reqs", {}).get("values", {})
    http_req_failed = metrics.get("http_req_failed", {}).get("values", {})
    data_received = metrics.get("data_received", {}).get("values", {})
    data_sent = metrics.get("data_sent", {}).get("values", {})
    vus_max = metrics.get("vus_max", {}).get("values", {})

    return {
        "avg_response_time_ms": http_req_duration.get("avg", 0),
        "min_response_time_ms": http_req_duration.get("min", 0),
        "max_response_time_ms": http_req_duration.get("max", 0),
        "p50_ms": http_req_duration.get("med", 0),
        "p90_ms": http_req_duration.get("p(90)", 0),
        "p95_ms": http_req_duration.get("p(95)", 0),
        "p99_ms": http_req_duration.get("p(99)", 0),
        "total_requests": int(http_reqs.get("count", 0)),
        "requests_per_second": http_reqs.get("rate", 0),
        "failed_requests": int(http_req_failed.get("passes", 0)),
        "error_rate": http_req_failed.get("rate", 0),
        "data_received_kb": data_received.get("count", 0) / 1024,
        "data_sent_kb": data_sent.get("count", 0) / 1024,
        "vus_max": int(vus_max.get("max", 0)),
        "raw_metrics": data,
    }


def run_k6_test(scenario: LoadTestScenario) -> LoadTestMetrics:
    """Run a k6 load test for the given scenario and return parsed metrics."""
    script_path = save_k6_script(scenario)

    summary_fd, summary_path = tempfile.mkstemp(suffix=".json")
    os.close(summary_fd)

    start_time = datetime.utcnow()

    try:
        result = subprocess.run(
            ["k6", "run", "--summary-export", summary_path, script_path],
            capture_output=True,
            text=True,
            timeout=600,
        )

        duration_seconds = (datetime.utcnow() - start_time).total_seconds()

        if os.path.exists(summary_path) and os.path.getsize(summary_path) > 0:
            parsed = _parse_k6_summary(summary_path)
        else:
            parsed = {
                "avg_response_time_ms": 0, "min_response_time_ms": 0,
                "max_response_time_ms": 0, "p50_ms": 0, "p90_ms": 0,
                "p95_ms": 0, "p99_ms": 0, "total_requests": 0,
                "requests_per_second": 0, "failed_requests": 0,
                "error_rate": 0, "data_received_kb": 0, "data_sent_kb": 0,
                "vus_max": 0,
                "raw_metrics": {"stdout": result.stdout, "stderr": result.stderr},
            }

        return LoadTestMetrics(
            id=str(uuid.uuid4()),
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            total_requests=parsed["total_requests"],
            failed_requests=parsed["failed_requests"],
            avg_response_time_ms=parsed["avg_response_time_ms"],
            min_response_time_ms=parsed["min_response_time_ms"],
            max_response_time_ms=parsed["max_response_time_ms"],
            p50_ms=parsed["p50_ms"],
            p90_ms=parsed["p90_ms"],
            p95_ms=parsed["p95_ms"],
            p99_ms=parsed["p99_ms"],
            requests_per_second=parsed["requests_per_second"],
            error_rate=parsed["error_rate"],
            data_received_kb=parsed["data_received_kb"],
            data_sent_kb=parsed["data_sent_kb"],
            duration_seconds=duration_seconds,
            vus_max=parsed["vus_max"],
            raw_metrics=parsed.get("raw_metrics", {}),
        )

    except subprocess.TimeoutExpired:
        return LoadTestMetrics(
            id=str(uuid.uuid4()),
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            raw_metrics={"error": "k6 test timed out after 600 seconds"},
        )
    except FileNotFoundError:
        return LoadTestMetrics(
            id=str(uuid.uuid4()),
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            raw_metrics={"error": "k6 is not installed. Install it with: brew install k6"},
        )
    finally:
        if os.path.exists(summary_path):
            os.unlink(summary_path)

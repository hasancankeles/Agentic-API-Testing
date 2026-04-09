from __future__ import annotations

import sys
import uuid
from datetime import datetime
from pathlib import Path
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from models.schemas import (  # noqa: E402
    FlowRunRecord,
    FlowRunStatus,
    FlowScenario,
    FlowStep,
    FlowStepResult,
    HttpMethod,
    TestStatus,
)


def _parse_payload() -> dict:
    spec = """
openapi: 3.0.0
info:
  title: Flow Route Test API
  version: "1.0"
servers:
  - url: https://example.com
paths:
  /login:
    post:
      summary: Login
      responses:
        "200":
          description: ok
  /posts:
    get:
      summary: List posts
      responses:
        "200":
          description: ok
"""
    return {"spec_content": spec}


def _sample_flow() -> FlowScenario:
    return FlowScenario(
        id=str(uuid.uuid4()),
        name="Route Flow",
        description="Flow for API route tests",
        persona="tester",
        preconditions=["parsed api exists"],
        tags=["workflow"],
        steps=[
            FlowStep(
                step_id="step-1",
                order=1,
                name="List posts",
                method=HttpMethod.GET,
                endpoint="/posts",
                expected_status=200,
            ),
            FlowStep(
                step_id="step-2",
                order=2,
                name="List posts again",
                method=HttpMethod.GET,
                endpoint="/posts",
                expected_status=200,
            ),
        ],
    )


class FlowRouteTests(TestCase):
    def test_flow_generate_list_get_update_endpoints(self) -> None:
        flow = _sample_flow()
        flow_summary = {
            "flows_generated": 1,
            "source": "deterministic_fallback",
            "fallback_used": True,
            "fallback_reason": "test",
            "dependency_hints_count": 1,
            "openapi_link_hints_count": 0,
            "llm_attempted": False,
            "llm_normalizations_applied": 0,
            "candidate_flows_reviewed": 0,
            "eliminated_flows_count": 0,
            "eliminated_flows": [],
            "reviewer_applied": False,
            "reviewer_mode": None,
            "negative_flows_added": 0,
            "negative_generation_skipped_reason": "disabled",
            "batch_created_at": datetime.utcnow().isoformat(),
        }

        with TestClient(main.app) as client:
            parse_res = client.post("/api/parse", json=_parse_payload())
            self.assertEqual(parse_res.status_code, 200)

            with patch("main.generate_flows", AsyncMock(return_value=([flow], flow_summary))):
                generate_res = client.post("/api/flows/generate", json={"max_flows": 2, "generation_mode": "pure_llm"})

            self.assertEqual(generate_res.status_code, 200)
            body = generate_res.json()
            self.assertIn("flow_generation_id", body)
            self.assertEqual(len(body["flows"]), 1)
            self.assertIn("llm_attempted", body["summary"])
            self.assertIn("llm_normalizations_applied", body["summary"])
            self.assertIn("candidate_flows_reviewed", body["summary"])
            self.assertIn("eliminated_flows_count", body["summary"])
            self.assertIn("eliminated_flows", body["summary"])
            self.assertIn("reviewer_applied", body["summary"])
            self.assertIn("negative_flows_added", body["summary"])
            flow_id = body["flows"][0]["id"]

            list_res = client.get("/api/flows")
            self.assertEqual(list_res.status_code, 200)
            self.assertTrue(any(item["id"] == flow_id for item in list_res.json()))

            detail_res = client.get(f"/api/flows/{flow_id}")
            self.assertEqual(detail_res.status_code, 200)
            detail = detail_res.json()
            self.assertEqual(detail["id"], flow_id)
            self.assertEqual(len(detail["steps"]), 2)

            detail["description"] = "Updated flow description"
            update_res = client.put(f"/api/flows/{flow_id}", json=detail)
            self.assertEqual(update_res.status_code, 200)
            updated = update_res.json()
            self.assertEqual(updated["description"], "Updated flow description")

    def test_flow_run_and_history_endpoints(self) -> None:
        flow = _sample_flow()
        run_id = str(uuid.uuid4())
        step_result_id = str(uuid.uuid4())
        flow_summary = {
            "flows_generated": 1,
            "source": "deterministic_fallback",
            "fallback_used": True,
            "fallback_reason": "test",
            "dependency_hints_count": 1,
            "openapi_link_hints_count": 0,
            "llm_attempted": False,
            "llm_normalizations_applied": 0,
            "candidate_flows_reviewed": 0,
            "eliminated_flows_count": 0,
            "eliminated_flows": [],
            "reviewer_applied": False,
            "reviewer_mode": None,
            "negative_flows_added": 0,
            "negative_generation_skipped_reason": "disabled",
            "batch_created_at": datetime.utcnow().isoformat(),
        }

        started = datetime.utcnow()
        finished = datetime.utcnow()
        run_record = FlowRunRecord(
            id=run_id,
            flow_id=flow.id,
            flow_name=flow.name,
            status=FlowRunStatus.PASSED,
            target_base_url="https://example.com",
            initial_context={"seed": "value"},
            final_context={"seed": "value", "x": 1},
            started_at=started,
            finished_at=finished,
            step_results=[
                FlowStepResult(
                    id=step_result_id,
                    flow_run_id=run_id,
                    flow_id=flow.id,
                    step_id="step-1",
                    order=1,
                    status=TestStatus.PASSED,
                    resolved_request={"url": "https://example.com/posts", "method": "GET"},
                    response_status=200,
                    response_headers={"content-type": "application/json"},
                    response_body={"ok": True},
                    assertions_passed=1,
                    assertions_total=1,
                    extracted_context_delta={},
                )
            ],
        )

        with TestClient(main.app) as client:
            parse_res = client.post("/api/parse", json=_parse_payload())
            self.assertEqual(parse_res.status_code, 200)

            with patch("main.generate_flows", AsyncMock(return_value=([flow], flow_summary))):
                generate_res = client.post("/api/flows/generate", json={})
            self.assertEqual(generate_res.status_code, 200)
            flow_id = generate_res.json()["flows"][0]["id"]

            with patch("main.run_flow_scenario", return_value=run_record):
                run_res = client.post(
                    "/api/flows/run",
                    json={"flow_ids": [flow_id], "target_base_url": "https://example.com", "initial_context": {"seed": "value"}},
                )

            self.assertEqual(run_res.status_code, 200)
            run_body = run_res.json()
            self.assertEqual(run_body["total_flows"], 1)
            self.assertEqual(run_body["passed"], 1)
            self.assertEqual(len(run_body["flow_runs"]), 1)

            runs_res = client.get("/api/flows/runs")
            self.assertEqual(runs_res.status_code, 200)
            runs = runs_res.json()
            self.assertTrue(any(item["id"] == run_id for item in runs))

            run_detail_res = client.get(f"/api/flows/runs/{run_id}")
            self.assertEqual(run_detail_res.status_code, 200)
            run_detail = run_detail_res.json()
            self.assertEqual(run_detail["id"], run_id)
            self.assertEqual(len(run_detail["step_results"]), 1)

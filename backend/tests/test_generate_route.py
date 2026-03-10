from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from generator.gemini_generator import GenerationDebugCapture, StructuredOutputError  # noqa: E402
from models.schemas import GenerationMeta, TestCategory  # noqa: E402


def _parse_payload() -> dict:
    spec = """
openapi: 3.0.0
info:
  title: Route Test API
  version: "1.0"
servers:
  - url: https://example.com/api/v1
paths:
  /ping:
    get:
      summary: Ping endpoint
      responses:
        "200":
          description: ok
"""
    return {"spec_content": spec}


class GenerateRouteTests(TestCase):
    def test_generate_returns_422_for_structured_output_error(self) -> None:
        with TestClient(main.app) as client:
            parse_res = client.post("/api/parse", json=_parse_payload())
            self.assertEqual(parse_res.status_code, 200)

            mock_generate = AsyncMock(
                side_effect=StructuredOutputError(
                    stage="planner",
                    errors=[{"msg": "invalid endpoint"}],
                    repair_attempted=True,
                )
            )

            with patch("main.generate_all", mock_generate):
                generate_res = client.post(
                    "/api/generate",
                    json={"categories": [TestCategory.INDIVIDUAL.value]},
                )

        self.assertEqual(generate_res.status_code, 422)
        body = generate_res.json()
        self.assertIn("detail", body)
        self.assertEqual(body["detail"]["stage"], "planner")
        self.assertTrue(body["detail"]["repair_attempted"])
        self.assertGreater(len(body["detail"]["errors"]), 0)

    def test_generate_includes_generation_meta_without_breaking_contract(self) -> None:
        with TestClient(main.app) as client:
            parse_res = client.post("/api/parse", json=_parse_payload())
            self.assertEqual(parse_res.status_code, 200)

            generation_meta = GenerationMeta(
                planner_model="gemini-2.5-pro",
                executor_model="gemini-2.5-flash",
                repair_attempted=False,
                dropped_items_count=2,
                executor_jobs_total=10,
                executor_jobs_succeeded=7,
                executor_jobs_failed=3,
                fallback_count=3,
                executor_concurrency=8,
            )

            mock_generate = AsyncMock(return_value=([], [], generation_meta))
            with patch("main.generate_all", mock_generate):
                generate_res = client.post("/api/generate", json={})

        self.assertEqual(generate_res.status_code, 200)
        body = generate_res.json()
        self.assertIn("generation_id", body)
        self.assertIn("suites", body)
        self.assertIn("load_scenarios", body)
        self.assertIn("summary", body)
        self.assertIn("generation_meta", body)
        self.assertEqual(body["generation_meta"]["dropped_items_count"], 2)
        self.assertEqual(body["generation_meta"]["executor_jobs_total"], 10)
        self.assertEqual(body["generation_meta"]["fallback_count"], 3)

    def test_generation_artifact_endpoints_return_debug_payload(self) -> None:
        with TestClient(main.app) as client:
            parse_res = client.post("/api/parse", json=_parse_payload())
            self.assertEqual(parse_res.status_code, 200)

            generation_meta = GenerationMeta(
                planner_model="gemini-3.1-pro",
                executor_model="gemini-3.1-flash-lite",
                repair_attempted=True,
                dropped_items_count=1,
                executor_jobs_total=2,
                executor_jobs_succeeded=1,
                executor_jobs_failed=1,
                fallback_count=1,
                executor_concurrency=8,
            )

            async def fake_generate(_parsed_api, _categories, debug_capture: GenerationDebugCapture | None = None):
                if debug_capture is not None:
                    debug_capture.planner_plan = {"version": "1.0", "metadata": {"source": "test"}}
                    debug_capture.executor_case_outcomes = {
                        "case_1": {
                            "status": "failed",
                            "repair_attempted": True,
                            "fallback_used": True,
                            "error_message": "planner validation issue",
                        }
                    }
                    debug_capture.fallback_case_ids = ["case_1"]
                    debug_capture.raw_llm_outputs = [
                        {"stage": "planner", "attempt": 0, "is_repair": False, "raw_output": "{\"x\":\"y\"}"}
                    ]
                return [], [], generation_meta

            with (
                patch("main.GEN_CAPTURE_RAW_LLM", True),
                patch("main.generate_all", AsyncMock(side_effect=fake_generate)),
            ):
                generate_res = client.post("/api/generate", json={})
                self.assertEqual(generate_res.status_code, 200)
                generation_id = generate_res.json()["generation_id"]

            list_res = client.get("/api/generations")
            self.assertEqual(list_res.status_code, 200)
            listed = list_res.json()
            self.assertTrue(any(item["generation_id"] == generation_id for item in listed))

            detail_res = client.get(f"/api/generations/{generation_id}")
            self.assertEqual(detail_res.status_code, 200)
            detail = detail_res.json()
            self.assertEqual(detail["generation_id"], generation_id)
            self.assertEqual(detail["fallback_case_ids"], ["case_1"])
            self.assertEqual(detail["raw_llm_outputs"], [])

            detail_raw_res = client.get(f"/api/generations/{generation_id}?include_raw=true")
            self.assertEqual(detail_raw_res.status_code, 200)
            detail_raw = detail_raw_res.json()
            self.assertEqual(len(detail_raw["raw_llm_outputs"]), 1)

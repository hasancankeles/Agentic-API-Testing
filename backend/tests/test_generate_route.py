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
from generator.gemini_generator import StructuredOutputError  # noqa: E402
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
        self.assertIn("suites", body)
        self.assertIn("load_scenarios", body)
        self.assertIn("summary", body)
        self.assertIn("generation_meta", body)
        self.assertEqual(body["generation_meta"]["dropped_items_count"], 2)
        self.assertEqual(body["generation_meta"]["executor_jobs_total"], 10)
        self.assertEqual(body["generation_meta"]["fallback_count"], 3)

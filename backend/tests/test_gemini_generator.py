from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from generator.gemini_generator import (  # noqa: E402
    ExecutorTestEnrichment,
    HttpExecutorJob,
    _normalize_assertions,
    _normalize_endpoint_path,
    _run_http_executor_queue,
    StructuredOutputError,
    UpstreamModelError,
    generate_all,
)
from models.schemas import ParsedAPI, PlannerTestCaseDraft, TestAssertion, TestCategory  # noqa: E402
from parser.openapi_parser import parse_openapi  # noqa: E402


def _parsed_api() -> ParsedAPI:
    spec = """
openapi: 3.0.0
info:
  title: Sample API
  version: "1.0"
servers:
  - url: https://example.com/api/v1
paths:
  /pets:
    get:
      summary: List pets
      responses:
        "200":
          description: OK
"""
    return parse_openapi(spec)


def _planner_output_json() -> str:
    payload = {
        "version": "1.0",
        "metadata": {"source": "unit"},
        "assumptions": ["none"],
        "endpoint_groups": [
            {"name": "Pets", "description": "pets routes", "endpoints": ["/pets"]}
        ],
        "individual_tests": [
            {
                "case_id": "case_ind_1",
                "name": "List pets",
                "description": "Happy path",
                "endpoint": "/pets",
                "method": "GET",
                "expected_status": 200,
                "category": "individual",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body_hint": None,
                "assertion_hints": [
                    {"field": "status_code", "operator": "eq", "expected": 200}
                ],
                "depends_on": [],
                "intent_labels": ["happy_path"],
            }
        ],
        "suite_tests": [
            {
                "suite_id": "suite_1",
                "name": "Pets Suite",
                "description": "Suite sample",
                "include_websocket": False,
                "test_cases": [
                    {
                        "case_id": "case_suite_1",
                        "name": "List pets in suite",
                        "description": "Suite happy path",
                        "endpoint": "/pets",
                        "method": "GET",
                        "expected_status": 200,
                        "category": "suite",
                        "headers": {},
                        "query_params": {},
                        "path_params": {},
                        "body_hint": None,
                        "assertion_hints": [],
                        "depends_on": [],
                        "intent_labels": ["suite"],
                    }
                ],
            }
        ],
        "load_scenarios": [
            {
                "scenario_id": "load_1",
                "name": "Load pets",
                "description": "Load sample",
                "target_endpoint": "/pets",
                "method": "GET",
                "vus": 10,
                "duration": "30s",
                "ramp_stages": [],
                "thresholds": {},
                "headers": {},
            }
        ],
    }
    return json.dumps(payload)


def _all_cases(suites: list) -> list:
    return [test_case for suite in suites for test_case in suite.test_cases]


class GeminiGeneratorTests(IsolatedAsyncioTestCase):
    def test_normalize_endpoint_path(self) -> None:
        base_url = "https://example.com/api/v1"
        self.assertEqual(_normalize_endpoint_path("/api/v1/pets", base_url), "/pets")
        self.assertEqual(_normalize_endpoint_path("/pets", base_url), "/pets")
        self.assertEqual(_normalize_endpoint_path("pets", base_url), "/pets")

    def test_normalize_assertions_operator_and_field(self) -> None:
        raw = [
            {"field": "id", "operator": "==", "expected": 10},
            {"field": "[0].status", "operator": "==", "expected": "available"},
            {"field": "HEADER.Content-Type", "operator": "contains", "expected": "json"},
            {"field": "response.body.user.name", "operator": "=", "expected": "alice"},
        ]
        normalized = _normalize_assertions([TestAssertion(**a) for a in raw])

        self.assertEqual(normalized[0].field, "body.id")
        self.assertEqual(normalized[0].operator, "eq")
        self.assertEqual(normalized[1].field, "body.0.status")
        self.assertEqual(normalized[1].operator, "eq")
        self.assertEqual(normalized[2].field, "headers.content-type")
        self.assertEqual(normalized[2].operator, "contains")
        self.assertEqual(normalized[3].field, "body.user.name")
        self.assertEqual(normalized[3].operator, "eq")

    async def test_generate_all_success_with_per_case_executor_queue(self) -> None:
        parsed = _parsed_api()

        async def fake_executor_call(_client: object, _parsed: ParsedAPI, draft: PlannerTestCaseDraft):
            return (
                ExecutorTestEnrichment(
                    case_id=draft.case_id,
                    query_params={"from_executor": draft.case_id},
                ),
                False,
            )

        with (
            patch("generator.gemini_generator._get_client", return_value=object()),
            patch("generator.gemini_generator._call_model_text", side_effect=[_planner_output_json()]),
            patch("generator.gemini_generator._executor_call_for_case", new=fake_executor_call),
        ):
            suites, load_scenarios, generation_meta = await generate_all(
                parsed,
                [TestCategory.INDIVIDUAL, TestCategory.SUITE, TestCategory.LOAD],
            )

        self.assertEqual(len(suites), 2)
        self.assertEqual(len(load_scenarios), 1)
        self.assertEqual(len(_all_cases(suites)), 2)
        from_executor_markers = {case.query_params.get("from_executor") for case in _all_cases(suites)}
        self.assertEqual(from_executor_markers, {"case_ind_1", "case_suite_1"})
        self.assertEqual(generation_meta.dropped_items_count, 0)
        self.assertEqual(generation_meta.executor_jobs_total, 2)
        self.assertEqual(generation_meta.executor_jobs_succeeded, 2)
        self.assertEqual(generation_meta.executor_jobs_failed, 0)
        self.assertEqual(generation_meta.fallback_count, 0)
        self.assertTrue(generation_meta.planner_model)
        self.assertTrue(generation_meta.executor_model)

    async def test_generate_all_per_case_structured_failure_uses_fallback(self) -> None:
        parsed = _parsed_api()

        async def fake_executor_call(_client: object, _parsed: ParsedAPI, draft: PlannerTestCaseDraft):
            if draft.case_id == "case_ind_1":
                raise StructuredOutputError(
                    stage=f"executor_case:{draft.case_id}",
                    errors=[{"msg": "bad JSON"}],
                    repair_attempted=True,
                )
            return (
                ExecutorTestEnrichment(
                    case_id=draft.case_id,
                    headers={"x-enriched": "true"},
                ),
                False,
            )

        with (
            patch("generator.gemini_generator._get_client", return_value=object()),
            patch("generator.gemini_generator._call_model_text", side_effect=[_planner_output_json()]),
            patch("generator.gemini_generator._executor_call_for_case", new=fake_executor_call),
        ):
            suites, _load_scenarios, generation_meta = await generate_all(
                parsed,
                [TestCategory.INDIVIDUAL, TestCategory.SUITE],
            )

        cases_by_name = {case.name: case for case in _all_cases(suites)}
        self.assertNotIn("x-enriched", cases_by_name["List pets"].headers)
        self.assertEqual(cases_by_name["List pets in suite"].headers.get("x-enriched"), "true")
        self.assertEqual(generation_meta.executor_jobs_total, 2)
        self.assertEqual(generation_meta.executor_jobs_succeeded, 1)
        self.assertEqual(generation_meta.executor_jobs_failed, 1)
        self.assertEqual(generation_meta.fallback_count, 1)
        self.assertTrue(generation_meta.repair_attempted)

    async def test_generate_all_per_case_upstream_failure_uses_fallback(self) -> None:
        parsed = _parsed_api()

        async def fake_executor_call(_client: object, _parsed: ParsedAPI, draft: PlannerTestCaseDraft):
            if draft.case_id == "case_suite_1":
                raise UpstreamModelError("executor timeout", status_code=503)
            return (
                ExecutorTestEnrichment(
                    case_id=draft.case_id,
                    headers={"x-enriched": "true"},
                ),
                False,
            )

        with (
            patch("generator.gemini_generator._get_client", return_value=object()),
            patch("generator.gemini_generator._call_model_text", side_effect=[_planner_output_json()]),
            patch("generator.gemini_generator._executor_call_for_case", new=fake_executor_call),
        ):
            suites, _load_scenarios, generation_meta = await generate_all(
                parsed,
                [TestCategory.INDIVIDUAL, TestCategory.SUITE],
            )

        cases_by_name = {case.name: case for case in _all_cases(suites)}
        self.assertEqual(cases_by_name["List pets"].headers.get("x-enriched"), "true")
        self.assertNotIn("x-enriched", cases_by_name["List pets in suite"].headers)
        self.assertEqual(generation_meta.executor_jobs_total, 2)
        self.assertEqual(generation_meta.executor_jobs_succeeded, 1)
        self.assertEqual(generation_meta.executor_jobs_failed, 1)
        self.assertEqual(generation_meta.fallback_count, 1)

    async def test_http_executor_queue_respects_concurrency_limit(self) -> None:
        parsed = _parsed_api()
        jobs = [
            HttpExecutorJob(
                case_id=f"case_{i}",
                draft=PlannerTestCaseDraft(
                    case_id=f"case_{i}",
                    name=f"Case {i}",
                    endpoint="/pets",
                    method="GET",
                    expected_status=200,
                    category="individual",
                ),
            )
            for i in range(12)
        ]

        async def fake_executor_call(_client: object, _parsed: ParsedAPI, draft: PlannerTestCaseDraft):
            await asyncio.sleep(0.01)
            return ExecutorTestEnrichment(case_id=draft.case_id), False

        with patch("generator.gemini_generator._executor_call_for_case", new=fake_executor_call):
            queue_result = await _run_http_executor_queue(
                client=object(),
                parsed_api=parsed,
                jobs=jobs,
                concurrency=3,
            )

        self.assertEqual(queue_result.jobs_total, 12)
        self.assertEqual(queue_result.jobs_succeeded, 12)
        self.assertEqual(queue_result.jobs_failed, 0)
        self.assertEqual(queue_result.fallback_count, 0)
        self.assertEqual(len(queue_result.enrichments), 12)
        self.assertLessEqual(queue_result.max_in_flight, 3)
        self.assertGreaterEqual(queue_result.max_in_flight, 2)

    async def test_generate_all_invalid_planner_after_repair_raises(self) -> None:
        parsed = _parsed_api()
        invalid_output = json.dumps({"individual_tests": [{"endpoint": "http://bad"}]})

        with patch("generator.gemini_generator._get_client", return_value=object()), patch(
            "generator.gemini_generator._call_model_text",
            side_effect=[invalid_output, invalid_output],
        ):
            with self.assertRaises(StructuredOutputError) as ctx:
                await generate_all(parsed, [TestCategory.INDIVIDUAL])

        self.assertEqual(ctx.exception.stage, "planner")
        self.assertTrue(ctx.exception.repair_attempted)
        self.assertGreater(len(ctx.exception.errors), 0)

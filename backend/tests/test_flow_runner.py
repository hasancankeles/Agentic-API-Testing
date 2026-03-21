from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flows.runner import render_templates, resolve_template_string, run_flow_scenario  # noqa: E402
from models.schemas import (  # noqa: E402
    FlowExtractRule,
    FlowScenario,
    FlowStep,
    HttpMethod,
)


class _FakeResponse:
    def __init__(self, status_code: int, body, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        raise ValueError("Not JSON")

    @property
    def text(self) -> str:
        return str(self._body)


class FlowRunnerTests(TestCase):
    def test_template_resolution(self) -> None:
        ctx = {"auth": {"token": "abc"}, "user_id": 42}
        self.assertEqual(resolve_template_string("Bearer {{ctx.auth.token}}", ctx), "Bearer abc")
        rendered = render_templates(
            {
                "headers": {"Authorization": "Bearer {{ctx.auth.token}}"},
                "path": "/users/{{ctx.user_id}}",
            },
            ctx,
        )
        self.assertEqual(rendered["headers"]["Authorization"], "Bearer abc")
        self.assertEqual(rendered["path"], "/users/42")

    def test_extraction_from_body_headers_and_status(self) -> None:
        flow = FlowScenario(
            id="flow_1",
            name="Extraction flow",
            steps=[
                FlowStep(
                    step_id="s1",
                    order=1,
                    name="Login",
                    method=HttpMethod.POST,
                    endpoint="/login",
                    extract=[
                        FlowExtractRule(var="token", source="headers", path="authorization", required=True),
                        FlowExtractRule(var="user_id", source="body", path="user.id", required=True),
                        FlowExtractRule(var="status", source="status_code", required=True),
                    ],
                    expected_status=200,
                ),
                FlowStep(
                    step_id="s2",
                    order=2,
                    name="Use token",
                    method=HttpMethod.GET,
                    endpoint="/users/{id}",
                    path_params={"id": "{{ctx.user_id}}"},
                    headers={"Authorization": "{{ctx.token}}"},
                    expected_status=200,
                ),
            ],
        )

        with patch(
            "flows.runner.http_requests.request",
            side_effect=[
                _FakeResponse(200, {"user": {"id": 99}}, {"authorization": "Bearer token-123"}),
                _FakeResponse(200, {"ok": True}, {}),
            ],
        ):
            result = run_flow_scenario(flow, "http://api.example.com")

        self.assertEqual(result.status.value, "passed")
        self.assertEqual(result.final_context.get("token"), "Bearer token-123")
        self.assertEqual(result.final_context.get("user_id"), 99)
        self.assertEqual(result.final_context.get("status"), 200)

    def test_fail_fast_on_required_step_failure(self) -> None:
        flow = FlowScenario(
            id="flow_2",
            name="Fail fast flow",
            steps=[
                FlowStep(
                    step_id="s1",
                    order=1,
                    name="Fail",
                    method=HttpMethod.GET,
                    endpoint="/first",
                    expected_status=200,
                    required=True,
                ),
                FlowStep(
                    step_id="s2",
                    order=2,
                    name="Should not execute",
                    method=HttpMethod.GET,
                    endpoint="/second",
                    expected_status=200,
                    required=True,
                ),
            ],
        )

        with patch(
            "flows.runner.http_requests.request",
            side_effect=[_FakeResponse(500, {"error": "boom"}, {})],
        ):
            result = run_flow_scenario(flow, "http://api.example.com")

        self.assertEqual(result.status.value, "failed")
        self.assertEqual(len(result.step_results), 1)
        self.assertEqual(result.step_results[0].status.value, "failed")

    def test_non_required_failure_does_not_abort(self) -> None:
        flow = FlowScenario(
            id="flow_3",
            name="Continue flow",
            steps=[
                FlowStep(
                    step_id="s1",
                    order=1,
                    name="Optional failing step",
                    method=HttpMethod.GET,
                    endpoint="/optional",
                    expected_status=404,
                    required=False,
                ),
                FlowStep(
                    step_id="s2",
                    order=2,
                    name="Required success",
                    method=HttpMethod.GET,
                    endpoint="/required",
                    expected_status=200,
                    required=True,
                ),
            ],
        )

        with patch(
            "flows.runner.http_requests.request",
            side_effect=[
                _FakeResponse(500, {"error": "x"}, {}),
                _FakeResponse(200, {"ok": True}, {}),
            ],
        ):
            result = run_flow_scenario(flow, "http://api.example.com")

        self.assertEqual(result.status.value, "passed")
        self.assertEqual(len(result.step_results), 2)
        self.assertEqual(result.step_results[0].status.value, "failed")
        self.assertEqual(result.step_results[1].status.value, "passed")

    def test_schema_validation_for_duplicate_step_ids(self) -> None:
        with self.assertRaises(ValueError):
            FlowScenario(
                id="flow_4",
                name="Invalid flow",
                steps=[
                    FlowStep(step_id="dup", order=1, name="A", method=HttpMethod.GET, endpoint="/a"),
                    FlowStep(step_id="dup", order=2, name="B", method=HttpMethod.GET, endpoint="/b"),
                ],
            )

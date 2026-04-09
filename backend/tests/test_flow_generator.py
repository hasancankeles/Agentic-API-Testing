from __future__ import annotations

import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flows.generator import (  # noqa: E402
    _build_dependency_hints,
    _flow_quality_errors,
    _infer_objectives,
    _llm_compose_flows,
    generate_flows,
)
from models.schemas import (  # noqa: E402
    FlowGenerateRequest,
    FlowGenerationMode,
    FlowMutationPolicy,
    FlowScenario,
    FlowStep,
    HttpMethod,
)
from parser.openapi_parser import parse_openapi  # noqa: E402


class FlowGeneratorTests(IsolatedAsyncioTestCase):
    async def test_generate_flows_uses_llm_path_when_available(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Social API
  version: "1.0"
servers:
  - url: https://example.com
paths:
  /posts:
    get:
      responses:
        "200":
          description: ok
  /posts/{postId}:
    get:
      parameters:
        - name: postId
          in: path
          required: true
          schema: { type: string }
      responses:
        "200":
          description: ok
"""
        )

        req = FlowGenerateRequest(max_flows=3, max_steps_per_flow=6)
        llm_flow = FlowScenario(
            name="LLM Flow",
            description="from llm",
            steps=[
                FlowStep(step_id="a", order=1, name="A", method=HttpMethod.GET, endpoint="/posts"),
                FlowStep(step_id="b", order=2, name="B", method=HttpMethod.GET, endpoint="/posts/{postId}", path_params={"postId": "123"}),
            ],
        )

        with (
            patch("flows.generator.GEMINI_API_KEY", "key"),
            patch("flows.generator._llm_refine_flows", return_value=([llm_flow], 3)),
        ):
            flows, summary = await generate_flows(parsed_api, req, "gen-1")

        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0].name, "LLM Flow")
        self.assertEqual(summary["source"], "llm_refined")
        self.assertFalse(summary["fallback_used"])
        self.assertTrue(summary["llm_attempted"])
        self.assertEqual(summary["llm_normalizations_applied"], 3)

    async def test_generate_flows_falls_back_when_llm_invalid(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Fallback API
  version: "1.0"
servers:
  - url: https://example.com
paths:
  /posts:
    get:
      responses:
        "200":
          description: ok
  /posts/{postId}:
    get:
      parameters:
        - name: postId
          in: path
          required: true
          schema: { type: string }
      responses:
        "200":
          description: ok
"""
        )

        req = FlowGenerateRequest(max_flows=2, max_steps_per_flow=5)

        with (
            patch("flows.generator.GEMINI_API_KEY", "key"),
            patch("flows.generator._llm_refine_flows", side_effect=RuntimeError("bad output")),
        ):
            flows, summary = await generate_flows(parsed_api, req, "gen-2")

        self.assertGreaterEqual(len(flows), 1)
        self.assertEqual(summary["source"], "deterministic_fallback")
        self.assertTrue(summary["fallback_used"])
        self.assertTrue(summary["llm_attempted"])
        self.assertEqual(summary["llm_normalizations_applied"], 0)

    def test_dependency_hints_include_openapi_links_and_param_hints(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Link API
  version: "1.0"
servers:
  - url: https://example.com
paths:
  /users:
    post:
      operationId: createUser
      responses:
        "201":
          description: created
          links:
            GetCreatedUser:
              operationId: getUserById
              parameters:
                userId: "$response.body#/id"
  /users/{userId}:
    get:
      operationId: getUserById
      parameters:
        - name: userId
          in: path
          required: true
          schema: { type: string }
      responses:
        "200":
          description: ok
"""
        )

        hints = _build_dependency_hints(parsed_api)
        self.assertTrue(any(hint.get("kind") == "openapi_link" for hint in hints))
        self.assertTrue(any(hint.get("kind") == "path_param_dependency" for hint in hints))
        self.assertTrue(any(hint.get("kind") == "dependency_edge" for hint in hints))

    def test_objective_inference_for_social_api(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Social API
  version: "1.0"
servers:
  - url: https://example.com
paths:
  /auth/login:
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
  /posts/{postId}:
    get:
      parameters:
        - name: postId
          in: path
          required: true
          schema: { type: string }
      responses:
        "200":
          description: ok
  /posts/{postId}/like:
    post:
      summary: Like post
      parameters:
        - name: postId
          in: path
          required: true
          schema: { type: string }
      responses:
        "200":
          description: ok
"""
        )
        objectives = _infer_objectives(parsed_api, FlowGenerateRequest())
        self.assertIn("authentication and session workflow", objectives)
        self.assertIn("browse and discovery workflow", objectives)
        self.assertIn("interaction workflow", objectives)

    def test_objective_inference_for_transactional_api(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Shop API
  version: "1.0"
servers:
  - url: https://example.com
paths:
  /cart:
    get:
      responses:
        "200":
          description: ok
  /checkout:
    post:
      summary: Checkout cart
      responses:
        "201":
          description: created
"""
        )
        objectives = _infer_objectives(parsed_api, FlowGenerateRequest())
        self.assertIn("transactional lifecycle workflow", objectives)

    def test_quality_gate_detects_missing_vars_and_path_params(self) -> None:
        flow = FlowScenario(
            id="quality_1",
            name="Invalid flow",
            steps=[
                FlowStep(
                    step_id="a",
                    order=1,
                    name="Invalid detail call",
                    method=HttpMethod.GET,
                    endpoint="/posts/{postId}",
                    headers={"Authorization": "Bearer {{ctx.user_token}}"},
                    path_params={},
                    expected_status=200,
                ),
            ],
        )
        errors = _flow_quality_errors(flow, FlowGenerateRequest())
        joined = " | ".join(errors)
        self.assertIn("unresolved endpoint placeholders", joined)
        self.assertIn("missing context vars", joined)

    def test_quality_gate_enforces_read_after_write(self) -> None:
        flow = FlowScenario(
            id="quality_2",
            name="Mutation-only flow",
            steps=[
                FlowStep(
                    step_id="create",
                    order=1,
                    name="Create item",
                    method=HttpMethod.POST,
                    endpoint="/items",
                    body={"name": "x"},
                    expected_status=201,
                ),
            ],
        )
        errors = _flow_quality_errors(flow, FlowGenerateRequest())
        self.assertTrue(any("read-after-write verification" in error for error in errors))

    async def test_auth_required_api_generates_auth_aware_steps(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Auth API
  version: "1.0"
servers:
  - url: https://example.com
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
security:
  - bearerAuth: []
paths:
  /auth/login:
    post:
      summary: Login and get token
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
        )
        req = FlowGenerateRequest(
            generation_mode=FlowGenerationMode.DETERMINISTIC_FIRST,
            mutation_policy=FlowMutationPolicy.SAFE,
            max_flows=2,
            max_steps_per_flow=4,
        )
        flows, _summary = await generate_flows(parsed_api, req, "auth-gen-1")
        self.assertGreaterEqual(len(flows), 1)
        has_login = any(
            any(step.endpoint == "/auth/login" for step in flow.steps)
            for flow in flows
        )
        has_auth_header = any(
            any("Authorization" in step.headers for step in flow.steps)
            for flow in flows
        )
        self.assertTrue(has_login or has_auth_header)

    async def test_llm_compose_normalizes_legacy_extract_schema(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Normalize API
  version: "1.0"
servers:
  - url: https://example.com
paths:
  /status:
    get:
      responses:
        "200":
          description: ok
"""
        )
        req = FlowGenerateRequest()
        llm_payload = {
            "flows": [
                {
                    "name": "Legacy flow",
                    "description": "legacy extract schema",
                    "persona": "tester",
                    "preconditions": [],
                    "tags": ["legacy"],
                    "steps": [
                        {
                            "step_id": "step1",
                            "order": 1,
                            "name": "Status",
                            "method": "GET",
                            "endpoint": "/status",
                            "extract": [
                                {
                                    "key": "api_status",
                                    "json_path": "$.status",
                                    "required": "false",
                                }
                            ],
                            "assertions": [{"field": "status_code", "operator": "eq", "expected": 200}],
                            "expected_status": 200,
                            "required": True,
                        }
                    ],
                }
            ]
        }

        with patch("flows.generator._llm_json_call", return_value=llm_payload):
            flows, normalizations = await _llm_compose_flows(
                client=object(),
                parsed_api=parsed_api,
                req=req,
                objectives=["health"],
                seed_flows=[],
                scenarios=[{"name": "legacy"}],
                dependency_hints=[],
            )

        self.assertEqual(len(flows), 1)
        self.assertGreater(normalizations, 0)
        extract = flows[0].steps[0].extract[0]
        self.assertEqual(extract.var, "api_status")
        self.assertEqual(extract.source.value, "body")
        self.assertEqual(extract.path, "status")
        self.assertFalse(extract.required)

    async def test_include_negative_adds_negative_step_when_feasible(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Negative API
  version: "1.0"
servers:
  - url: https://example.com
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
paths:
  /auth/login:
    post:
      summary: Login
      responses:
        "200":
          description: ok
  /private/posts:
    get:
      security:
        - bearerAuth: []
      summary: List private posts
      responses:
        "200":
          description: ok
        "401":
          description: unauthorized
"""
        )
        req = FlowGenerateRequest(
            generation_mode=FlowGenerationMode.DETERMINISTIC_FIRST,
            include_negative=True,
            max_flows=2,
            max_steps_per_flow=6,
        )

        flows, summary = await generate_flows(parsed_api, req, "neg-gen-1")
        self.assertGreaterEqual(len(flows), 1)
        self.assertEqual(summary["negative_flows_added"], 1)
        self.assertIsNone(summary["negative_generation_skipped_reason"])
        self.assertTrue(
            any(
                any((not step.required) and step.name.lower().startswith("negative") for step in flow.steps)
                for flow in flows
            )
        )

    async def test_include_negative_reports_skip_reason_when_infeasible(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Public API
  version: "1.0"
servers:
  - url: https://example.com
paths:
  /posts:
    get:
      responses:
        "200":
          description: ok
  /posts/{postId}:
    get:
      parameters:
        - name: postId
          in: path
          required: true
          schema: { type: string }
      responses:
        "200":
          description: ok
"""
        )
        req = FlowGenerateRequest(
            generation_mode=FlowGenerationMode.DETERMINISTIC_FIRST,
            include_negative=True,
            max_flows=2,
            max_steps_per_flow=5,
        )

        _flows, summary = await generate_flows(parsed_api, req, "neg-gen-2")
        self.assertEqual(summary["negative_flows_added"], 0)
        self.assertEqual(
            summary["negative_generation_skipped_reason"],
            "no_auth_or_validation_negative_pattern",
        )

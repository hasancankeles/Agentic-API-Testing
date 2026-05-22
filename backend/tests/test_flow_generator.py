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
    _llm_critic_repair,
    _llm_generate_candidate_flows,
    _llm_plan_scenarios,
    _llm_review_candidates,
    _normalize_llm_flow_payload,
    _inject_negative_step,
    _review_candidate_flows,
    generate_flows,
)
from models.schemas import (  # noqa: E402
    FlowEliminatedCandidate,
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
            patch("flows.generator._review_candidate_flows", return_value=([llm_flow], [], True)),
        ):
            flows, summary = await generate_flows(parsed_api, req, "gen-1")

        self.assertGreaterEqual(len(flows), 1)
        self.assertEqual(flows[0].name, "LLM Flow")
        self.assertEqual(summary["source"], "llm_refined")
        self.assertFalse(summary["fallback_used"])
        self.assertTrue(summary["llm_attempted"])
        self.assertEqual(summary["llm_normalizations_applied"], 3)
        self.assertEqual(summary["llm_deterministic_backfill_count"], len(flows) - 1)
        self.assertTrue(summary["reviewer_applied"])

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

    def test_quality_gate_requires_read_after_final_write_not_before(self) -> None:
        flow = FlowScenario(
            id="quality_read_order",
            name="Read before write",
            steps=[
                FlowStep(
                    step_id="list",
                    order=1,
                    name="List items",
                    method=HttpMethod.GET,
                    endpoint="/items",
                    expected_status=200,
                ),
                FlowStep(
                    step_id="create",
                    order=2,
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

    def test_quality_gate_treats_authorized_business_write_as_mutation(self) -> None:
        flow = FlowScenario(
            id="quality_authorized_write",
            name="Authorized write without verification",
            steps=[
                FlowStep(
                    step_id="auth",
                    order=1,
                    name="Authenticate",
                    method=HttpMethod.POST,
                    endpoint="/auth",
                    extract=[{"var": "auth_token", "from": "body", "path": "token", "required": True}],
                    expected_status=200,
                ),
                FlowStep(
                    step_id="update",
                    order=2,
                    name="Update booking",
                    method=HttpMethod.PATCH,
                    endpoint="/booking/{id}",
                    path_params={"id": "{{ctx.booking_id}}"},
                    headers={"Authorization": "Bearer {{ctx.auth_token}}"},
                    body={"firstname": "Updated"},
                    expected_status=200,
                ),
            ],
        )

        errors = _flow_quality_errors(flow, FlowGenerateRequest())

        self.assertTrue(any("read-after-write verification" in error for error in errors))

    def test_quality_gate_safe_policy_ignores_auth_step_as_business_mutation(self) -> None:
        flow = FlowScenario(
            id="quality_3",
            name="Auth then patch",
            steps=[
                FlowStep(
                    step_id="list",
                    order=1,
                    name="List bookings",
                    method=HttpMethod.GET,
                    endpoint="/booking",
                    extract=[{"var": "booking_id", "from": "body", "path": "0.bookingid", "required": True}],
                    expected_status=200,
                ),
                FlowStep(
                    step_id="auth",
                    order=2,
                    name="Authenticate",
                    method=HttpMethod.POST,
                    endpoint="/auth",
                    body={"username": "admin", "password": "password123"},
                    extract=[{"var": "auth_token", "from": "body", "path": "token", "required": True}],
                    expected_status=200,
                ),
                FlowStep(
                    step_id="patch",
                    order=3,
                    name="Patch booking",
                    method=HttpMethod.PATCH,
                    endpoint="/booking/{id}",
                    path_params={"id": "{{ctx.booking_id}}"},
                    headers={"Authorization": "Bearer {{ctx.auth_token}}"},
                    body={"firstname": "Updated"},
                    expected_status=200,
                ),
            ],
        )

        errors = _flow_quality_errors(
            flow,
            FlowGenerateRequest(mutation_policy=FlowMutationPolicy.SAFE),
        )
        self.assertFalse(any("mutation ratio" in error for error in errors))

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

    async def test_swagger_booker_style_flows_use_runnable_auth_and_id_hints(self) -> None:
        parsed_api = parse_openapi(
            """
swagger: "2.0"
info:
  title: Booker
  version: "1.0"
paths:
  /auth:
    post:
      summary: Get an authorization token
      parameters:
        - in: body
          name: body
          required: true
          schema:
            $ref: "#/definitions/AuthParams"
      responses:
        "200":
          description: ok
          schema:
            $ref: "#/definitions/AuthResponse"
  /booking:
    get:
      summary: Get booking IDs
      responses:
        "200":
          description: ok
          schema:
            type: array
            items:
              $ref: "#/definitions/GetIdsResponse"
  /booking/{id}:
    put:
      summary: Update a booking
      parameters:
        - name: id
          in: path
          required: true
          schema: { type: string }
        - name: Authorization
          in: header
          required: true
          schema: { type: string }
        - in: body
          name: body
          required: true
          schema:
            $ref: "#/definitions/Booking"
      responses:
        "200":
          description: ok
definitions:
  AuthParams:
    type: object
    properties:
      username: { type: string }
      password: { type: string }
  AuthResponse:
    type: object
    properties:
      token: { type: string }
  GetIdsResponse:
    type: object
    properties:
      bookingid: { type: integer }
  Booking:
    type: object
    properties:
      firstname: { type: string }
      lastname: { type: string }
      totalprice: { type: integer }
      depositpaid: { type: boolean }
      bookingdates:
        type: object
        properties:
          checkin: { type: string }
          checkout: { type: string }
      additionalneeds: { type: string }
"""
        )
        req = FlowGenerateRequest(
            generation_mode=FlowGenerationMode.DETERMINISTIC_FIRST,
            mutation_policy=FlowMutationPolicy.SAFE,
            max_flows=2,
            max_steps_per_flow=4,
        )

        flows, _summary = await generate_flows(parsed_api, req, "booker-gen")

        all_steps = [step for flow in flows for step in flow.steps]
        auth_step = next(step for step in all_steps if step.endpoint == "/auth")
        list_step = next(step for step in all_steps if step.endpoint == "/booking")
        protected_step = next(step for step in all_steps if step.endpoint == "/booking/{id}")

        self.assertEqual(auth_step.body, {"username": "admin", "password": "password123"})
        self.assertTrue(
            any(rule.var == "booking_id" and rule.path == "0.bookingid" for rule in list_step.extract)
        )
        self.assertEqual(protected_step.headers.get("Cookie"), "token={{ctx.auth_token}}")
        self.assertEqual(protected_step.path_params.get("id"), "{{ctx.booking_id}}")
        for flow in flows:
            update_steps = [step for step in flow.steps if step.method == HttpMethod.PUT]
            if not update_steps:
                continue
            last_update_order = max(step.order for step in update_steps)
            self.assertTrue(
                any(step.method == HttpMethod.GET and step.order > last_update_order for step in flow.steps)
            )

    def test_llm_normalization_adds_dual_auth_headers_for_protected_steps(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Auth API
  version: "1.0"
paths:
  /booking/{id}:
    put:
      parameters:
        - name: id
          in: path
          required: true
          schema: { type: string }
        - name: Authorization
          in: header
          required: true
          schema: { type: string }
      responses:
        "200":
          description: ok
"""
        )
        raw_flow = {
            "name": "LLM booking update",
            "steps": [
                {
                    "step_id": "update",
                    "order": 1,
                    "name": "Update",
                    "method": "PUT",
                    "endpoint": "/booking/{id}",
                    "path_params": {"id": "{{ctx.booking_id}}"},
                    "headers": {"Cookie": "token={{ctx.auth_token}}"},
                    "expected_status": 200,
                    "extract": [],
                }
            ],
        }

        normalized, normalizations = _normalize_llm_flow_payload(raw_flow, parsed_api)

        headers = normalized["steps"][0]["headers"]
        self.assertGreaterEqual(normalizations, 1)
        self.assertEqual(headers.get("Authorization"), "Bearer {{ctx.auth_token}}")
        self.assertEqual(headers.get("Cookie"), "token={{ctx.auth_token}}")

    def test_llm_normalization_reconciles_success_status_with_openapi(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Status API
  version: "1.0"
paths:
  /ping:
    get:
      responses:
        "201":
          description: created
"""
        )
        raw_flow = {
            "name": "LLM status flow",
            "steps": [
                {
                    "step_id": "ping",
                    "order": 1,
                    "name": "Ping",
                    "method": "GET",
                    "endpoint": "/ping",
                    "expected_status": 200,
                    "assertions": [{"field": "status_code", "operator": "eq", "expected": 200}],
                    "extract": [],
                }
            ],
        }

        normalized, normalizations = _normalize_llm_flow_payload(raw_flow, parsed_api)

        step = normalized["steps"][0]
        self.assertGreaterEqual(normalizations, 1)
        self.assertEqual(step["expected_status"], 201)
        self.assertEqual(step["assertions"][0]["expected"], 201)

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

    async def test_llm_first_prompts_request_multiple_distinct_flows(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Multi Flow Prompt API
  version: "1.0"
paths:
  /items:
    get:
      responses:
        "200":
          description: ok
  /items/{id}:
    get:
      parameters:
        - name: id
          in: path
          required: true
          schema: { type: string }
      responses:
        "200":
          description: ok
"""
        )
        req = FlowGenerateRequest(max_flows=3, max_steps_per_flow=5)
        captured: dict[str, str] = {}

        async def fake_json_call(_client, _model, prompt: str, label: str):
            captured[label] = prompt
            if label == "scenario planner":
                return {
                    "scenarios": [
                        {
                            "name": "Browse",
                            "description": "browse items",
                            "persona": "tester",
                            "tags": ["browse"],
                            "objective": "browse",
                            "ordered_operations": [{"operation": "GET /items", "reason": "list"}],
                        }
                    ]
                }
            return {
                "flows": [
                    {
                        "name": "Browse flow",
                        "description": "browse items",
                        "persona": "tester",
                        "preconditions": [],
                        "tags": ["browse"],
                        "steps": [
                            {
                                "step_id": "list_items",
                                "order": 1,
                                "name": "List items",
                                "method": "GET",
                                "endpoint": "/items",
                                "assertions": [{"field": "status_code", "operator": "eq", "expected": 200}],
                                "expected_status": 200,
                                "required": True,
                            }
                        ],
                    }
                ]
            }

        with patch("flows.generator._llm_json_call", side_effect=fake_json_call):
            scenarios = await _llm_plan_scenarios(
                client=object(),
                parsed_api=parsed_api,
                req=req,
                objectives=["browse", "detail", "create"],
                dependency_hints=[],
            )
            flows, _normalizations = await _llm_compose_flows(
                client=object(),
                parsed_api=parsed_api,
                req=req,
                objectives=["browse", "detail", "create"],
                seed_flows=[],
                scenarios=scenarios,
                dependency_hints=[],
            )
            await _llm_critic_repair(
                client=object(),
                parsed_api=parsed_api,
                req=req,
                flows=flows,
            )

        self.assertIn("target_scenario_count=3", captured["scenario planner"])
        self.assertIn("Return exactly max_flows scenarios", captured["scenario planner"])
        self.assertIn("Do not plan one-step flows", captured["scenario planner"])
        self.assertIn("include an earlier producer operation", captured["scenario planner"])
        self.assertIn("target_flow_count=3", captured["flow composer"])
        self.assertIn("aim for exactly max_flows", captured["flow composer"])
        self.assertIn("Never return a one-step flow", captured["flow composer"])
        self.assertIn("an earlier step in the same flow must extract that exact ctx variable", captured["flow composer"])
        self.assertIn("Preserve as many distinct valid flows as possible", captured["flow critic"])
        self.assertIn("Repair one-step flows", captured["flow critic"])
        self.assertIn("Repair missing id dependencies", captured["flow critic"])
        self.assertIn("max_flows=3", captured["flow critic"])

    async def test_pure_llm_prompt_omits_seed_flow_scaffolding(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Prompt API
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
            generation_mode=FlowGenerationMode.PURE_LLM,
            max_flows=2,
            max_steps_per_flow=4,
        )
        captured: dict[str, str] = {}

        async def fake_json_call(_client, _model, prompt: str, _label: str):
            captured["prompt"] = prompt
            return {
                "flows": [
                    {
                        "name": "Pure flow",
                        "description": "candidate",
                        "persona": "tester",
                        "preconditions": [],
                        "tags": ["workflow"],
                        "steps": [
                            {
                                "step_id": "list_posts",
                                "order": 1,
                                "name": "List posts",
                                "method": "GET",
                                "endpoint": "/posts",
                                "extract": [{"var": "post_id", "from": "body", "path": "0.id", "required": False}],
                                "assertions": [{"field": "status_code", "operator": "eq", "expected": 200}],
                                "expected_status": 200,
                                "required": True,
                            },
                            {
                                "step_id": "get_post",
                                "order": 2,
                                "name": "Get post",
                                "method": "GET",
                                "endpoint": "/posts/{postId}",
                                "path_params": {"postId": "{{ctx.post_id}}"},
                                "assertions": [{"field": "status_code", "operator": "eq", "expected": 200}],
                                "expected_status": 200,
                                "required": True,
                            },
                        ],
                    }
                ]
            }

        with patch("flows.generator._llm_json_call", side_effect=fake_json_call):
            flows, _normalizations, _schema_invalid = await _llm_generate_candidate_flows(
                client=object(),
                parsed_api=parsed_api,
                req=req,
                objectives=["browse and discovery workflow"],
                dependency_hints=[],
            )

        self.assertEqual(len(flows), 1)
        self.assertIn("candidate_limit=6", captured["prompt"])
        self.assertIn("accepted_flow_target=2", captured["prompt"])
        self.assertIn("Return exactly candidate_limit candidate flows", captured["prompt"])
        self.assertIn("Do not stop after one valid flow", captured["prompt"])
        self.assertIn("Never return a one-step flow", captured["prompt"])
        self.assertIn("an earlier step must extract that exact variable", captured["prompt"])
        self.assertNotIn("Deterministic seed flows", captured["prompt"])

    async def test_pure_llm_collects_schema_invalid_candidates(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Schema API
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

        with patch(
            "flows.generator._llm_json_call",
            return_value={"flows": [{"name": "Broken flow", "steps": "not-a-list"}]},
        ):
            flows, _normalizations, schema_invalid = await _llm_generate_candidate_flows(
                client=object(),
                parsed_api=parsed_api,
                req=FlowGenerateRequest(generation_mode=FlowGenerationMode.PURE_LLM),
                objectives=["health"],
                dependency_hints=[],
            )

        self.assertEqual(flows, [])
        self.assertEqual(len(schema_invalid), 1)
        self.assertEqual(schema_invalid[0].reason_code, "schema_invalid")

    async def test_pure_llm_normalizes_embedded_ctx_path_segments(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Booking API
  version: "1.0"
servers:
  - url: https://example.com
paths:
  /booking:
    post:
      responses:
        "200":
          description: ok
  /booking/{id}:
    put:
      parameters:
        - name: id
          in: path
          required: true
          schema: { type: string }
      responses:
        "200":
          description: ok
"""
        )
        payload = {
            "flows": [
                {
                    "name": "Booking update",
                    "description": "embedded ctx path segment",
                    "persona": "tester",
                    "preconditions": [],
                    "tags": ["booking"],
                    "steps": [
                        {
                            "step_id": "create",
                            "order": 1,
                            "name": "Create booking",
                            "method": "POST",
                            "endpoint": "/booking",
                            "extract": [{"var": "booking_id", "from": "body", "path": "bookingid", "required": True}],
                            "expected_status": 200,
                            "required": True,
                        },
                        {
                            "step_id": "update",
                            "order": 2,
                            "name": "Update booking",
                            "method": "PUT",
                            "endpoint": "/booking/{{ctx.booking_id}}",
                            "path_params": {},
                            "expected_status": 200,
                            "required": True,
                        },
                    ],
                }
            ]
        }

        with patch("flows.generator._llm_json_call", return_value=payload):
            flows, normalizations, _schema_invalid = await _llm_generate_candidate_flows(
                client=object(),
                parsed_api=parsed_api,
                req=FlowGenerateRequest(generation_mode=FlowGenerationMode.PURE_LLM),
                objectives=["update and verify workflow"],
                dependency_hints=[],
            )

        self.assertEqual(len(flows), 1)
        self.assertGreater(normalizations, 0)
        step = flows[0].steps[1]
        self.assertEqual(step.endpoint, "/booking/{id}")
        self.assertEqual(step.path_params, {"id": "{{ctx.booking_id}}"})

    async def test_pure_llm_returns_zero_flows_without_deterministic_fallback(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Pure API
  version: "1.0"
servers:
  - url: https://example.com
paths:
  /items:
    get:
      responses:
        "200":
          description: ok
"""
        )

        with (
            patch("flows.generator.GEMINI_API_KEY", "key"),
            patch(
                "flows.generator._llm_generate_candidate_flows",
                return_value=(
                    [],
                    0,
                    [
                        FlowEliminatedCandidate(
                            name="Broken candidate",
                            reason_code="schema_invalid",
                            reason="candidate failed schema validation",
                        )
                    ],
                ),
            ),
        ):
            flows, summary = await generate_flows(
                parsed_api,
                FlowGenerateRequest(generation_mode=FlowGenerationMode.PURE_LLM),
                "pure-gen-1",
            )

        self.assertEqual(flows, [])
        self.assertEqual(summary["source"], "pure_llm")
        self.assertFalse(summary["fallback_used"])
        self.assertEqual(summary["flows_generated"], 0)
        self.assertEqual(summary["eliminated_flows_count"], 1)
        self.assertEqual(summary["fallback_reason"], "pure_llm_reviewer_rejected_all_candidates")

    async def test_pure_llm_reviewed_down_to_max_flows(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Review API
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
        accepted = [
            FlowScenario(
                name=f"Flow {index}",
                description="candidate",
                persona="tester",
                steps=[
                    FlowStep(step_id=f"list_{index}", order=1, name="List", method=HttpMethod.GET, endpoint="/posts"),
                    FlowStep(
                        step_id=f"detail_{index}",
                        order=2,
                        name="Detail",
                        method=HttpMethod.GET,
                        endpoint="/posts/{postId}",
                        path_params={"postId": str(index)},
                    ),
                ],
            )
            for index in range(1, 4)
        ]

        with (
            patch("flows.generator.GEMINI_API_KEY", "key"),
            patch("flows.generator._llm_generate_candidate_flows", return_value=(accepted, 1, [])),
            patch("flows.generator._review_candidate_flows", return_value=(accepted, [], True)),
        ):
            flows, summary = await generate_flows(
                parsed_api,
                FlowGenerateRequest(generation_mode=FlowGenerationMode.PURE_LLM, max_flows=2),
                "pure-gen-2",
            )

        self.assertEqual(len(flows), 2)
        self.assertEqual(summary["flows_generated"], 2)
        self.assertEqual(summary["candidate_flows_reviewed"], 3)

    async def test_reviewer_rejects_impossible_extraction_for_text_response(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Login API
  version: "1.0"
servers:
  - url: https://example.com
paths:
  /user/login:
    get:
      parameters:
        - name: username
          in: query
          required: true
          schema: { type: string }
        - name: password
          in: query
          required: true
          schema: { type: string }
      responses:
        "200":
          description: logged in
          content:
            text/plain:
              example: "Logged in user session: 123"
  /pet/findByStatus:
    get:
      responses:
        "200":
          description: ok
"""
        )
        bad_flow = FlowScenario(
            name="Login token flow",
            description="broken extraction",
            persona="tester",
            steps=[
                FlowStep(
                    step_id="login",
                    order=1,
                    name="Login",
                    method=HttpMethod.GET,
                    endpoint="/user/login",
                    query_params={"username": "tester", "password": "password"},
                    extract=[
                        {
                            "var": "auth_token",
                            "from": "body",
                            "path": "message",
                            "required": True,
                        }
                    ],
                    expected_status=200,
                ),
                FlowStep(
                    step_id="browse",
                    order=2,
                    name="Browse",
                    method=HttpMethod.GET,
                    endpoint="/pet/findByStatus",
                    expected_status=200,
                ),
            ],
        )

        accepted, eliminated, reviewer_applied = await _review_candidate_flows(
            parsed_api,
            FlowGenerateRequest(generation_mode=FlowGenerationMode.PURE_LLM),
            [bad_flow],
        )

        self.assertEqual(accepted, [])
        self.assertFalse(reviewer_applied)
        self.assertEqual(eliminated[0].reason_code, "impossible_extraction")

    async def test_reviewer_rejects_duplicate_and_unresolved_context_candidates(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Duplicate API
  version: "1.0"
servers:
  - url: https://example.com
paths:
  /posts:
    get:
      responses:
        "200":
          description: ok
          content:
            application/json:
              example:
                - id: "post-1"
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
        first = FlowScenario(
            name="Flow one",
            steps=[
                FlowStep(
                    step_id="list",
                    order=1,
                    name="List",
                    method=HttpMethod.GET,
                    endpoint="/posts",
                    extract=[{"var": "post_id", "from": "body", "path": "0.id", "required": True}],
                ),
                FlowStep(
                    step_id="detail",
                    order=2,
                    name="Detail",
                    method=HttpMethod.GET,
                    endpoint="/posts/{postId}",
                    path_params={"postId": "{{ctx.post_id}}"},
                ),
            ],
        )
        second = FlowScenario(
            name="Flow two",
            steps=[
                FlowStep(
                    step_id="list_again",
                    order=1,
                    name="List",
                    method=HttpMethod.GET,
                    endpoint="/posts",
                    extract=[{"var": "post_id", "from": "body", "path": "0.id", "required": True}],
                ),
                FlowStep(
                    step_id="detail_again",
                    order=2,
                    name="Detail",
                    method=HttpMethod.GET,
                    endpoint="/posts/{postId}",
                    path_params={"postId": "{{ctx.post_id}}"},
                ),
            ],
        )
        unresolved = FlowScenario(
            name="Flow three",
            steps=[
                FlowStep(
                    step_id="list_seed",
                    order=1,
                    name="List",
                    method=HttpMethod.GET,
                    endpoint="/posts",
                ),
                FlowStep(
                    step_id="needs_ctx",
                    order=2,
                    name="Needs ctx",
                    method=HttpMethod.GET,
                    endpoint="/posts",
                    headers={"Authorization": "Bearer {{ctx.missing_token}}"},
                ),
            ],
        )

        with (
            patch("flows.generator.GEMINI_API_KEY", "key"),
            patch(
                "flows.generator._llm_review_candidates",
                return_value={
                    "candidate_1": type("Decision", (), {"keep": True, "reason_code": "accepted", "reason": ""})()
                },
            ),
        ):
            accepted, eliminated, reviewer_applied = await _review_candidate_flows(
                parsed_api,
                FlowGenerateRequest(generation_mode=FlowGenerationMode.PURE_LLM),
                [first, second, unresolved],
            )

        self.assertEqual(len(accepted), 1)
        self.assertTrue(reviewer_applied)
        self.assertEqual(len(eliminated), 2)
        self.assertTrue(any(item.reason_code == "duplicate_flow" for item in eliminated))
        self.assertTrue(any(item.reason_code == "unresolved_context_dependency" for item in eliminated))

    async def test_reviewer_preserves_valid_flow(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Valid API
  version: "1.0"
servers:
  - url: https://example.com
paths:
  /posts:
    get:
      responses:
        "200":
          description: ok
          content:
            application/json:
              example:
                - id: "post-1"
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
        good_flow = FlowScenario(
            name="Valid flow",
            description="supported extraction",
            persona="tester",
            steps=[
                FlowStep(
                    step_id="list",
                    order=1,
                    name="List",
                    method=HttpMethod.GET,
                    endpoint="/posts",
                    extract=[{"var": "post_id", "from": "body", "path": "0.id", "required": True}],
                    expected_status=200,
                ),
                FlowStep(
                    step_id="detail",
                    order=2,
                    name="Detail",
                    method=HttpMethod.GET,
                    endpoint="/posts/{postId}",
                    path_params={"postId": "{{ctx.post_id}}"},
                    expected_status=200,
                ),
            ],
        )

        with (
            patch("flows.generator.GEMINI_API_KEY", "key"),
            patch(
                "flows.generator._llm_review_candidates",
                return_value={
                    "candidate_1": type("Decision", (), {"keep": True, "reason_code": "accepted", "reason": ""})()
                },
            ),
        ):
            accepted, eliminated, reviewer_applied = await _review_candidate_flows(
                parsed_api,
                FlowGenerateRequest(generation_mode=FlowGenerationMode.PURE_LLM),
                [good_flow],
            )

        self.assertEqual(len(accepted), 1)
        self.assertEqual(eliminated, [])
        self.assertTrue(reviewer_applied)

    async def test_reviewer_prompt_allows_token_template_auth_variants(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Auth Review API
  version: "1.0"
paths:
  /private:
    get:
      parameters:
        - name: Authorization
          in: header
          required: true
          schema: { type: string }
      responses:
        "200":
          description: ok
"""
        )
        flow = FlowScenario(
            name="Protected read",
            steps=[
                FlowStep(
                    step_id="read",
                    order=1,
                    name="Read",
                    method=HttpMethod.GET,
                    endpoint="/private",
                    headers={"Authorization": "Bearer {{ctx.auth_token}}"},
                    expected_status=200,
                )
            ],
        )
        captured_prompt = ""

        async def fake_llm_json_call(_client, _model, prompt, _label):
            nonlocal captured_prompt
            captured_prompt = prompt
            return {
                "decisions": [
                    {"candidate_id": "candidate_1", "keep": True, "reason_code": "accepted", "reason": ""}
                ]
            }

        with patch("flows.generator._llm_json_call", new=fake_llm_json_call):
            await _llm_review_candidates(
                client=object(),
                parsed_api=parsed_api,
                req=FlowGenerateRequest(),
                flows=[("candidate_1", flow)],
            )

        self.assertIn("Do not reject solely because an Authorization header uses a Bearer token template", captured_prompt)
        self.assertIn("Authorization and/or Cookie", captured_prompt)

    def test_invalid_negative_injection_is_skipped_with_reason(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Negative Validation API
  version: "1.0"
servers:
  - url: https://example.com
paths:
  /private/posts:
    get:
      security:
        - bearerAuth: []
      responses:
        "200":
          description: ok
        "401":
          description: unauthorized
"""
        )
        req = FlowGenerateRequest(include_negative=True)
        flow = FlowScenario(
            name="Candidate",
            steps=[
                FlowStep(step_id="list", order=1, name="List", method=HttpMethod.GET, endpoint="/private/posts")
            ],
        )
        invalid_negative = FlowStep(
            step_id="bad_negative",
            order=2,
            name="Bad negative",
            method=HttpMethod.GET,
            endpoint="/private/{postId}",
            required=False,
        )

        with patch("flows.generator._build_negative_auth_step", return_value=invalid_negative):
            updated, added, reason = _inject_negative_step([flow], parsed_api, req)

        self.assertEqual(updated, [flow])
        self.assertEqual(added, 0)
        self.assertIsNotNone(reason)
        self.assertTrue(str(reason).startswith("negative_step_invalid:"))

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

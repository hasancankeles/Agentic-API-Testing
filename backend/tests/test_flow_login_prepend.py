from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flows.generator import _inject_login_prepend  # noqa: E402
from models.schemas import (  # noqa: E402
    FlowExtractRule,
    FlowGenerateRequest,
    FlowScenario,
    FlowStep,
    HttpMethod,
    TestAssertion,
)
from parser.openapi_parser import parse_openapi  # noqa: E402


def _parse(spec_yaml: str):
    # parse_openapi tries to open the input as a file path before falling back
    # to parsing as a string, so route long YAML through a temp file.
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        handle.write(spec_yaml)
        path = handle.name
    try:
        return parse_openapi(path)
    finally:
        Path(path).unlink(missing_ok=True)


SPEC_WITH_LOGIN_AND_REGISTER = """
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
  /auth/register:
    post:
      summary: Register a new user
      security: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [email, password]
              properties:
                email: { type: string }
                password: { type: string }
      responses:
        "201":
          description: created
  /auth/login:
    post:
      summary: Login and get token
      security: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [email, password]
              properties:
                email: { type: string }
                password: { type: string }
      responses:
        "200":
          description: ok
  /cart/items:
    post:
      summary: Add item to cart
      responses:
        "200":
          description: ok
  /orders:
    post:
      summary: Place order
      responses:
        "201":
          description: ok
"""

SPEC_WITHOUT_LOGIN = """
openapi: 3.0.0
info:
  title: Public API
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
  /cart/items:
    post:
      summary: Add item to cart
      responses:
        "200":
          description: ok
"""

SPEC_LOGIN_ONLY = """
openapi: 3.0.0
info:
  title: Login Only API
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
      security: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [email, password]
              properties:
                email: { type: string }
                password: { type: string }
      responses:
        "200":
          description: ok
  /cart/items:
    post:
      summary: Add item to cart
      responses:
        "200":
          description: ok
"""


def _make_flow(steps: list[FlowStep], name: str = "Test Flow") -> FlowScenario:
    return FlowScenario(id="flow-1", name=name, steps=steps)


def _step(
    *,
    step_id: str,
    order: int,
    endpoint: str,
    method: HttpMethod = HttpMethod.GET,
    headers: dict | None = None,
    body=None,
    extract: list[FlowExtractRule] | None = None,
) -> FlowStep:
    return FlowStep(
        step_id=step_id,
        order=order,
        name=step_id,
        method=method,
        endpoint=endpoint,
        headers=headers or {},
        body=body,
        extract=extract or [],
        assertions=[TestAssertion(field="status_code", operator="eq", expected=200)],
        expected_status=200,
    )


class InjectLoginPrependTests(TestCase):
    def test_register_and_login_prepended_when_no_credentials_provided(self) -> None:
        parsed_api = _parse(SPEC_WITH_LOGIN_AND_REGISTER)
        flow = _make_flow(
            [
                _step(
                    step_id="add_item",
                    order=1,
                    endpoint="/cart/items",
                    method=HttpMethod.POST,
                    headers={"Authorization": "Bearer {{ctx.auth_token}}"},
                    body={"product_id": 1},
                ),
                _step(
                    step_id="place_order",
                    order=2,
                    endpoint="/orders",
                    method=HttpMethod.POST,
                    headers={"Authorization": "Bearer {{ctx.auth_token}}"},
                ),
            ]
        )

        req = FlowGenerateRequest(max_flows=5, max_steps_per_flow=10)
        flows, injected, skip_reasons = _inject_login_prepend([flow], parsed_api, req)

        self.assertEqual(injected, 1)
        self.assertEqual(skip_reasons, {})
        self.assertEqual(len(flows), 1)

        steps = flows[0].steps
        self.assertEqual(len(steps), 4)
        self.assertEqual(steps[0].endpoint, "/auth/register")
        self.assertEqual(steps[0].method, HttpMethod.POST)
        self.assertEqual(steps[0].order, 1)
        self.assertEqual(steps[1].endpoint, "/auth/login")
        self.assertEqual(steps[1].method, HttpMethod.POST)
        self.assertEqual(steps[1].order, 2)
        self.assertEqual(steps[2].endpoint, "/cart/items")
        self.assertEqual(steps[2].order, 3)
        self.assertEqual(steps[3].endpoint, "/orders")
        self.assertEqual(steps[3].order, 4)

        # Login step must extract auth_token from access_token.
        login_extract_vars = {rule.var for rule in steps[1].extract}
        self.assertIn("auth_token", login_extract_vars)

        # Register and login bodies should reference the same templated identifiers
        # so they target the same just-registered user.
        self.assertEqual(steps[0].body.get("email"), steps[1].body.get("email"))
        self.assertEqual(steps[0].body.get("password"), steps[1].body.get("password"))

    def test_login_only_prepended_when_app_context_credentials_provided(self) -> None:
        parsed_api = _parse(SPEC_WITH_LOGIN_AND_REGISTER)
        flow = _make_flow(
            [
                _step(
                    step_id="add_item",
                    order=1,
                    endpoint="/cart/items",
                    method=HttpMethod.POST,
                    headers={"Authorization": "Bearer {{ctx.auth_token}}"},
                ),
            ]
        )

        req = FlowGenerateRequest(
            max_flows=5,
            max_steps_per_flow=10,
            app_context={
                "auth": {
                    "test_user": {
                        "email": "u@example.com",
                        "password": "Passw0rd!",
                    }
                }
            },
        )
        flows, injected, skip_reasons = _inject_login_prepend([flow], parsed_api, req)

        self.assertEqual(injected, 1)
        self.assertEqual(len(flows[0].steps), 2)
        self.assertEqual(flows[0].steps[0].endpoint, "/auth/login")
        self.assertEqual(flows[0].steps[0].body.get("email"), "u@example.com")
        self.assertEqual(flows[0].steps[0].body.get("password"), "Passw0rd!")

    def test_no_prepend_when_no_auth_required_steps(self) -> None:
        parsed_api = parse_openapi(
            """
openapi: 3.0.0
info:
  title: Public API
  version: "1.0"
servers:
  - url: https://example.com
paths:
  /products:
    get:
      responses:
        "200":
          description: ok
"""
        )
        flow = _make_flow(
            [
                _step(step_id="list_products", order=1, endpoint="/products", method=HttpMethod.GET),
            ]
        )
        req = FlowGenerateRequest(max_flows=5, max_steps_per_flow=10)
        flows, injected, skip_reasons = _inject_login_prepend([flow], parsed_api, req)

        self.assertEqual(injected, 0)
        self.assertEqual(len(flows[0].steps), 1)

    def test_skips_when_no_login_endpoint_in_spec(self) -> None:
        parsed_api = _parse(SPEC_WITHOUT_LOGIN)
        flow = _make_flow(
            [
                _step(
                    step_id="add_item",
                    order=1,
                    endpoint="/cart/items",
                    method=HttpMethod.POST,
                    headers={"Authorization": "Bearer {{ctx.auth_token}}"},
                ),
            ]
        )
        req = FlowGenerateRequest(max_flows=5, max_steps_per_flow=10)
        flows, injected, skip_reasons = _inject_login_prepend([flow], parsed_api, req)

        self.assertEqual(injected, 0)
        self.assertIn("flow-1", skip_reasons)
        self.assertEqual(skip_reasons["flow-1"], "no_login_endpoint_in_spec")
        self.assertEqual(len(flows[0].steps), 1)

    def test_skips_when_flow_at_max_steps(self) -> None:
        # Spec has both register + login (prepend_count=2). With a 1-step flow and
        # max_steps_per_flow=2, prepending would exceed the cap, so we expect a skip.
        parsed_api = _parse(SPEC_WITH_LOGIN_AND_REGISTER)
        flow = _make_flow(
            [
                _step(
                    step_id="add_item",
                    order=1,
                    endpoint="/cart/items",
                    method=HttpMethod.POST,
                    headers={"Authorization": "Bearer {{ctx.auth_token}}"},
                ),
            ]
        )
        req = FlowGenerateRequest(max_flows=5, max_steps_per_flow=2)
        flows, injected, skip_reasons = _inject_login_prepend([flow], parsed_api, req)

        self.assertEqual(injected, 0)
        self.assertEqual(skip_reasons.get("flow-1"), "max_steps_exceeded")
        self.assertEqual(len(flows[0].steps), 1)

    def test_register_prepended_when_login_present_but_register_missing(self) -> None:
        # LLM-style flow: login step with invented credentials and no register
        # step. We should prepend a register that reuses the login body so the
        # invented user actually exists on the server before login runs.
        parsed_api = _parse(SPEC_WITH_LOGIN_AND_REGISTER)
        invented_body = {
            "email": "newuser-21580d44@example.com",
            "password": "Password123!",
        }
        flow = _make_flow(
            [
                _step(
                    step_id="llm_login",
                    order=1,
                    endpoint="/auth/login",
                    method=HttpMethod.POST,
                    body=invented_body,
                    extract=[
                        FlowExtractRule(var="auth_token", source="body", path="access_token"),
                    ],
                ),
                _step(
                    step_id="add_item",
                    order=2,
                    endpoint="/cart/items",
                    method=HttpMethod.POST,
                    headers={"Authorization": "Bearer {{ctx.auth_token}}"},
                ),
            ]
        )
        req = FlowGenerateRequest(max_flows=5, max_steps_per_flow=10)
        flows, injected, skip_reasons = _inject_login_prepend([flow], parsed_api, req)

        self.assertEqual(injected, 1)
        self.assertEqual(skip_reasons, {})
        steps = flows[0].steps
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0].endpoint, "/auth/register")
        self.assertEqual(steps[0].order, 1)
        self.assertEqual(steps[1].endpoint, "/auth/login")
        self.assertEqual(steps[1].step_id, "llm_login")  # LLM's login left intact
        self.assertEqual(steps[1].order, 2)
        self.assertEqual(steps[2].endpoint, "/cart/items")
        self.assertEqual(steps[2].order, 3)

        # Register body must reuse the LLM's login credentials so the user it
        # creates is exactly the one login then tries to authenticate.
        self.assertEqual(steps[0].body.get("email"), invented_body["email"])
        self.assertEqual(steps[0].body.get("password"), invented_body["password"])

    def test_no_changes_when_login_and_register_both_present(self) -> None:
        parsed_api = _parse(SPEC_WITH_LOGIN_AND_REGISTER)
        flow = _make_flow(
            [
                _step(
                    step_id="register",
                    order=1,
                    endpoint="/auth/register",
                    method=HttpMethod.POST,
                    body={"email": "u@example.com", "password": "Passw0rd!"},
                ),
                _step(
                    step_id="login",
                    order=2,
                    endpoint="/auth/login",
                    method=HttpMethod.POST,
                    body={"email": "u@example.com", "password": "Passw0rd!"},
                    extract=[
                        FlowExtractRule(var="auth_token", source="body", path="access_token"),
                    ],
                ),
                _step(
                    step_id="add_item",
                    order=3,
                    endpoint="/cart/items",
                    method=HttpMethod.POST,
                    headers={"Authorization": "Bearer {{ctx.auth_token}}"},
                ),
            ]
        )
        req = FlowGenerateRequest(max_flows=5, max_steps_per_flow=10)
        flows, injected, skip_reasons = _inject_login_prepend([flow], parsed_api, req)

        self.assertEqual(injected, 0)
        self.assertEqual(skip_reasons, {})
        self.assertEqual(len(flows[0].steps), 3)

    def test_login_only_when_register_endpoint_missing(self) -> None:
        parsed_api = _parse(SPEC_LOGIN_ONLY)
        flow = _make_flow(
            [
                _step(
                    step_id="add_item",
                    order=1,
                    endpoint="/cart/items",
                    method=HttpMethod.POST,
                    headers={"Authorization": "Bearer {{ctx.auth_token}}"},
                ),
            ]
        )
        req = FlowGenerateRequest(max_flows=5, max_steps_per_flow=10)
        flows, injected, skip_reasons = _inject_login_prepend([flow], parsed_api, req)

        self.assertEqual(injected, 1)
        self.assertEqual(len(flows[0].steps), 2)
        self.assertEqual(flows[0].steps[0].endpoint, "/auth/login")
        self.assertEqual(flows[0].steps[1].endpoint, "/cart/items")

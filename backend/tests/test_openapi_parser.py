from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from parser.openapi_parser import parse_openapi  # noqa: E402


class OpenAPIParserBaseUrlTests(TestCase):
    def test_prefers_absolute_http_server_url(self) -> None:
        spec = """
openapi: 3.0.0
info:
  title: Demo
  version: "1.0"
servers:
  - url: https://api.example.com/v1
paths:
  /health:
    get:
      responses:
        "200":
          description: ok
"""
        parsed = parse_openapi(spec)
        self.assertEqual(parsed.base_url, "https://api.example.com/v1")

    def test_resolves_relative_server_against_spec_url(self) -> None:
        spec_dict = {
            "openapi": "3.0.0",
            "info": {"title": "Petstore", "version": "1.0"},
            "servers": [{"url": "/api/v3"}],
            "paths": {
                "/pet/findByStatus": {
                    "get": {"responses": {"200": {"description": "ok"}}}
                }
            },
        }
        with patch("parser.openapi_parser.load_spec", return_value=spec_dict):
            parsed = parse_openapi("https://petstore3.swagger.io/api/v3/openapi.json")
        self.assertEqual(parsed.base_url, "https://petstore3.swagger.io/api/v3")

    def test_relative_server_without_url_source_uses_localhost_fallback(self) -> None:
        spec = """
openapi: 3.0.0
info:
  title: Local Demo
  version: "1.0"
servers:
  - url: /api/v3
paths:
  /users:
    get:
      responses:
        "200":
          description: ok
"""
        parsed = parse_openapi(spec)
        self.assertEqual(parsed.base_url, "http://localhost:8080/api/v3")

    def test_expands_server_variables_with_defaults(self) -> None:
        spec = """
openapi: 3.0.0
info:
  title: Var Demo
  version: "1.0"
servers:
  - url: https://{env}.example.com/{version}
    variables:
      env:
        default: staging
      version:
        default: api/v2
paths:
  /users:
    get:
      responses:
        "200":
          description: ok
"""
        parsed = parse_openapi(spec)
        self.assertEqual(parsed.base_url, "https://staging.example.com/api/v2")

    def test_swagger_2_body_and_response_schema_hints_are_extracted(self) -> None:
        spec = """
swagger: "2.0"
info:
  title: Booker
  version: "1.0"
paths:
  /auth:
    post:
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
      responses:
        "200":
          description: ok
          schema:
            type: array
            items:
              $ref: "#/definitions/GetIdsResponse"
definitions:
  AuthParams:
    type: object
    properties:
      username:
        type: string
      password:
        type: string
  AuthResponse:
    type: object
    properties:
      token:
        type: string
  GetIdsResponse:
    type: object
    properties:
      bookingid:
        type: integer
"""
        parsed = parse_openapi(spec)
        auth = next(endpoint for endpoint in parsed.endpoints if endpoint.path == "/auth")
        booking_list = next(endpoint for endpoint in parsed.endpoints if endpoint.path == "/booking")

        self.assertEqual(
            auth.request_body_example,
            {"username": "admin", "password": "password123"},
        )
        self.assertEqual(auth.responses[0].example, {"token": "sample-token"})
        self.assertEqual(booking_list.responses[0].example, [{"bookingid": 1}])

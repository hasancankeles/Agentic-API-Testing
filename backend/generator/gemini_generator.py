from __future__ import annotations

import json
import os
import uuid

from google import genai

from models.schemas import (
    HttpMethod,
    LoadTestScenario,
    ParsedAPI,
    TestAssertion,
    TestCase,
    TestCategory,
    TestSuite,
    WebSocketStep,
    WebSocketTestCase,
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")


def _get_client() -> genai.Client:
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY environment variable is not set")
    return genai.Client(api_key=GEMINI_API_KEY)


def _build_api_context(parsed_api: ParsedAPI) -> str:
    """Build a detailed context string from the parsed API for the LLM prompt."""
    lines = [
        f"API: {parsed_api.title} v{parsed_api.version}",
        f"Base URL: {parsed_api.base_url}",
        "",
        "=== REST ENDPOINTS ===",
    ]

    for ep in parsed_api.endpoints:
        lines.append(f"\n{ep.method.value} {ep.path}")
        lines.append(f"  Summary: {ep.summary}")
        if ep.parameters:
            lines.append("  Parameters:")
            for p in ep.parameters:
                lines.append(
                    f"    - {p.name} ({p.location}, {p.schema_type}, required={p.required}): {p.description}"
                )
        if ep.responses:
            lines.append("  Responses:")
            for r in ep.responses:
                lines.append(f"    {r.status_code}: {r.description}")
                if r.example:
                    lines.append(f"      Example: {json.dumps(r.example, indent=2)}")

    if parsed_api.schemas:
        lines.append("\n=== SCHEMAS ===")
        for name, schema in parsed_api.schemas.items():
            lines.append(f"\n{name}:")
            lines.append(json.dumps(schema, indent=2))

    if parsed_api.websocket_messages:
        lines.append("\n=== WEBSOCKET MESSAGES ===")
        for msg in parsed_api.websocket_messages:
            lines.append(
                f"  {msg.direction}: {msg.type} - fields: {json.dumps(msg.fields)} - {msg.description}"
            )

    return "\n".join(lines)


INDIVIDUAL_TEST_PROMPT = """You are an expert API tester. Given the following API specification, generate comprehensive individual test cases.

{api_context}

Generate test cases covering:
1. **Happy path tests** - Valid requests that should return successful responses
2. **Edge case tests** - Boundary values, empty parameters, special characters
3. **Negative tests** - Invalid inputs, non-existent resources, wrong formats

Return ONLY a valid JSON array of test case objects. Each object must have:
- "name": descriptive test name
- "description": what the test validates
- "endpoint": the API path (e.g., "/api/health")
- "method": HTTP method (e.g., "GET")
- "path_params": object of path parameters (e.g., {{"sessionId": "some-value"}})
- "query_params": object of query parameters
- "headers": object of headers
- "expected_status": expected HTTP status code (integer)
- "assertions": array of assertion objects with "field", "operator", "expected"
  - field: use "status_code", "body.<jsonpath>", "headers.<name>"
  - operator: one of "eq", "ne", "gt", "lt", "contains", "exists", "type"
  - expected: the expected value

Generate at least 10 test cases. Return ONLY the JSON array, no markdown formatting."""


SUITE_TEST_PROMPT = """You are an expert API tester. Given the following API specification, generate test scenario suites that group related tests together.

{api_context}

Generate test suites for:
1. **Health & Monitoring Suite** - Health check endpoint validations
2. **Game Listing Suite** - Games list endpoint with various states
3. **Game Details Suite** - Individual game lookup, including 404 scenarios
4. **Full Integration Suite** - End-to-end flow testing multiple endpoints together
5. **Error Handling Suite** - All error paths grouped

For WebSocket tests, include test flows that:
- Create a game and verify response
- Join a game session
- Complete game flow (create -> join -> start -> answer -> end)

Return ONLY a valid JSON array of suite objects. Each suite has:
- "name": suite name
- "description": what this suite tests
- "category": "suite"
- "test_cases": array of HTTP test case objects (same format: name, description, endpoint, method, path_params, query_params, headers, expected_status, assertions)
- "ws_test_cases": array of WebSocket test objects with:
  - "name": test name
  - "description": what it tests
  - "url": "ws://localhost:8080/"
  - "steps": array of step objects:
    - "action": "send" or "expect"
    - "message": the JSON message object
    - "timeout_seconds": timeout (default 5)
    - "assertions": array of assertion objects

Generate at least 4 suites. Return ONLY the JSON array, no markdown formatting."""


LOAD_TEST_PROMPT = """You are an expert performance tester. Given the following API specification, generate load test scenarios for k6.

{api_context}

Generate load test scenarios:
1. **Sustained Load** - Constant 50 virtual users hitting /api/health for 2 minutes
2. **Ramp-Up Load** - Gradually increase from 0 to 100 to 200 VUs over 5 minutes on /api/games
3. **Spike Test** - Normal load with a sudden burst of 500 VUs for 30 seconds
4. **Stress Test** - Gradually increase VUs until the server shows degradation
5. **Endpoint Mix** - Realistic traffic pattern hitting all endpoints with weighted distribution

Return ONLY a valid JSON array of scenario objects with:
- "name": scenario name
- "description": what it tests
- "target_url": full URL to test (e.g., "http://localhost:8080/api/health")
- "method": HTTP method
- "vus": number of virtual users (for constant load)
- "duration": duration string (e.g., "2m", "5m")
- "ramp_stages": array of {{"duration": "30s", "target": 100}} objects (for ramp-up tests)
- "thresholds": object like {{"http_req_duration": ["p(95)<500", "p(99)<1000"], "http_req_failed": ["rate<0.01"]}}
- "headers": object of HTTP headers

Generate at least 5 scenarios. Return ONLY the JSON array, no markdown formatting."""


def _parse_test_cases_response(raw: str) -> list[dict]:
    """Parse the LLM response, stripping markdown code fences if present."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    return json.loads(text)


def _dict_to_test_case(d: dict) -> TestCase:
    assertions = []
    for a in d.get("assertions", []):
        assertions.append(
            TestAssertion(
                field=a.get("field", "status_code"),
                operator=a.get("operator", "eq"),
                expected=a.get("expected"),
            )
        )
    return TestCase(
        id=str(uuid.uuid4()),
        name=d.get("name", "Unnamed Test"),
        description=d.get("description", ""),
        endpoint=d.get("endpoint", ""),
        method=HttpMethod(d.get("method", "GET").upper()),
        headers=d.get("headers", {}),
        query_params=d.get("query_params", {}),
        path_params=d.get("path_params", {}),
        body=d.get("body"),
        expected_status=d.get("expected_status", 200),
        assertions=assertions,
        category=TestCategory(d.get("category", "individual")),
    )


def _dict_to_ws_test(d: dict) -> WebSocketTestCase:
    steps = []
    for s in d.get("steps", []):
        step_assertions = []
        for a in s.get("assertions", []):
            step_assertions.append(
                TestAssertion(
                    field=a.get("field", "type"),
                    operator=a.get("operator", "eq"),
                    expected=a.get("expected"),
                )
            )
        steps.append(
            WebSocketStep(
                action=s.get("action", "send"),
                message=s.get("message", {}),
                timeout_seconds=s.get("timeout_seconds", 5.0),
                assertions=step_assertions,
            )
        )
    return WebSocketTestCase(
        id=str(uuid.uuid4()),
        name=d.get("name", "Unnamed WS Test"),
        description=d.get("description", ""),
        url=d.get("url", "ws://localhost:8080/"),
        steps=steps,
        category=TestCategory.SUITE,
    )


async def generate_individual_tests(parsed_api: ParsedAPI) -> TestSuite:
    client = _get_client()

    api_context = _build_api_context(parsed_api)
    prompt = INDIVIDUAL_TEST_PROMPT.format(api_context=api_context)

    response = await client.aio.models.generate_content(
        model="gemini-3-flash-preview", contents=prompt
    )
    raw = response.text
    test_dicts = _parse_test_cases_response(raw)

    test_cases = []
    for d in test_dicts:
        d["category"] = "individual"
        test_cases.append(_dict_to_test_case(d))

    return TestSuite(
        id=str(uuid.uuid4()),
        name="Individual Test Cases",
        description="AI-generated individual test cases covering happy paths, edge cases, and negative tests",
        category=TestCategory.INDIVIDUAL,
        test_cases=test_cases,
    )


async def generate_test_suites(parsed_api: ParsedAPI) -> list[TestSuite]:
    client = _get_client()

    api_context = _build_api_context(parsed_api)
    prompt = SUITE_TEST_PROMPT.format(api_context=api_context)

    response = await client.aio.models.generate_content(
        model="gemini-3-flash-preview", contents=prompt
    )
    raw = response.text
    suite_dicts = _parse_test_cases_response(raw)

    suites = []
    for sd in suite_dicts:
        test_cases = [_dict_to_test_case({**d, "category": "suite"}) for d in sd.get("test_cases", [])]
        ws_tests = [_dict_to_ws_test(d) for d in sd.get("ws_test_cases", [])]
        suites.append(
            TestSuite(
                id=str(uuid.uuid4()),
                name=sd.get("name", "Unnamed Suite"),
                description=sd.get("description", ""),
                category=TestCategory.SUITE,
                test_cases=test_cases,
                ws_test_cases=ws_tests,
            )
        )

    return suites


async def generate_load_test_scenarios(parsed_api: ParsedAPI) -> list[LoadTestScenario]:
    client = _get_client()

    api_context = _build_api_context(parsed_api)
    prompt = LOAD_TEST_PROMPT.format(api_context=api_context)

    response = await client.aio.models.generate_content(
        model="gemini-3-flash-preview", contents=prompt
    )
    raw = response.text
    scenario_dicts = _parse_test_cases_response(raw)

    scenarios = []
    for sd in scenario_dicts:
        scenarios.append(
            LoadTestScenario(
                id=str(uuid.uuid4()),
                name=sd.get("name", "Unnamed Scenario"),
                description=sd.get("description", ""),
                target_url=sd.get("target_url", "http://localhost:8080/api/health"),
                method=HttpMethod(sd.get("method", "GET").upper()),
                vus=sd.get("vus", 10),
                duration=sd.get("duration", "30s"),
                ramp_stages=sd.get("ramp_stages", []),
                thresholds=sd.get("thresholds", {}),
                headers=sd.get("headers", {}),
            )
        )

    return scenarios


async def generate_all(parsed_api: ParsedAPI, categories: list[TestCategory] | None = None):
    """Generate all test categories. Returns (suites, load_scenarios)."""
    if categories is None:
        categories = [TestCategory.INDIVIDUAL, TestCategory.SUITE, TestCategory.LOAD]

    suites: list[TestSuite] = []
    load_scenarios: list[LoadTestScenario] = []

    if TestCategory.INDIVIDUAL in categories:
        individual_suite = await generate_individual_tests(parsed_api)
        suites.append(individual_suite)

    if TestCategory.SUITE in categories:
        scenario_suites = await generate_test_suites(parsed_api)
        suites.extend(scenario_suites)

    if TestCategory.LOAD in categories:
        load_scenarios = await generate_load_test_scenarios(parsed_api)

    return suites, load_scenarios

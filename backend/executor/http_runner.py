from __future__ import annotations

import json
import time
import uuid
from typing import Any

import requests as http_requests

from models.schemas import TestAssertion, TestCase, TestResult, TestCategory, TestStatus


def _resolve_url(base_url: str, endpoint: str, path_params: dict[str, str]) -> str:
    url = endpoint
    for key, value in path_params.items():
        url = url.replace(f"{{{key}}}", str(value))
    return f"{base_url.rstrip('/')}{url}"


def _evaluate_assertion(assertion: TestAssertion, status_code: int, body: Any, headers: dict) -> bool:
    """Evaluate a single assertion against the response data."""
    field = assertion.field
    expected = assertion.expected
    operator = assertion.operator

    if field == "status_code":
        actual = status_code
    elif field.startswith("body."):
        path = field[5:].split(".")
        actual = body
        for key in path:
            if isinstance(actual, dict):
                actual = actual.get(key)
            elif isinstance(actual, list) and key.isdigit():
                actual = actual[int(key)] if int(key) < len(actual) else None
            else:
                actual = None
                break
    elif field.startswith("headers."):
        header_name = field[8:]
        actual = headers.get(header_name, headers.get(header_name.lower()))
    elif field == "body":
        actual = body
    else:
        actual = None

    if operator == "eq":
        return actual == expected
    elif operator == "ne":
        return actual != expected
    elif operator == "gt":
        return actual is not None and actual > expected
    elif operator == "lt":
        return actual is not None and actual < expected
    elif operator == "gte":
        return actual is not None and actual >= expected
    elif operator == "lte":
        return actual is not None and actual <= expected
    elif operator == "contains":
        if isinstance(actual, str):
            return str(expected) in actual
        if isinstance(actual, (list, dict)):
            return expected in actual
        return False
    elif operator == "exists":
        return actual is not None if expected else actual is None
    elif operator == "type":
        type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
        expected_type = type_map.get(str(expected).lower())
        return isinstance(actual, expected_type) if expected_type else False
    return False


def _build_expected_body(test_case: TestCase) -> dict | None:
    """Build expected body from assertions for comparison display."""
    expected = {}
    for a in test_case.assertions:
        if a.field.startswith("body."):
            path = a.field[5:]
            expected[path] = a.expected
        elif a.field == "body":
            return a.expected
    return expected if expected else None


def run_single_test(test_case: TestCase, base_url: str) -> TestResult:
    """Execute a single HTTP test case and return the result."""
    url = _resolve_url(base_url, test_case.endpoint, test_case.path_params)

    result_id = str(uuid.uuid4())
    expected_body = _build_expected_body(test_case)

    try:
        start = time.perf_counter()
        response = http_requests.request(
            method=test_case.method.value,
            url=url,
            headers=test_case.headers or None,
            params=test_case.query_params or None,
            json=test_case.body if test_case.body else None,
            timeout=30,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000

        try:
            actual_body = response.json()
        except (json.JSONDecodeError, ValueError):
            actual_body = response.text

        response_headers = dict(response.headers)

        assertions_passed = 0
        assertions_total = len(test_case.assertions)
        for a in test_case.assertions:
            if _evaluate_assertion(a, response.status_code, actual_body, response_headers):
                assertions_passed += 1

        status_match = response.status_code == test_case.expected_status
        all_assertions = assertions_passed == assertions_total

        test_status = TestStatus.PASSED if (status_match and all_assertions) else TestStatus.FAILED

        return TestResult(
            id=result_id,
            test_case_id=test_case.id,
            test_case_name=test_case.name,
            endpoint=test_case.endpoint,
            method=test_case.method.value,
            category=test_case.category,
            status=test_status,
            expected_status=test_case.expected_status,
            actual_status=response.status_code,
            expected_body=expected_body,
            actual_body=actual_body,
            response_time_ms=round(elapsed_ms, 2),
            assertions_passed=assertions_passed,
            assertions_total=assertions_total,
        )

    except http_requests.exceptions.ConnectionError as e:
        return TestResult(
            id=result_id,
            test_case_id=test_case.id,
            test_case_name=test_case.name,
            endpoint=test_case.endpoint,
            method=test_case.method.value,
            category=test_case.category,
            status=TestStatus.ERROR,
            expected_status=test_case.expected_status,
            expected_body=expected_body,
            error_message=f"Connection error: {e}",
        )
    except http_requests.exceptions.Timeout:
        return TestResult(
            id=result_id,
            test_case_id=test_case.id,
            test_case_name=test_case.name,
            endpoint=test_case.endpoint,
            method=test_case.method.value,
            category=test_case.category,
            status=TestStatus.ERROR,
            expected_status=test_case.expected_status,
            expected_body=expected_body,
            error_message="Request timed out after 30s",
        )
    except Exception as e:
        return TestResult(
            id=result_id,
            test_case_id=test_case.id,
            test_case_name=test_case.name,
            endpoint=test_case.endpoint,
            method=test_case.method.value,
            category=test_case.category,
            status=TestStatus.ERROR,
            expected_status=test_case.expected_status,
            expected_body=expected_body,
            error_message=str(e),
        )


def run_test_cases(test_cases: list[TestCase], base_url: str) -> list[TestResult]:
    """Execute a list of test cases sequentially."""
    return [run_single_test(tc, base_url) for tc in test_cases]

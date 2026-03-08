from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import websockets

from models.schemas import TestAssertion, TestResult, TestCategory, TestStatus, WebSocketTestCase


def _evaluate_ws_assertion(assertion: TestAssertion, message: dict) -> bool:
    """Evaluate an assertion against a WebSocket message."""
    field = assertion.field
    expected = assertion.expected
    operator = assertion.operator

    path = field.split(".")
    actual = message
    for key in path:
        if isinstance(actual, dict):
            actual = actual.get(key)
        else:
            actual = None
            break

    if operator == "eq":
        return actual == expected
    elif operator == "ne":
        return actual != expected
    elif operator == "exists":
        return actual is not None if expected else actual is None
    elif operator == "contains":
        return str(expected) in str(actual) if actual is not None else False
    elif operator == "type":
        type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
        expected_type = type_map.get(str(expected).lower())
        return isinstance(actual, expected_type) if expected_type else False
    return False


async def run_ws_test(test_case: WebSocketTestCase) -> TestResult:
    """Execute a WebSocket test case by running through its steps."""
    result_id = str(uuid.uuid4())
    assertions_passed = 0
    assertions_total = 0
    step_results: list[dict[str, Any]] = []
    expected_messages: list[dict] = []
    actual_messages: list[dict] = []

    start = time.perf_counter()

    try:
        async with websockets.connect(test_case.url) as ws:
            for step in test_case.steps:
                if step.action == "send":
                    await ws.send(json.dumps(step.message))
                    step_results.append({"action": "send", "message": step.message, "status": "ok"})

                elif step.action == "expect":
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=step.timeout_seconds)
                        received = json.loads(raw)
                        actual_messages.append(received)
                        expected_messages.append(step.message)

                        step_ok = True
                        for a in step.assertions:
                            assertions_total += 1
                            if _evaluate_ws_assertion(a, received):
                                assertions_passed += 1
                            else:
                                step_ok = False

                        step_results.append({
                            "action": "expect",
                            "expected": step.message,
                            "actual": received,
                            "passed": step_ok,
                        })

                    except asyncio.TimeoutError:
                        assertions_total += len(step.assertions)
                        step_results.append({
                            "action": "expect",
                            "expected": step.message,
                            "actual": None,
                            "passed": False,
                            "error": f"Timed out after {step.timeout_seconds}s",
                        })

        elapsed_ms = (time.perf_counter() - start) * 1000
        all_passed = assertions_passed == assertions_total and assertions_total > 0
        status = TestStatus.PASSED if all_passed else TestStatus.FAILED

        return TestResult(
            id=result_id,
            test_case_id=test_case.id,
            test_case_name=test_case.name,
            endpoint=test_case.url,
            method="WS",
            category=TestCategory.SUITE,
            status=status,
            expected_status=0,
            actual_status=0,
            expected_body={"steps": [s.model_dump() for s in test_case.steps]},
            actual_body={"step_results": step_results},
            response_time_ms=round(elapsed_ms, 2),
            assertions_passed=assertions_passed,
            assertions_total=assertions_total,
        )

    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return TestResult(
            id=result_id,
            test_case_id=test_case.id,
            test_case_name=test_case.name,
            endpoint=test_case.url,
            method="WS",
            category=TestCategory.SUITE,
            status=TestStatus.ERROR,
            expected_status=0,
            expected_body={"steps": [s.model_dump() for s in test_case.steps]},
            response_time_ms=round(elapsed_ms, 2),
            assertions_passed=assertions_passed,
            assertions_total=assertions_total,
            error_message=str(e),
        )


async def run_ws_tests(test_cases: list[WebSocketTestCase]) -> list[TestResult]:
    """Run multiple WebSocket test cases sequentially (they share connections)."""
    results = []
    for tc in test_cases:
        result = await run_ws_test(tc)
        results.append(result)
    return results

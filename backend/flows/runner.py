from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime
from typing import Any

import requests as http_requests

from models.schemas import (
    FlowExtractRule,
    FlowExtractSource,
    FlowRunRecord,
    FlowRunStatus,
    FlowScenario,
    FlowStep,
    FlowStepResult,
    TestAssertion,
    TestStatus,
)

_CONTEXT_TEMPLATE = re.compile(r"\{\{\s*ctx\.([a-zA-Z0-9_.-]+)\s*\}\}")
_CONTEXT_TEMPLATE_FULL = re.compile(r"^\s*\{\{\s*ctx\.([a-zA-Z0-9_.-]+)\s*\}\}\s*$")
_PATH_PARAM_TEMPLATE = re.compile(r"\{([^{}]+)\}")


def _context_get(ctx: dict[str, Any], key: str) -> Any:
    current: Any = ctx
    for part in key.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise KeyError(f"Missing context key: {key}")
    return current


def resolve_template_string(value: str, ctx: dict[str, Any]) -> Any:
    full_match = _CONTEXT_TEMPLATE_FULL.match(value)
    if full_match:
        return _context_get(ctx, full_match.group(1))

    def _replace(match: re.Match[str]) -> str:
        resolved = _context_get(ctx, match.group(1))
        return str(resolved)

    return _CONTEXT_TEMPLATE.sub(_replace, value)


def render_templates(value: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return resolve_template_string(value, ctx)
    if isinstance(value, dict):
        return {str(k): render_templates(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render_templates(item, ctx) for item in value]
    return value


def _extract_from_body(body: Any, path: str) -> Any:
    if not path:
        return body

    if path.startswith("$response.body#/"):
        pointer_parts = [part for part in path[len("$response.body#/") :].split("/") if part]
        current = body
        for part in pointer_parts:
            key = part.replace("~1", "/").replace("~0", "~")
            if isinstance(current, list) and key.isdigit():
                index = int(key)
                if index >= len(current):
                    return None
                current = current[index]
            elif isinstance(current, dict):
                current = current.get(key)
            else:
                return None
        return current

    current = body
    parts = [part for part in path.split(".") if part]
    for part in parts:
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _extract_rule_value(rule: FlowExtractRule, status_code: int, headers: dict[str, Any], body: Any) -> Any:
    if rule.source == FlowExtractSource.STATUS_CODE:
        return status_code

    if rule.source == FlowExtractSource.HEADERS:
        path = rule.path or ""
        header_key = path
        if header_key.startswith("$response.header."):
            header_key = header_key[len("$response.header.") :]
        lowered_headers = {str(k).lower(): v for k, v in headers.items()}
        return lowered_headers.get(header_key.lower())

    return _extract_from_body(body, rule.path)


def _evaluate_assertion(assertion: TestAssertion, status_code: int, body: Any, headers: dict[str, Any]) -> bool:
    field = assertion.field
    expected = assertion.expected
    operator = assertion.operator

    if field == "status_code":
        actual = status_code
    elif field.startswith("body."):
        actual = _extract_from_body(body, field[5:])
    elif field == "body":
        actual = body
    elif field.startswith("headers."):
        actual = headers.get(field[8:], headers.get(field[8:].lower()))
    else:
        actual = None

    if operator == "eq":
        return actual == expected
    if operator == "ne":
        return actual != expected
    if operator == "gt":
        return actual is not None and actual > expected
    if operator == "lt":
        return actual is not None and actual < expected
    if operator == "gte":
        return actual is not None and actual >= expected
    if operator == "lte":
        return actual is not None and actual <= expected
    if operator == "contains":
        if isinstance(actual, str):
            return str(expected) in actual
        if isinstance(actual, (list, dict)):
            return expected in actual
        return False
    if operator == "exists":
        return actual is not None if expected else actual is None
    if operator == "type":
        type_map = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        expected_type = type_map.get(str(expected).lower())
        return isinstance(actual, expected_type) if expected_type else False
    return False


def _resolve_endpoint(endpoint: str, path_params: dict[str, Any]) -> str:
    resolved = endpoint
    for key, value in path_params.items():
        resolved = resolved.replace(f"{{{key}}}", str(value))

    unresolved = _PATH_PARAM_TEMPLATE.findall(resolved)
    if unresolved:
        unresolved_list = ", ".join(unresolved)
        raise KeyError(f"Missing path params for endpoint {endpoint}: {unresolved_list}")
    return resolved


def _parse_response_body(response: http_requests.Response) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError):
        return response.text


def _build_url(base_url: str, endpoint: str) -> str:
    return f"{base_url.rstrip('/')}{endpoint}"


def _execute_step(step: FlowStep, flow_id: str, flow_run_id: str, base_url: str, ctx: dict[str, Any]) -> FlowStepResult:
    executed_at = datetime.utcnow()
    resolved_request: dict[str, Any] = {}

    try:
        resolved_headers = render_templates(step.headers, ctx)
        resolved_query = render_templates(step.query_params, ctx)
        resolved_path_params = render_templates(step.path_params, ctx)
        resolved_body = render_templates(step.body, ctx)
        resolved_endpoint = resolve_template_string(step.endpoint, ctx)
        if not isinstance(resolved_endpoint, str):
            raise ValueError("Resolved endpoint must be a string")

        endpoint_with_params = _resolve_endpoint(resolved_endpoint, resolved_path_params)
        url = _build_url(base_url, endpoint_with_params)

        resolved_request = {
            "url": url,
            "endpoint": endpoint_with_params,
            "method": step.method.value,
            "headers": resolved_headers,
            "query_params": resolved_query,
            "path_params": resolved_path_params,
            "body": resolved_body,
        }

        start = time.perf_counter()
        response = http_requests.request(
            method=step.method.value,
            url=url,
            headers=resolved_headers or None,
            params=resolved_query or None,
            json=resolved_body if resolved_body is not None else None,
            timeout=30,
        )
        _elapsed_ms = round((time.perf_counter() - start) * 1000, 2)

        response_body = _parse_response_body(response)
        response_headers = dict(response.headers)

        assertions_total = len(step.assertions) + (1 if step.expected_status is not None else 0)
        assertions_passed = 0

        if step.expected_status is None or response.status_code == step.expected_status:
            if step.expected_status is not None:
                assertions_passed += 1
        
        for assertion in step.assertions:
            if _evaluate_assertion(assertion, response.status_code, response_body, response_headers):
                assertions_passed += 1

        extracted_delta: dict[str, Any] = {}
        extraction_errors: list[str] = []
        for rule in step.extract:
            extracted = _extract_rule_value(rule, response.status_code, response_headers, response_body)
            if extracted is None and rule.required:
                extraction_errors.append(f"Missing required extraction for '{rule.var}' from {rule.source.value}:{rule.path}")
                continue
            if extracted is not None:
                extracted_delta[rule.var] = extracted

        all_passed = assertions_passed == assertions_total and not extraction_errors
        step_status = TestStatus.PASSED if all_passed else TestStatus.FAILED
        error_message = "; ".join(extraction_errors) if extraction_errors else None

        if step_status == TestStatus.PASSED:
            ctx.update(extracted_delta)

        return FlowStepResult(
            id=str(uuid.uuid4()),
            flow_run_id=flow_run_id,
            flow_id=flow_id,
            step_id=step.step_id,
            order=step.order,
            status=step_status,
            resolved_request=resolved_request,
            response_status=response.status_code,
            response_headers=response_headers,
            response_body=response_body,
            assertions_passed=assertions_passed,
            assertions_total=assertions_total,
            extracted_context_delta=extracted_delta if step_status == TestStatus.PASSED else {},
            error_message=error_message,
            executed_at=executed_at,
        )
    except http_requests.exceptions.Timeout:
        return FlowStepResult(
            id=str(uuid.uuid4()),
            flow_run_id=flow_run_id,
            flow_id=flow_id,
            step_id=step.step_id,
            order=step.order,
            status=TestStatus.ERROR,
            resolved_request=resolved_request,
            error_message="Request timed out after 30s",
            executed_at=executed_at,
        )
    except http_requests.exceptions.ConnectionError as exc:
        return FlowStepResult(
            id=str(uuid.uuid4()),
            flow_run_id=flow_run_id,
            flow_id=flow_id,
            step_id=step.step_id,
            order=step.order,
            status=TestStatus.ERROR,
            resolved_request=resolved_request,
            error_message=f"Connection error: {exc}",
            executed_at=executed_at,
        )
    except Exception as exc:
        return FlowStepResult(
            id=str(uuid.uuid4()),
            flow_run_id=flow_run_id,
            flow_id=flow_id,
            step_id=step.step_id,
            order=step.order,
            status=TestStatus.ERROR,
            resolved_request=resolved_request,
            error_message=str(exc),
            executed_at=executed_at,
        )


def run_flow_scenario(
    flow: FlowScenario,
    target_base_url: str,
    initial_context: dict[str, Any] | None = None,
) -> FlowRunRecord:
    flow_run_id = str(uuid.uuid4())
    started_at = datetime.utcnow()

    ctx: dict[str, Any] = {
        "run_id": flow_run_id,
        "timestamp": started_at.isoformat(),
    }
    if initial_context:
        ctx.update(initial_context)

    step_results: list[FlowStepResult] = []
    status = FlowRunStatus.PASSED

    for step in sorted(flow.steps, key=lambda item: item.order):
        step_result = _execute_step(step, flow.id, flow_run_id, target_base_url, ctx)
        step_results.append(step_result)

        if step_result.status in {TestStatus.FAILED, TestStatus.ERROR} and step.required:
            status = FlowRunStatus.FAILED if step_result.status == TestStatus.FAILED else FlowRunStatus.ERROR
            break

    finished_at = datetime.utcnow()

    return FlowRunRecord(
        id=flow_run_id,
        flow_id=flow.id,
        flow_name=flow.name,
        status=status,
        target_base_url=target_base_url,
        initial_context=initial_context or {},
        final_context=ctx,
        started_at=started_at,
        finished_at=finished_at,
        step_results=step_results,
    )


__all__ = ["resolve_template_string", "render_templates", "run_flow_scenario"]

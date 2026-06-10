from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError

from llm.client import OPENROUTER_DEFAULT_MODEL, complete_text, get_client

from models.schemas import (
    FlowEliminatedCandidate,
    FlowExtractRule,
    FlowGenerateRequest,
    FlowGenerationMode,
    FlowMutationPolicy,
    FlowScenario,
    FlowStep,
    HttpMethod,
    ParsedAPI,
    ParsedEndpoint,
    TestAssertion,
)

logger = logging.getLogger("agentic.flow_generator")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
FLOW_PLANNER_MODEL = os.getenv("FLOW_PLANNER_MODEL", OPENROUTER_DEFAULT_MODEL)
FLOW_COMPOSER_MODEL = os.getenv("FLOW_COMPOSER_MODEL", FLOW_PLANNER_MODEL)
FLOW_CRITIC_MODEL = os.getenv("FLOW_CRITIC_MODEL", FLOW_PLANNER_MODEL)
FLOW_REVIEWER_MODEL = os.getenv("FLOW_REVIEWER_MODEL", FLOW_CRITIC_MODEL)

_TEMPLATE_VAR_PATTERN = re.compile(r"\{\{\s*ctx\.([a-zA-Z0-9_.-]+)\s*\}\}")
_FULL_CTX_TEMPLATE_PATTERN = re.compile(r"^\{\{\s*ctx\.([a-zA-Z0-9_.-]+)\s*\}\}$")
_PATH_PARAM_PATTERN = re.compile(r"\{([^{}]+)\}")
_AUTH_KEYWORDS = {"login", "signin", "auth", "token", "session", "oauth"}
_INTERACTION_KEYWORDS = {"like", "comment", "vote", "react", "follow", "share"}
_TRANSACTIONAL_KEYWORDS = {"order", "checkout", "cart", "payment", "purchase", "invoice", "booking"}
_SEARCH_KEYWORDS = {"search", "find", "list", "browse", "filter"}
_AUTH_CONTEXT_VARS = {"auth_token", "access_token", "refresh_token", "api_key"}
_DEFAULT_EXTERNAL_CTX_VARS = {"run_id", "unique_id", "timestamp", *_AUTH_CONTEXT_VARS}


class FlowGeneratorError(Exception):
    pass


@dataclass(frozen=True)
class _EndpointIOMeta:
    key: str
    endpoint: ParsedEndpoint
    resource: str
    consumed_vars: set[str]
    produced_vars: set[str]
    is_auth: bool
    is_mutating: bool


@dataclass(frozen=True)
class _DependencyEdge:
    source: str
    target: str
    vars: tuple[str, ...]
    priority: str
    reason: str


class _FlowReviewDecision(BaseModel):
    candidate_id: str
    keep: bool
    reason_code: str = "accepted"
    reason: str = ""


class _FlowReviewEnvelope(BaseModel):
    decisions: list[_FlowReviewDecision] = Field(default_factory=list)


def _get_api_key() -> str:
    return (os.getenv("OPENROUTER_API_KEY") or OPENROUTER_API_KEY).strip()


def _default_expected_status(method: HttpMethod) -> int:
    if method == HttpMethod.POST:
        return 201
    if method == HttpMethod.DELETE:
        return 204
    return 200


def _choose_expected_status(endpoint: ParsedEndpoint) -> int:
    candidates = {str(response.status_code) for response in endpoint.responses}
    preferred = []
    if endpoint.method == HttpMethod.POST:
        preferred = ["201", "200"]
    elif endpoint.method == HttpMethod.DELETE:
        preferred = ["204", "200"]
    else:
        preferred = ["200", "201", "204"]

    for status in preferred:
        if status in candidates:
            return int(status)

    for status in sorted(candidates):
        if status.isdigit() and status.startswith("2"):
            return int(status)

    return _default_expected_status(endpoint.method)


def _strip_code_fences(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _parse_json_response(raw: str) -> dict:
    text = _strip_code_fences(raw)
    if not text:
        raise FlowGeneratorError("Flow planner returned empty output")

    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise FlowGeneratorError("Flow planner output must be a JSON object")
        return parsed
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidate = text[start : end + 1]
            parsed = json.loads(candidate)
            if not isinstance(parsed, dict):
                raise FlowGeneratorError("Flow planner output must be a JSON object")
            return parsed
        raise


def _normalize_json_path_like(path: object) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    if raw == "$":
        return ""
    if raw.startswith("$."):
        raw = raw[2:]
    elif raw.startswith("$"):
        raw = raw[1:]
    raw = raw.lstrip(".")
    raw = re.sub(r"\[(\d+)\]", r".\1", raw)
    raw = raw.lstrip(".")
    if raw.startswith("body."):
        raw = raw[5:]
    if raw.startswith("headers."):
        raw = raw[8:]
    return raw


def _sanitize_ctx_var_name(value: str, fallback: str = "value") -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip()).strip("_").lower()
    if not normalized:
        return fallback
    if normalized[0].isdigit():
        return f"v_{normalized}"
    return normalized


def _normalize_extract_source(value: object) -> str:
    lowered = str(value or "").strip().lower()
    if lowered in {"body", "response_body"}:
        return "body"
    if lowered in {"headers", "header", "response_headers"}:
        return "headers"
    if lowered in {"status_code", "status", "code"}:
        return "status_code"
    return "body"


def _normalize_extract_entry(entry: object) -> tuple[dict | None, int]:
    if not isinstance(entry, dict):
        return None, 0

    source_input = entry.get("from", entry.get("source"))
    source = _normalize_extract_source(source_input)
    path = _normalize_json_path_like(
        entry.get("path", entry.get("json_path", entry.get("jsonPath", "")))
    )
    if source == "status_code":
        path = ""

    var_input = entry.get("var", entry.get("key", entry.get("name", "")))
    var_candidate = str(var_input or "").strip()
    if not var_candidate:
        if source == "status_code":
            var_candidate = "status_code"
        elif path:
            tail = [part for part in path.split(".") if part and not part.isdigit()]
            var_candidate = tail[-1] if tail else "value"
        elif source == "headers":
            var_candidate = "header_value"
        else:
            var_candidate = "value"
    var = _sanitize_ctx_var_name(var_candidate)

    required_raw = entry.get("required", True)
    if isinstance(required_raw, str):
        required = required_raw.strip().lower() in {"1", "true", "yes", "on"}
    else:
        required = bool(required_raw)

    normalized = {
        "var": var,
        "from": source,
        "path": path,
        "required": required,
    }

    legacy_keys = {"key", "json_path", "jsonPath", "source", "name"}
    used_legacy = any(key in entry for key in legacy_keys)
    changed = 1 if used_legacy else 0
    if (
        entry.get("var") != normalized["var"]
        or entry.get("from") != normalized["from"]
        or entry.get("path", "") != normalized["path"]
        or bool(entry.get("required", True)) != normalized["required"]
    ):
        changed = 1

    return normalized, changed


def _normalize_path_params_from_endpoint(
    parsed_api: ParsedAPI,
    method_value: object,
    endpoint_value: object,
    path_params_value: object,
) -> tuple[object, object, int]:
    if not isinstance(endpoint_value, str):
        return endpoint_value, path_params_value, 0

    endpoint = endpoint_value.strip()
    if "{{ctx." not in endpoint:
        return endpoint_value, path_params_value, 0

    try:
        method = HttpMethod(str(method_value).upper())
    except Exception:
        method = HttpMethod.GET

    raw_path = endpoint
    if raw_path.startswith(("http://", "https://")):
        parsed = urlparse(raw_path)
        raw_path = parsed.path or "/"
    if not raw_path.startswith("/"):
        raw_path = f"/{raw_path}"

    raw_parts = [part for part in raw_path.split("/") if part]
    candidates = [
        candidate
        for candidate in parsed_api.endpoints
        if candidate.method == method
        and len([part for part in candidate.path.split("/") if part]) == len(raw_parts)
    ]

    best_match: tuple[ParsedEndpoint, dict[str, object]] | None = None
    best_score = -1
    for candidate in candidates:
        candidate_parts = [part for part in candidate.path.split("/") if part]
        mapped_path_params: dict[str, object] = {}
        score = 0
        valid = True
        for raw_part, candidate_part in zip(raw_parts, candidate_parts, strict=False):
            if raw_part == candidate_part:
                score += 2
                continue
            if candidate_part.startswith("{") and candidate_part.endswith("}"):
                param_name = candidate_part[1:-1]
                full_ctx_match = _FULL_CTX_TEMPLATE_PATTERN.match(raw_part)
                if full_ctx_match:
                    mapped_path_params[param_name] = raw_part
                    score += 3
                    continue
                if raw_part:
                    mapped_path_params[param_name] = raw_part
                    score += 1
                    continue
            valid = False
            break
        if valid and score > best_score and mapped_path_params:
            best_score = score
            best_match = (candidate, mapped_path_params)

    if best_match is None:
        return endpoint_value, path_params_value, 0

    candidate, mapped_path_params = best_match
    normalized_path_params = (
        dict(path_params_value) if isinstance(path_params_value, dict) else {}
    )
    changed = 1 if candidate.path != endpoint_value else 0
    for key, value in mapped_path_params.items():
        if normalized_path_params.get(key) != value:
            normalized_path_params[key] = value
            changed = 1

    return candidate.path, normalized_path_params, changed


def _coerce_http_method(value: object) -> HttpMethod | None:
    try:
        return value if isinstance(value, HttpMethod) else HttpMethod(str(value).upper())
    except ValueError:
        return None


def _endpoint_for_raw_step(parsed_api: ParsedAPI, step: dict) -> ParsedEndpoint | None:
    method = _coerce_http_method(step.get("method"))
    if method is None:
        return None
    endpoint_path = _normalize_path(str(step.get("endpoint") or ""), parsed_api.base_url)
    return _endpoint_lookup(parsed_api).get((method, endpoint_path))


def _normalize_auth_headers_for_raw_step(parsed_api: ParsedAPI, step: dict) -> int:
    endpoint = _endpoint_for_raw_step(parsed_api, step)
    if endpoint is None or not endpoint.requires_auth or _is_auth_endpoint(endpoint):
        return 0

    raw_headers = step.get("headers")
    headers = dict(raw_headers) if isinstance(raw_headers, dict) else {}
    header_names = {str(key).lower() for key in headers}
    changed = 0

    if "authorization" not in header_names:
        headers["Authorization"] = "Bearer {{ctx.auth_token}}"
        changed += 1
    if "cookie" not in header_names:
        headers["Cookie"] = "token={{ctx.auth_token}}"
        changed += 1

    if changed:
        step["headers"] = headers
    return changed


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _normalize_success_status_for_raw_step(parsed_api: ParsedAPI, step: dict) -> int:
    endpoint = _endpoint_for_raw_step(parsed_api, step)
    if endpoint is None:
        return 0

    raw_expected = _int_or_none(step.get("expected_status"))
    if raw_expected is None or not 200 <= raw_expected < 300:
        return 0

    documented_statuses = _endpoint_status_codes(endpoint)
    if not documented_statuses or raw_expected in documented_statuses:
        return 0

    expected_status = _choose_expected_status(endpoint)
    if expected_status == raw_expected or not 200 <= expected_status < 300:
        return 0

    step["expected_status"] = expected_status
    changed = 1

    assertions = step.get("assertions")
    if isinstance(assertions, list):
        normalized_assertions: list[object] = []
        for assertion in assertions:
            if not isinstance(assertion, dict):
                normalized_assertions.append(assertion)
                continue
            normalized_assertion = dict(assertion)
            field = str(normalized_assertion.get("field") or "").strip().lower()
            operator = str(normalized_assertion.get("operator") or "eq").strip().lower()
            if (
                field == "status_code"
                and operator in {"eq", "=", "==", "equals", "equal"}
                and _int_or_none(normalized_assertion.get("expected")) == raw_expected
            ):
                normalized_assertion["expected"] = expected_status
                changed += 1
            normalized_assertions.append(normalized_assertion)
        step["assertions"] = normalized_assertions

    return changed


def _normalize_llm_flow_payload(raw_flow: dict, parsed_api: ParsedAPI | None = None) -> tuple[dict, int]:
    if not isinstance(raw_flow, dict):
        return raw_flow, 0

    normalized_flow = dict(raw_flow)
    normalizations = 0
    raw_steps = raw_flow.get("steps")
    if not isinstance(raw_steps, list):
        return normalized_flow, normalizations

    normalized_steps: list[dict] = []
    for raw_step in raw_steps:
        if not isinstance(raw_step, dict):
            normalized_steps.append(raw_step)
            continue

        step = dict(raw_step)
        if parsed_api is not None:
            normalized_endpoint, normalized_path_params, changed = _normalize_path_params_from_endpoint(
                parsed_api,
                step.get("method"),
                step.get("endpoint"),
                step.get("path_params", {}),
            )
            if changed:
                step["endpoint"] = normalized_endpoint
                step["path_params"] = normalized_path_params
                normalizations += changed
            normalizations += _normalize_auth_headers_for_raw_step(parsed_api, step)
            normalizations += _normalize_success_status_for_raw_step(parsed_api, step)
        raw_extract = raw_step.get("extract")
        if isinstance(raw_extract, dict):
            raw_extract = [raw_extract]
            normalizations += 1

        if isinstance(raw_extract, list):
            normalized_extract: list[dict] = []
            for entry in raw_extract:
                normalized_entry, changed = _normalize_extract_entry(entry)
                if normalized_entry is None:
                    continue
                normalizations += changed
                normalized_extract.append(normalized_entry)
            step["extract"] = normalized_extract
        normalized_steps.append(step)

    normalized_flow["steps"] = normalized_steps
    return normalized_flow, normalizations


def _normalize_path(path: str, base_url: str) -> str:
    raw = (path or "").strip()
    if not raw:
        return "/"
    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        raw = parsed.path or "/"
    if not raw.startswith("/"):
        raw = f"/{raw}"

    base_path = urlparse(base_url).path.rstrip("/")
    if base_path and raw.startswith(base_path):
        trimmed = raw[len(base_path) :] or "/"
        return trimmed if trimmed.startswith("/") else f"/{trimmed}"
    return raw


def _resource_key(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    for part in parts:
        if not part.startswith("{"):
            return part.lower()
    return "resource"


def _singular(name: str) -> str:
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith("s") and len(name) > 1:
        return name[:-1]
    return name


def _ctx_var_for_param(param_name: str, resource: str) -> str:
    lowered = param_name.lower()
    if lowered in {"id", "_id"}:
        return f"{_singular(resource)}_id"
    if lowered.endswith("id"):
        return re.sub(r"[^a-z0-9]+", "_", lowered)
    return f"{_singular(resource)}_{re.sub(r'[^a-z0-9]+', '_', lowered)}"


def _find_path_params(endpoint: ParsedEndpoint) -> list[str]:
    names = []
    for p in endpoint.parameters:
        if p.location == "path" and p.name:
            names.append(p.name)
    return names


def _extract_ctx_vars(value) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        for match in _TEMPLATE_VAR_PATTERN.finditer(value):
            found.add(match.group(1))
        return found
    if isinstance(value, list):
        for item in value:
            found.update(_extract_ctx_vars(item))
        return found
    if isinstance(value, dict):
        for item in value.values():
            found.update(_extract_ctx_vars(item))
        return found
    return found


def _is_auth_endpoint(endpoint: ParsedEndpoint) -> bool:
    combined = " ".join([
        endpoint.path,
        endpoint.summary,
        endpoint.description,
        endpoint.operation_id,
        " ".join(endpoint.tags),
    ]).lower()

    if endpoint.requires_auth and endpoint.method in {HttpMethod.POST, HttpMethod.GET}:
        if any(token in combined for token in _AUTH_KEYWORDS):
            return True

    if any(token in combined for token in _AUTH_KEYWORDS):
        return endpoint.method in {HttpMethod.POST, HttpMethod.GET}

    return False


def _endpoint_text(endpoint: ParsedEndpoint) -> str:
    return " ".join([
        endpoint.path,
        endpoint.summary,
        endpoint.description,
        endpoint.operation_id,
        " ".join(endpoint.tags),
    ]).lower()


def _keyword_in_endpoint(endpoint: ParsedEndpoint, keywords: set[str]) -> bool:
    haystack = _endpoint_text(endpoint)
    return any(keyword in haystack for keyword in keywords)


def _looks_like_collection_get(endpoint: ParsedEndpoint) -> bool:
    return endpoint.method == HttpMethod.GET and "{" not in endpoint.path


def _looks_like_detail_get(endpoint: ParsedEndpoint) -> bool:
    return endpoint.method == HttpMethod.GET and "{" in endpoint.path


def _collect_candidate_vars_from_examples(value) -> set[str]:
    found: set[str] = set()

    def _walk(item):
        if isinstance(item, dict):
            for key, child in item.items():
                lowered = str(key).lower()
                if lowered in {"id", "token", "access_token", "refresh_token", "location"}:
                    found.add(lowered)
                elif lowered.endswith("id"):
                    found.add(re.sub(r"[^a-z0-9]+", "_", lowered))
                _walk(child)
        elif isinstance(item, list):
            for child in item:
                _walk(child)

    _walk(value)
    return found


def _produced_vars(endpoint: ParsedEndpoint, resource: str) -> set[str]:
    produced: set[str] = set()

    if _is_auth_endpoint(endpoint):
        produced.update({"auth_token", "access_token", "refresh_token"})

    if endpoint.method in {HttpMethod.POST, HttpMethod.PUT, HttpMethod.PATCH}:
        produced.add(f"{_singular(resource)}_id")
        produced.add("id")

    if _looks_like_collection_get(endpoint):
        produced.add(f"{_singular(resource)}_id")

    for response in endpoint.responses:
        if response.example is not None:
            produced.update(_collect_candidate_vars_from_examples(response.example))
        for example in (response.examples or {}).values():
            produced.update(_collect_candidate_vars_from_examples(example))

    return produced


def _consumed_vars(endpoint: ParsedEndpoint, resource: str) -> set[str]:
    consumed: set[str] = set()

    for param_name in _find_path_params(endpoint):
        consumed.add(_ctx_var_for_param(param_name, resource))

    if endpoint.requires_auth:
        consumed.add("auth_token")

    for parameter in endpoint.parameters:
        if parameter.location == "header" and parameter.name.lower() in {"authorization", "api_key", "x-api-key"}:
            consumed.add("auth_token")

    for field in endpoint.request_body_required_fields:
        lowered = field.lower()
        if lowered in {"token", "access_token", "refresh_token"}:
            consumed.add(lowered)
        elif lowered.endswith("id"):
            consumed.add(re.sub(r"[^a-z0-9]+", "_", lowered))

    return consumed


def _endpoint_key(endpoint: ParsedEndpoint) -> str:
    return f"{endpoint.method.value} {_normalize_path(endpoint.path, '')}"


def _build_endpoint_io(endpoints: list[ParsedEndpoint]) -> dict[str, _EndpointIOMeta]:
    io_map: dict[str, _EndpointIOMeta] = {}
    for endpoint in endpoints:
        resource = _resource_key(endpoint.path)
        key = _endpoint_key(endpoint)
        io_map[key] = _EndpointIOMeta(
            key=key,
            endpoint=endpoint,
            resource=resource,
            consumed_vars=_consumed_vars(endpoint, resource),
            produced_vars=_produced_vars(endpoint, resource),
            is_auth=_is_auth_endpoint(endpoint),
            is_mutating=endpoint.method in {HttpMethod.POST, HttpMethod.PUT, HttpMethod.PATCH, HttpMethod.DELETE},
        )
    return io_map


def _infer_objectives(parsed_api: ParsedAPI, req: FlowGenerateRequest) -> list[str]:
    explicit = [item.strip() for item in req.objectives if item and item.strip()]
    if explicit:
        unique_explicit: list[str] = []
        seen: set[str] = set()
        for objective in explicit:
            lowered = objective.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            unique_explicit.append(objective)
        return unique_explicit

    endpoints = parsed_api.endpoints
    if not endpoints:
        return ["core api workflow"]

    objectives: list[str] = []

    has_auth = any(endpoint.requires_auth or _is_auth_endpoint(endpoint) for endpoint in endpoints)
    if has_auth:
        objectives.append("authentication and session workflow")

    if any(_keyword_in_endpoint(endpoint, _SEARCH_KEYWORDS) or _looks_like_collection_get(endpoint) for endpoint in endpoints):
        objectives.append("browse and discovery workflow")

    if any(_looks_like_detail_get(endpoint) for endpoint in endpoints):
        objectives.append("detail retrieval workflow")

    if any(_keyword_in_endpoint(endpoint, _INTERACTION_KEYWORDS) for endpoint in endpoints):
        objectives.append("interaction workflow")

    if any(_keyword_in_endpoint(endpoint, _TRANSACTIONAL_KEYWORDS) for endpoint in endpoints):
        objectives.append("transactional lifecycle workflow")

    resources: dict[str, set[HttpMethod]] = {}
    for endpoint in endpoints:
        resources.setdefault(_resource_key(endpoint.path), set()).add(endpoint.method)
    if any({HttpMethod.POST, HttpMethod.GET}.issubset(methods) for methods in resources.values()):
        objectives.append("create and verify workflow")
    if any({HttpMethod.PUT, HttpMethod.PATCH, HttpMethod.GET}.issubset(methods) for methods in resources.values()):
        objectives.append("update and verify workflow")

    if not objectives:
        objectives.append("core api workflow")

    unique_objectives: list[str] = []
    seen: set[str] = set()
    for objective in objectives:
        lowered = objective.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        unique_objectives.append(objective)

    return unique_objectives


def _build_dependency_hints(parsed_api: ParsedAPI, io_map: dict[str, _EndpointIOMeta] | None = None) -> list[dict]:
    hints: list[dict] = []
    io_index = io_map if io_map is not None else _build_endpoint_io(parsed_api.endpoints)

    operation_to_keys: dict[str, list[str]] = {}
    for endpoint in parsed_api.endpoints:
        if endpoint.operation_id:
            operation_to_keys.setdefault(endpoint.operation_id, []).append(_endpoint_key(endpoint))

    for endpoint in parsed_api.endpoints:
        from_key = endpoint.operation_id or _endpoint_key(endpoint)
        for response in endpoint.responses:
            for link_name, link in (response.links or {}).items():
                if not isinstance(link, dict):
                    continue
                hints.append(
                    {
                        "kind": "openapi_link",
                        "priority": "high",
                        "from": from_key,
                        "status_code": response.status_code,
                        "link_name": link_name,
                        "to_operation_id": link.get("operationId"),
                        "to_operation_ref": link.get("operationRef"),
                        "parameters": link.get("parameters", {}),
                    }
                )

    producer_patterns = ("id", "token", "access_token", "userId", "postId")
    for endpoint in parsed_api.endpoints:
        path_params = [p.name for p in endpoint.parameters if p.location == "path"]
        for param in path_params:
            if any(marker.lower() in param.lower() for marker in producer_patterns):
                hints.append(
                    {
                        "kind": "path_param_dependency",
                        "priority": "medium",
                        "consumer": _endpoint_key(endpoint),
                        "param": param,
                    }
                )

    io_items = list(io_index.values())
    for producer in io_items:
        if not producer.produced_vars:
            continue
        for consumer in io_items:
            if producer.key == consumer.key:
                continue
            overlap = sorted(producer.produced_vars & consumer.consumed_vars)
            if not overlap:
                continue
            priority = "high" if any(var in _AUTH_CONTEXT_VARS for var in overlap) else "medium"
            hints.append(
                {
                    "kind": "dependency_edge",
                    "priority": priority,
                    "producer": producer.key,
                    "consumer": consumer.key,
                    "vars": overlap,
                }
            )

    # Map OpenAPI links to concrete edges when operationId can be resolved.
    for hint in [h for h in hints if h.get("kind") == "openapi_link"]:
        from_id = str(hint.get("from") or "")
        to_operation_id = hint.get("to_operation_id")
        from_candidates = operation_to_keys.get(from_id, [from_id])
        to_candidates = operation_to_keys.get(str(to_operation_id), []) if to_operation_id else []
        if not to_candidates:
            continue
        for source in from_candidates:
            for target in to_candidates:
                if source == target:
                    continue
                hints.append(
                    {
                        "kind": "dependency_edge",
                        "priority": "high",
                        "producer": source,
                        "consumer": target,
                        "vars": ["linked_dependency"],
                        "from_link": True,
                    }
                )

    return hints


def _build_dependency_edges(hints: list[dict]) -> list[_DependencyEdge]:
    edges: list[_DependencyEdge] = []
    for hint in hints:
        if hint.get("kind") != "dependency_edge":
            continue
        source = str(hint.get("producer") or "").strip()
        target = str(hint.get("consumer") or "").strip()
        if not source or not target:
            continue
        vars_raw = hint.get("vars", [])
        vars_list = []
        if isinstance(vars_raw, list):
            vars_list = [str(item) for item in vars_raw if item]
        edges.append(
            _DependencyEdge(
                source=source,
                target=target,
                vars=tuple(sorted(set(vars_list))),
                priority=str(hint.get("priority") or "medium"),
                reason="openapi_link" if hint.get("from_link") else "producer_consumer",
            )
        )
    return edges


def _build_request_body(endpoint: ParsedEndpoint, resource: str, available_vars: set[str]) -> dict | None:
    if endpoint.method not in {HttpMethod.POST, HttpMethod.PUT, HttpMethod.PATCH}:
        return None

    if isinstance(endpoint.request_body_example, dict) and endpoint.request_body_example:
        body = endpoint.request_body_example.copy()
    else:
        body = {}

    if not body and _is_auth_endpoint(endpoint):
        body = {"username": "admin", "password": "password123"}

    required_fields = endpoint.request_body_required_fields or []
    for field in required_fields:
        if field in body:
            continue

        lowered = field.lower()
        if lowered in {"username", "user_name", "email"}:
            body[field] = "admin" if _is_auth_endpoint(endpoint) else "demo_user"
        elif lowered in {"password", "pass", "secret"}:
            body[field] = "password123" if _is_auth_endpoint(endpoint) else "demo_pass"
        elif lowered.endswith("id"):
            candidate = re.sub(r"[^a-z0-9]+", "_", lowered)
            if candidate in available_vars:
                body[field] = f"{{{{ctx.{candidate}}}}}"
            else:
                body[field] = 1
        elif lowered in {"name", "title"}:
            body[field] = f"auto-{resource}-{{{{ctx.run_id}}}}"
        elif lowered in {"content", "message", "text", "description"}:
            body[field] = "Generated by flow planner"
        elif "token" in lowered:
            body[field] = "{{ctx.auth_token}}"
        else:
            body[field] = "sample"

    if not body:
        # Keep deterministic fallback body for common create/update semantics.
        body = {
            "name": f"auto-{resource}-{{{{ctx.run_id}}}}",
            "content": "Generated by flow planner",
        }

    return body


def _build_step_extract_rules(endpoint: ParsedEndpoint, resource: str, io_meta: _EndpointIOMeta) -> list[FlowExtractRule]:
    rules: list[FlowExtractRule] = []

    if io_meta.is_auth:
        rules.extend(
            [
                FlowExtractRule(var="auth_token", source="body", path="token", required=False),
                FlowExtractRule(var="auth_token", source="body", path="access_token", required=False),
                FlowExtractRule(var="auth_token", source="headers", path="authorization", required=False),
            ]
        )

    resource_id_var = f"{_singular(resource)}_id"
    if resource_id_var in io_meta.produced_vars or endpoint.method in {HttpMethod.POST, HttpMethod.GET}:
        compact_resource_id = f"{_singular(resource)}id"
        rules.append(FlowExtractRule(var=resource_id_var, source="body", path="id", required=False))
        rules.append(FlowExtractRule(var=resource_id_var, source="body", path="0.id", required=False))
        rules.append(FlowExtractRule(var=resource_id_var, source="body", path=compact_resource_id, required=False))
        rules.append(FlowExtractRule(var=resource_id_var, source="body", path=f"0.{compact_resource_id}", required=False))
        rules.append(FlowExtractRule(var=resource_id_var, source="headers", path="location", required=False))

    for produced in sorted(io_meta.produced_vars):
        if produced in {"id", resource_id_var, "auth_token", "access_token", "refresh_token", "location"}:
            continue
        if produced.endswith("id"):
            rules.append(FlowExtractRule(var=produced, source="body", path=produced, required=False))
            rules.append(FlowExtractRule(var=produced, source="body", path=f"0.{produced}", required=False))
        elif "token" in produced:
            rules.append(FlowExtractRule(var=produced, source="body", path=produced, required=False))

    # Remove duplicates while preserving order.
    deduped: list[FlowExtractRule] = []
    seen: set[tuple[str, str, str]] = set()
    for rule in rules:
        key = (rule.var, rule.source.value, rule.path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(rule)
    return deduped


def _build_step(
    endpoint: ParsedEndpoint,
    io_meta: _EndpointIOMeta,
    order: int,
    available_vars: set[str],
    req: FlowGenerateRequest,
) -> FlowStep:
    resource = io_meta.resource
    step_id_seed = endpoint.operation_id or f"{endpoint.method.value}_{resource}_{order}"
    step_id = re.sub(r"[^a-zA-Z0-9_]+", "_", step_id_seed).strip("_").lower() or f"step_{order}"
    endpoint_path = _normalize_path(endpoint.path, "")

    path_params: dict[str, object] = {}
    for path_param in _find_path_params(endpoint):
        var_name = _ctx_var_for_param(path_param, resource)
        if var_name in available_vars or var_name in _DEFAULT_EXTERNAL_CTX_VARS:
            path_params[path_param] = f"{{{{ctx.{var_name}}}}}"
        else:
            path_params[path_param] = 1

    headers: dict[str, object] = {}
    if endpoint.requires_auth and not io_meta.is_auth:
        headers["Authorization"] = "Bearer {{ctx.auth_token}}"
        headers["Cookie"] = "token={{ctx.auth_token}}"
    for parameter in endpoint.parameters:
        if parameter.location != "header":
            continue
        lowered = parameter.name.lower()
        if lowered in {"authorization", "api_key", "x-api-key"}:
            if lowered == "authorization":
                if not io_meta.is_auth:
                    headers[parameter.name] = "Bearer {{ctx.auth_token}}"
                    headers.setdefault("Cookie", "token={{ctx.auth_token}}")
            else:
                headers[parameter.name] = "{{ctx.api_key}}"

    query_params: dict[str, object] = {}
    for parameter in endpoint.parameters:
        if parameter.location != "query":
            continue
        lowered = parameter.name.lower()
        if lowered in {"status"}:
            query_params[parameter.name] = "available"
        elif lowered in {"limit", "page_size", "size"}:
            query_params[parameter.name] = 10
        elif lowered in {"page", "offset"}:
            query_params[parameter.name] = 1
        elif "search" in lowered or "query" in lowered:
            query_params[parameter.name] = "demo"

    body = _build_request_body(endpoint, resource, available_vars)
    extract_rules = _build_step_extract_rules(endpoint, resource, io_meta)

    required = True
    if req.mutation_policy == FlowMutationPolicy.SAFE and endpoint.method == HttpMethod.DELETE:
        required = False

    assertions = [
        TestAssertion(
            field="status_code",
            operator="eq",
            expected=_choose_expected_status(endpoint),
        )
    ]

    return FlowStep(
        step_id=step_id,
        order=order,
        name=endpoint.summary or f"{endpoint.method.value} {endpoint_path}",
        endpoint=endpoint_path,
        method=endpoint.method,
        headers=headers,
        query_params=query_params,
        path_params=path_params,
        body=body,
        extract=extract_rules,
        assertions=assertions,
        expected_status=_choose_expected_status(endpoint),
        required=required,
    )


def _objective_score(endpoint: ParsedEndpoint, objective: str) -> int:
    text = _endpoint_text(endpoint)
    score = 0
    objective_tokens = [token for token in re.findall(r"[a-zA-Z0-9_]+", objective.lower()) if len(token) > 2]
    for token in objective_tokens:
        if token in text:
            score += 2

    if "auth" in objective.lower() and _is_auth_endpoint(endpoint):
        score += 6
    if "interaction" in objective.lower() and _keyword_in_endpoint(endpoint, _INTERACTION_KEYWORDS):
        score += 4
    if "transaction" in objective.lower() and _keyword_in_endpoint(endpoint, _TRANSACTIONAL_KEYWORDS):
        score += 4
    if "browse" in objective.lower() and _looks_like_collection_get(endpoint):
        score += 3
    if "detail" in objective.lower() and _looks_like_detail_get(endpoint):
        score += 3

    if endpoint.method == HttpMethod.GET:
        score += 1

    return score


def _prune_steps_for_mutation_policy(steps: list[FlowStep], mutation_policy: FlowMutationPolicy) -> list[FlowStep]:
    if mutation_policy == FlowMutationPolicy.FULL_LIFECYCLE:
        return steps

    mutating_methods = {HttpMethod.POST, HttpMethod.PUT, HttpMethod.PATCH, HttpMethod.DELETE}

    if mutation_policy == FlowMutationPolicy.SAFE:
        filtered = [step for step in steps if step.method != HttpMethod.DELETE]
        mutating_count = 0
        result: list[FlowStep] = []
        for step in filtered:
            if step.method in mutating_methods and not _flow_step_is_auth_like(step):
                if mutating_count >= 2:
                    continue
                mutating_count += 1
            result.append(step)
        return result

    # Balanced: allow at most one DELETE and keep other methods.
    delete_seen = 0
    result: list[FlowStep] = []
    for step in steps:
        if step.method == HttpMethod.DELETE:
            delete_seen += 1
            if delete_seen > 1:
                continue
        result.append(step)
    return result


_MUTATING_METHODS = {HttpMethod.POST, HttpMethod.PUT, HttpMethod.PATCH, HttpMethod.DELETE}


def _flow_step_is_auth_like(step: FlowStep) -> bool:
    combined = " ".join(
        [
            step.name,
            step.endpoint,
            str(step.body),
            " ".join(rule.var for rule in step.extract),
        ]
    ).lower()
    if any(token in combined for token in _AUTH_KEYWORDS):
        return True
    if any("token" in rule.var.lower() for rule in step.extract):
        return True
    return False


def _is_business_mutation_step(step: FlowStep) -> bool:
    return step.method in _MUTATING_METHODS and not _flow_step_is_auth_like(step)


def _step_consumed_ctx_vars(step: FlowStep) -> set[str]:
    consumed: set[str] = set()
    consumed.update(_extract_ctx_vars(step.endpoint))
    consumed.update(_extract_ctx_vars(step.path_params))
    consumed.update(_extract_ctx_vars(step.query_params))
    consumed.update(_extract_ctx_vars(step.headers))
    consumed.update(_extract_ctx_vars(step.body))
    return consumed


def _known_vars_after_steps(steps: list[FlowStep], req: FlowGenerateRequest) -> set[str]:
    known_vars = set(_DEFAULT_EXTERNAL_CTX_VARS)
    known_vars.update(str(key) for key in req.app_context.keys())
    for step in sorted(steps, key=lambda item: item.order):
        known_vars.update(rule.var for rule in step.extract)
    return known_vars


def _flow_quality_errors(flow: FlowScenario, req: FlowGenerateRequest) -> list[str]:
    errors: list[str] = []
    sorted_steps = sorted(flow.steps, key=lambda step: step.order)

    known_vars = set(_DEFAULT_EXTERNAL_CTX_VARS)
    known_vars.update(str(key) for key in req.app_context.keys())

    for step in sorted_steps:
        placeholders = set(_PATH_PARAM_PATTERN.findall(step.endpoint))
        missing_path_param_keys = placeholders - set(step.path_params.keys())
        if missing_path_param_keys:
            errors.append(f"step {step.step_id}: unresolved endpoint placeholders {sorted(missing_path_param_keys)}")

        consumed = _step_consumed_ctx_vars(step)

        missing_vars = sorted(consumed - known_vars)
        if missing_vars:
            errors.append(f"step {step.step_id}: missing context vars {missing_vars}")

        produced = {rule.var for rule in step.extract}
        known_vars.update(produced)

    business_mutations = [step for step in sorted_steps if _is_business_mutation_step(step)]
    has_mutation = bool(business_mutations)
    if has_mutation:
        last_mutation_order = max(step.order for step in business_mutations)
        has_read_after_write = any(
            step.method == HttpMethod.GET and step.order > last_mutation_order
            for step in sorted_steps
        )
        if not has_read_after_write:
            errors.append("flow missing read-after-write verification GET step")

    if req.mutation_policy == FlowMutationPolicy.SAFE:
        delete_count = sum(1 for step in sorted_steps if step.method == HttpMethod.DELETE)
        if delete_count > 0:
            errors.append("safe mutation policy forbids DELETE steps")

        mutation_count = len(business_mutations)
        if mutation_count > max(1, len(sorted_steps) // 2):
            errors.append("safe mutation policy exceeded mutation ratio")

    return errors


def _flow_signature(flow: FlowScenario) -> tuple[str, ...]:
    ordered_steps = sorted(flow.steps, key=lambda step: step.order)
    return tuple(f"{step.method.value}:{step.endpoint}" for step in ordered_steps)


def _endpoint_lookup(parsed_api: ParsedAPI) -> dict[tuple[HttpMethod, str], ParsedEndpoint]:
    lookup: dict[tuple[HttpMethod, str], ParsedEndpoint] = {}
    for endpoint in parsed_api.endpoints:
        lookup[(endpoint.method, _normalize_path(endpoint.path, parsed_api.base_url))] = endpoint
    return lookup


def _successful_responses(endpoint: ParsedEndpoint) -> list:
    successful = [
        response
        for response in endpoint.responses
        if str(response.status_code).isdigit() and 200 <= int(response.status_code) < 300
    ]
    return successful or list(endpoint.responses)


def _extract_from_example(value: Any, path: str) -> Any:
    normalized = _normalize_json_path_like(path)
    if not normalized:
        return value

    current = value
    for part in [item for item in normalized.split(".") if item]:
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


def _response_examples(endpoint: ParsedEndpoint) -> list[Any]:
    examples: list[Any] = []
    for response in _successful_responses(endpoint):
        if response.example is not None:
            examples.append(response.example)
        for example in (response.examples or {}).values():
            if example is not None:
                examples.append(example)
    return examples


def _response_supports_body_path(endpoint: ParsedEndpoint, path: str) -> bool:
    normalized = _normalize_json_path_like(path)
    if not normalized:
        return True

    examples = _response_examples(endpoint)
    if examples:
        return any(_extract_from_example(example, normalized) is not None for example in examples)

    content_types = [str(response.content_type or "").lower() for response in _successful_responses(endpoint)]
    return any("json" in content_type or "+json" in content_type for content_type in content_types)


def _reason_code_for_quality_error(error: str) -> str:
    lowered = error.lower()
    if "unresolved endpoint placeholders" in lowered:
        return "unresolved_path_params"
    if "missing context vars" in lowered:
        return "unresolved_context_dependency"
    if "read-after-write verification" in lowered:
        return "incoherent_flow"
    if "mutation policy" in lowered or "forbids delete" in lowered:
        return "mutation_policy_violation"
    return "quality_gate"


def _summarize_reasons(reasons: list[tuple[str, str]]) -> tuple[str, str]:
    if not reasons:
        return "accepted", ""

    primary_code = reasons[0][0]
    fragments: list[str] = []
    seen: set[str] = set()
    for _code, reason in reasons:
        if reason in seen:
            continue
        seen.add(reason)
        fragments.append(reason)
        if len(fragments) >= 2:
            break
    return primary_code, "; ".join(fragments)


def _static_review_flow(
    flow: FlowScenario,
    parsed_api: ParsedAPI,
    req: FlowGenerateRequest,
    endpoint_map: dict[tuple[HttpMethod, str], ParsedEndpoint],
    seen_signatures: set[tuple[str, ...]],
) -> list[tuple[str, str]]:
    reasons: list[tuple[str, str]] = []
    sorted_steps = sorted(flow.steps, key=lambda item: item.order)
    future_consumed_by_order: dict[int, set[str]] = {}
    consumed_after: set[str] = set()
    for step in reversed(sorted_steps):
        future_consumed_by_order[step.order] = set(consumed_after)
        consumed_after.update(_step_consumed_ctx_vars(step))

    if len(flow.steps) < 2:
        reasons.append(("too_short", "flow must contain at least two executable steps"))

    signature = _flow_signature(flow)
    if signature in seen_signatures:
        reasons.append(("duplicate_flow", "flow duplicates an existing candidate signature"))

    for error in _flow_quality_errors(flow, req):
        reasons.append((_reason_code_for_quality_error(error), error))

    for step in sorted_steps:
        endpoint_key = (step.method, _normalize_path(step.endpoint, parsed_api.base_url))
        endpoint = endpoint_map.get(endpoint_key)
        if endpoint is None:
            reasons.append(
                (
                    "unknown_endpoint",
                    f"step {step.step_id}: {step.method.value} {step.endpoint} is not present in the parsed API",
                )
            )
            continue

        body_rules_by_var: dict[str, list[FlowExtractRule]] = {}
        for rule in step.extract:
            if rule.source.value == "body":
                body_rules_by_var.setdefault(rule.var, []).append(rule)
            if (
                rule.required
                and rule.source.value == "body"
                and not _response_supports_body_path(endpoint, rule.path)
            ):
                reasons.append(
                    (
                        "impossible_extraction",
                        (
                            f"step {step.step_id}: extract '{rule.var}' from body:{rule.path or '<body>'} "
                            f"is unsupported by {step.method.value} {step.endpoint} response shape"
                        ),
                    )
                )
            if (
                rule.source.value == "body"
                and ("token" in rule.var.lower() or "token" in rule.path.lower())
                and not _response_supports_body_path(endpoint, rule.path or "token")
            ):
                reasons.append(
                    (
                        "unsupported_auth_assumption",
                        (
                            f"step {step.step_id}: auth/token extraction is not supported by "
                            f"{step.method.value} {step.endpoint} response examples or content type"
                        ),
                    )
                )

        for var in set(body_rules_by_var) & future_consumed_by_order.get(step.order, set()):
            if any(_response_supports_body_path(endpoint, rule.path) for rule in body_rules_by_var[var]):
                continue
            reasons.append(
                (
                    "impossible_extraction",
                    (
                        f"step {step.step_id}: no supported extraction path for reused variable "
                        f"'{var}' in {step.method.value} {step.endpoint} response shape"
                    ),
                )
            )

    return reasons


def _build_api_context(parsed_api: ParsedAPI) -> str:
    lines: list[str] = [
        f"API: {parsed_api.title} v{parsed_api.version}",
        f"Base URL: {parsed_api.base_url}",
        "Endpoints:",
    ]
    for endpoint in parsed_api.endpoints:
        params = [f"{p.location}:{p.name}" for p in endpoint.parameters]
        lines.append(
            f"- {endpoint.method.value} {endpoint.path} | auth={endpoint.requires_auth} | tags={endpoint.tags} | summary={endpoint.summary!r} | params={params}"
        )
        if endpoint.request_body_required_fields:
            lines.append(f"  request_required_fields={endpoint.request_body_required_fields}")
        if endpoint.request_body_example is not None:
            lines.append(f"  request_example={json.dumps(endpoint.request_body_example, ensure_ascii=True)}")
        if endpoint.response_examples:
            lines.append(f"  response_examples={json.dumps(endpoint.response_examples, ensure_ascii=True)}")
        for response in endpoint.responses:
            lines.append(
                "  response "
                f"{response.status_code} content_type={response.content_type!r} "
                f"description={response.description!r} schema_ref={response.schema_ref!r}"
            )
            if response.example is not None:
                lines.append(f"    example={json.dumps(response.example, ensure_ascii=True)}")
            if response.links:
                lines.append(
                    f"  response {response.status_code} links={json.dumps(response.links, ensure_ascii=True)}"
                )
    return "\n".join(lines)


def _build_seed_flow_name(resource: str, objective: str) -> str:
    objective_text = objective.strip().capitalize() if objective else "Core workflow"
    return f"{resource.title()} journey: {objective_text}"


def _build_seed_flow_description(objective: str, resource: str) -> str:
    return f"Realistic user journey for {resource} endpoints focused on: {objective}."


def _dependency_producer_score(meta: _EndpointIOMeta, missing_vars: set[str]) -> tuple[int, int, int, int]:
    overlap = meta.produced_vars & missing_vars
    auth_overlap = overlap & _AUTH_CONTEXT_VARS
    return (
        6 if auth_overlap and meta.is_auth else 0,
        5 if _looks_like_collection_get(meta.endpoint) else 0,
        3 if meta.endpoint.method == HttpMethod.POST and not meta.is_auth else 0,
        -len(_find_path_params(meta.endpoint)),
    )


def _build_dependency_prelude(
    target_meta: _EndpointIOMeta,
    io_map: dict[str, _EndpointIOMeta],
    available_vars: set[str],
    used_keys: set[str],
    max_steps: int,
) -> list[ParsedEndpoint]:
    prelude: list[ParsedEndpoint] = []
    missing_vars = set(target_meta.consumed_vars - available_vars)

    while missing_vars and len(prelude) < max_steps:
        producer_candidates = [
            meta
            for meta in io_map.values()
            if meta.key not in used_keys
            and meta.key != target_meta.key
            and bool(meta.produced_vars & missing_vars)
            and meta.consumed_vars.issubset(available_vars)
        ]
        if not producer_candidates:
            break

        producer = sorted(
            producer_candidates,
            key=lambda meta: _dependency_producer_score(meta, missing_vars),
            reverse=True,
        )[0]
        prelude.append(producer.endpoint)
        used_keys.add(producer.key)
        available_vars.update(producer.produced_vars)
        missing_vars = set(target_meta.consumed_vars - available_vars)

    return prelude


def _build_seed_flows(
    parsed_api: ParsedAPI,
    req: FlowGenerateRequest,
    objectives: list[str],
    dependency_hints: list[dict],
) -> list[FlowScenario]:
    endpoints = sorted(parsed_api.endpoints, key=lambda item: (item.path, item.method.value))
    if not endpoints:
        return []

    io_map = _build_endpoint_io(endpoints)
    edges = _build_dependency_edges(dependency_hints)

    by_source: dict[str, list[_DependencyEdge]] = {}
    for edge in edges:
        by_source.setdefault(edge.source, []).append(edge)

    auth_candidates = [meta.endpoint for meta in io_map.values() if meta.is_auth]
    auth_endpoint = auth_candidates[0] if auth_candidates else None

    objective_queue = objectives.copy()
    while len(objective_queue) < req.max_flows:
        objective_queue.append("core api workflow")

    generated: list[FlowScenario] = []
    signatures: set[tuple[str, ...]] = set()

    for flow_index, objective in enumerate(objective_queue[: req.max_flows * 2], start=1):
        if len(generated) >= req.max_flows:
            break

        start_candidates = sorted(
            endpoints,
            key=lambda endpoint: (
                _objective_score(endpoint, objective),
                2 if endpoint.method == HttpMethod.POST else 1 if endpoint.method == HttpMethod.GET else 0,
                -len(_find_path_params(endpoint)),
            ),
            reverse=True,
        )

        if not start_candidates:
            continue

        chosen = start_candidates[0]
        chain: list[ParsedEndpoint] = []
        available_vars = set(_DEFAULT_EXTERNAL_CTX_VARS)
        if auth_endpoint is not None:
            available_vars.difference_update(_AUTH_CONTEXT_VARS)
        used_keys: set[str] = set()
        chosen_meta = io_map[_endpoint_key(chosen)]

        prelude = _build_dependency_prelude(
            chosen_meta,
            io_map,
            available_vars,
            used_keys,
            max(req.max_steps_per_flow - 1, 0),
        )
        chain.extend(prelude)

        chain.append(chosen)
        used_keys.add(_endpoint_key(chosen))
        available_vars.update(chosen_meta.produced_vars)

        while len(chain) < req.max_steps_per_flow:
            current = chain[-1]
            current_key = _endpoint_key(current)
            outgoing = by_source.get(current_key, [])

            candidate_endpoints: list[ParsedEndpoint] = []
            for edge in outgoing:
                target_meta = io_map.get(edge.target)
                if target_meta is None:
                    continue
                if target_meta.key in used_keys:
                    continue
                if not target_meta.consumed_vars.issubset(available_vars):
                    continue
                candidate_endpoints.append(target_meta.endpoint)

            if not candidate_endpoints:
                for endpoint in endpoints:
                    key = _endpoint_key(endpoint)
                    meta = io_map[key]
                    if key in used_keys:
                        continue
                    if not meta.consumed_vars.issubset(available_vars):
                        continue
                    candidate_endpoints.append(endpoint)

            if not candidate_endpoints:
                break

            candidate_endpoints = sorted(
                candidate_endpoints,
                key=lambda endpoint: (
                    _objective_score(endpoint, objective),
                    2 if endpoint.method == HttpMethod.GET else 1,
                    1 if _resource_key(endpoint.path) == _resource_key(current.path) else 0,
                ),
                reverse=True,
            )

            next_endpoint = candidate_endpoints[0]
            next_key = _endpoint_key(next_endpoint)
            auth_key = _endpoint_key(auth_endpoint) if auth_endpoint is not None else None
            next_consumes_auth = bool(io_map[next_key].consumed_vars & _AUTH_CONTEXT_VARS)
            if (
                auth_endpoint is not None
                and auth_key is not None
                and auth_key not in used_keys
                and next_key != auth_key
                and next_consumes_auth
            ):
                chain.append(auth_endpoint)
                used_keys.add(auth_key)
                available_vars.update(io_map[auth_key].produced_vars)
                if len(chain) >= req.max_steps_per_flow:
                    break

            chain.append(next_endpoint)
            used_keys.add(next_key)
            available_vars.update(io_map[next_key].produced_vars)

        if len(chain) < 2:
            continue

        steps: list[FlowStep] = []
        known_vars = set(_DEFAULT_EXTERNAL_CTX_VARS)
        for order, endpoint in enumerate(chain[: req.max_steps_per_flow], start=1):
            meta = io_map[_endpoint_key(endpoint)]
            step = _build_step(endpoint, meta, order, known_vars, req)
            steps.append(step)
            known_vars.update(rule.var for rule in step.extract)

        steps = _prune_steps_for_mutation_policy(steps, req.mutation_policy)
        steps = _ensure_read_after_write_verification(steps, endpoints, io_map, req)
        if len(steps) < 2:
            continue

        # Normalize ordering after mutation pruning.
        normalized_steps = _renumber_steps(steps)

        resource = _resource_key(normalized_steps[0].endpoint)
        persona = req.personas[(len(generated)) % len(req.personas)] if req.personas else (
            "authenticated_user" if any(step.headers.get("Authorization") for step in normalized_steps) else "api_user"
        )

        flow = FlowScenario(
            id=str(uuid.uuid4()),
            name=_build_seed_flow_name(resource, objective),
            description=_build_seed_flow_description(objective, resource),
            persona=persona,
            preconditions=["Base URL reachable", "API spec parsed"],
            tags=[resource, "workflow", "stateful", "deterministic_seed"],
            steps=normalized_steps,
        )

        signature = tuple(f"{step.method.value}:{step.endpoint}" for step in flow.steps)
        if signature in signatures:
            continue
        signatures.add(signature)
        generated.append(flow)

    if generated:
        return generated[: req.max_flows]

    generic_steps: list[FlowStep] = []
    known_vars = set(_DEFAULT_EXTERNAL_CTX_VARS)
    for order, endpoint in enumerate(endpoints[: req.max_steps_per_flow], start=1):
        meta = io_map[_endpoint_key(endpoint)]
        generic_steps.append(_build_step(endpoint, meta, order, known_vars, req))

    generic_steps = _prune_steps_for_mutation_policy(generic_steps, req.mutation_policy)
    generic_steps = _ensure_read_after_write_verification(generic_steps, endpoints, io_map, req)
    if len(generic_steps) < 2:
        return []

    return [
        FlowScenario(
            id=str(uuid.uuid4()),
            name="Generic API journey",
            description="Fallback deterministic journey generated from available endpoints.",
            persona=req.personas[0] if req.personas else "api_client",
            preconditions=["Base URL reachable", "API spec parsed"],
            tags=["workflow", "fallback"],
            steps=_renumber_steps(generic_steps),
        )
    ]


def _finalize_flows(
    flows: list[FlowScenario],
    req: FlowGenerateRequest,
    flow_generation_id: str,
    created_at: datetime,
) -> list[FlowScenario]:
    finalized: list[FlowScenario] = []
    for flow in flows[: req.max_flows]:
        trimmed_steps = list(flow.steps)[: req.max_steps_per_flow]
        normalized_steps = []
        for index, step in enumerate(trimmed_steps, start=1):
            normalized_steps.append(step.model_copy(update={"order": index}))

        normalized_flow = flow.model_copy(
            update={
                "id": flow.id or str(uuid.uuid4()),
                "steps": normalized_steps,
                "source_generation_id": flow_generation_id,
                "created_at": created_at,
            }
        )
        finalized.append(normalized_flow)
    return finalized


def _quality_filter(
    flows: list[FlowScenario],
    req: FlowGenerateRequest,
) -> tuple[list[FlowScenario], list[dict[str, object]]]:
    accepted: list[FlowScenario] = []
    dropped: list[dict[str, object]] = []

    for flow in flows:
        errors = _flow_quality_errors(flow, req)
        if errors:
            dropped.append({"flow_id": flow.id, "flow_name": flow.name, "errors": errors})
            continue
        accepted.append(flow)

    return accepted, dropped


def _renumber_steps(steps: list[FlowStep]) -> list[FlowStep]:
    return [step.model_copy(update={"order": index}) for index, step in enumerate(steps, start=1)]


def _verification_endpoint_score(
    endpoint: ParsedEndpoint,
    mutation_step: FlowStep,
    io_meta: _EndpointIOMeta,
) -> tuple[int, int, int, int]:
    endpoint_path = _normalize_path(endpoint.path, "")
    mutation_path = _normalize_path(mutation_step.endpoint, "")
    same_template = endpoint_path == mutation_path
    same_resource = io_meta.resource == _resource_key(mutation_step.endpoint)
    is_detail = _looks_like_detail_get(endpoint)
    is_collection = _looks_like_collection_get(endpoint)
    return (
        4 if same_template else 0,
        3 if same_resource else 0,
        2 if is_detail else 0,
        1 if is_collection else 0,
    )


def _find_verification_endpoint(
    endpoints: list[ParsedEndpoint],
    io_map: dict[str, _EndpointIOMeta],
    mutation_step: FlowStep,
    known_vars: set[str],
) -> ParsedEndpoint | None:
    candidates: list[tuple[tuple[int, int, int, int], ParsedEndpoint]] = []
    for endpoint in endpoints:
        if endpoint.method != HttpMethod.GET:
            continue
        meta = io_map[_endpoint_key(endpoint)]
        if not meta.consumed_vars.issubset(known_vars):
            continue
        score = _verification_endpoint_score(endpoint, mutation_step, meta)
        if not any(score):
            continue
        candidates.append((score, endpoint))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _try_append_verification_step(
    steps: list[FlowStep],
    verification_endpoint: ParsedEndpoint,
    io_map: dict[str, _EndpointIOMeta],
    req: FlowGenerateRequest,
) -> list[FlowStep] | None:
    known_vars = _known_vars_after_steps(steps, req)
    meta = io_map[_endpoint_key(verification_endpoint)]
    if not meta.consumed_vars.issubset(known_vars):
        return None

    verification_step = _build_step(
        verification_endpoint,
        meta,
        len(steps) + 1,
        known_vars,
        req,
    )
    verification_step = verification_step.model_copy(
        update={
            "step_id": f"{verification_step.step_id}_verify",
            "name": f"Verify after write: {verification_step.name}",
            "extract": [],
        }
    )
    return _renumber_steps([*steps, verification_step])


def _ensure_read_after_write_verification(
    steps: list[FlowStep],
    endpoints: list[ParsedEndpoint],
    io_map: dict[str, _EndpointIOMeta],
    req: FlowGenerateRequest,
) -> list[FlowStep]:
    normalized_steps = _renumber_steps(steps)
    business_mutations = [step for step in normalized_steps if _is_business_mutation_step(step)]
    if not business_mutations:
        return normalized_steps

    last_mutation = max(business_mutations, key=lambda step: step.order)
    if any(step.method == HttpMethod.GET and step.order > last_mutation.order for step in normalized_steps):
        return normalized_steps

    known_vars = _known_vars_after_steps(normalized_steps, req)
    verification_endpoint = _find_verification_endpoint(endpoints, io_map, last_mutation, known_vars)
    if verification_endpoint is None:
        return normalized_steps

    if len(normalized_steps) < req.max_steps_per_flow:
        updated_steps = _try_append_verification_step(normalized_steps, verification_endpoint, io_map, req)
        return updated_steps or normalized_steps

    removable_indices = [
        index
        for index, step in enumerate(normalized_steps)
        if step.method == HttpMethod.GET and step.order < last_mutation.order
    ]
    removable_indices.sort(
        key=lambda index: (
            0 if _normalize_path(normalized_steps[index].endpoint, "") == _normalize_path(verification_endpoint.path, "") else 1,
            0 if "{" in normalized_steps[index].endpoint else 1,
            -normalized_steps[index].order,
        )
    )

    for index in removable_indices:
        candidate_steps = [step for current_index, step in enumerate(normalized_steps) if current_index != index]
        candidate_steps = _renumber_steps(candidate_steps)
        updated_steps = _try_append_verification_step(candidate_steps, verification_endpoint, io_map, req)
        if updated_steps is None:
            continue
        probe_flow = FlowScenario(id="read_after_write_probe", name="read_after_write_probe", steps=updated_steps)
        if not _flow_quality_errors(probe_flow, req):
            return updated_steps

    return normalized_steps


def _read_companion_score(endpoint: ParsedEndpoint) -> tuple[int, int, int]:
    text = _endpoint_text(endpoint)
    return (
        4 if any(token in text for token in ("ping", "health", "status")) else 0,
        2 if _looks_like_collection_get(endpoint) else 0,
        -len(_find_path_params(endpoint)),
    )


def _append_read_companion_if_short(
    steps: list[FlowStep],
    parsed_api: ParsedAPI,
    io_map: dict[str, _EndpointIOMeta],
    req: FlowGenerateRequest,
) -> list[FlowStep]:
    normalized_steps = _renumber_steps(steps)
    if len(normalized_steps) != 1 or len(normalized_steps) >= req.max_steps_per_flow:
        return normalized_steps

    known_vars = _known_vars_after_steps(normalized_steps, req)
    existing_signature = f"{normalized_steps[0].method.value}:{_normalize_path(normalized_steps[0].endpoint, '')}"
    candidates = [
        endpoint
        for endpoint in parsed_api.endpoints
        if endpoint.method == HttpMethod.GET
        and f"{endpoint.method.value}:{_normalize_path(endpoint.path, '')}" != existing_signature
        and io_map[_endpoint_key(endpoint)].consumed_vars.issubset(known_vars)
    ]
    if not candidates:
        return normalized_steps

    companion_endpoint = sorted(candidates, key=_read_companion_score, reverse=True)[0]
    companion_meta = io_map[_endpoint_key(companion_endpoint)]
    companion_step = _build_step(
        companion_endpoint,
        companion_meta,
        len(normalized_steps) + 1,
        known_vars,
        req,
    ).model_copy(
        update={
            "step_id": f"{_sanitize_ctx_var_name(companion_endpoint.operation_id or companion_endpoint.path, fallback='read')}_companion",
            "name": f"Companion read: {companion_endpoint.summary or companion_endpoint.method.value + ' ' + companion_endpoint.path}",
            "extract": [],
        }
    )
    return _renumber_steps([*normalized_steps, companion_step])


def _prepend_missing_context_producers(
    steps: list[FlowStep],
    parsed_api: ParsedAPI,
    io_map: dict[str, _EndpointIOMeta],
    req: FlowGenerateRequest,
) -> list[FlowStep]:
    endpoint_map = _endpoint_lookup(parsed_api)
    available_vars = set(_DEFAULT_EXTERNAL_CTX_VARS)
    if any(meta.is_auth for meta in io_map.values()):
        available_vars.difference_update(_AUTH_CONTEXT_VARS)
    available_vars.update(str(key) for key in req.app_context.keys())
    used_keys: set[str] = set()
    repaired_steps: list[FlowStep] = []

    for step in sorted(steps, key=lambda item: item.order):
        endpoint = endpoint_map.get((step.method, _normalize_path(step.endpoint, parsed_api.base_url)))
        missing_vars = _step_consumed_ctx_vars(step) - available_vars
        if endpoint is not None and missing_vars and len(repaired_steps) < req.max_steps_per_flow - 1:
            target_meta = io_map[_endpoint_key(endpoint)]
            prelude = _build_dependency_prelude(
                target_meta,
                io_map,
                available_vars,
                used_keys,
                max(req.max_steps_per_flow - len(repaired_steps) - 1, 0),
            )
            for prelude_endpoint in prelude:
                prelude_meta = io_map[_endpoint_key(prelude_endpoint)]
                prelude_step = _build_step(
                    prelude_endpoint,
                    prelude_meta,
                    len(repaired_steps) + 1,
                    available_vars,
                    req,
                )
                repaired_steps.append(prelude_step)
                available_vars.update(rule.var for rule in prelude_step.extract)

        if len(repaired_steps) >= req.max_steps_per_flow:
            break
        repaired_step = step.model_copy(update={"order": len(repaired_steps) + 1})
        repaired_steps.append(repaired_step)
        if endpoint is not None:
            used_keys.add(_endpoint_key(endpoint))
        available_vars.update(rule.var for rule in repaired_step.extract)

    return _renumber_steps(repaired_steps)


def _repair_candidate_flows_static(
    flows: list[FlowScenario],
    parsed_api: ParsedAPI,
    req: FlowGenerateRequest,
) -> list[FlowScenario]:
    if not flows:
        return []
    io_map = _build_endpoint_io(parsed_api.endpoints)
    repaired: list[FlowScenario] = []
    for flow in flows:
        policy_steps = _prune_steps_for_mutation_policy(list(flow.steps), req.mutation_policy)
        removed_delete = any(step.method == HttpMethod.DELETE for step in flow.steps) and not any(
            step.method == HttpMethod.DELETE for step in policy_steps
        )
        producer_steps = _prepend_missing_context_producers(policy_steps, parsed_api, io_map, req)
        companion_steps = _append_read_companion_if_short(producer_steps, parsed_api, io_map, req)
        repaired_steps = _ensure_read_after_write_verification(companion_steps, parsed_api.endpoints, io_map, req)
        update: dict[str, object] = {"steps": repaired_steps}
        if removed_delete:
            update["name"] = re.sub(
                r"\b(delete|deletion|remove|removal)\b",
                "Safe",
                flow.name,
                flags=re.IGNORECASE,
            )
            update["description"] = (
                f"{flow.description} Unsafe delete step removed for safe mutation policy."
            ).strip()
            update["tags"] = [
                tag
                for tag in flow.tags
                if "delete" not in tag.lower() and "deletion" not in tag.lower()
            ]
        repaired.append(flow.model_copy(update=update))
    return repaired


def _merge_distinct_flows(
    primary: list[FlowScenario],
    backfill: list[FlowScenario],
    req: FlowGenerateRequest,
) -> tuple[list[FlowScenario], int]:
    merged: list[FlowScenario] = []
    signatures: set[tuple[str, ...]] = set()
    backfill_added = 0

    for flow in primary:
        if len(merged) >= req.max_flows:
            break
        signature = _flow_signature(flow)
        if signature in signatures:
            continue
        signatures.add(signature)
        merged.append(flow)

    for flow in backfill:
        if len(merged) >= req.max_flows:
            break
        signature = _flow_signature(flow)
        if signature in signatures:
            continue
        signatures.add(signature)
        merged.append(flow)
        backfill_added += 1

    return merged, backfill_added


def _endpoint_status_codes(endpoint: ParsedEndpoint) -> set[int]:
    codes: set[int] = set()
    for response in endpoint.responses:
        status = str(response.status_code)
        if status.isdigit():
            codes.add(int(status))
    return codes


def _build_negative_auth_step(
    endpoint: ParsedEndpoint,
    req: FlowGenerateRequest,
    order: int,
) -> FlowStep:
    io_meta = _build_endpoint_io([endpoint])[_endpoint_key(endpoint)]
    step = _build_step(endpoint, io_meta, order, set(_DEFAULT_EXTERNAL_CTX_VARS), req)
    step_id_base = _sanitize_ctx_var_name(step.step_id or "negative_auth", fallback="negative_auth")
    status_codes = _endpoint_status_codes(endpoint)
    expected = 401 if 401 in status_codes or 403 not in status_codes else 403

    filtered_headers: dict[str, object] = {}
    for key, value in step.headers.items():
        lowered = str(key).lower()
        if lowered in {"authorization", "x-api-key", "api_key"}:
            continue
        filtered_headers[key] = value

    return step.model_copy(
        update={
            "step_id": f"{step_id_base}_neg_auth",
            "name": f"Negative auth: {step.name}",
            "headers": filtered_headers,
            "extract": [],
            "assertions": [TestAssertion(field="status_code", operator="eq", expected=expected)],
            "expected_status": expected,
            "required": False,
        }
    )


def _build_negative_validation_step(
    endpoint: ParsedEndpoint,
    req: FlowGenerateRequest,
    order: int,
) -> FlowStep:
    io_meta = _build_endpoint_io([endpoint])[_endpoint_key(endpoint)]
    step = _build_step(endpoint, io_meta, order, set(_DEFAULT_EXTERNAL_CTX_VARS), req)
    required_fields = [field for field in endpoint.request_body_required_fields if field]
    missing_field = required_fields[0] if required_fields else "required_field"

    raw_body = step.body if isinstance(step.body, dict) else {}
    body = dict(raw_body)
    body.pop(missing_field, None)

    step_id_base = _sanitize_ctx_var_name(step.step_id or "negative_validation", fallback="negative_validation")
    return step.model_copy(
        update={
            "step_id": f"{step_id_base}_neg_validation",
            "name": f"Negative validation: missing {missing_field}",
            "body": body,
            "extract": [],
            "assertions": [TestAssertion(field="status_code", operator="eq", expected=400)],
            "expected_status": 400,
            "required": False,
        }
    )


_LOGIN_KEYWORDS = ("login", "signin", "sign_in", "sign-in")
_REGISTER_KEYWORDS = ("register", "signup", "sign_up", "sign-up")


def _endpoint_matches_keywords(endpoint: ParsedEndpoint, keywords: tuple[str, ...]) -> bool:
    haystack = " ".join(
        [
            endpoint.path,
            endpoint.operation_id,
            endpoint.summary,
            endpoint.description,
            " ".join(endpoint.tags),
        ]
    ).lower()
    return any(keyword in haystack for keyword in keywords)


def _find_auth_endpoint(parsed_api: ParsedAPI, keywords: tuple[str, ...]) -> ParsedEndpoint | None:
    for endpoint in parsed_api.endpoints:
        if endpoint.method != HttpMethod.POST:
            continue
        if _endpoint_matches_keywords(endpoint, keywords):
            return endpoint
    return None


def _flow_has_auth_producer(flow: FlowScenario) -> bool:
    for step in flow.steps:
        for rule in step.extract:
            if rule.var in {"auth_token", "access_token"}:
                return True
    return False


def _find_step_matching_endpoint(
    flow: FlowScenario,
    target_endpoint: ParsedEndpoint | None,
    parsed_api: ParsedAPI,
) -> FlowStep | None:
    if target_endpoint is None:
        return None
    target_path = _normalize_path(target_endpoint.path, parsed_api.base_url)
    for step in flow.steps:
        if step.method != target_endpoint.method:
            continue
        if _normalize_path(step.endpoint, parsed_api.base_url) == target_path:
            return step
    return None


def _flow_consumes_auth(flow: FlowScenario) -> bool:
    auth_var_tokens = ("{{ctx.auth_token}}", "{{ctx.access_token}}")
    for step in flow.steps:
        for value in step.headers.values():
            if isinstance(value, str) and any(token in value for token in auth_var_tokens):
                return True
    return False


def _flow_targets_auth_required_endpoint(flow: FlowScenario, parsed_api: ParsedAPI) -> bool:
    lookup = _endpoint_lookup(parsed_api)
    for step in flow.steps:
        endpoint_path = _normalize_path(step.endpoint, parsed_api.base_url)
        endpoint = lookup.get((step.method, endpoint_path))
        if endpoint is None:
            continue
        if endpoint.requires_auth and not _is_auth_endpoint(endpoint):
            return True
    return False


_AUTH_IDENTITY_FIELDS = {"email", "username", "user_name"}
_AUTH_PASSWORD_FIELDS = {"password", "pass", "secret"}


def _auth_body_field_names(*items: ParsedEndpoint | dict | None) -> list[str]:
    fields: list[str] = []

    def _add(name: object) -> None:
        if not isinstance(name, str) or not name:
            return
        if name not in fields:
            fields.append(name)

    for item in items:
        if isinstance(item, ParsedEndpoint):
            for field in item.request_body_required_fields:
                _add(field)
            if isinstance(item.request_body_example, dict):
                for field in item.request_body_example:
                    _add(field)
        elif isinstance(item, dict):
            for field in item:
                _add(field)

    return fields


def _credentials_from_app_context(app_context: dict, body_fields: list[str]) -> dict[str, object] | None:
    auth = app_context.get("auth") if isinstance(app_context, dict) else None
    if not isinstance(auth, dict):
        return None
    test_user = auth.get("test_user")
    if not isinstance(test_user, dict):
        return None

    credentials: dict[str, object] = {}
    fields = body_fields or ["email", "password"]
    for field in fields:
        lowered = field.lower()
        candidates = [field, lowered]
        if lowered == "user_name":
            candidates.append("username")
        elif lowered == "pass":
            candidates.append("password")
        elif lowered == "secret":
            candidates.append("password")

        for candidate in candidates:
            value = test_user.get(candidate)
            if isinstance(value, str) and value:
                credentials[field] = value
                break

    has_identity = any(field.lower() in _AUTH_IDENTITY_FIELDS for field in credentials)
    has_password = any(field.lower() in _AUTH_PASSWORD_FIELDS for field in credentials)
    return credentials if has_identity and has_password else None


def _unique_auth_credentials(body_fields: list[str]) -> dict[str, object]:
    fields = body_fields or ["email", "password"]
    credentials: dict[str, object] = {}

    has_identity = False
    has_password = False
    for field in fields:
        lowered = field.lower()
        if lowered == "email":
            credentials[field] = "user_{{ctx.unique_id}}@example.com"
            has_identity = True
        elif lowered in {"username", "user_name"}:
            credentials[field] = "user_{{ctx.unique_id}}"
            has_identity = True
        elif lowered in _AUTH_PASSWORD_FIELDS:
            credentials[field] = "Passw0rd!{{ctx.unique_id}}"
            has_password = True

    if not has_identity:
        credentials["email"] = "user_{{ctx.unique_id}}@example.com"
    if not has_password:
        credentials["password"] = "Passw0rd!{{ctx.unique_id}}"
    return credentials


def _auth_credentials_for_steps(
    req: FlowGenerateRequest,
    register_endpoint: ParsedEndpoint | None,
    login_endpoint: ParsedEndpoint | None,
    existing_login_body: dict | None = None,
) -> tuple[dict[str, object], bool]:
    body_fields = _auth_body_field_names(register_endpoint, login_endpoint, existing_login_body)
    app_context_credentials = _credentials_from_app_context(req.app_context, body_fields)
    if app_context_credentials is not None:
        return app_context_credentials, False
    return _unique_auth_credentials(body_fields), True


def _build_auth_step(
    endpoint: ParsedEndpoint,
    order: int,
    credentials: dict[str, object],
    *,
    step_id: str,
    name: str,
    soft: bool = False,
) -> FlowStep:
    """Build a synthetic register/login step.

    When ``soft=True``, the step is non-blocking and tolerant of any HTTP
    status — used for prepended register steps so a 409 "user already exists"
    on a second run does not stop the subsequent login from running.
    """

    io_meta = _build_endpoint_io([endpoint])[_endpoint_key(endpoint)]
    base = _build_step(endpoint, io_meta, order, set(_DEFAULT_EXTERNAL_CTX_VARS), FlowGenerateRequest())
    body = dict(base.body) if isinstance(base.body, dict) else {}
    for field, value in credentials.items():
        body[field] = value
    update: dict[str, object] = {"step_id": step_id, "name": name, "body": body}
    if soft:
        update["required"] = False
        update["assertions"] = []
        update["expected_status"] = None
    return base.model_copy(update=update)


def _inject_login_prepend(
    flows: list[FlowScenario],
    parsed_api: ParsedAPI,
    req: FlowGenerateRequest,
) -> tuple[list[FlowScenario], int, dict[str, str]]:
    """Ensure flows that need auth have a working register+login prefix.

    Three cases per flow:
      1. Flow has neither login nor register: prepend both (or login alone if
         `req.app_context.auth.test_user` provides literal credentials, in which
         case the user is assumed pre-registered).
      2. Flow has a login step but no register step: prepend register and align
         both steps on either supplied app-context credentials or unique
         run-scoped credentials.
      3. Flow already has both (or doesn't need auth): unchanged.
    """

    if not flows:
        return flows, 0, {}

    login_endpoint = _find_auth_endpoint(parsed_api, _LOGIN_KEYWORDS)
    register_endpoint = _find_auth_endpoint(parsed_api, _REGISTER_KEYWORDS)

    updated_flows: list[FlowScenario] = []
    injected = 0
    skip_reasons: dict[str, str] = {}

    for flow in flows:
        flow_id = flow.id or flow.name
        needs_auth = _flow_targets_auth_required_endpoint(flow, parsed_api) or _flow_consumes_auth(flow)
        if not needs_auth:
            updated_flows.append(flow)
            continue

        existing_login = _find_step_matching_endpoint(flow, login_endpoint, parsed_api)
        existing_register = _find_step_matching_endpoint(flow, register_endpoint, parsed_api)

        # Case 2: login present but register missing. Prepend register and make
        # both steps use the same run-scoped credentials unless app_context
        # supplies a fixed test user.
        if existing_login is not None:
            if existing_register is not None:
                updated_flows.append(flow)
                continue
            if register_endpoint is None:
                # Spec has no register endpoint — nothing more we can do here.
                updated_flows.append(flow)
                continue
            if len(flow.steps) + 1 > req.max_steps_per_flow:
                skip_reasons[flow_id] = "max_steps_exceeded"
                updated_flows.append(flow)
                continue

            existing_login_body = existing_login.body if isinstance(existing_login.body, dict) else {}
            login_body, _uses_generated_credentials = _auth_credentials_for_steps(
                req,
                register_endpoint,
                login_endpoint,
                existing_login_body,
            )
            register_step = _build_auth_step(
                register_endpoint,
                existing_login.order,  # placeholder; renumbered below
                login_body,
                step_id="auto_register",
                name="Register test user (idempotent)",
                soft=True,
            )

            new_steps: list[FlowStep] = []
            next_order = 1
            for step in flow.steps:
                if step.step_id == existing_login.step_id:
                    new_steps.append(register_step.model_copy(update={"order": next_order}))
                    next_order += 1
                    step = step.model_copy(update={"body": login_body})
                new_steps.append(step.model_copy(update={"order": next_order}))
                next_order += 1
            updated_flows.append(flow.model_copy(update={"steps": new_steps}))
            injected += 1
            continue

        # Case 1: no login step. Prepend login (and register if using templated creds).
        if _flow_has_auth_producer(flow):
            # Some other step already produces auth_token (e.g. an OAuth callback);
            # don't intrude.
            updated_flows.append(flow)
            continue
        if login_endpoint is None:
            skip_reasons[flow_id] = "no_login_endpoint_in_spec"
            updated_flows.append(flow)
            continue

        credentials, uses_generated_credentials = _auth_credentials_for_steps(
            req,
            register_endpoint,
            login_endpoint,
        )
        prefer_register_for_new_login = uses_generated_credentials
        use_register = prefer_register_for_new_login and register_endpoint is not None
        prepend_count = 2 if use_register else 1
        if len(flow.steps) + prepend_count > req.max_steps_per_flow:
            skip_reasons[flow_id] = "max_steps_exceeded"
            updated_flows.append(flow)
            continue

        prepended_steps: list[FlowStep] = []
        next_order = 1
        if use_register:
            prepended_steps.append(
                _build_auth_step(
                    register_endpoint,
                    next_order,
                    credentials,
                    step_id="auto_register",
                    name="Register test user (idempotent)",
                    soft=True,
                )
            )
            next_order += 1
        prepended_steps.append(
            _build_auth_step(
                login_endpoint,
                next_order,
                credentials,
                step_id="auto_login",
                name="Login test user",
            )
        )
        next_order += 1

        renumbered_existing = [
            step.model_copy(update={"order": next_order + index})
            for index, step in enumerate(flow.steps)
        ]
        updated_flows.append(
            flow.model_copy(update={"steps": [*prepended_steps, *renumbered_existing]})
        )
        injected += 1

    return updated_flows, injected, skip_reasons


def _inject_negative_step(
    flows: list[FlowScenario],
    parsed_api: ParsedAPI,
    req: FlowGenerateRequest,
) -> tuple[list[FlowScenario], int, str | None]:
    if not req.include_negative:
        return flows, 0, None
    if not flows:
        return flows, 0, "no_flows_available"

    target_flow_index: int | None = None
    for index, flow in enumerate(flows):
        if len(flow.steps) < req.max_steps_per_flow:
            target_flow_index = index
            break
    if target_flow_index is None:
        return flows, 0, "all_flows_at_max_steps"

    target_flow = flows[target_flow_index]
    next_order = len(target_flow.steps) + 1

    auth_candidates = sorted(
        [
            endpoint
            for endpoint in parsed_api.endpoints
            if endpoint.requires_auth or _is_auth_endpoint(endpoint)
        ],
        key=lambda endpoint: (
            0 if endpoint.requires_auth else 1,
            0 if {401, 403} & _endpoint_status_codes(endpoint) else 1,
            0 if endpoint.method == HttpMethod.GET else 1,
        ),
    )
    if auth_candidates:
        negative_step = _build_negative_auth_step(auth_candidates[0], req, next_order)
    else:
        validation_candidates = [
            endpoint
            for endpoint in parsed_api.endpoints
            if endpoint.method in {HttpMethod.POST, HttpMethod.PUT, HttpMethod.PATCH}
            and bool(endpoint.request_body_required_fields)
        ]
        if not validation_candidates:
            return flows, 0, "no_auth_or_validation_negative_pattern"
        negative_step = _build_negative_validation_step(validation_candidates[0], req, next_order)

    updated_flow = target_flow.model_copy(update={"steps": [*target_flow.steps, negative_step]})
    validation_errors = _flow_quality_errors(updated_flow, req)
    if validation_errors:
        return flows, 0, f"negative_step_invalid: {validation_errors[0]}"
    updated_flows = list(flows)
    updated_flows[target_flow_index] = updated_flow
    return updated_flows, 1, None


async def _llm_json_call(
    client: AsyncOpenAI,
    model: str,
    prompt: str,
    label: str,
) -> dict:
    base_prompt = prompt
    last_error: Exception | None = None

    for attempt in range(2):
        try:
            text = await complete_text(client, model, base_prompt)
            payload = _parse_json_response(text)
            return payload
        except Exception as exc:
            last_error = exc
            if attempt == 1:
                break
            base_prompt = (
                f"{prompt}\n\n"
                f"Repair instruction: your previous {label} output was invalid ({exc}). "
                "Return ONLY a valid JSON object following the required contract."
            )

    raise FlowGeneratorError(f"{label} failed after repair attempt: {last_error}")


async def _llm_plan_scenarios(
    client: AsyncOpenAI,
    parsed_api: ParsedAPI,
    req: FlowGenerateRequest,
    objectives: list[str],
    dependency_hints: list[dict],
) -> list[dict]:
    contract = {
        "scenarios": [
            {
                "name": "...",
                "description": "...",
                "persona": "...",
                "tags": ["..."],
                "objective": "...",
                "ordered_operations": [
                    {
                        "operation": "GET /items",
                        "reason": "...",
                    }
                ],
            }
        ]
    }

    prompt = "\n".join(
        [
            "You are planning realistic API user journeys.",
            "Output JSON only.",
            "Contract:",
            json.dumps(contract, ensure_ascii=True, indent=2),
            "Rules:",
            "- Use business-like journeys, not random endpoint lists.",
            "- Keep operations as HTTP method + normalized endpoint path.",
            "- Align with objectives and dependencies.",
            "- Return exactly max_flows scenarios when the API has enough distinct operations or objectives.",
            "- Do not stop after one obvious journey; split coverage by goal such as discovery, detail retrieval, create+verify, update+verify, auth+protected action, and negative-safe validation.",
            "- Do not plan one-step flows; every scenario must have at least two operations.",
            "- For auth/session scenarios, pair auth with a health check or a protected/read operation.",
            "- For any operation with a path parameter such as /items/{id}, include an earlier producer operation that creates, lists, or otherwise extracts that id.",
            "- Keep total scenarios <= max_flows.",
            "Objectives:",
            json.dumps(objectives, ensure_ascii=True),
            "Request preferences:",
            f"target_scenario_count={req.max_flows}",
            f"max_flows={req.max_flows}",
            f"max_steps_per_flow={req.max_steps_per_flow}",
            f"mutation_policy={req.mutation_policy.value}",
            f"personas={json.dumps(req.personas, ensure_ascii=True)}",
            f"app_context={json.dumps(req.app_context, ensure_ascii=True)}",
            "Dependency hints:",
            json.dumps(dependency_hints, ensure_ascii=True, indent=2),
            "API context:",
            _build_api_context(parsed_api),
        ]
    )

    payload = await _llm_json_call(client, FLOW_PLANNER_MODEL, prompt, "scenario planner")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise FlowGeneratorError("scenario planner returned no scenarios")
    return [item for item in scenarios if isinstance(item, dict)]


async def _llm_compose_flows(
    client: AsyncOpenAI,
    parsed_api: ParsedAPI,
    req: FlowGenerateRequest,
    objectives: list[str],
    seed_flows: list[FlowScenario],
    scenarios: list[dict],
    dependency_hints: list[dict],
) -> tuple[list[FlowScenario], int]:
    flow_contract = {
        "flows": [
            {
                "name": "...",
                "description": "...",
                "persona": "...",
                "preconditions": ["..."],
                "tags": ["..."],
                "steps": [
                    {
                        "step_id": "...",
                        "order": 1,
                        "name": "...",
                        "method": "GET",
                        "endpoint": "/...",
                        "headers": {"Authorization": "Bearer {{ctx.auth_token}}"},
                        "query_params": {},
                        "path_params": {},
                        "body": None,
                        "extract": [{"var": "item_id", "from": "body", "path": "id", "required": True}],
                        "assertions": [{"field": "status_code", "operator": "eq", "expected": 200}],
                        "expected_status": 200,
                        "required": True,
                    }
                ],
            }
        ]
    }

    prompt = "\n".join(
        [
            "You are composing executable flow scenarios from API journey plans.",
            "Output JSON only.",
            "Contract:",
            json.dumps(flow_contract, ensure_ascii=True, indent=2),
            "Hard rules:",
            "- Keep only HTTP steps.",
            "- Endpoint must be normalized relative path.",
            "- Keep endpoint path templates exactly as declared in the OpenAPI spec.",
            "- Never place {{ctx.var}} directly inside endpoint strings.",
            "- Put dynamic path values only in path_params, for example endpoint=/booking/{id} and path_params={\"id\": \"{{ctx.booking_id}}\"}.",
            "- Use {{ctx.var}} for dependencies.",
            "- Every mutating flow should include a verification read step.",
            "- Respect mutation policy and max steps.",
            "- Create one executable flow per planner scenario whenever possible.",
            "- Return up to max_flows flows and aim for exactly max_flows when enough distinct scenarios are provided.",
            "- Do not merge separate workflows into one large flow; breadth of valid flows is more important than one long flow.",
            "- Short complete flows with 2-4 steps are acceptable when they cover a clear journey.",
            "- Never return a one-step flow.",
            "- For auth/session flows, use at least two steps such as GET /ping then POST /auth, or login then a protected/read endpoint.",
            "- For any step using path_params with {{ctx.*}}, an earlier step in the same flow must extract that exact ctx variable.",
            "- For update/delete/detail paths with {id}, add a prior create or list step that extracts the id, then reuse it through path_params.",
            "Request preferences:",
            f"target_flow_count={req.max_flows}",
            f"max_flows={req.max_flows}",
            f"max_steps_per_flow={req.max_steps_per_flow}",
            f"mutation_policy={req.mutation_policy.value}",
            f"personas={json.dumps(req.personas, ensure_ascii=True)}",
            f"app_context={json.dumps(req.app_context, ensure_ascii=True)}",
            "Objectives:",
            json.dumps(objectives, ensure_ascii=True),
            "Scenarios from planner:",
            json.dumps(scenarios, ensure_ascii=True, indent=2),
            "Deterministic seed flows:",
            json.dumps([flow.model_dump(mode="json", by_alias=True) for flow in seed_flows], ensure_ascii=True, indent=2),
            "Dependency hints:",
            json.dumps(dependency_hints, ensure_ascii=True, indent=2),
            "API context:",
            _build_api_context(parsed_api),
        ]
    )

    payload = await _llm_json_call(client, FLOW_COMPOSER_MODEL, prompt, "flow composer")
    raw_flows = payload.get("flows")
    if not isinstance(raw_flows, list):
        raise FlowGeneratorError("flow composer output must contain a 'flows' array")

    validated: list[FlowScenario] = []
    total_normalizations = 0
    for item in raw_flows:
        if not isinstance(item, dict):
            continue
        normalized_item, normalizations = _normalize_llm_flow_payload(item, parsed_api)
        total_normalizations += normalizations
        validated.append(FlowScenario.model_validate(normalized_item))

    if not validated:
        raise FlowGeneratorError("flow composer returned no valid flows")
    return validated, total_normalizations


async def _llm_critic_repair(
    client: AsyncOpenAI,
    parsed_api: ParsedAPI,
    req: FlowGenerateRequest,
    flows: list[FlowScenario],
) -> tuple[list[FlowScenario], int]:
    contract = {
        "flows": [
            {
                "name": "...",
                "description": "...",
                "persona": "...",
                "preconditions": ["..."],
                "tags": ["..."],
                "steps": [
                    {
                        "step_id": "...",
                        "order": 1,
                        "name": "...",
                        "method": "GET",
                        "endpoint": "/...",
                        "headers": {},
                        "query_params": {},
                        "path_params": {},
                        "body": None,
                        "extract": [],
                        "assertions": [],
                        "expected_status": 200,
                        "required": True,
                    }
                ],
            }
        ]
    }

    prompt = "\n".join(
        [
            "You are a strict API flow quality critic.",
            "Output JSON only.",
            "Contract:",
            json.dumps(contract, ensure_ascii=True, indent=2),
            "Review and repair flows to satisfy:",
            "- No unresolved path params.",
            "- Endpoint strings must keep OpenAPI path templates; move dynamic values into path_params.",
            "- No broken ctx variable dependencies.",
            "- Ordered state progression (extract -> reuse -> verify).",
            "- Mutating flows include read verification.",
            f"- Respect mutation_policy={req.mutation_policy.value}.",
            f"- Preserve as many distinct valid flows as possible, up to max_flows={req.max_flows}.",
            "- Repair weak flows instead of collapsing the batch to one flow when several distinct journeys can be valid.",
            "- Repair one-step flows by adding a compatible health check, auth, list, detail, or verification step.",
            "- Repair missing id dependencies by adding an earlier create/list step that extracts the id, or by removing the invalid dependent step.",
            "- Keep one representative only for true duplicates; otherwise maintain coverage diversity.",
            "Candidate flows:",
            json.dumps([flow.model_dump(mode="json", by_alias=True) for flow in flows], ensure_ascii=True, indent=2),
        ]
    )

    payload = await _llm_json_call(client, FLOW_CRITIC_MODEL, prompt, "flow critic")
    raw_flows = payload.get("flows")
    if not isinstance(raw_flows, list):
        raise FlowGeneratorError("flow critic output must contain a 'flows' array")

    validated: list[FlowScenario] = []
    total_normalizations = 0
    for item in raw_flows:
        if not isinstance(item, dict):
            continue
        normalized_item, normalizations = _normalize_llm_flow_payload(item, parsed_api)
        total_normalizations += normalizations
        validated.append(FlowScenario.model_validate(normalized_item))

    if not validated:
        raise FlowGeneratorError("flow critic returned no valid flows")

    return validated, total_normalizations


def _pure_llm_candidate_limit(req: FlowGenerateRequest) -> int:
    return max(req.max_flows + 2, min(req.max_flows * 3, 24))


async def _llm_generate_candidate_flows(
    client: AsyncOpenAI,
    parsed_api: ParsedAPI,
    req: FlowGenerateRequest,
    objectives: list[str],
    dependency_hints: list[dict],
) -> tuple[list[FlowScenario], int, list[FlowEliminatedCandidate]]:
    candidate_limit = _pure_llm_candidate_limit(req)
    flow_contract = {
        "flows": [
            {
                "name": "...",
                "description": "...",
                "persona": "...",
                "preconditions": ["..."],
                "tags": ["..."],
                "steps": [
                    {
                        "step_id": "...",
                        "order": 1,
                        "name": "...",
                        "method": "GET",
                        "endpoint": "/...",
                        "headers": {"Authorization": "Bearer {{ctx.auth_token}}"},
                        "query_params": {},
                        "path_params": {},
                        "body": None,
                        "extract": [{"var": "item_id", "from": "body", "path": "id", "required": True}],
                        "assertions": [{"field": "status_code", "operator": "eq", "expected": 200}],
                        "expected_status": 200,
                        "required": True,
                    }
                ],
            }
        ]
    }

    prompt = "\n".join(
        [
            "You are generating executable API flow tests directly from parsed OpenAPI context.",
            "Output JSON only.",
            "Contract:",
            json.dumps(flow_contract, ensure_ascii=True, indent=2),
            "Hard rules:",
            "- Use only operations that exist in the API context.",
            "- Endpoint must be a normalized relative path, never a full URL.",
            "- Keep endpoint path templates exactly as declared in the OpenAPI spec.",
            "- Never place {{ctx.var}} directly inside endpoint strings.",
            "- Put dynamic path values only in path_params, for example endpoint=/booking/{id} and path_params={\"id\": \"{{ctx.booking_id}}\"}.",
            "- Use {{ctx.var}} only when an earlier step extracts that variable or it exists in app_context.",
            "- Prefer distinct workflows with 3+ steps when the API supports them.",
            "- Return exactly candidate_limit candidate flows when the API supports enough distinct journeys.",
            "- candidate_limit is the requested candidate count, not the final saved count; max_flows is only the final accepted trim after review.",
            "- Do not stop after one valid flow.",
            "- Prefer breadth: several small complete flows are better than one broad flow.",
            "- Never return a one-step flow; every candidate must have at least two executable steps.",
            "- For auth/session candidates, pair auth with a health check or protected/read operation.",
            "- For every {{ctx.*}} value in path_params, headers, query, or body, an earlier step must extract that exact variable.",
            "- For {id} paths, first create or list the resource and extract the id, then use it in path_params.",
            "- Every mutating flow should include a verification read step when possible.",
            "- Under safe mutation policy, keep only minimal business mutations; authentication should be separate from business write steps.",
            "- Do not invent auth tokens or body fields that are not supported by response content types or examples.",
            "- Respect mutation policy, include_negative separately, and max steps.",
            "Request preferences:",
            f"candidate_limit={candidate_limit}",
            f"accepted_flow_target={req.max_flows}",
            f"max_steps_per_flow={req.max_steps_per_flow}",
            f"mutation_policy={req.mutation_policy.value}",
            f"personas={json.dumps(req.personas, ensure_ascii=True)}",
            f"app_context={json.dumps(req.app_context, ensure_ascii=True)}",
            "Diversity goals:",
            json.dumps(objectives, ensure_ascii=True),
            "Dependency hints:",
            json.dumps(dependency_hints, ensure_ascii=True, indent=2),
            "API context:",
            _build_api_context(parsed_api),
        ]
    )

    payload = await _llm_json_call(client, FLOW_COMPOSER_MODEL, prompt, "pure llm flow generator")
    raw_flows = payload.get("flows")
    if not isinstance(raw_flows, list):
        raise FlowGeneratorError("pure llm generator output must contain a 'flows' array")

    validated: list[FlowScenario] = []
    schema_invalid: list[FlowEliminatedCandidate] = []
    total_normalizations = 0
    for index, item in enumerate(raw_flows, start=1):
        if not isinstance(item, dict):
            schema_invalid.append(
                FlowEliminatedCandidate(
                    name=f"Candidate {index}",
                    reason_code="schema_invalid",
                    reason="candidate flow must be a JSON object",
                )
            )
            continue
        normalized_item, normalizations = _normalize_llm_flow_payload(item, parsed_api)
        total_normalizations += normalizations
        try:
            validated.append(FlowScenario.model_validate(normalized_item))
        except ValidationError as exc:
            schema_invalid.append(
                FlowEliminatedCandidate(
                    name=str(item.get("name") or f"Candidate {index}"),
                    reason_code="schema_invalid",
                    reason=str(exc.errors()[0].get("msg") or "candidate failed schema validation"),
                )
            )

    if not validated and not schema_invalid:
        raise FlowGeneratorError("pure llm generator returned no valid flows")

    return validated, total_normalizations, schema_invalid


async def _llm_review_candidates(
    client: AsyncOpenAI,
    parsed_api: ParsedAPI,
    req: FlowGenerateRequest,
    flows: list[tuple[str, FlowScenario]],
) -> dict[str, _FlowReviewDecision]:
    contract = {
        "decisions": [
            {
                "candidate_id": "candidate_1",
                "keep": True,
                "reason_code": "accepted",
                "reason": "brief explanation",
            }
        ]
    }

    candidates_payload = [
        {
            "candidate_id": candidate_id,
            "flow": flow.model_dump(mode="json", by_alias=True),
        }
        for candidate_id, flow in flows
    ]
    prompt = "\n".join(
        [
            "You are a strict reviewer for generated API flow tests.",
            "Output JSON only.",
            "Contract:",
            json.dumps(contract, ensure_ascii=True, indent=2),
            "Reject a candidate when it has broken dependencies, impossible extractions, unsupported auth/token assumptions, duplicate behavior, unknown endpoints, or incoherent state progression.",
            "Do not reject solely because an Authorization header uses a Bearer token template when the OpenAPI spec does not define the exact auth scheme.",
            "If a flow extracts auth_token from an auth step and a protected step sends {{ctx.auth_token}} through Authorization and/or Cookie, treat that as a supported token propagation pattern unless another concrete issue exists.",
            "Keep the reason concise and actionable.",
            "Request preferences:",
            f"mutation_policy={req.mutation_policy.value}",
            f"max_steps_per_flow={req.max_steps_per_flow}",
            "API context:",
            _build_api_context(parsed_api),
            "Candidate flows:",
            json.dumps(candidates_payload, ensure_ascii=True, indent=2),
        ]
    )

    payload = await _llm_json_call(client, FLOW_REVIEWER_MODEL, prompt, "flow reviewer")
    envelope = _FlowReviewEnvelope.model_validate(payload)
    return {decision.candidate_id: decision for decision in envelope.decisions}


async def _review_candidate_flows(
    parsed_api: ParsedAPI,
    req: FlowGenerateRequest,
    flows: list[FlowScenario],
    initial_eliminated: list[FlowEliminatedCandidate] | None = None,
) -> tuple[list[FlowScenario], list[FlowEliminatedCandidate], bool]:
    endpoint_map = _endpoint_lookup(parsed_api)
    seen_signatures: set[tuple[str, ...]] = set()
    reviewable: list[tuple[str, FlowScenario]] = []
    eliminated: list[FlowEliminatedCandidate] = list(initial_eliminated or [])

    for index, flow in enumerate(flows, start=1):
        reasons = _static_review_flow(flow, parsed_api, req, endpoint_map, seen_signatures)
        if reasons:
            reason_code, reason = _summarize_reasons(reasons)
            eliminated.append(
                FlowEliminatedCandidate(
                    name=flow.name or f"Candidate {index}",
                    reason_code=reason_code,
                    reason=reason,
                )
            )
            continue
        seen_signatures.add(_flow_signature(flow))
        reviewable.append((f"candidate_{index}", flow))

    if not reviewable:
        return [], eliminated, False

    api_key = _get_api_key()
    if not api_key:
        raise FlowGeneratorError("reviewer_missing_openrouter_api_key")

    client = get_client(api_key)
    decisions = await _llm_review_candidates(client, parsed_api, req, reviewable)

    accepted: list[FlowScenario] = []
    soft_rejected: list[tuple[FlowScenario, _FlowReviewDecision]] = []
    for candidate_id, flow in reviewable:
        decision = decisions.get(candidate_id)
        if decision is None:
            eliminated.append(
                FlowEliminatedCandidate(
                    name=flow.name,
                    reason_code="reviewer_missing_decision",
                    reason="reviewer returned no decision for this candidate",
                )
            )
            continue
        if decision.keep:
            accepted.append(flow)
            continue
        soft_rejected.append((flow, decision))

    for flow, decision in soft_rejected:
        if len(accepted) < req.max_flows:
            accepted.append(flow)
            continue
        eliminated.append(
            FlowEliminatedCandidate(
                name=flow.name,
                reason_code=decision.reason_code or "reviewer_rejected",
                reason=decision.reason or "reviewer rejected this candidate",
            )
        )

    return accepted[: req.max_flows], eliminated, True


async def _llm_refine_flows(
    parsed_api: ParsedAPI,
    req: FlowGenerateRequest,
    seed_flows: list[FlowScenario],
    dependency_hints: list[dict],
    objectives: list[str],
) -> tuple[list[FlowScenario], int]:
    api_key = _get_api_key()
    if not api_key:
        raise FlowGeneratorError("OPENROUTER_API_KEY is not set")

    client = get_client(api_key)

    try:
        scenarios = await _llm_plan_scenarios(client, parsed_api, req, objectives, dependency_hints)
        composed, compose_normalizations = await _llm_compose_flows(
            client,
            parsed_api,
            req,
            objectives,
            seed_flows,
            scenarios,
            dependency_hints,
        )
        criticized, critic_normalizations = await _llm_critic_repair(client, parsed_api, req, composed)
        return criticized, compose_normalizations + critic_normalizations
    except openai.APIError as exc:
        raise FlowGeneratorError(f"flow planner upstream error: {exc}") from exc
    except Exception as exc:
        raise FlowGeneratorError(f"flow planner error: {exc}") from exc


async def generate_flows(
    parsed_api: ParsedAPI,
    req: FlowGenerateRequest,
    flow_generation_id: str,
) -> tuple[list[FlowScenario], dict]:
    objectives = _infer_objectives(parsed_api, req)
    io_map = _build_endpoint_io(parsed_api.endpoints)
    dependency_hints = _build_dependency_hints(parsed_api, io_map)

    deterministic_flows = _build_seed_flows(parsed_api, req, objectives, dependency_hints)
    deterministic_flows, deterministic_dropped = _quality_filter(deterministic_flows, req)

    source = "deterministic_fallback"
    fallback_reason = "deterministic_only"
    candidate_flows: list[FlowScenario] = deterministic_flows
    llm_attempted = False
    llm_normalizations_applied = 0
    llm_deterministic_backfill_count = 0
    candidate_flows_reviewed = 0
    reviewer_applied = False
    reviewer_mode: str | None = None
    eliminated_flows: list[FlowEliminatedCandidate] = []

    mode = req.generation_mode
    api_key_present = bool(_get_api_key())
    created_at = datetime.utcnow()

    if mode == FlowGenerationMode.PURE_LLM:
        llm_attempted = api_key_present
        source = "pure_llm"
        candidate_flows = []
        fallback_reason = ""
        if not api_key_present:
            fallback_reason = "missing_openrouter_api_key"
        else:
            try:
                client = get_client(_get_api_key())
                generated_flows, normalization_count, schema_invalid = await _llm_generate_candidate_flows(
                    client,
                    parsed_api,
                    req,
                    objectives,
                    dependency_hints,
                )
                llm_normalizations_applied = normalization_count
                generated_flows = _repair_candidate_flows_static(generated_flows, parsed_api, req)
                candidate_flows_reviewed = len(generated_flows) + len(schema_invalid)
                candidate_flows, eliminated_flows, reviewer_applied = await _review_candidate_flows(
                    parsed_api,
                    req,
                    generated_flows,
                    schema_invalid,
                )
                reviewer_mode = "static_llm" if reviewer_applied else None
                if not candidate_flows:
                    fallback_reason = "pure_llm_reviewer_rejected_all_candidates"
            except Exception as exc:
                logger.warning("flow.generate.pure_llm_failed reason=%s", exc)
                fallback_reason = str(exc)
        finalized = _finalize_flows(candidate_flows, req, flow_generation_id, created_at)
    else:
        if mode == FlowGenerationMode.DETERMINISTIC_FIRST:
            llm_should_run = False
        elif mode == FlowGenerationMode.HYBRID_AUTO:
            llm_should_run = api_key_present
            fallback_reason = ""
        else:  # LLM_FIRST
            llm_should_run = api_key_present
            fallback_reason = "" if api_key_present else "missing_openrouter_api_key"

        if llm_should_run and api_key_present:
            llm_attempted = True
            try:
                refined_flows, normalization_count = await _llm_refine_flows(
                    parsed_api,
                    req,
                    deterministic_flows,
                    dependency_hints,
                    objectives,
                )
                llm_normalizations_applied = normalization_count
                refined_flows = _repair_candidate_flows_static(refined_flows, parsed_api, req)
                candidate_flows_reviewed = len(refined_flows)
                reviewed_flows, eliminated_flows, reviewer_applied = await _review_candidate_flows(
                    parsed_api,
                    req,
                    refined_flows,
                )
                reviewer_mode = "static_llm" if reviewer_applied else None
                if reviewed_flows:
                    candidate_flows, llm_deterministic_backfill_count = _merge_distinct_flows(
                        reviewed_flows,
                        deterministic_flows,
                        req,
                    )
                    source = "llm_refined"
                    fallback_reason = ""
                else:
                    fallback_reason = "llm_candidates_eliminated_by_reviewer"
            except Exception as exc:
                logger.warning("flow.generate.llm_fallback reason=%s", exc)
                fallback_reason = str(exc)
        elif mode == FlowGenerationMode.LLM_FIRST and not api_key_present:
            logger.warning("flow.generate.llm_first_without_key")

        finalized = _finalize_flows(candidate_flows, req, flow_generation_id, created_at)

        if source != "llm_refined" and not finalized and deterministic_flows:
            finalized = _finalize_flows(deterministic_flows, req, flow_generation_id, created_at)
            source = "deterministic_fallback"
            if not fallback_reason:
                fallback_reason = "empty_llm_output"

        if source != "llm_refined" and not finalized:
            fallback_seed = _build_seed_flows(parsed_api, req, ["core api workflow"], dependency_hints)
            fallback_seed, _dropped = _quality_filter(fallback_seed, req)
            finalized = _finalize_flows(fallback_seed, req, flow_generation_id, created_at)
            source = "deterministic_fallback"
            if not fallback_reason:
                fallback_reason = "quality_filter_removed_all_flows"

    login_flows_prepended = 0
    login_prepend_skip_reasons: dict[str, str] = {}
    if finalized:
        finalized, login_flows_prepended, login_prepend_skip_reasons = _inject_login_prepend(
            finalized,
            parsed_api,
            req,
        )

    negative_flows_added = 0
    negative_generation_skipped_reason: str | None = None
    if finalized:
        finalized, negative_flows_added, negative_generation_skipped_reason = _inject_negative_step(
            finalized,
            parsed_api,
            req,
        )

    fallback_used = False
    if mode in {FlowGenerationMode.LLM_FIRST, FlowGenerationMode.HYBRID_AUTO}:
        fallback_used = source != "llm_refined"

    summary = {
        "flows_generated": len(finalized),
        "source": source,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "dependency_hints_count": len(dependency_hints),
        "openapi_link_hints_count": sum(1 for hint in dependency_hints if hint.get("kind") == "openapi_link"),
        "objectives_used": objectives,
        "generation_mode": req.generation_mode.value,
        "mutation_policy": req.mutation_policy.value,
        "deterministic_quality_dropped": len(deterministic_dropped),
        "llm_attempted": llm_attempted,
        "llm_normalizations_applied": llm_normalizations_applied,
        "llm_deterministic_backfill_count": llm_deterministic_backfill_count,
        "candidate_flows_reviewed": candidate_flows_reviewed,
        "eliminated_flows_count": len(eliminated_flows),
        "eliminated_flows": [item.model_dump() for item in eliminated_flows],
        "reviewer_applied": reviewer_applied,
        "reviewer_mode": reviewer_mode,
        "negative_flows_added": negative_flows_added,
        "negative_generation_skipped_reason": negative_generation_skipped_reason,
        "login_flows_prepended": login_flows_prepended,
        "login_prepend_skip_reasons": login_prepend_skip_reasons,
        "batch_created_at": created_at.isoformat(),
    }
    return finalized, summary


__all__ = ["generate_flows", "_build_dependency_hints", "_infer_objectives"]

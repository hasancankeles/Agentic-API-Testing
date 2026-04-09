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

from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel, Field, ValidationError

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

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
FLOW_PLANNER_MODEL = os.getenv("FLOW_PLANNER_MODEL", "gemini-3.1-flash-lite-preview")
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
_DEFAULT_EXTERNAL_CTX_VARS = {"run_id", "timestamp", *_AUTH_CONTEXT_VARS}


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


def _get_gemini_api_key() -> str:
    return os.getenv("GEMINI_API_KEY", GEMINI_API_KEY).strip()


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

    for parameter in endpoint.parameters:
        if parameter.location == "header" and parameter.name.lower() in {"authorization", "api_key", "x-api-key"}:
            return True

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

    required_fields = endpoint.request_body_required_fields or []
    for field in required_fields:
        if field in body:
            continue

        lowered = field.lower()
        if lowered in {"username", "user_name", "email"}:
            body[field] = "demo_user"
        elif lowered in {"password", "pass", "secret"}:
            body[field] = "demo_pass"
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
        rules.append(FlowExtractRule(var=resource_id_var, source="body", path="id", required=False))
        rules.append(FlowExtractRule(var=resource_id_var, source="body", path="0.id", required=False))
        rules.append(FlowExtractRule(var=resource_id_var, source="headers", path="location", required=False))

    for produced in sorted(io_meta.produced_vars):
        if produced in {"id", resource_id_var, "auth_token", "access_token", "refresh_token", "location"}:
            continue
        if produced.endswith("id"):
            rules.append(FlowExtractRule(var=produced, source="body", path=produced, required=False))
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
    for parameter in endpoint.parameters:
        if parameter.location != "header":
            continue
        lowered = parameter.name.lower()
        if lowered in {"authorization", "api_key", "x-api-key"}:
            if lowered == "authorization":
                if not io_meta.is_auth:
                    headers[parameter.name] = "Bearer {{ctx.auth_token}}"
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
            if step.method in mutating_methods:
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
    header_names = {str(key).lower() for key in step.headers.keys()}
    return bool({"authorization", "x-api-key", "api_key"} & header_names)


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

        consumed = set()
        consumed.update(_extract_ctx_vars(step.endpoint))
        consumed.update(_extract_ctx_vars(step.path_params))
        consumed.update(_extract_ctx_vars(step.query_params))
        consumed.update(_extract_ctx_vars(step.headers))
        consumed.update(_extract_ctx_vars(step.body))

        missing_vars = sorted(consumed - known_vars)
        if missing_vars:
            errors.append(f"step {step.step_id}: missing context vars {missing_vars}")

        produced = {rule.var for rule in step.extract}
        known_vars.update(produced)

    mutating_methods = {HttpMethod.POST, HttpMethod.PUT, HttpMethod.PATCH, HttpMethod.DELETE}
    business_mutations = [
        step
        for step in sorted_steps
        if step.method in mutating_methods and not _flow_step_is_auth_like(step)
    ]
    has_mutation = bool(business_mutations)
    if has_mutation and not any(step.method == HttpMethod.GET for step in sorted_steps):
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

    if len(flow.steps) < 2:
        reasons.append(("too_short", "flow must contain at least two executable steps"))

    signature = _flow_signature(flow)
    if signature in seen_signatures:
        reasons.append(("duplicate_flow", "flow duplicates an existing candidate signature"))

    for error in _flow_quality_errors(flow, req):
        reasons.append((_reason_code_for_quality_error(error), error))

    for step in flow.steps:
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

        for rule in step.extract:
            if rule.source.value == "body" and not _response_supports_body_path(endpoint, rule.path):
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
        used_keys: set[str] = set()

        if (
            auth_endpoint is not None
            and auth_endpoint != chosen
            and (_build_endpoint_io([chosen])[_endpoint_key(chosen)].consumed_vars & _AUTH_CONTEXT_VARS)
        ):
            chain.append(auth_endpoint)
            used_keys.add(_endpoint_key(auth_endpoint))
            available_vars.update(_build_endpoint_io([auth_endpoint])[_endpoint_key(auth_endpoint)].produced_vars)

        chain.append(chosen)
        used_keys.add(_endpoint_key(chosen))
        available_vars.update(io_map[_endpoint_key(chosen)].produced_vars)

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
        if len(steps) < 2:
            continue

        # Normalize ordering after mutation pruning.
        normalized_steps = [step.model_copy(update={"order": idx}) for idx, step in enumerate(steps, start=1)]

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
            steps=[
                step.model_copy(update={"order": idx}) for idx, step in enumerate(generic_steps, start=1)
            ],
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
    client: genai.Client,
    model: str,
    prompt: str,
    label: str,
) -> dict:
    base_prompt = prompt
    last_error: Exception | None = None

    for attempt in range(2):
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=base_prompt,
            )
            payload = _parse_json_response(response.text or "")
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
    client: genai.Client,
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
            "- Keep total scenarios <= max_flows.",
            "Objectives:",
            json.dumps(objectives, ensure_ascii=True),
            "Request preferences:",
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
    client: genai.Client,
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
            "Request preferences:",
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
    client: genai.Client,
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
    return max(req.max_flows, min(req.max_flows * 2, 24))


async def _llm_generate_candidate_flows(
    client: genai.Client,
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
            "- Every mutating flow should include a verification read step when possible.",
            "- Under safe mutation policy, keep only minimal business mutations; authentication should be separate from business write steps.",
            "- Do not invent auth tokens or body fields that are not supported by response content types or examples.",
            "- Respect mutation policy, include_negative separately, and max steps.",
            "Request preferences:",
            f"candidate_limit={candidate_limit}",
            f"max_flows={req.max_flows}",
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
    client: genai.Client,
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

    api_key = _get_gemini_api_key()
    if not api_key:
        raise FlowGeneratorError("reviewer_missing_gemini_api_key")

    client = genai.Client(api_key=api_key)
    decisions = await _llm_review_candidates(client, parsed_api, req, reviewable)

    accepted: list[FlowScenario] = []
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
    api_key = _get_gemini_api_key()
    if not api_key:
        raise FlowGeneratorError("GEMINI_API_KEY is not set")

    client = genai.Client(api_key=api_key)

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
    except genai_errors.APIError as exc:
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
    candidate_flows_reviewed = 0
    reviewer_applied = False
    reviewer_mode: str | None = None
    eliminated_flows: list[FlowEliminatedCandidate] = []

    mode = req.generation_mode
    api_key_present = bool(_get_gemini_api_key())
    created_at = datetime.utcnow()

    if mode == FlowGenerationMode.PURE_LLM:
        llm_attempted = api_key_present
        source = "pure_llm"
        candidate_flows = []
        fallback_reason = ""
        if not api_key_present:
            fallback_reason = "missing_gemini_api_key"
        else:
            try:
                client = genai.Client(api_key=_get_gemini_api_key())
                generated_flows, normalization_count, schema_invalid = await _llm_generate_candidate_flows(
                    client,
                    parsed_api,
                    req,
                    objectives,
                    dependency_hints,
                )
                llm_normalizations_applied = normalization_count
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
            fallback_reason = "" if api_key_present else "missing_gemini_api_key"

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
                candidate_flows_reviewed = len(refined_flows)
                reviewed_flows, eliminated_flows, reviewer_applied = await _review_candidate_flows(
                    parsed_api,
                    req,
                    refined_flows,
                )
                reviewer_mode = "static_llm" if reviewer_applied else None
                if reviewed_flows:
                    candidate_flows = reviewed_flows
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
        "candidate_flows_reviewed": candidate_flows_reviewed,
        "eliminated_flows_count": len(eliminated_flows),
        "eliminated_flows": [item.model_dump() for item in eliminated_flows],
        "reviewer_applied": reviewer_applied,
        "reviewer_mode": reviewer_mode,
        "negative_flows_added": negative_flows_added,
        "negative_generation_skipped_reason": negative_generation_skipped_reason,
        "batch_created_at": created_at.isoformat(),
    }
    return finalized, summary


__all__ = ["generate_flows", "_build_dependency_hints", "_infer_objectives"]

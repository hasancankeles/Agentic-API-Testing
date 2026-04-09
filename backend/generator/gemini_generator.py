from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar
from urllib.parse import urlparse

from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel, Field, StrictInt, StrictStr, ValidationError, field_validator

from models.schemas import (
    GenerationMeta,
    HttpMethod,
    LoadTestScenario,
    ParsedAPI,
    PlannerLoadScenarioDraft,
    PlannerTestCaseDraft,
    PlannerTestPlan,
    TestAssertion,
    TestCase,
    TestCategory,
    TestSuite,
    WebSocketStep,
    WebSocketTestCase,
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_PLANNER_MODEL = os.getenv("GEMINI_PLANNER_MODEL", "gemini-3.1-pro-preview")
GEMINI_EXECUTOR_MODEL = os.getenv("GEMINI_EXECUTOR_MODEL", "gemini-3.1-flash-lite-preview")
logger = logging.getLogger("agentic.generator")


def _get_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


GENERATOR_REPAIR_MAX_ATTEMPTS = _get_env_int("GENERATOR_REPAIR_MAX_ATTEMPTS", 1)
GENERATOR_EXECUTOR_CONCURRENCY = max(1, min(_get_env_int("GENERATOR_EXECUTOR_CONCURRENCY", 8), 32))
GEN_DEBUG_ARTIFACTS = _get_env_bool("GEN_DEBUG_ARTIFACTS", True)
GEN_CAPTURE_RAW_LLM = _get_env_bool("GEN_CAPTURE_RAW_LLM", False)

SENSITIVE_KEY_MARKERS = (
    "authorization",
    "token",
    "api_key",
    "apikey",
    "password",
    "cookie",
    "secret",
    "x-api-key",
)


def _is_sensitive_key(key: object) -> bool:
    lowered = str(key).strip().lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def _sanitize_for_debug(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                sanitized[str(key)] = "[REDACTED]"
            else:
                sanitized[str(key)] = _sanitize_for_debug(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_for_debug(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_for_debug(item) for item in value]
    return value


def _sanitize_raw_text(raw_text: str) -> str:
    text = (raw_text or "").strip()
    if len(text) > 20_000:
        text = f"{text[:20_000]}\n...[truncated]"
    if not text:
        return text
    try:
        payload = _parse_json_response(text)
        return json.dumps(_sanitize_for_debug(payload), ensure_ascii=True)
    except Exception:
        pattern = re.compile(
            r'(?i)("?(authorization|token|api[_-]?key|password|cookie|secret)"?\s*[:=]\s*")([^"]*)(")'
        )
        return pattern.sub(r"\1[REDACTED]\4", text)


class GenerationPipelineError(Exception):
    """Base exception for generation pipeline failures."""


class StructuredOutputError(GenerationPipelineError):
    """Raised when planner/executor output cannot be validated after repair attempts."""

    def __init__(self, stage: str, errors: list[dict[str, Any]], repair_attempted: bool):
        super().__init__(f"{stage} produced invalid structured output")
        self.stage = stage
        self.errors = errors
        self.repair_attempted = repair_attempted


class UpstreamModelError(GenerationPipelineError):
    """Raised for upstream LLM API/network failures."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class ExecutorTestEnrichment(BaseModel):
    case_id: StrictStr
    endpoint: StrictStr | None = None
    method: HttpMethod | None = None
    headers: dict[str, StrictStr] = Field(default_factory=dict)
    query_params: dict[str, StrictStr] = Field(default_factory=dict)
    path_params: dict[str, StrictStr] = Field(default_factory=dict)
    body: Any = None
    expected_status: StrictInt | None = None
    assertions: list[TestAssertion] = Field(default_factory=list)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, endpoint: str | None) -> str | None:
        if endpoint is None:
            return None
        if not endpoint.startswith("/") or "://" in endpoint or any(ch.isspace() for ch in endpoint):
            raise ValueError("endpoint must be a normalized path")
        return endpoint

    @field_validator("expected_status")
    @classmethod
    def validate_expected_status(cls, expected_status: int | None) -> int | None:
        if expected_status is None:
            return None
        if expected_status < 100 or expected_status > 599:
            raise ValueError("expected_status must be between 100 and 599")
        return expected_status


class ExecutorLoadEnrichment(BaseModel):
    scenario_id: StrictStr
    target_url: StrictStr | None = None
    method: HttpMethod | None = None
    vus: StrictInt | None = None
    duration: StrictStr | None = None
    ramp_stages: list[dict[str, Any]] = Field(default_factory=list)
    thresholds: dict[str, list[StrictStr]] = Field(default_factory=dict)
    headers: dict[str, StrictStr] = Field(default_factory=dict)

    @field_validator("vus")
    @classmethod
    def validate_vus(cls, vus: int | None) -> int | None:
        if vus is None:
            return None
        if vus <= 0:
            raise ValueError("vus must be > 0")
        return vus


class ExecutorWebSocketDraft(BaseModel):
    suite_id: StrictStr
    name: StrictStr
    description: str = ""
    url: StrictStr = "ws://localhost:8080/"
    steps: list[WebSocketStep] = Field(default_factory=list)


class ExecutorOutput(BaseModel):
    test_enrichments: list[ExecutorTestEnrichment] = Field(default_factory=list)
    load_enrichments: list[ExecutorLoadEnrichment] = Field(default_factory=list)
    websocket_tests: list[ExecutorWebSocketDraft] = Field(default_factory=list)


@dataclass
class HttpExecutorJob:
    case_id: str
    draft: PlannerTestCaseDraft


@dataclass
class HttpExecutorQueueResult:
    enrichments: dict[str, ExecutorTestEnrichment] = field(default_factory=dict)
    case_outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)
    jobs_total: int = 0
    jobs_succeeded: int = 0
    jobs_failed: int = 0
    fallback_count: int = 0
    repair_attempted: bool = False
    max_in_flight: int = 0


@dataclass
class GenerationDebugCapture:
    generation_id: str
    parsed_api_id: str | None = None
    parsed_api_title: str = ""
    categories: list[str] = field(default_factory=list)
    capture_raw_llm: bool = False
    planner_plan: dict[str, Any] = field(default_factory=dict)
    executor_case_outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)
    fallback_case_ids: list[str] = field(default_factory=list)
    final_suites: list[dict[str, Any]] = field(default_factory=list)
    final_load_scenarios: list[dict[str, Any]] = field(default_factory=list)
    generation_meta: dict[str, Any] = field(default_factory=dict)
    raw_llm_outputs: list[dict[str, Any]] = field(default_factory=list)

    def set_planner_plan(self, plan: PlannerTestPlan) -> None:
        self.planner_plan = _sanitize_for_debug(plan.model_dump(mode="json"))

    def set_case_outcomes(self, case_outcomes: dict[str, dict[str, Any]]) -> None:
        sanitized_outcomes = _sanitize_for_debug(case_outcomes)
        self.executor_case_outcomes = sanitized_outcomes
        fallback_ids = [
            case_id
            for case_id, outcome in sanitized_outcomes.items()
            if isinstance(outcome, dict) and bool(outcome.get("fallback_used"))
        ]
        self.fallback_case_ids = sorted(set(fallback_ids))

    def set_materialized_outputs(self, suites: list[TestSuite], load_scenarios: list[LoadTestScenario]) -> None:
        self.final_suites = _sanitize_for_debug([suite.model_dump(mode="json") for suite in suites])
        self.final_load_scenarios = _sanitize_for_debug(
            [scenario.model_dump(mode="json") for scenario in load_scenarios]
        )

    def set_generation_meta(self, generation_meta: GenerationMeta) -> None:
        self.generation_meta = _sanitize_for_debug(generation_meta.model_dump(mode="json"))

    def record_raw_output(self, stage: str, attempt: int, is_repair: bool, raw_text: str) -> None:
        if not self.capture_raw_llm:
            return
        self.raw_llm_outputs.append(
            {
                "stage": stage,
                "attempt": attempt,
                "is_repair": is_repair,
                "raw_output": _sanitize_raw_text(raw_text),
            }
        )

    def to_persist_payload(self, include_raw: bool) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "parsed_api_id": self.parsed_api_id,
            "parsed_api_title": self.parsed_api_title,
            "categories": _sanitize_for_debug(self.categories),
            "planner_plan": _sanitize_for_debug(self.planner_plan),
            "executor_case_outcomes": _sanitize_for_debug(self.executor_case_outcomes),
            "fallback_case_ids": _sanitize_for_debug(self.fallback_case_ids),
            "final_suites": _sanitize_for_debug(self.final_suites),
            "final_load_scenarios": _sanitize_for_debug(self.final_load_scenarios),
            "generation_meta": _sanitize_for_debug(self.generation_meta),
            "raw_llm_outputs": _sanitize_for_debug(self.raw_llm_outputs) if include_raw else [],
        }


PLANNER_PROMPT = """You are a senior API test planner.

Given this API context, return ONLY valid JSON for a strict planner contract.

API context:
{api_context}

Requested categories:
{categories}

Return a JSON object with exactly these top-level keys:
- version (string)
- metadata (object)
- assumptions (array of strings)
- endpoint_groups (array of objects: name, description, endpoints[])
- individual_tests (array of planner test case drafts)
- suite_tests (array of planner suites)
- load_scenarios (array of planner load drafts)

Planner test case draft fields:
- case_id (string, unique globally)
- name (string)
- description (string)
- endpoint (normalized path only, must start with "/", no full URL)
- method (one of GET,POST,PUT,DELETE,PATCH,OPTIONS,HEAD)
- expected_status (integer)
- category ("individual" or "suite")
- headers/query_params/path_params (object values MUST be strings)
- body_hint (optional JSON)
- assertion_hints (optional array of {{field, operator, expected}})
- depends_on (optional array of case_id strings)
- intent_labels (optional array of strings)

Planner suite fields:
- suite_id (string, unique)
- name
- description
- include_websocket (boolean)
- test_cases (array of planner test case drafts; category must be "suite")

Planner load scenario fields:
- scenario_id (string, unique)
- name
- description
- target_endpoint (normalized path only, must start with "/")
- method
- vus (integer > 0)
- duration (string like "30s" or "2m")
- ramp_stages (array of {{duration, target}})
- thresholds (object of string -> array[string])
- headers (object values are strings)

Rules:
- Include only requested categories; leave unrequested arrays empty.
- Do not include markdown.
- Do not include comments.
- Do not output prose.
"""


EXECUTOR_PROMPT = """You are a precise test implementation assistant.

Given a validated planner test plan, return ONLY valid JSON with optional enrichments.
You must NOT invent case_id/suite_id/scenario_id values not present in the plan.

Planner test plan:
{planner_plan}

API context:
{api_context}

Return a JSON object with exactly:
- test_enrichments: array of
  - case_id
  - endpoint (optional normalized path)
  - method (optional)
  - headers/query_params/path_params (optional string maps)
  - body (optional JSON)
  - expected_status (optional int)
  - assertions (optional array of {{field, operator, expected}})
- load_enrichments: array of
  - scenario_id
  - target_url (optional)
  - method (optional)
  - vus (optional int)
  - duration (optional string)
  - ramp_stages (optional array)
  - thresholds (optional object string -> array[string])
  - headers (optional object string -> string)
- websocket_tests: array of
  - suite_id
  - name
  - description
  - url
  - steps (array of {{action, message, timeout_seconds, assertions}})

Rules:
- If unsure, return empty arrays.
- Assertions must use runner-compatible operators only: eq, ne, gt, lt, gte, lte, contains, exists, type.
- Assertion fields must be one of: status_code, body.<path>, headers.<name>.
- No markdown, no prose, only JSON.
"""

EXECUTOR_CASE_PROMPT = """You are a precise test implementation assistant.

Given one validated test case draft and endpoint context, return ONLY JSON for this exact case.
Do not invent any other case IDs.

Case draft:
{case_draft}

Endpoint context:
{endpoint_context}

Return a JSON object with these fields:
- case_id
- endpoint (optional normalized path, must start with '/')
- method (optional one of GET,POST,PUT,DELETE,PATCH,OPTIONS,HEAD)
- headers/query_params/path_params (optional object with string values)
- body (optional JSON)
- expected_status (optional int)
- assertions (optional array of {{field, operator, expected}})

Rules:
- case_id must match input case_id exactly.
- Assertions operators must be one of: eq, ne, gt, lt, gte, lte, contains, exists, type.
- Assertion fields must be one of: status_code, body.<path>, headers.<name>.
- No markdown, no prose, only JSON.
"""


REPAIR_PROMPT = """The previous {stage} output failed strict validation.

Validation errors:
{errors}

Original output:
{raw_output}

Return ONLY corrected JSON that satisfies the contract. No markdown, no prose.
"""

T = TypeVar("T")


def _get_client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY", GEMINI_API_KEY).strip()
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not set")
    return genai.Client(api_key=api_key)


def _build_api_context(parsed_api: ParsedAPI) -> str:
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


def _strip_code_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _parse_json_response(raw: str) -> Any:
    text = _strip_code_fences(raw)
    if not text:
        raise ValueError("Model returned empty output")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if start >= 0 and end > start:
                candidate = text[start : end + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue
        raise


def _normalize_errors(exc: Exception) -> list[dict[str, Any]]:
    if isinstance(exc, ValidationError):
        return exc.errors()
    return [{"msg": str(exc), "type": exc.__class__.__name__}]


def _normalize_endpoint_path(endpoint: object, base_url: str) -> str:
    raw = str(endpoint).strip() if endpoint is not None else ""
    if not raw:
        return "/"

    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        raw = parsed.path or "/"
        if parsed.query:
            raw = f"{raw}?{parsed.query}"

    if not raw.startswith("/"):
        raw = f"/{raw}"

    base_path = urlparse(base_url).path.rstrip("/")
    if base_path:
        if raw == base_path:
            return "/"
        if raw.startswith(f"{base_path}/"):
            normalized = raw[len(base_path) :]
            return normalized if normalized else "/"

    return raw


def _compose_target_url(base_url: str, endpoint: str) -> str:
    normalized_endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    if base_url:
        return f"{base_url.rstrip('/')}{normalized_endpoint}"
    return f"http://localhost:8080{normalized_endpoint}"


def _normalize_load_target_url(target_url: str, base_url: str) -> str:
    raw = (target_url or "").strip()
    if not raw:
        return raw
    if raw.startswith(("http://", "https://")):
        return raw
    endpoint = raw if raw.startswith("/") else f"/{raw}"
    return _compose_target_url(base_url, endpoint)


def _normalize_assertion_field(field: str) -> str:
    raw = field.strip()
    if not raw:
        return "status_code"

    lowered = raw.lower()
    if lowered in {"status_code", "body"}:
        return lowered

    if lowered.startswith("response.body."):
        raw = f"body.{raw[14:]}"
        lowered = raw.lower()
    elif lowered.startswith("response.headers."):
        raw = f"headers.{raw[17:]}"
        lowered = raw.lower()
    elif lowered.startswith("header."):
        raw = f"headers.{raw[7:]}"
        lowered = raw.lower()

    if raw.startswith("["):
        raw = f"body.{raw}"
        lowered = raw.lower()

    if lowered.startswith("body."):
        suffix = raw[5:]
        suffix = re.sub(r"\[(\d+)\]", r".\1", suffix)
        suffix = suffix.lstrip(".")
        return f"body.{suffix}" if suffix else "body"

    if lowered.startswith("headers."):
        suffix = raw[8:].strip()
        suffix = suffix.lower()
        return f"headers.{suffix}" if suffix else "headers.content-type"

    if lowered == "response.status_code":
        return "status_code"

    normalized = re.sub(r"\[(\d+)\]", r".\1", raw).lstrip(".")
    return f"body.{normalized}" if normalized else "body"


def _normalize_assertion_operator(operator: str) -> str:
    mapped = {
        "==": "eq",
        "=": "eq",
        "equals": "eq",
        "equal": "eq",
        "eq": "eq",
        "!=": "ne",
        "<>": "ne",
        "not_equals": "ne",
        "not_equal": "ne",
        "ne": "ne",
        ">": "gt",
        "gt": "gt",
        "<": "lt",
        "lt": "lt",
        ">=": "gte",
        "gte": "gte",
        "<=": "lte",
        "lte": "lte",
        "in": "contains",
        "contains": "contains",
        "exists": "exists",
        "type": "type",
    }
    return mapped.get(operator.strip().lower(), "eq")


def _normalize_assertions(assertions: list[TestAssertion]) -> list[TestAssertion]:
    normalized: list[TestAssertion] = []
    for assertion in assertions:
        try:
            normalized.append(
                TestAssertion(
                    field=_normalize_assertion_field(str(assertion.field)),
                    operator=_normalize_assertion_operator(str(assertion.operator)),
                    expected=assertion.expected,
                )
            )
        except Exception:
            continue
    return normalized


async def _call_model_text(client: genai.Client, model_name: str, prompt: str, stage: str) -> str:
    try:
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
        )
        return response.text or ""
    except genai_errors.ServerError as exc:
        raise UpstreamModelError(f"{stage} model error: {exc}", status_code=503) from exc
    except genai_errors.ClientError as exc:
        raise UpstreamModelError(f"{stage} model error: {exc}", status_code=502) from exc
    except genai_errors.APIError as exc:
        raise UpstreamModelError(f"{stage} model error: {exc}", status_code=502) from exc
    except Exception as exc:
        lower = str(exc).lower()
        status_code = 503 if any(token in lower for token in ("timeout", "timed out", "connection", "temporarily")) else 502
        raise UpstreamModelError(f"{stage} model error: {exc}", status_code=status_code) from exc


def _build_repair_prompt(stage: str, raw_output: str, errors: list[dict[str, Any]]) -> str:
    return REPAIR_PROMPT.format(
        stage=stage,
        errors=json.dumps(errors, indent=2, default=str),
        raw_output=raw_output,
    )


def _validate_planner_payload(payload: Any) -> PlannerTestPlan:
    if isinstance(payload, dict) and isinstance(payload.get("plan"), dict):
        payload = payload["plan"]
    if not isinstance(payload, dict):
        raise ValueError("Planner output must be a JSON object")
    return PlannerTestPlan.model_validate(payload)


def _validate_executor_payload(payload: Any) -> ExecutorOutput:
    if isinstance(payload, dict) and isinstance(payload.get("materialization"), dict):
        payload = payload["materialization"]
    if not isinstance(payload, dict):
        raise ValueError("Executor output must be a JSON object")
    return ExecutorOutput.model_validate(payload)


def _validate_executor_case_payload(payload: Any, expected_case_id: str) -> ExecutorTestEnrichment:
    if isinstance(payload, dict):
        if isinstance(payload.get("test_enrichment"), dict):
            payload = payload["test_enrichment"]
        elif isinstance(payload.get("test_enrichments"), list) and payload["test_enrichments"]:
            payload = payload["test_enrichments"][0]

    if not isinstance(payload, dict):
        raise ValueError("Executor case output must be a JSON object")

    enrichment = ExecutorTestEnrichment.model_validate(payload)
    if enrichment.case_id != expected_case_id:
        raise ValueError(f"Executor case_id mismatch: expected {expected_case_id}, got {enrichment.case_id}")
    return enrichment


async def _model_validate_with_repair(
    client: genai.Client,
    model_name: str,
    prompt: str,
    stage: str,
    validator: Callable[[Any], T],
    raw_output_observer: Callable[[str, int, bool, str], None] | None = None,
) -> tuple[T, bool]:
    repair_attempted = False
    attempts = 0
    raw_output = await _call_model_text(client, model_name, prompt, stage=stage)
    if raw_output_observer is not None:
        raw_output_observer(stage, attempts, False, raw_output)

    while True:
        try:
            payload = _parse_json_response(raw_output)
            return validator(payload), repair_attempted
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
            errors = _normalize_errors(exc)
            if attempts >= GENERATOR_REPAIR_MAX_ATTEMPTS:
                raise StructuredOutputError(stage=stage, errors=errors, repair_attempted=repair_attempted) from exc

            attempts += 1
            repair_attempted = True
            raw_output = await _call_model_text(
                client,
                model_name,
                _build_repair_prompt(stage=stage, raw_output=raw_output, errors=errors),
                stage=f"{stage}_repair",
            )
            if raw_output_observer is not None:
                raw_output_observer(stage, attempts, True, raw_output)


def _categories_to_text(categories: list[TestCategory]) -> str:
    return ", ".join(c.value for c in categories)


def _normalize_planner_categories(plan: PlannerTestPlan, categories: set[TestCategory]) -> PlannerTestPlan:
    individual_tests = list(plan.individual_tests) if TestCategory.INDIVIDUAL in categories else []
    suite_tests = list(plan.suite_tests) if TestCategory.SUITE in categories else []
    load_scenarios = list(plan.load_scenarios) if TestCategory.LOAD in categories else []

    for draft in individual_tests:
        draft.category = TestCategory.INDIVIDUAL

    for suite in suite_tests:
        for draft in suite.test_cases:
            draft.category = TestCategory.SUITE

    return plan.model_copy(
        update={
            "individual_tests": individual_tests,
            "suite_tests": suite_tests,
            "load_scenarios": load_scenarios,
        }
    )


def _draft_to_test_case(draft: PlannerTestCaseDraft, base_url: str, category: TestCategory) -> TestCase:
    return TestCase(
        id=str(uuid.uuid4()),
        name=draft.name,
        description=draft.description,
        endpoint=_normalize_endpoint_path(draft.endpoint, base_url),
        method=draft.method,
        headers={k: str(v) for k, v in draft.headers.items()},
        query_params={k: str(v) for k, v in draft.query_params.items()},
        path_params={k: str(v) for k, v in draft.path_params.items()},
        body=draft.body_hint,
        expected_status=int(draft.expected_status),
        assertions=_normalize_assertions(list(draft.assertion_hints)),
        category=category,
    )


def _draft_to_load_scenario(draft: PlannerLoadScenarioDraft, base_url: str) -> LoadTestScenario:
    return LoadTestScenario(
        id=str(uuid.uuid4()),
        name=draft.name,
        description=draft.description,
        target_url=_compose_target_url(base_url, draft.target_endpoint),
        method=draft.method,
        vus=int(draft.vus),
        duration=str(draft.duration),
        ramp_stages=[{"duration": stage.duration, "target": int(stage.target)} for stage in draft.ramp_stages],
        thresholds={k: [str(v) for v in values] for k, values in draft.thresholds.items()},
        headers={k: str(v) for k, v in draft.headers.items()},
        query_params={},
        body=None,
        expected_statuses=[200],
    )


async def plan_tests(
    parsed_api: ParsedAPI,
    categories: list[TestCategory],
    debug_capture: GenerationDebugCapture | None = None,
) -> tuple[PlannerTestPlan, bool]:
    client = _get_client()
    planner_prompt = PLANNER_PROMPT.format(
        api_context=_build_api_context(parsed_api),
        categories=_categories_to_text(categories),
    )

    plan, repair_attempted = await _model_validate_with_repair(
        client=client,
        model_name=GEMINI_PLANNER_MODEL,
        prompt=planner_prompt,
        stage="planner",
        validator=_validate_planner_payload,
        raw_output_observer=debug_capture.record_raw_output if debug_capture else None,
    )

    normalized_plan = _normalize_planner_categories(plan, set(categories))
    if debug_capture is not None:
        debug_capture.set_planner_plan(normalized_plan)

    return normalized_plan, repair_attempted


async def _executor_output(
    plan: PlannerTestPlan,
    parsed_api: ParsedAPI,
    raw_output_observer: Callable[[str, int, bool, str], None] | None = None,
) -> tuple[ExecutorOutput, bool]:
    client = _get_client()
    executor_prompt = EXECUTOR_PROMPT.format(
        planner_plan=json.dumps(plan.model_dump(mode="json"), indent=2),
        api_context=_build_api_context(parsed_api),
    )

    return await _model_validate_with_repair(
        client=client,
        model_name=GEMINI_EXECUTOR_MODEL,
        prompt=executor_prompt,
        stage="executor",
        validator=_validate_executor_payload,
        raw_output_observer=raw_output_observer,
    )


def _endpoint_context_for_draft(parsed_api: ParsedAPI, draft: PlannerTestCaseDraft) -> dict[str, Any]:
    normalized_draft_path = _normalize_endpoint_path(draft.endpoint, parsed_api.base_url)
    for endpoint in parsed_api.endpoints:
        normalized_spec_path = _normalize_endpoint_path(endpoint.path, parsed_api.base_url)
        if endpoint.method == draft.method and normalized_spec_path == normalized_draft_path:
            return {
                "path": endpoint.path,
                "method": endpoint.method.value,
                "summary": endpoint.summary,
                "description": endpoint.description,
                "parameters": [p.model_dump() for p in endpoint.parameters],
                "responses": [r.model_dump() for r in endpoint.responses],
                "request_body": endpoint.request_body,
            }
    return {
        "path": normalized_draft_path,
        "method": draft.method.value,
        "summary": "",
        "description": "",
        "parameters": [],
        "responses": [],
        "request_body": None,
    }


async def _executor_call_for_case(
    client: genai.Client,
    parsed_api: ParsedAPI,
    draft: PlannerTestCaseDraft,
    raw_output_observer: Callable[[str, int, bool, str], None] | None = None,
) -> tuple[ExecutorTestEnrichment, bool]:
    prompt = EXECUTOR_CASE_PROMPT.format(
        case_draft=json.dumps(draft.model_dump(mode="json"), indent=2),
        endpoint_context=json.dumps(_endpoint_context_for_draft(parsed_api, draft), indent=2),
    )
    return await _model_validate_with_repair(
        client=client,
        model_name=GEMINI_EXECUTOR_MODEL,
        prompt=prompt,
        stage=f"executor_case:{draft.case_id}",
        validator=lambda payload: _validate_executor_case_payload(payload, draft.case_id),
        raw_output_observer=raw_output_observer,
    )


def _build_http_jobs(plan: PlannerTestPlan, categories: set[TestCategory]) -> list[HttpExecutorJob]:
    jobs: list[HttpExecutorJob] = []
    if TestCategory.INDIVIDUAL in categories:
        jobs.extend(HttpExecutorJob(case_id=draft.case_id, draft=draft) for draft in plan.individual_tests)
    if TestCategory.SUITE in categories:
        for suite in plan.suite_tests:
            jobs.extend(HttpExecutorJob(case_id=draft.case_id, draft=draft) for draft in suite.test_cases)
    return jobs


async def _run_http_executor_queue(
    client: genai.Client,
    parsed_api: ParsedAPI,
    jobs: list[HttpExecutorJob],
    concurrency: int,
    raw_output_observer: Callable[[str, int, bool, str], None] | None = None,
) -> HttpExecutorQueueResult:
    result = HttpExecutorQueueResult(jobs_total=len(jobs))
    if not jobs:
        return result

    worker_count = max(1, min(concurrency, len(jobs)))
    queue: asyncio.Queue[HttpExecutorJob | None] = asyncio.Queue()
    state_lock = asyncio.Lock()
    in_flight = 0

    for job in jobs:
        queue.put_nowait(job)

    async def worker() -> None:
        nonlocal in_flight
        while True:
            job = await queue.get()
            if job is None:
                queue.task_done()
                break

            async with state_lock:
                in_flight += 1
                result.max_in_flight = max(result.max_in_flight, in_flight)

            try:
                enrichment, repair_attempted = await _executor_call_for_case(
                    client,
                    parsed_api,
                    job.draft,
                    raw_output_observer=raw_output_observer,
                )
                async with state_lock:
                    result.enrichments[job.case_id] = enrichment
                    result.case_outcomes[job.case_id] = {
                        "status": "succeeded",
                        "repair_attempted": repair_attempted,
                        "fallback_used": False,
                        "error_message": None,
                    }
                    result.jobs_succeeded += 1
                    result.repair_attempted = result.repair_attempted or repair_attempted
            except Exception as exc:
                async with state_lock:
                    structured_repair_attempted = False
                    error_message = str(exc)
                    if isinstance(exc, StructuredOutputError):
                        structured_repair_attempted = exc.repair_attempted
                        result.repair_attempted = result.repair_attempted or structured_repair_attempted
                        error_message = f"{exc.stage}: {json.dumps(exc.errors, default=str)}"
                    result.case_outcomes[job.case_id] = {
                        "status": "failed",
                        "repair_attempted": structured_repair_attempted,
                        "fallback_used": True,
                        "error_message": error_message,
                    }
                    result.jobs_failed += 1
                    result.fallback_count += 1
            finally:
                async with state_lock:
                    in_flight -= 1
                queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
    await queue.join()
    for _ in workers:
        queue.put_nowait(None)
    await asyncio.gather(*workers)
    return result


def _apply_test_enrichment(test_case: TestCase, enrichment: ExecutorTestEnrichment, base_url: str) -> None:
    if enrichment.endpoint is not None:
        test_case.endpoint = _normalize_endpoint_path(enrichment.endpoint, base_url)
    if enrichment.method is not None:
        test_case.method = enrichment.method
    if enrichment.headers:
        test_case.headers.update({k: str(v) for k, v in enrichment.headers.items()})
    if enrichment.query_params:
        test_case.query_params.update({k: str(v) for k, v in enrichment.query_params.items()})
    if enrichment.path_params:
        test_case.path_params.update({k: str(v) for k, v in enrichment.path_params.items()})
    if enrichment.body is not None:
        test_case.body = enrichment.body
    if enrichment.expected_status is not None:
        test_case.expected_status = int(enrichment.expected_status)
    if enrichment.assertions:
        test_case.assertions = _normalize_assertions(list(enrichment.assertions))


def _apply_load_enrichment(
    scenario: LoadTestScenario,
    enrichment: ExecutorLoadEnrichment,
    base_url: str,
) -> None:
    if enrichment.target_url:
        scenario.target_url = _normalize_load_target_url(str(enrichment.target_url), base_url)
    if enrichment.method is not None:
        scenario.method = enrichment.method
    if enrichment.vus is not None:
        scenario.vus = int(enrichment.vus)
    if enrichment.duration:
        scenario.duration = str(enrichment.duration)
    if enrichment.ramp_stages:
        scenario.ramp_stages = list(enrichment.ramp_stages)
    if enrichment.thresholds:
        scenario.thresholds = {k: [str(v) for v in values] for k, values in enrichment.thresholds.items()}
    if enrichment.headers:
        scenario.headers.update({k: str(v) for k, v in enrichment.headers.items()})


async def materialize_tests(
    plan: PlannerTestPlan,
    parsed_api: ParsedAPI,
    categories: list[TestCategory],
    debug_capture: GenerationDebugCapture | None = None,
) -> tuple[list[TestSuite], list[LoadTestScenario], int, bool, HttpExecutorQueueResult]:
    dropped_items_count = 0
    suites: list[TestSuite] = []
    load_scenarios: list[LoadTestScenario] = []

    case_lookup: dict[str, TestCase] = {}
    suite_lookup: dict[str, TestSuite] = {}
    load_lookup: dict[str, LoadTestScenario] = {}

    category_set = set(categories)

    if TestCategory.INDIVIDUAL in category_set:
        individual_cases = []
        for draft in plan.individual_tests:
            test_case = _draft_to_test_case(draft, parsed_api.base_url, TestCategory.INDIVIDUAL)
            individual_cases.append(test_case)
            case_lookup[draft.case_id] = test_case

        suites.append(
            TestSuite(
                id=str(uuid.uuid4()),
                name="Individual Test Cases",
                description="Planner-generated individual test cases",
                category=TestCategory.INDIVIDUAL,
                test_cases=individual_cases,
            )
        )

    if TestCategory.SUITE in category_set:
        for suite_draft in plan.suite_tests:
            suite_cases = []
            for draft in suite_draft.test_cases:
                test_case = _draft_to_test_case(draft, parsed_api.base_url, TestCategory.SUITE)
                suite_cases.append(test_case)
                case_lookup[draft.case_id] = test_case

            suite = TestSuite(
                id=str(uuid.uuid4()),
                name=suite_draft.name,
                description=suite_draft.description,
                category=TestCategory.SUITE,
                test_cases=suite_cases,
                ws_test_cases=[],
            )
            suites.append(suite)
            suite_lookup[suite_draft.suite_id] = suite

    if TestCategory.LOAD in category_set:
        for draft in plan.load_scenarios:
            scenario = _draft_to_load_scenario(draft, parsed_api.base_url)
            load_scenarios.append(scenario)
            load_lookup[draft.scenario_id] = scenario

    queue_result = HttpExecutorQueueResult()
    repair_attempted = False

    if case_lookup:
        client = _get_client()
        jobs = _build_http_jobs(plan, category_set)
        queue_result = await _run_http_executor_queue(
            client=client,
            parsed_api=parsed_api,
            jobs=jobs,
            concurrency=GENERATOR_EXECUTOR_CONCURRENCY,
            raw_output_observer=debug_capture.record_raw_output if debug_capture else None,
        )
        repair_attempted = queue_result.repair_attempted

        for case_id, enrichment in queue_result.enrichments.items():
            target_case = case_lookup.get(case_id)
            if not target_case:
                dropped_items_count += 1
                continue
            _apply_test_enrichment(target_case, enrichment, parsed_api.base_url)

        if debug_capture is not None:
            debug_capture.set_case_outcomes(queue_result.case_outcomes)

    executor_output = ExecutorOutput()
    should_fetch_executor_output = bool(load_lookup) or (
        parsed_api.websocket_messages and TestCategory.SUITE in category_set and suites
    )
    if should_fetch_executor_output:
        try:
            executor_output, ws_or_load_repair_attempted = await _executor_output(
                plan,
                parsed_api,
                raw_output_observer=debug_capture.record_raw_output if debug_capture else None,
            )
            repair_attempted = repair_attempted or ws_or_load_repair_attempted
        except Exception:
            executor_output = ExecutorOutput()

    if load_lookup:
        for load_enrichment in executor_output.load_enrichments:
            target_scenario = load_lookup.get(load_enrichment.scenario_id)
            if not target_scenario:
                dropped_items_count += 1
                continue
            _apply_load_enrichment(target_scenario, load_enrichment, parsed_api.base_url)

    # Keep existing websocket behavior as a best-effort enrichment path.
    if parsed_api.websocket_messages and TestCategory.SUITE in category_set and suites:
        for ws_draft in executor_output.websocket_tests:
            target_suite = suite_lookup.get(ws_draft.suite_id)
            if not target_suite:
                dropped_items_count += 1
                continue
            target_suite.ws_test_cases.append(
                WebSocketTestCase(
                    id=str(uuid.uuid4()),
                    name=ws_draft.name,
                    description=ws_draft.description,
                    url=ws_draft.url,
                    steps=list(ws_draft.steps),
                    category=TestCategory.SUITE,
                )
            )

    if debug_capture is not None:
        debug_capture.set_materialized_outputs(suites, load_scenarios)

    return suites, load_scenarios, dropped_items_count, repair_attempted, queue_result


async def generate_all(
    parsed_api: ParsedAPI,
    categories: list[TestCategory] | None = None,
    debug_capture: GenerationDebugCapture | None = None,
) -> tuple[list[TestSuite], list[LoadTestScenario], GenerationMeta]:
    if categories is None:
        categories = [TestCategory.INDIVIDUAL, TestCategory.SUITE, TestCategory.LOAD]
    if debug_capture is not None and not debug_capture.categories:
        debug_capture.categories = [category.value for category in categories]

    planner_plan, planner_repair_attempted = await plan_tests(
        parsed_api,
        categories,
        debug_capture=debug_capture,
    )
    suites, load_scenarios, dropped_items_count, executor_repair_attempted, queue_result = await materialize_tests(
        planner_plan,
        parsed_api,
        categories,
        debug_capture=debug_capture,
    )

    generation_meta = GenerationMeta(
        planner_model=GEMINI_PLANNER_MODEL,
        executor_model=GEMINI_EXECUTOR_MODEL,
        repair_attempted=planner_repair_attempted or executor_repair_attempted,
        dropped_items_count=dropped_items_count,
        executor_jobs_total=queue_result.jobs_total,
        executor_jobs_succeeded=queue_result.jobs_succeeded,
        executor_jobs_failed=queue_result.jobs_failed,
        fallback_count=queue_result.fallback_count,
        executor_concurrency=GENERATOR_EXECUTOR_CONCURRENCY,
    )
    logger.info(
        "generate.queue_complete jobs_total=%s jobs_succeeded=%s jobs_failed=%s fallback_count=%s",
        queue_result.jobs_total,
        queue_result.jobs_succeeded,
        queue_result.jobs_failed,
        queue_result.fallback_count,
    )
    if debug_capture is not None:
        debug_capture.set_generation_meta(generation_meta)

    return suites, load_scenarios, generation_meta

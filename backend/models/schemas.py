from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, StrictInt, StrictStr, field_validator, model_validator


class TestCategory(str, Enum):
    INDIVIDUAL = "individual"
    SUITE = "suite"
    LOAD = "load"


class TestStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class HttpMethod(str, Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"
    OPTIONS = "OPTIONS"
    HEAD = "HEAD"


class LoadTestPreset(str, Enum):
    SMOKE = "smoke"
    LOAD = "load"
    STRESS = "stress"


# ── OpenAPI parsed models ──


class ParsedParameter(BaseModel):
    name: str
    location: str  # query, path, header, cookie
    required: bool = False
    schema_type: str = "string"
    description: str = ""


class ParsedResponse(BaseModel):
    status_code: str
    description: str = ""
    content_type: str = "application/json"
    schema_ref: str | None = None
    example: Any = None
    examples: dict[str, Any] = Field(default_factory=dict)
    links: dict[str, Any] = Field(default_factory=dict)


class ParsedEndpoint(BaseModel):
    path: str
    method: HttpMethod
    summary: str = ""
    description: str = ""
    operation_id: str = ""
    tags: list[str] = Field(default_factory=list)
    parameters: list[ParsedParameter] = Field(default_factory=list)
    responses: list[ParsedResponse] = Field(default_factory=list)
    security: list[dict[str, Any]] = Field(default_factory=list)
    requires_auth: bool = False
    request_body: dict[str, Any] | None = None
    request_body_required_fields: list[str] = Field(default_factory=list)
    request_body_example: Any = None
    response_examples: dict[str, Any] = Field(default_factory=dict)


class ParsedWebSocketMessage(BaseModel):
    type: str
    direction: str  # client_to_server or server_to_client
    fields: dict[str, Any] = Field(default_factory=dict)
    description: str = ""


class ParsedAPI(BaseModel):
    title: str = ""
    description: str = ""
    version: str = ""
    base_url: str = ""
    endpoints: list[ParsedEndpoint] = Field(default_factory=list)
    schemas: dict[str, Any] = Field(default_factory=dict)
    websocket_messages: list[ParsedWebSocketMessage] = Field(default_factory=list)


# ── Planner contracts ──


def _validate_normalized_path(value: str, field_name: str) -> str:
    if not value.startswith("/"):
        raise ValueError(f"{field_name} must start with '/'")
    if "://" in value:
        raise ValueError(f"{field_name} must be a normalized path, not a full URL")
    if any(ch.isspace() for ch in value):
        raise ValueError(f"{field_name} must not include whitespace")
    return value


class PlannerEndpointGroup(BaseModel):
    name: StrictStr
    description: str = ""
    endpoints: list[StrictStr] = Field(default_factory=list)

    @field_validator("endpoints")
    @classmethod
    def validate_endpoints(cls, endpoints: list[str]) -> list[str]:
        return [_validate_normalized_path(endpoint, "endpoint_groups.endpoints") for endpoint in endpoints]


class PlannerTestCaseDraft(BaseModel):
    case_id: StrictStr
    name: StrictStr
    description: str = ""
    endpoint: StrictStr
    method: HttpMethod = HttpMethod.GET
    expected_status: StrictInt = 200
    category: TestCategory = TestCategory.INDIVIDUAL
    headers: dict[str, StrictStr] = Field(default_factory=dict)
    query_params: dict[str, StrictStr] = Field(default_factory=dict)
    path_params: dict[str, StrictStr] = Field(default_factory=dict)
    body_hint: Any = None
    assertion_hints: list[TestAssertion] = Field(default_factory=list)
    depends_on: list[StrictStr] = Field(default_factory=list)
    intent_labels: list[StrictStr] = Field(default_factory=list)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, endpoint: str) -> str:
        return _validate_normalized_path(endpoint, "test_case.endpoint")

    @field_validator("expected_status")
    @classmethod
    def validate_expected_status(cls, expected_status: int) -> int:
        if expected_status < 100 or expected_status > 599:
            raise ValueError("expected_status must be between 100 and 599")
        return expected_status


class PlannerSuiteDraft(BaseModel):
    suite_id: StrictStr
    name: StrictStr
    description: str = ""
    test_cases: list[PlannerTestCaseDraft] = Field(default_factory=list)
    include_websocket: bool = False


class PlannerRampStageDraft(BaseModel):
    duration: StrictStr
    target: StrictInt

    @field_validator("target")
    @classmethod
    def validate_target(cls, target: int) -> int:
        if target < 0:
            raise ValueError("target must be >= 0")
        return target


class PlannerLoadScenarioDraft(BaseModel):
    scenario_id: StrictStr
    name: StrictStr
    description: str = ""
    target_endpoint: StrictStr
    method: HttpMethod = HttpMethod.GET
    vus: StrictInt = 10
    duration: StrictStr = "30s"
    ramp_stages: list[PlannerRampStageDraft] = Field(default_factory=list)
    thresholds: dict[str, list[StrictStr]] = Field(default_factory=dict)
    headers: dict[str, StrictStr] = Field(default_factory=dict)

    @field_validator("target_endpoint")
    @classmethod
    def validate_target_endpoint(cls, target_endpoint: str) -> str:
        return _validate_normalized_path(target_endpoint, "load_scenario.target_endpoint")

    @field_validator("vus")
    @classmethod
    def validate_vus(cls, vus: int) -> int:
        if vus <= 0:
            raise ValueError("vus must be > 0")
        return vus


class PlannerTestPlan(BaseModel):
    version: StrictStr = "1.0"
    metadata: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[StrictStr] = Field(default_factory=list)
    endpoint_groups: list[PlannerEndpointGroup] = Field(default_factory=list)
    individual_tests: list[PlannerTestCaseDraft] = Field(default_factory=list)
    suite_tests: list[PlannerSuiteDraft] = Field(default_factory=list)
    load_scenarios: list[PlannerLoadScenarioDraft] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "PlannerTestPlan":
        case_ids: set[str] = set()
        suite_ids: set[str] = set()
        scenario_ids: set[str] = set()

        for test_case in self.individual_tests:
            if test_case.case_id in case_ids:
                raise ValueError(f"Duplicate case_id found: {test_case.case_id}")
            case_ids.add(test_case.case_id)

        for suite in self.suite_tests:
            if suite.suite_id in suite_ids:
                raise ValueError(f"Duplicate suite_id found: {suite.suite_id}")
            suite_ids.add(suite.suite_id)

            for test_case in suite.test_cases:
                if test_case.case_id in case_ids:
                    raise ValueError(f"Duplicate case_id found: {test_case.case_id}")
                case_ids.add(test_case.case_id)

        for scenario in self.load_scenarios:
            if scenario.scenario_id in scenario_ids:
                raise ValueError(f"Duplicate scenario_id found: {scenario.scenario_id}")
            scenario_ids.add(scenario.scenario_id)

        return self


# ── Test scenario models ──


class TestAssertion(BaseModel):
    field: str  # "status_code", "body.status", "body.active_games", "headers.content-type"
    operator: str  # "eq", "ne", "gt", "lt", "gte", "lte", "contains", "exists", "type"
    expected: Any


class TestCase(BaseModel):
    id: str = ""
    name: str
    description: str = ""
    endpoint: str
    method: HttpMethod = HttpMethod.GET
    headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict)
    path_params: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    expected_status: int = 200
    assertions: list[TestAssertion] = Field(default_factory=list)
    category: TestCategory = TestCategory.INDIVIDUAL


class WebSocketStep(BaseModel):
    action: str  # "send" or "expect"
    message: dict[str, Any]
    timeout_seconds: float = 5.0
    assertions: list[TestAssertion] = Field(default_factory=list)


class WebSocketTestCase(BaseModel):
    id: str = ""
    name: str
    description: str = ""
    url: str = "ws://localhost:8080/"
    steps: list[WebSocketStep] = Field(default_factory=list)
    category: TestCategory = TestCategory.SUITE


class TestSuite(BaseModel):
    id: str = ""
    name: str
    description: str = ""
    category: TestCategory
    test_cases: list[TestCase] = Field(default_factory=list)
    ws_test_cases: list[WebSocketTestCase] = Field(default_factory=list)


# ── Test result models ──


class TestResult(BaseModel):
    id: str = ""
    test_case_id: str
    test_case_name: str
    suite_id: str | None = None
    suite_name: str | None = None
    endpoint: str
    method: str
    category: TestCategory
    status: TestStatus
    expected_status: int
    actual_status: int | None = None
    expected_body: Any = None
    actual_body: Any = None
    response_time_ms: float = 0
    assertions_passed: int = 0
    assertions_total: int = 0
    error_message: str | None = None
    executed_at: datetime = Field(default_factory=datetime.utcnow)


class TestRunSummary(BaseModel):
    id: str = ""
    total_tests: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    avg_response_time_ms: float = 0
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None


# ── Load test models ──


class LoadTestScenario(BaseModel):
    id: str = ""
    name: str
    description: str = ""
    target_url: str
    method: HttpMethod = HttpMethod.GET
    vus: int = 10
    duration: str = "30s"
    ramp_stages: list[dict[str, Any]] = Field(default_factory=list)
    thresholds: dict[str, list[str]] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, Any] = Field(default_factory=dict)
    body: Any = None
    expected_statuses: list[int] = Field(default_factory=lambda: [200])

    @field_validator("expected_statuses")
    @classmethod
    def validate_expected_statuses(cls, expected_statuses: list[int]) -> list[int]:
        cleaned: list[int] = []
        seen: set[int] = set()
        for status in expected_statuses or [200]:
            value = int(status)
            if value < 100 or value > 599:
                raise ValueError("expected_statuses values must be between 100 and 599")
            if value not in seen:
                cleaned.append(value)
                seen.add(value)
        if not cleaned:
            cleaned = [200]
        return cleaned


class LoadTestProfile(BaseModel):
    id: str
    name: str
    base_url: str
    default_headers: dict[str, str] = Field(default_factory=dict)


class LoadTestMetrics(BaseModel):
    id: str = ""
    scenario_id: str
    scenario_name: str
    total_requests: int = 0
    failed_requests: int = 0
    avg_response_time_ms: float = 0
    min_response_time_ms: float = 0
    max_response_time_ms: float = 0
    p50_ms: float = 0
    p90_ms: float = 0
    p95_ms: float = 0
    p99_ms: float = 0
    requests_per_second: float = 0
    error_rate: float = 0
    data_received_kb: float = 0
    data_sent_kb: float = 0
    executed_at: datetime = Field(default_factory=datetime.utcnow)
    duration_seconds: float = 0
    vus_max: int = 0
    runner_status: str = "passed"
    runner_message: str = ""
    runner_exit_code: int | None = None
    runner_stdout_excerpt: str = ""
    runner_stderr_excerpt: str = ""
    metric_shape: str | None = None
    request_count_source: str | None = None
    error_rate_source: str | None = None
    parse_warnings: list[str] = Field(default_factory=list)
    raw_metrics: dict[str, Any] = Field(default_factory=dict)


class LoadTestScenarioUpsertRequest(BaseModel):
    id: str = ""
    name: str
    description: str = ""
    target_url: str
    method: HttpMethod = HttpMethod.GET
    vus: int = 10
    duration: str = "30s"
    ramp_stages: list[dict[str, Any]] = Field(default_factory=list)
    thresholds: dict[str, list[str]] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, Any] = Field(default_factory=dict)
    body: Any = None
    expected_statuses: list[int] = Field(default_factory=lambda: [200])
    preset: LoadTestPreset | None = None

    @field_validator("expected_statuses")
    @classmethod
    def validate_expected_statuses(cls, expected_statuses: list[int]) -> list[int]:
        return LoadTestScenario.validate_expected_statuses(expected_statuses)


# ── Dashboard ──


class DashboardSummary(BaseModel):
    total_tests: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    pass_rate: float = 0
    avg_response_time_ms: float = 0
    functional_summary: dict[str, int] = Field(default_factory=dict)
    suite_summary: dict[str, int] = Field(default_factory=dict)
    load_summary: dict[str, Any] = Field(default_factory=dict)
    recent_runs: list[TestRunSummary] = Field(default_factory=list)


class GenerationMeta(BaseModel):
    planner_model: str
    executor_model: str
    repair_attempted: bool = False
    dropped_items_count: int = 0
    executor_jobs_total: int = 0
    executor_jobs_succeeded: int = 0
    executor_jobs_failed: int = 0
    fallback_count: int = 0
    executor_concurrency: int = 0


# ── Flow models ──


class FlowExtractSource(str, Enum):
    BODY = "body"
    HEADERS = "headers"
    STATUS_CODE = "status_code"


class FlowRunStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    RUNNING = "running"


class FlowGenerationMode(str, Enum):
    HYBRID_AUTO = "hybrid_auto"
    LLM_FIRST = "llm_first"
    DETERMINISTIC_FIRST = "deterministic_first"
    PURE_LLM = "pure_llm"


class FlowMutationPolicy(str, Enum):
    SAFE = "safe"
    BALANCED = "balanced"
    FULL_LIFECYCLE = "full_lifecycle"


class FlowExtractRule(BaseModel):
    var: StrictStr
    source: FlowExtractSource = Field(default=FlowExtractSource.BODY, alias="from")
    path: str = ""
    required: bool = True

    model_config = {"populate_by_name": True}

    @field_validator("var")
    @classmethod
    def validate_var(cls, var: str) -> str:
        if not var.strip():
            raise ValueError("extract.var must not be empty")
        return var.strip()


class FlowStep(BaseModel):
    step_id: StrictStr
    order: StrictInt
    name: StrictStr
    method: HttpMethod = HttpMethod.GET
    endpoint: StrictStr
    headers: dict[str, Any] = Field(default_factory=dict)
    query_params: dict[str, Any] = Field(default_factory=dict)
    path_params: dict[str, Any] = Field(default_factory=dict)
    body: Any = None
    extract: list[FlowExtractRule] = Field(default_factory=list)
    assertions: list[TestAssertion] = Field(default_factory=list)
    expected_status: int | None = None
    required: bool = True

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, endpoint: str) -> str:
        return _validate_normalized_path(endpoint, "flow_step.endpoint")

    @field_validator("order")
    @classmethod
    def validate_order(cls, order: int) -> int:
        if order <= 0:
            raise ValueError("flow_step.order must be >= 1")
        return order

    @field_validator("expected_status")
    @classmethod
    def validate_expected_status(cls, expected_status: int | None) -> int | None:
        if expected_status is None:
            return None
        if expected_status < 100 or expected_status > 599:
            raise ValueError("expected_status must be between 100 and 599")
        return expected_status


class FlowScenario(BaseModel):
    id: str = ""
    name: str
    description: str = ""
    persona: str = ""
    preconditions: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    steps: list[FlowStep] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    source_generation_id: str | None = None

    @model_validator(mode="after")
    def validate_steps(self) -> "FlowScenario":
        step_ids: set[str] = set()
        orders: set[int] = set()
        for step in self.steps:
            if step.step_id in step_ids:
                raise ValueError(f"Duplicate flow step_id found: {step.step_id}")
            step_ids.add(step.step_id)

            if step.order in orders:
                raise ValueError(f"Duplicate flow step.order found: {step.order}")
            orders.add(step.order)

        ordered = [step.order for step in self.steps]
        if ordered != sorted(ordered):
            raise ValueError("Flow steps must be ordered by ascending step.order")
        return self


class FlowEliminatedCandidate(BaseModel):
    name: str
    reason_code: str
    reason: str


class FlowStepResult(BaseModel):
    id: str = ""
    flow_run_id: str
    flow_id: str
    step_id: str
    order: int
    status: TestStatus
    resolved_request: dict[str, Any] = Field(default_factory=dict)
    response_status: int | None = None
    response_headers: dict[str, Any] = Field(default_factory=dict)
    response_body: Any = None
    assertions_passed: int = 0
    assertions_total: int = 0
    extracted_context_delta: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    executed_at: datetime = Field(default_factory=datetime.utcnow)


class FlowRunRecord(BaseModel):
    id: str
    flow_id: str
    flow_name: str
    status: FlowRunStatus
    target_base_url: str
    initial_context: dict[str, Any] = Field(default_factory=dict)
    final_context: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime | None = None
    step_results: list[FlowStepResult] = Field(default_factory=list)


# ── API request/response models ──


class ParseRequest(BaseModel):
    spec_url: str | None = None
    spec_content: str | None = None
    spec_path: str | None = None


class GenerateRequest(BaseModel):
    categories: list[TestCategory] = Field(
        default_factory=lambda: [TestCategory.INDIVIDUAL, TestCategory.SUITE, TestCategory.LOAD]
    )


class ExecuteRequest(BaseModel):
    suite_ids: list[str] | None = None
    target_base_url: str | None = None


class LoadTestRunRequest(BaseModel):
    scenario_ids: list[str] | None = None
    target_base_url: str | None = None
    profile_id: str | None = None
    headers_override: dict[str, str] = Field(default_factory=dict)


class FlowGenerateRequest(BaseModel):
    max_flows: int = Field(default=5, ge=1, le=20)
    max_steps_per_flow: int = Field(default=8, ge=2, le=20)
    objectives: list[str] = Field(default_factory=list)
    include_negative: bool = True
    generation_mode: FlowGenerationMode = FlowGenerationMode.HYBRID_AUTO
    mutation_policy: FlowMutationPolicy = FlowMutationPolicy.SAFE
    app_context: dict[str, Any] = Field(default_factory=dict)
    personas: list[str] = Field(default_factory=list)


class FlowRunRequest(BaseModel):
    flow_ids: list[str] | None = None
    target_base_url: str | None = None
    initial_context: dict[str, Any] = Field(default_factory=dict)

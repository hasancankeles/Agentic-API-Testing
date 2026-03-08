from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


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


class ParsedEndpoint(BaseModel):
    path: str
    method: HttpMethod
    summary: str = ""
    description: str = ""
    operation_id: str = ""
    tags: list[str] = Field(default_factory=list)
    parameters: list[ParsedParameter] = Field(default_factory=list)
    responses: list[ParsedResponse] = Field(default_factory=list)
    request_body: dict[str, Any] | None = None


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
    raw_metrics: dict[str, Any] = Field(default_factory=dict)


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
    target_base_url: str = "http://localhost:8080"


class LoadTestRunRequest(BaseModel):
    scenario_ids: list[str] | None = None
    target_base_url: str = "http://localhost:8080"

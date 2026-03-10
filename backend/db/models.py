import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, JSON
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def generate_uuid() -> str:
    return str(uuid.uuid4())


class DBTestSuite(Base):
    __tablename__ = "test_suites"

    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False)
    description = Column(Text, default="")
    category = Column(String, nullable=False)
    test_cases_json = Column(JSON, default=list)
    ws_test_cases_json = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)


class DBTestResult(Base):
    __tablename__ = "test_results"

    id = Column(String, primary_key=True, default=generate_uuid)
    run_id = Column(String, nullable=False)
    test_case_id = Column(String, nullable=False)
    test_case_name = Column(String, nullable=False)
    suite_id = Column(String, nullable=True)
    suite_name = Column(String, nullable=True)
    endpoint = Column(String, nullable=False)
    method = Column(String, nullable=False)
    category = Column(String, nullable=False)
    status = Column(String, nullable=False)
    expected_status = Column(Integer, default=200)
    actual_status = Column(Integer, nullable=True)
    expected_body = Column(JSON, nullable=True)
    actual_body = Column(JSON, nullable=True)
    response_time_ms = Column(Float, default=0)
    assertions_passed = Column(Integer, default=0)
    assertions_total = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    executed_at = Column(DateTime, default=datetime.utcnow)


class DBTestRun(Base):
    __tablename__ = "test_runs"

    id = Column(String, primary_key=True, default=generate_uuid)
    total_tests = Column(Integer, default=0)
    passed = Column(Integer, default=0)
    failed = Column(Integer, default=0)
    errors = Column(Integer, default=0)
    avg_response_time_ms = Column(Float, default=0)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)


class DBLoadTestScenario(Base):
    __tablename__ = "load_test_scenarios"

    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False)
    description = Column(Text, default="")
    target_url = Column(String, nullable=False)
    method = Column(String, default="GET")
    vus = Column(Integer, default=10)
    duration = Column(String, default="30s")
    ramp_stages_json = Column(JSON, default=list)
    thresholds_json = Column(JSON, default=dict)
    headers_json = Column(JSON, default=dict)
    k6_script = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class DBLoadTestResult(Base):
    __tablename__ = "load_test_results"

    id = Column(String, primary_key=True, default=generate_uuid)
    scenario_id = Column(String, nullable=False)
    scenario_name = Column(String, nullable=False)
    total_requests = Column(Integer, default=0)
    failed_requests = Column(Integer, default=0)
    avg_response_time_ms = Column(Float, default=0)
    min_response_time_ms = Column(Float, default=0)
    max_response_time_ms = Column(Float, default=0)
    p50_ms = Column(Float, default=0)
    p90_ms = Column(Float, default=0)
    p95_ms = Column(Float, default=0)
    p99_ms = Column(Float, default=0)
    requests_per_second = Column(Float, default=0)
    error_rate = Column(Float, default=0)
    data_received_kb = Column(Float, default=0)
    data_sent_kb = Column(Float, default=0)
    duration_seconds = Column(Float, default=0)
    vus_max = Column(Integer, default=0)
    raw_metrics = Column(JSON, default=dict)
    executed_at = Column(DateTime, default=datetime.utcnow)


class DBParsedAPI(Base):
    __tablename__ = "parsed_apis"

    id = Column(String, primary_key=True, default=generate_uuid)
    title = Column(String, default="")
    description = Column(Text, default="")
    version = Column(String, default="")
    base_url = Column(String, default="")
    spec_json = Column(JSON, nullable=False)
    parsed_at = Column(DateTime, default=datetime.utcnow)


class DBGenerationArtifact(Base):
    __tablename__ = "generation_artifacts"

    id = Column(String, primary_key=True, default=generate_uuid)
    parsed_api_id = Column(String, nullable=True)
    parsed_api_title = Column(String, default="")
    categories_json = Column(JSON, default=list)
    planner_plan_json = Column(JSON, default=dict)
    executor_case_outcomes_json = Column(JSON, default=dict)
    fallback_case_ids_json = Column(JSON, default=list)
    suites_json = Column(JSON, default=list)
    load_scenarios_json = Column(JSON, default=list)
    generation_meta_json = Column(JSON, default=dict)
    raw_llm_outputs_json = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import case, select, func
from sqlalchemy.ext.asyncio import AsyncSession

# Load environment variables early so modules that read os.getenv at import-time
# can see project-level values.
try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - fallback if dotenv is unavailable
    load_dotenv = None  # type: ignore[assignment]

if load_dotenv is not None:
    _BACKEND_DIR = Path(__file__).resolve().parent
    _PROJECT_ROOT = _BACKEND_DIR.parent
    load_dotenv(_PROJECT_ROOT / ".env", override=False)
    load_dotenv(_BACKEND_DIR / ".env", override=False)

from db.database import get_db, init_db
from db.models import (
    DBFlowRun,
    DBFlowScenario,
    DBFlowStepResult,
    DBGenerationArtifact,
    DBLoadTestResult,
    DBLoadTestScenario,
    DBParsedAPI,
    DBTestResult,
    DBTestRun,
    DBTestSuite,
)
from models.schemas import (
    DashboardSummary,
    ExecuteRequest,
    FlowGenerateRequest,
    FlowRunRequest,
    FlowRunStatus,
    FlowScenario,
    FlowStep,
    FlowStepResult,
    GenerateRequest,
    LoadTestMetrics,
    LoadTestPreset,
    LoadTestRunRequest,
    LoadTestScenario,
    LoadTestScenarioUpsertRequest,
    ParsedAPI,
    ParseRequest,
    TestCase,
    TestCategory,
    TestResult,
    TestRunSummary,
    TestSuite,
    WebSocketTestCase,
)
from parser.openapi_parser import parse_openapi
from generator.gemini_generator import (
    GEN_CAPTURE_RAW_LLM,
    GEN_DEBUG_ARTIFACTS,
    GenerationDebugCapture,
    StructuredOutputError,
    UpstreamModelError,
    generate_all,
)
from executor.http_runner import run_test_cases
from executor.ws_runner import run_ws_tests
from loadtest.k6_generator import get_all_load_test_presets, load_test_preset_config
from loadtest.k6_runner import run_k6_test
from loadtest.profiles import (
    LoadProfileResolutionError,
    get_load_test_profiles,
    resolve_profile_headers,
)
from flows.generator import generate_flows
from flows.runner import run_flow_scenario


def _get_env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip() or default


LOG_LEVEL = _get_env_str("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("agentic.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="Agentic API Testing Platform",
    description="AI-powered API testing with Gemini, supporting functional tests, test suites, and load testing",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────
#  Parse
# ──────────────────────────────────────────────

@app.post("/api/parse")
async def parse_spec(req: ParseRequest, db: AsyncSession = Depends(get_db)):
    source = req.spec_url or req.spec_path or req.spec_content
    if not source:
        raise HTTPException(status_code=400, detail="Provide spec_url, spec_path, or spec_content")
    logger.info("parse.start source_kind=%s", "spec_url" if req.spec_url else "spec_path" if req.spec_path else "spec_content")

    try:
        parsed = parse_openapi(source)
    except Exception as e:
        logger.exception("parse.failed error=%s", e)
        raise HTTPException(status_code=400, detail=f"Failed to parse spec: {e}")

    db_parsed = DBParsedAPI(
        id=str(uuid.uuid4()),
        title=parsed.title,
        description=parsed.description,
        version=parsed.version,
        base_url=parsed.base_url,
        spec_json=parsed.model_dump(),
    )
    db.add(db_parsed)
    await db.commit()
    logger.info("parse.complete parsed_api_id=%s title=%s base_url=%s", db_parsed.id, parsed.title, parsed.base_url)

    return {"id": db_parsed.id, "parsed_api": parsed.model_dump()}


# ──────────────────────────────────────────────
#  Generate
# ──────────────────────────────────────────────

@app.post("/api/generate")
async def generate_tests(req: GenerateRequest, db: AsyncSession = Depends(get_db)):
    generation_id = str(uuid.uuid4())
    logger.info("generate.start generation_id=%s categories=%s", generation_id, [c.value for c in req.categories])
    result = await db.execute(select(DBParsedAPI).order_by(DBParsedAPI.parsed_at.desc()).limit(1))
    db_parsed = result.scalar_one_or_none()
    if not db_parsed:
        raise HTTPException(status_code=404, detail="No parsed API found. Call /api/parse first.")

    parsed_api = ParsedAPI(**db_parsed.spec_json)
    debug_capture: GenerationDebugCapture | None = None
    if GEN_DEBUG_ARTIFACTS:
        debug_capture = GenerationDebugCapture(
            generation_id=generation_id,
            parsed_api_id=db_parsed.id,
            parsed_api_title=parsed_api.title,
            categories=[category.value for category in req.categories],
            capture_raw_llm=GEN_CAPTURE_RAW_LLM,
        )

    try:
        suites, load_scenarios, generation_meta = await generate_all(
            parsed_api,
            req.categories,
            debug_capture=debug_capture,
        )
    except StructuredOutputError as e:
        logger.warning(
            "generate.structured_output_error generation_id=%s stage=%s repair_attempted=%s",
            generation_id,
            e.stage,
            e.repair_attempted,
        )
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(e),
                "stage": e.stage,
                "errors": e.errors,
                "repair_attempted": e.repair_attempted,
            },
        )
    except UpstreamModelError as e:
        logger.warning("generate.upstream_error generation_id=%s status_code=%s error=%s", generation_id, e.status_code, e)
        raise HTTPException(status_code=e.status_code, detail=f"Generation failed: {e}")
    except Exception as e:
        logger.exception("generate.failed generation_id=%s error=%s", generation_id, e)
        raise HTTPException(status_code=500, detail=f"Generation failed: {e}")

    batch_created_at = datetime.utcnow()

    for suite in suites:
        db_suite = DBTestSuite(
            id=suite.id,
            name=suite.name,
            description=suite.description,
            category=suite.category.value,
            test_cases_json=[tc.model_dump() for tc in suite.test_cases],
            ws_test_cases_json=[ws.model_dump() for ws in suite.ws_test_cases],
            created_at=batch_created_at,
        )
        db.add(db_suite)

    for scenario in load_scenarios:
        db_scenario = DBLoadTestScenario(
            id=scenario.id,
            name=scenario.name,
            description=scenario.description,
            target_url=scenario.target_url,
            method=scenario.method.value,
            vus=scenario.vus,
            duration=scenario.duration,
            ramp_stages_json=scenario.ramp_stages,
            thresholds_json=scenario.thresholds,
            headers_json=scenario.headers,
            query_params_json=scenario.query_params,
            body_json=scenario.body,
            expected_statuses_json=scenario.expected_statuses,
            created_at=batch_created_at,
        )
        db.add(db_scenario)

    if debug_capture is not None:
        if not debug_capture.final_suites and not debug_capture.final_load_scenarios:
            debug_capture.set_materialized_outputs(suites, load_scenarios)
        if not debug_capture.generation_meta:
            debug_capture.set_generation_meta(generation_meta)

        payload = debug_capture.to_persist_payload(include_raw=GEN_CAPTURE_RAW_LLM)
        db_artifact = DBGenerationArtifact(
            id=generation_id,
            parsed_api_id=payload.get("parsed_api_id"),
            parsed_api_title=str(payload.get("parsed_api_title") or ""),
            categories_json=payload.get("categories") or [],
            planner_plan_json=payload.get("planner_plan") or {},
            executor_case_outcomes_json=payload.get("executor_case_outcomes") or {},
            fallback_case_ids_json=payload.get("fallback_case_ids") or [],
            suites_json=payload.get("final_suites") or [],
            load_scenarios_json=payload.get("final_load_scenarios") or [],
            generation_meta_json=payload.get("generation_meta") or {},
            raw_llm_outputs_json=payload.get("raw_llm_outputs") or [],
            created_at=batch_created_at,
        )
        db.add(db_artifact)

    await db.commit()
    logger.info(
        "generate.complete generation_id=%s suites=%s load_scenarios=%s fallback_count=%s",
        generation_id,
        len(suites),
        len(load_scenarios),
        generation_meta.fallback_count,
    )

    return {
        "generation_id": generation_id,
        "suites": [s.model_dump() for s in suites],
        "load_scenarios": [s.model_dump() for s in load_scenarios],
        "summary": {
            "test_suites_generated": len(suites),
            "total_test_cases": sum(len(s.test_cases) + len(s.ws_test_cases) for s in suites),
            "load_scenarios_generated": len(load_scenarios),
            "batch_created_at": batch_created_at.isoformat(),
        },
        "generation_meta": generation_meta.model_dump(),
    }


# ──────────────────────────────────────────────
#  Execute
# ──────────────────────────────────────────────

@app.post("/api/execute")
async def execute_tests(req: ExecuteRequest, db: AsyncSession = Depends(get_db)):
    logger.info("execute.start suite_ids=%s target_base_url=%s", req.suite_ids, req.target_base_url)
    query = select(DBTestSuite)
    latest_batch_created_at: datetime | None = None
    if req.suite_ids:
        query = query.where(DBTestSuite.id.in_(req.suite_ids))
    else:
        latest_batch_result = await db.execute(select(func.max(DBTestSuite.created_at)))
        latest_batch_created_at = latest_batch_result.scalar_one_or_none()
        if latest_batch_created_at:
            query = query.where(DBTestSuite.created_at == latest_batch_created_at)

    result = await db.execute(query)
    db_suites = result.scalars().all()

    if not db_suites:
        raise HTTPException(status_code=404, detail="No test suites found")

    target_base_url = req.target_base_url
    if not target_base_url:
        latest_parsed_result = await db.execute(
            select(DBParsedAPI).order_by(DBParsedAPI.parsed_at.desc()).limit(1)
        )
        latest_parsed = latest_parsed_result.scalar_one_or_none()
        target_base_url = latest_parsed.base_url if latest_parsed and latest_parsed.base_url else "http://localhost:8080"

    run_id = str(uuid.uuid4())
    started_at = datetime.utcnow()
    all_results: list[TestResult] = []

    for db_suite in db_suites:
        test_cases = [TestCase(**tc) for tc in (db_suite.test_cases_json or [])]
        ws_tests = [WebSocketTestCase(**ws) for ws in (db_suite.ws_test_cases_json or [])]

        if test_cases:
            http_results = run_test_cases(test_cases, target_base_url)
            for r in http_results:
                r.suite_id = db_suite.id
                r.suite_name = db_suite.name
            all_results.extend(http_results)

        if ws_tests:
            ws_results = await run_ws_tests(ws_tests)
            for r in ws_results:
                r.suite_id = db_suite.id
                r.suite_name = db_suite.name
            all_results.extend(ws_results)

    finished_at = datetime.utcnow()
    passed = sum(1 for r in all_results if r.status.value == "passed")
    failed = sum(1 for r in all_results if r.status.value == "failed")
    errors = sum(1 for r in all_results if r.status.value == "error")
    avg_time = sum(r.response_time_ms for r in all_results) / len(all_results) if all_results else 0

    db_run = DBTestRun(
        id=run_id,
        total_tests=len(all_results),
        passed=passed,
        failed=failed,
        errors=errors,
        avg_response_time_ms=round(avg_time, 2),
        started_at=started_at,
        finished_at=finished_at,
    )
    db.add(db_run)

    for r in all_results:
        db_result = DBTestResult(
            id=r.id,
            run_id=run_id,
            test_case_id=r.test_case_id,
            test_case_name=r.test_case_name,
            suite_id=r.suite_id,
            suite_name=r.suite_name,
            endpoint=r.endpoint,
            method=r.method,
            category=r.category.value,
            status=r.status.value,
            expected_status=r.expected_status,
            actual_status=r.actual_status,
            expected_body=r.expected_body,
            actual_body=r.actual_body,
            response_time_ms=r.response_time_ms,
            assertions_passed=r.assertions_passed,
            assertions_total=r.assertions_total,
            error_message=r.error_message,
            executed_at=r.executed_at,
        )
        db.add(db_result)

    await db.commit()
    logger.info(
        "execute.complete run_id=%s total=%s passed=%s failed=%s errors=%s",
        run_id,
        len(all_results),
        passed,
        failed,
        errors,
    )

    return {
        "run_id": run_id,
        "target_base_url": target_base_url,
        "batch_created_at": latest_batch_created_at.isoformat() if latest_batch_created_at else None,
        "total": len(all_results),
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "avg_response_time_ms": round(avg_time, 2),
        "results": [r.model_dump() for r in all_results],
    }


# ──────────────────────────────────────────────
#  Load Tests
# ──────────────────────────────────────────────


def _coerce_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    return {str(k): str(v) for k, v in (headers or {}).items()}


def _merge_headers(
    scenario_headers: dict[str, Any] | None,
    profile_headers: dict[str, str],
    override_headers: dict[str, str],
) -> dict[str, str]:
    merged = _coerce_headers(scenario_headers)
    merged.update(_coerce_headers(profile_headers))
    merged.update(_coerce_headers(override_headers))
    return merged


def _apply_base_url_override(target_url: str, base_url: str | None) -> str:
    if not base_url:
        return target_url

    base = urlsplit(base_url)
    if not base.scheme or not base.netloc:
        return target_url

    target = urlsplit(target_url)
    path = target.path
    if not path:
        path = "/"
    elif not path.startswith("/"):
        path = "/" + path

    return urlunsplit((base.scheme, base.netloc, path, target.query, target.fragment))


def _db_load_scenario_to_model(db_scenario: DBLoadTestScenario) -> LoadTestScenario:
    return LoadTestScenario(
        id=db_scenario.id,
        name=db_scenario.name,
        description=db_scenario.description,
        target_url=db_scenario.target_url,
        method=db_scenario.method,
        vus=db_scenario.vus,
        duration=db_scenario.duration,
        ramp_stages=db_scenario.ramp_stages_json or [],
        thresholds=db_scenario.thresholds_json or {},
        headers=db_scenario.headers_json or {},
        query_params=db_scenario.query_params_json or {},
        body=db_scenario.body_json,
        expected_statuses=db_scenario.expected_statuses_json or [200],
    )


def _scenario_to_dict(scenario: LoadTestScenario, created_at: datetime | None = None) -> dict[str, Any]:
    return {
        "id": scenario.id,
        "name": scenario.name,
        "description": scenario.description,
        "target_url": scenario.target_url,
        "method": scenario.method.value if hasattr(scenario.method, "value") else str(scenario.method),
        "vus": scenario.vus,
        "duration": scenario.duration,
        "ramp_stages": scenario.ramp_stages,
        "thresholds": scenario.thresholds,
        "headers": scenario.headers,
        "query_params": scenario.query_params,
        "body": scenario.body,
        "expected_statuses": scenario.expected_statuses,
        "created_at": created_at.isoformat() if created_at else None,
    }


def _db_load_result_to_dict(db_result: DBLoadTestResult, include_raw: bool = False) -> dict[str, Any]:
    parser_meta = {}
    if isinstance(db_result.raw_metrics, dict):
        maybe_meta = db_result.raw_metrics.get("_parser")
        if isinstance(maybe_meta, dict):
            parser_meta = maybe_meta

    payload = {
        "id": db_result.id,
        "scenario_id": db_result.scenario_id,
        "scenario_name": db_result.scenario_name,
        "total_requests": db_result.total_requests,
        "failed_requests": db_result.failed_requests,
        "avg_response_time_ms": db_result.avg_response_time_ms,
        "min_response_time_ms": db_result.min_response_time_ms,
        "max_response_time_ms": db_result.max_response_time_ms,
        "p50_ms": db_result.p50_ms,
        "p90_ms": db_result.p90_ms,
        "p95_ms": db_result.p95_ms,
        "p99_ms": db_result.p99_ms,
        "requests_per_second": db_result.requests_per_second,
        "error_rate": db_result.error_rate,
        "data_received_kb": db_result.data_received_kb,
        "data_sent_kb": db_result.data_sent_kb,
        "duration_seconds": db_result.duration_seconds,
        "vus_max": db_result.vus_max,
        "runner_status": db_result.runner_status or "passed",
        "runner_message": db_result.runner_message or "",
        "runner_exit_code": db_result.runner_exit_code,
        "runner_stdout_excerpt": db_result.runner_stdout_excerpt or "",
        "runner_stderr_excerpt": db_result.runner_stderr_excerpt or "",
        "metric_shape": db_result.metric_shape or parser_meta.get("metric_shape"),
        "request_count_source": db_result.request_count_source or parser_meta.get("request_count_source"),
        "error_rate_source": db_result.error_rate_source or parser_meta.get("error_rate_source"),
        "parse_warnings": list(
            db_result.parse_warnings_json
            if isinstance(db_result.parse_warnings_json, list)
            else parser_meta.get("parse_warnings") or []
        ),
        "executed_at": db_result.executed_at.isoformat() if db_result.executed_at else None,
    }
    if include_raw:
        payload["raw_metrics"] = db_result.raw_metrics or {}
    return payload


def _apply_load_preset(scenario: LoadTestScenario, preset: LoadTestPreset) -> LoadTestScenario:
    config = load_test_preset_config(preset)
    updated = scenario.model_copy(
        update={
            "vus": int(config.get("vus", scenario.vus)),
            "duration": str(config.get("duration", scenario.duration)),
            "ramp_stages": list(config.get("ramp_stages", scenario.ramp_stages)),
            "thresholds": {
                str(k): [str(v) for v in values]
                for k, values in (config.get("thresholds", scenario.thresholds) or {}).items()
            },
        }
    )
    return updated


@app.post("/api/loadtest/run")
async def run_load_tests(req: LoadTestRunRequest, db: AsyncSession = Depends(get_db)):
    logger.info(
        "loadtest.start scenario_ids=%s target_base_url=%s profile_id=%s",
        req.scenario_ids,
        req.target_base_url,
        req.profile_id,
    )
    query = select(DBLoadTestScenario)
    latest_batch_created_at: datetime | None = None
    if req.scenario_ids:
        query = query.where(DBLoadTestScenario.id.in_(req.scenario_ids))
    else:
        latest_batch_result = await db.execute(select(func.max(DBLoadTestScenario.created_at)))
        latest_batch_created_at = latest_batch_result.scalar_one_or_none()
        if latest_batch_created_at:
            query = query.where(DBLoadTestScenario.created_at == latest_batch_created_at)

    result = await db.execute(query)
    db_scenarios = result.scalars().all()

    if not db_scenarios:
        raise HTTPException(status_code=404, detail="No load test scenarios found")

    selected_profile = None
    profile_headers: dict[str, str] = {}
    profile_base_url: str | None = None
    if req.profile_id:
        profiles = get_load_test_profiles()
        selected_profile = next((profile for profile in profiles if profile.id == req.profile_id), None)
        if not selected_profile:
            raise HTTPException(status_code=404, detail=f"Load profile '{req.profile_id}' not found")
        try:
            profile_headers = resolve_profile_headers(selected_profile)
        except LoadProfileResolutionError as e:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Missing environment variables for load profile headers",
                    "profile_id": e.profile_id,
                    "missing_env_vars": e.missing_env_vars,
                },
            )
        profile_base_url = selected_profile.base_url

    override_headers = _coerce_headers(req.headers_override)
    all_metrics: list[LoadTestMetrics] = []
    passed = 0
    failed = 0
    errors = 0

    for db_scenario in db_scenarios:
        scenario = _db_load_scenario_to_model(db_scenario)
        effective_base_url = req.target_base_url or profile_base_url
        scenario = scenario.model_copy(
            update={
                "target_url": _apply_base_url_override(scenario.target_url, effective_base_url),
                "headers": _merge_headers(scenario.headers, profile_headers, override_headers),
            }
        )

        metrics = run_k6_test(scenario)
        all_metrics.append(metrics)

        if metrics.runner_status == "passed":
            passed += 1
        elif metrics.runner_status == "failed":
            failed += 1
        else:
            errors += 1

        db_result = DBLoadTestResult(
            id=metrics.id,
            scenario_id=metrics.scenario_id,
            scenario_name=metrics.scenario_name,
            total_requests=metrics.total_requests,
            failed_requests=metrics.failed_requests,
            avg_response_time_ms=metrics.avg_response_time_ms,
            min_response_time_ms=metrics.min_response_time_ms,
            max_response_time_ms=metrics.max_response_time_ms,
            p50_ms=metrics.p50_ms,
            p90_ms=metrics.p90_ms,
            p95_ms=metrics.p95_ms,
            p99_ms=metrics.p99_ms,
            requests_per_second=metrics.requests_per_second,
            error_rate=metrics.error_rate,
            data_received_kb=metrics.data_received_kb,
            data_sent_kb=metrics.data_sent_kb,
            duration_seconds=metrics.duration_seconds,
            vus_max=metrics.vus_max,
            runner_status=metrics.runner_status,
            runner_message=metrics.runner_message,
            runner_exit_code=metrics.runner_exit_code,
            runner_stdout_excerpt=metrics.runner_stdout_excerpt,
            runner_stderr_excerpt=metrics.runner_stderr_excerpt,
            metric_shape=metrics.metric_shape,
            request_count_source=metrics.request_count_source,
            error_rate_source=metrics.error_rate_source,
            parse_warnings_json=metrics.parse_warnings,
            raw_metrics=metrics.raw_metrics,
        )
        db.add(db_result)

    await db.commit()
    logger.info("loadtest.complete total_scenarios=%s", len(all_metrics))

    return {
        "batch_created_at": latest_batch_created_at.isoformat() if latest_batch_created_at else None,
        "profile_id": selected_profile.id if selected_profile else None,
        "total_scenarios": len(all_metrics),
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "results": [m.model_dump() for m in all_metrics],
    }


# ──────────────────────────────────────────────
#  Flow Tests
# ──────────────────────────────────────────────

def _db_flow_to_model(db_flow: DBFlowScenario) -> FlowScenario:
    steps = [FlowStep.model_validate(step) for step in (db_flow.steps_json or [])]
    return FlowScenario(
        id=db_flow.id,
        name=db_flow.name,
        description=db_flow.description,
        persona=db_flow.persona or "",
        preconditions=db_flow.preconditions_json or [],
        tags=db_flow.tags_json or [],
        steps=steps,
        created_at=db_flow.created_at or datetime.utcnow(),
        source_generation_id=db_flow.source_generation_id,
    )


def _flow_to_dict(flow: FlowScenario) -> dict[str, Any]:
    return flow.model_dump(mode="json", by_alias=True)


def _db_flow_step_result_to_dict(step: DBFlowStepResult) -> dict[str, Any]:
    return {
        "id": step.id,
        "flow_run_id": step.flow_run_id,
        "flow_id": step.flow_id,
        "step_id": step.step_id,
        "order": step.step_order,
        "status": step.status,
        "resolved_request": step.resolved_request_json or {},
        "response_status": step.response_status,
        "response_headers": step.response_headers_json or {},
        "response_body": step.response_body_json,
        "assertions_passed": step.assertions_passed,
        "assertions_total": step.assertions_total,
        "extracted_context_delta": step.extracted_context_delta_json or {},
        "error_message": step.error_message,
        "executed_at": step.executed_at.isoformat() if step.executed_at else None,
    }


@app.post("/api/flows/generate")
async def generate_flow_tests(req: FlowGenerateRequest, db: AsyncSession = Depends(get_db)):
    flow_generation_id = str(uuid.uuid4())
    logger.info("flow.generate.start flow_generation_id=%s max_flows=%s", flow_generation_id, req.max_flows)

    result = await db.execute(select(DBParsedAPI).order_by(DBParsedAPI.parsed_at.desc()).limit(1))
    db_parsed = result.scalar_one_or_none()
    if not db_parsed:
        raise HTTPException(status_code=404, detail="No parsed API found. Call /api/parse first.")

    parsed_api = ParsedAPI(**db_parsed.spec_json)
    try:
        flows, summary = await generate_flows(parsed_api, req, flow_generation_id)
    except Exception as e:
        logger.exception("flow.generate.failed flow_generation_id=%s error=%s", flow_generation_id, e)
        raise HTTPException(status_code=500, detail=f"Flow generation failed: {e}")

    batch_created_at = datetime.utcnow()
    persisted_flows: list[FlowScenario] = []
    for flow in flows:
        flow_record = flow.model_copy(
            update={
                "source_generation_id": flow_generation_id,
                "created_at": batch_created_at,
            }
        )
        db_flow = DBFlowScenario(
            id=flow_record.id,
            name=flow_record.name,
            description=flow_record.description,
            persona=flow_record.persona,
            preconditions_json=flow_record.preconditions,
            tags_json=flow_record.tags,
            steps_json=[step.model_dump(mode="json", by_alias=True) for step in flow_record.steps],
            source_generation_id=flow_generation_id,
            created_at=batch_created_at,
        )
        db.add(db_flow)
        persisted_flows.append(flow_record)

    await db.commit()
    logger.info("flow.generate.complete flow_generation_id=%s flows=%s", flow_generation_id, len(persisted_flows))

    return {
        "flow_generation_id": flow_generation_id,
        "flows": [_flow_to_dict(flow) for flow in persisted_flows],
        "summary": {
            **summary,
            "batch_created_at": batch_created_at.isoformat(),
        },
    }


@app.get("/api/flows")
async def list_flows(
    latest_batch: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
):
    query = select(DBFlowScenario)
    if latest_batch:
        latest_batch_result = await db.execute(select(func.max(DBFlowScenario.created_at)))
        latest_batch_created_at = latest_batch_result.scalar_one_or_none()
        if latest_batch_created_at:
            query = query.where(DBFlowScenario.created_at == latest_batch_created_at)

    result = await db.execute(query.order_by(DBFlowScenario.created_at.desc()))
    flows = result.scalars().all()

    return [
        {
            "id": flow.id,
            "name": flow.name,
            "description": flow.description,
            "persona": flow.persona,
            "tags": flow.tags_json or [],
            "step_count": len(flow.steps_json or []),
            "source_generation_id": flow.source_generation_id,
            "created_at": flow.created_at.isoformat() if flow.created_at else None,
        }
        for flow in flows
    ]


@app.post("/api/flows/run")
async def run_flows(req: FlowRunRequest, db: AsyncSession = Depends(get_db)):
    logger.info("flow.run.start flow_ids=%s target_base_url=%s", req.flow_ids, req.target_base_url)
    query = select(DBFlowScenario)
    if req.flow_ids:
        query = query.where(DBFlowScenario.id.in_(req.flow_ids))
    else:
        latest_batch_result = await db.execute(select(func.max(DBFlowScenario.created_at)))
        latest_batch_created_at = latest_batch_result.scalar_one_or_none()
        if latest_batch_created_at:
            query = query.where(DBFlowScenario.created_at == latest_batch_created_at)

    result = await db.execute(query)
    db_flows = result.scalars().all()
    if not db_flows:
        raise HTTPException(status_code=404, detail="No flow scenarios found")

    target_base_url = req.target_base_url
    if not target_base_url:
        latest_parsed_result = await db.execute(
            select(DBParsedAPI).order_by(DBParsedAPI.parsed_at.desc()).limit(1)
        )
        latest_parsed = latest_parsed_result.scalar_one_or_none()
        target_base_url = latest_parsed.base_url if latest_parsed and latest_parsed.base_url else "http://localhost:8080"

    run_group_id = str(uuid.uuid4())
    run_records = []
    passed = 0
    failed = 0
    errors = 0

    for db_flow in db_flows:
        flow_model = _db_flow_to_model(db_flow)
        run_record = run_flow_scenario(
            flow_model,
            target_base_url=target_base_url,
            initial_context=req.initial_context,
        )
        run_records.append(run_record)

        db_run = DBFlowRun(
            id=run_record.id,
            flow_id=run_record.flow_id,
            flow_name=run_record.flow_name,
            status=run_record.status.value,
            target_base_url=run_record.target_base_url,
            initial_context_json=run_record.initial_context,
            final_context_json=run_record.final_context,
            started_at=run_record.started_at,
            finished_at=run_record.finished_at,
        )
        db.add(db_run)

        for step_result in run_record.step_results:
            db_step = DBFlowStepResult(
                id=step_result.id,
                flow_run_id=step_result.flow_run_id,
                flow_id=step_result.flow_id,
                step_id=step_result.step_id,
                step_order=step_result.order,
                status=step_result.status.value,
                resolved_request_json=step_result.resolved_request,
                response_status=step_result.response_status,
                response_headers_json=step_result.response_headers,
                response_body_json=step_result.response_body,
                assertions_passed=step_result.assertions_passed,
                assertions_total=step_result.assertions_total,
                extracted_context_delta_json=step_result.extracted_context_delta,
                error_message=step_result.error_message,
                executed_at=step_result.executed_at,
            )
            db.add(db_step)

        if run_record.status == FlowRunStatus.PASSED:
            passed += 1
        elif run_record.status == FlowRunStatus.ERROR:
            errors += 1
        else:
            failed += 1

    await db.commit()
    logger.info("flow.run.complete run_group_id=%s total=%s", run_group_id, len(run_records))

    return {
        "run_group_id": run_group_id,
        "total_flows": len(run_records),
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "flow_runs": [record.model_dump(mode="json", by_alias=True) for record in run_records],
    }


@app.get("/api/flows/runs")
async def list_flow_runs(
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DBFlowRun).order_by(DBFlowRun.started_at.desc()).limit(limit)
    )
    runs = result.scalars().all()
    return [
        {
            "id": run.id,
            "flow_id": run.flow_id,
            "flow_name": run.flow_name,
            "status": run.status,
            "target_base_url": run.target_base_url,
            "initial_context": run.initial_context_json or {},
            "final_context": run.final_context_json or {},
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }
        for run in runs
    ]


@app.get("/api/flows/runs/{run_id}")
async def get_flow_run(run_id: str, db: AsyncSession = Depends(get_db)):
    run_result = await db.execute(select(DBFlowRun).where(DBFlowRun.id == run_id))
    run = run_result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Flow run not found")

    steps_result = await db.execute(
        select(DBFlowStepResult)
        .where(DBFlowStepResult.flow_run_id == run_id)
        .order_by(DBFlowStepResult.step_order.asc(), DBFlowStepResult.executed_at.asc())
    )
    step_results = steps_result.scalars().all()
    return {
        "id": run.id,
        "flow_id": run.flow_id,
        "flow_name": run.flow_name,
        "status": run.status,
        "target_base_url": run.target_base_url,
        "initial_context": run.initial_context_json or {},
        "final_context": run.final_context_json or {},
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "step_results": [_db_flow_step_result_to_dict(step) for step in step_results],
    }


@app.get("/api/flows/{flow_id}")
async def get_flow(flow_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(DBFlowScenario).where(DBFlowScenario.id == flow_id))
    flow = result.scalar_one_or_none()
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")

    return _flow_to_dict(_db_flow_to_model(flow))


@app.put("/api/flows/{flow_id}")
async def update_flow(flow_id: str, updated_flow: FlowScenario, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(DBFlowScenario).where(DBFlowScenario.id == flow_id))
    db_flow = result.scalar_one_or_none()
    if not db_flow:
        raise HTTPException(status_code=404, detail="Flow not found")

    if updated_flow.id and updated_flow.id != flow_id:
        raise HTTPException(status_code=400, detail="Flow ID in payload does not match path")

    db_flow.name = updated_flow.name
    db_flow.description = updated_flow.description
    db_flow.persona = updated_flow.persona
    db_flow.preconditions_json = updated_flow.preconditions
    db_flow.tags_json = updated_flow.tags
    db_flow.steps_json = [step.model_dump(mode="json", by_alias=True) for step in updated_flow.steps]
    if updated_flow.source_generation_id is not None:
        db_flow.source_generation_id = updated_flow.source_generation_id

    await db.commit()
    await db.refresh(db_flow)
    return _flow_to_dict(_db_flow_to_model(db_flow))


# ──────────────────────────────────────────────
#  Read endpoints
# ──────────────────────────────────────────────

@app.get("/api/suites")
async def list_suites(
    include_history: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    query = select(DBTestSuite)
    if not include_history:
        latest_batch_result = await db.execute(select(func.max(DBTestSuite.created_at)))
        latest_batch_created_at = latest_batch_result.scalar_one_or_none()
        if latest_batch_created_at:
            query = query.where(DBTestSuite.created_at == latest_batch_created_at)

    result = await db.execute(query.order_by(DBTestSuite.created_at.desc()))
    suites = result.scalars().all()

    output = []
    for s in suites:
        test_count = len(s.test_cases_json or []) + len(s.ws_test_cases_json or [])

        results_query = await db.execute(
            select(DBTestResult.status, func.count(DBTestResult.id))
            .where(DBTestResult.suite_id == s.id)
            .group_by(DBTestResult.status)
        )
        status_counts = dict(results_query.all())

        output.append({
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "category": s.category,
            "test_count": test_count,
            "passed": status_counts.get("passed", 0),
            "failed": status_counts.get("failed", 0),
            "errors": status_counts.get("error", 0),
            "created_at": s.created_at.isoformat() if s.created_at else None,
        })

    return output


@app.get("/api/suites/{suite_id}")
async def get_suite(suite_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(DBTestSuite).where(DBTestSuite.id == suite_id))
    suite = result.scalar_one_or_none()
    if not suite:
        raise HTTPException(status_code=404, detail="Suite not found")

    return {
        "id": suite.id,
        "name": suite.name,
        "description": suite.description,
        "category": suite.category,
        "test_cases": suite.test_cases_json,
        "ws_test_cases": suite.ws_test_cases_json,
        "created_at": suite.created_at.isoformat() if suite.created_at else None,
    }


@app.get("/api/suites/{suite_id}/results")
async def get_suite_results(suite_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(DBTestResult)
        .where(DBTestResult.suite_id == suite_id)
        .order_by(DBTestResult.executed_at.desc())
    )
    results = result.scalars().all()
    return [_db_result_to_dict(r) for r in results]


@app.get("/api/results")
async def list_results(
    status: str | None = None,
    category: str | None = None,
    endpoint: str | None = None,
    run_id: str | None = None,
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    query = select(DBTestResult)
    if status:
        query = query.where(DBTestResult.status == status)
    if category:
        query = query.where(DBTestResult.category == category)
    if endpoint:
        query = query.where(DBTestResult.endpoint.contains(endpoint))
    if run_id:
        query = query.where(DBTestResult.run_id == run_id)

    query = query.order_by(DBTestResult.executed_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    results = result.scalars().all()
    return [_db_result_to_dict(r) for r in results]


@app.get("/api/results/{result_id}")
async def get_result(result_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(DBTestResult).where(DBTestResult.id == result_id))
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Result not found")
    return _db_result_to_dict(r)


@app.get("/api/loadtest/results")
async def list_load_test_results(
    scenario_id: str | None = None,
    limit: int = Query(default=50, le=200),
    include_raw: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    query = select(DBLoadTestResult)
    if scenario_id:
        query = query.where(DBLoadTestResult.scenario_id == scenario_id)
    query = query.order_by(DBLoadTestResult.executed_at.desc()).limit(limit)

    result = await db.execute(query)
    results = result.scalars().all()

    return [_db_load_result_to_dict(r, include_raw=include_raw) for r in results]


@app.get("/api/loadtest/results/{result_id}")
async def get_load_test_result(
    result_id: str,
    include_raw: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(DBLoadTestResult).where(DBLoadTestResult.id == result_id))
    db_result = result.scalar_one_or_none()
    if not db_result:
        raise HTTPException(status_code=404, detail="Load test result not found")
    return _db_load_result_to_dict(db_result, include_raw=include_raw)


@app.get("/api/loadtest/scenarios")
async def list_load_test_scenarios(
    include_history: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    query = select(DBLoadTestScenario)
    if not include_history:
        latest_batch_result = await db.execute(select(func.max(DBLoadTestScenario.created_at)))
        latest_batch_created_at = latest_batch_result.scalar_one_or_none()
        if latest_batch_created_at:
            query = query.where(DBLoadTestScenario.created_at == latest_batch_created_at)

    result = await db.execute(query.order_by(DBLoadTestScenario.created_at.desc()))
    scenarios = result.scalars().all()

    return [_scenario_to_dict(_db_load_scenario_to_model(s), s.created_at) for s in scenarios]


@app.post("/api/loadtest/scenarios")
async def create_load_test_scenario(
    req: LoadTestScenarioUpsertRequest,
    db: AsyncSession = Depends(get_db),
):
    scenario = LoadTestScenario(
        id=req.id,
        name=req.name,
        description=req.description,
        target_url=req.target_url,
        method=req.method,
        vus=req.vus,
        duration=req.duration,
        ramp_stages=req.ramp_stages,
        thresholds=req.thresholds,
        headers=req.headers,
        query_params=req.query_params,
        body=req.body,
        expected_statuses=req.expected_statuses,
    )
    if req.preset is not None:
        scenario = _apply_load_preset(scenario, req.preset)

    scenario_id = str(uuid.uuid4())
    created_at = datetime.utcnow()
    db_scenario = DBLoadTestScenario(
        id=scenario_id,
        name=scenario.name,
        description=scenario.description,
        target_url=scenario.target_url,
        method=scenario.method.value if hasattr(scenario.method, "value") else str(scenario.method),
        vus=scenario.vus,
        duration=scenario.duration,
        ramp_stages_json=scenario.ramp_stages,
        thresholds_json=scenario.thresholds,
        headers_json=scenario.headers,
        query_params_json=scenario.query_params,
        body_json=scenario.body,
        expected_statuses_json=scenario.expected_statuses,
        created_at=created_at,
    )
    db.add(db_scenario)
    await db.commit()
    await db.refresh(db_scenario)
    return _scenario_to_dict(_db_load_scenario_to_model(db_scenario), db_scenario.created_at)


@app.put("/api/loadtest/scenarios/{scenario_id}")
async def update_load_test_scenario(
    scenario_id: str,
    req: LoadTestScenarioUpsertRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(DBLoadTestScenario).where(DBLoadTestScenario.id == scenario_id))
    db_scenario = result.scalar_one_or_none()
    if not db_scenario:
        raise HTTPException(status_code=404, detail="Load test scenario not found")
    if req.id and req.id != scenario_id:
        raise HTTPException(status_code=400, detail="Scenario ID in payload does not match path")

    scenario = LoadTestScenario(
        id=scenario_id,
        name=req.name,
        description=req.description,
        target_url=req.target_url,
        method=req.method,
        vus=req.vus,
        duration=req.duration,
        ramp_stages=req.ramp_stages,
        thresholds=req.thresholds,
        headers=req.headers,
        query_params=req.query_params,
        body=req.body,
        expected_statuses=req.expected_statuses,
    )
    if req.preset is not None:
        scenario = _apply_load_preset(scenario, req.preset)

    db_scenario.name = scenario.name
    db_scenario.description = scenario.description
    db_scenario.target_url = scenario.target_url
    db_scenario.method = scenario.method.value if hasattr(scenario.method, "value") else str(scenario.method)
    db_scenario.vus = scenario.vus
    db_scenario.duration = scenario.duration
    db_scenario.ramp_stages_json = scenario.ramp_stages
    db_scenario.thresholds_json = scenario.thresholds
    db_scenario.headers_json = scenario.headers
    db_scenario.query_params_json = scenario.query_params
    db_scenario.body_json = scenario.body
    db_scenario.expected_statuses_json = scenario.expected_statuses
    await db.commit()
    await db.refresh(db_scenario)
    return _scenario_to_dict(_db_load_scenario_to_model(db_scenario), db_scenario.created_at)


@app.delete("/api/loadtest/scenarios/{scenario_id}")
async def delete_load_test_scenario(
    scenario_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(DBLoadTestScenario).where(DBLoadTestScenario.id == scenario_id))
    db_scenario = result.scalar_one_or_none()
    if not db_scenario:
        raise HTTPException(status_code=404, detail="Load test scenario not found")
    await db.delete(db_scenario)
    await db.commit()
    return {"id": scenario_id, "deleted": True}


@app.get("/api/loadtest/profiles")
async def list_load_test_profiles():
    profiles = get_load_test_profiles()
    return {
        "profiles": [profile.model_dump(mode="json") for profile in profiles],
        "presets": get_all_load_test_presets(),
    }


def _count_suite_tests(suites_json: list[dict[str, Any]] | None) -> int:
    if not suites_json:
        return 0
    total = 0
    for suite in suites_json:
        if not isinstance(suite, dict):
            continue
        total += len(suite.get("test_cases") or [])
        total += len(suite.get("ws_test_cases") or [])
    return total


def _artifact_summary(artifact: DBGenerationArtifact) -> dict[str, Any]:
    generation_meta = artifact.generation_meta_json or {}
    suites = artifact.suites_json or []
    load_scenarios = artifact.load_scenarios_json or []
    return {
        "generation_id": artifact.id,
        "parsed_api_id": artifact.parsed_api_id,
        "parsed_api_title": artifact.parsed_api_title,
        "categories": artifact.categories_json or [],
        "suites_generated": len(suites),
        "total_test_cases": _count_suite_tests(suites),
        "load_scenarios_generated": len(load_scenarios),
        "executor_jobs_total": generation_meta.get("executor_jobs_total", 0),
        "executor_jobs_failed": generation_meta.get("executor_jobs_failed", 0),
        "fallback_count": generation_meta.get("fallback_count", 0),
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
    }


@app.get("/api/generations")
async def list_generations(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DBGenerationArtifact)
        .order_by(DBGenerationArtifact.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    artifacts = result.scalars().all()
    return [_artifact_summary(artifact) for artifact in artifacts]


@app.get("/api/generations/{generation_id}")
async def get_generation_artifact(
    generation_id: str,
    include_raw: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(DBGenerationArtifact).where(DBGenerationArtifact.id == generation_id))
    artifact = result.scalar_one_or_none()
    if not artifact:
        raise HTTPException(status_code=404, detail="Generation artifact not found")

    return {
        "generation_id": artifact.id,
        "parsed_api_id": artifact.parsed_api_id,
        "parsed_api_title": artifact.parsed_api_title,
        "categories": artifact.categories_json or [],
        "planner_plan": artifact.planner_plan_json or {},
        "executor_case_outcomes": artifact.executor_case_outcomes_json or {},
        "fallback_case_ids": artifact.fallback_case_ids_json or [],
        "suites": artifact.suites_json or [],
        "load_scenarios": artifact.load_scenarios_json or [],
        "generation_meta": artifact.generation_meta_json or {},
        "raw_llm_outputs": (artifact.raw_llm_outputs_json or []) if include_raw else [],
        "raw_llm_outputs_included": include_raw,
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
    }


@app.get("/api/dashboard/summary")
async def dashboard_summary(db: AsyncSession = Depends(get_db)):
    latest_run_result = await db.execute(
        select(DBTestRun.id).order_by(DBTestRun.started_at.desc()).limit(1)
    )
    latest_run_id = latest_run_result.scalar_one_or_none()

    total = 0
    passed = 0
    failed = 0
    errors = 0
    avg_time = 0.0
    functional_summary: dict[str, int] = {}
    suite_summary: dict[str, int] = {}

    if latest_run_id:
        result = await db.execute(
            select(
                func.count(DBTestResult.id),
                func.sum(case((DBTestResult.status == "passed", 1), else_=0)),
                func.sum(case((DBTestResult.status == "failed", 1), else_=0)),
                func.sum(case((DBTestResult.status == "error", 1), else_=0)),
                func.avg(DBTestResult.response_time_ms),
            ).where(DBTestResult.run_id == latest_run_id)
        )
        row = result.one()
        total = row[0] or 0
        passed = row[1] or 0
        failed = row[2] or 0
        errors = row[3] or 0
        avg_time = round(row[4] or 0, 2)

        func_result = await db.execute(
            select(DBTestResult.status, func.count(DBTestResult.id))
            .where(DBTestResult.category == "individual", DBTestResult.run_id == latest_run_id)
            .group_by(DBTestResult.status)
        )
        functional_summary = dict(func_result.all())

        suite_result = await db.execute(
            select(DBTestResult.status, func.count(DBTestResult.id))
            .where(DBTestResult.category == "suite", DBTestResult.run_id == latest_run_id)
            .group_by(DBTestResult.status)
        )
        suite_summary = dict(suite_result.all())

    load_result = await db.execute(
        select(DBLoadTestResult).order_by(DBLoadTestResult.executed_at.desc()).limit(1)
    )
    latest_load = load_result.scalar_one_or_none()
    load_summary: dict[str, Any] = {}
    if latest_load:
        load_summary = {
            "avg_response_time_ms": latest_load.avg_response_time_ms,
            "p95_ms": latest_load.p95_ms,
            "requests_per_second": latest_load.requests_per_second,
            "error_rate": latest_load.error_rate,
        }

    runs_result = await db.execute(
        select(DBTestRun).order_by(DBTestRun.started_at.desc()).limit(10)
    )
    recent_runs = [
        TestRunSummary(
            id=r.id,
            total_tests=r.total_tests,
            passed=r.passed,
            failed=r.failed,
            errors=r.errors,
            avg_response_time_ms=r.avg_response_time_ms,
            started_at=r.started_at,
            finished_at=r.finished_at,
        )
        for r in runs_result.scalars().all()
    ]

    pass_rate = round((passed / total * 100), 1) if total > 0 else 0

    return DashboardSummary(
        total_tests=total,
        passed=passed,
        failed=failed,
        errors=errors,
        pass_rate=pass_rate,
        avg_response_time_ms=avg_time,
        functional_summary=functional_summary,
        suite_summary=suite_summary,
        load_summary=load_summary,
        recent_runs=recent_runs,
    ).model_dump()


@app.get("/api/runs")
async def list_runs(limit: int = Query(default=20, le=100), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(DBTestRun).order_by(DBTestRun.started_at.desc()).limit(limit)
    )
    runs = result.scalars().all()
    return [
        {
            "id": r.id,
            "total_tests": r.total_tests,
            "passed": r.passed,
            "failed": r.failed,
            "errors": r.errors,
            "avg_response_time_ms": r.avg_response_time_ms,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        }
        for r in runs
    ]


def _db_result_to_dict(r: DBTestResult) -> dict:
    return {
        "id": r.id,
        "run_id": r.run_id,
        "test_case_id": r.test_case_id,
        "test_case_name": r.test_case_name,
        "suite_id": r.suite_id,
        "suite_name": r.suite_name,
        "endpoint": r.endpoint,
        "method": r.method,
        "category": r.category,
        "status": r.status,
        "expected_status": r.expected_status,
        "actual_status": r.actual_status,
        "expected_body": r.expected_body,
        "actual_body": r.actual_body,
        "response_time_ms": r.response_time_ms,
        "assertions_passed": r.assertions_passed,
        "assertions_total": r.assertions_total,
        "error_message": r.error_message,
        "executed_at": r.executed_at.isoformat() if r.executed_at else None,
    }

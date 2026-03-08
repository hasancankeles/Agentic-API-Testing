from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import case, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import get_db, init_db
from db.models import (
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
    GenerateRequest,
    LoadTestMetrics,
    LoadTestRunRequest,
    LoadTestScenario,
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
    StructuredOutputError,
    UpstreamModelError,
    generate_all,
)
from executor.http_runner import run_test_cases
from executor.ws_runner import run_ws_tests
from loadtest.k6_runner import run_k6_test


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

    try:
        parsed = parse_openapi(source)
    except Exception as e:
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

    return {"id": db_parsed.id, "parsed_api": parsed.model_dump()}


# ──────────────────────────────────────────────
#  Generate
# ──────────────────────────────────────────────

@app.post("/api/generate")
async def generate_tests(req: GenerateRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(DBParsedAPI).order_by(DBParsedAPI.parsed_at.desc()).limit(1))
    db_parsed = result.scalar_one_or_none()
    if not db_parsed:
        raise HTTPException(status_code=404, detail="No parsed API found. Call /api/parse first.")

    parsed_api = ParsedAPI(**db_parsed.spec_json)

    try:
        suites, load_scenarios, generation_meta = await generate_all(parsed_api, req.categories)
    except StructuredOutputError as e:
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
        raise HTTPException(status_code=e.status_code, detail=f"Generation failed: {e}")
    except Exception as e:
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
            created_at=batch_created_at,
        )
        db.add(db_scenario)

    await db.commit()

    return {
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

@app.post("/api/loadtest/run")
async def run_load_tests(req: LoadTestRunRequest, db: AsyncSession = Depends(get_db)):
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

    all_metrics: list[LoadTestMetrics] = []

    for db_scenario in db_scenarios:
        scenario = LoadTestScenario(
            id=db_scenario.id,
            name=db_scenario.name,
            description=db_scenario.description,
            target_url=db_scenario.target_url.replace(
                "http://localhost:8080", req.target_base_url
            ) if req.target_base_url != "http://localhost:8080" else db_scenario.target_url,
            method=db_scenario.method,
            vus=db_scenario.vus,
            duration=db_scenario.duration,
            ramp_stages=db_scenario.ramp_stages_json or [],
            thresholds=db_scenario.thresholds_json or {},
            headers=db_scenario.headers_json or {},
        )

        metrics = run_k6_test(scenario)
        all_metrics.append(metrics)

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
            raw_metrics=metrics.raw_metrics,
        )
        db.add(db_result)

    await db.commit()

    return {
        "batch_created_at": latest_batch_created_at.isoformat() if latest_batch_created_at else None,
        "total_scenarios": len(all_metrics),
        "results": [m.model_dump() for m in all_metrics],
    }


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
    db: AsyncSession = Depends(get_db),
):
    query = select(DBLoadTestResult)
    if scenario_id:
        query = query.where(DBLoadTestResult.scenario_id == scenario_id)
    query = query.order_by(DBLoadTestResult.executed_at.desc()).limit(limit)

    result = await db.execute(query)
    results = result.scalars().all()

    return [
        {
            "id": r.id,
            "scenario_id": r.scenario_id,
            "scenario_name": r.scenario_name,
            "total_requests": r.total_requests,
            "failed_requests": r.failed_requests,
            "avg_response_time_ms": r.avg_response_time_ms,
            "min_response_time_ms": r.min_response_time_ms,
            "max_response_time_ms": r.max_response_time_ms,
            "p50_ms": r.p50_ms,
            "p90_ms": r.p90_ms,
            "p95_ms": r.p95_ms,
            "p99_ms": r.p99_ms,
            "requests_per_second": r.requests_per_second,
            "error_rate": r.error_rate,
            "data_received_kb": r.data_received_kb,
            "data_sent_kb": r.data_sent_kb,
            "duration_seconds": r.duration_seconds,
            "vus_max": r.vus_max,
            "executed_at": r.executed_at.isoformat() if r.executed_at else None,
        }
        for r in results
    ]


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

    return [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "target_url": s.target_url,
            "method": s.method,
            "vus": s.vus,
            "duration": s.duration,
            "ramp_stages": s.ramp_stages_json,
            "thresholds": s.thresholds_json,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in scenarios
    ]


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

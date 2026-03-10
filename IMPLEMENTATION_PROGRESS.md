# Implementation Progress

## Summary

This project evolved from a single-call test generator into a robust two-stage generation and execution pipeline:

1. OpenAPI parsing and normalization
2. Planner LLM generates structured test plan drafts
3. Per-test executor LLM materializes HTTP tests with controlled concurrency
4. Deterministic fallback keeps tests valid even when executor calls fail
5. Tests are executed and tracked with run-level and dashboard summaries

This milestone adds a backend debug artifact trail and retrieval APIs to make failures easier to diagnose.

## Milestones Achieved

### 1) Structured planning contract

- Added strict planner schemas (test drafts, suite drafts, load drafts) with validation rules.
- Enforced endpoint normalization, status validation, and duplicate ID checks.
- Added structured repair flow when planner/executor outputs are invalid.

### 2) Two-agent generation pipeline

- Split generation into:
  - Planner stage for intent and structure
  - Executor stage for concrete test implementation
- Added queue-based per-test executor flow for HTTP tests with bounded concurrency.
- Added deterministic fallback policy on per-case failures.

### 3) API compatibility and robustness

- Preserved existing `/api/parse`, `/api/generate`, and `/api/execute` workflows.
- Added additive `generation_meta` diagnostics without breaking frontend contracts.
- Improved error semantics:
  - `422` for structured-output validation failures
  - `502/503` for upstream model failures

### 4) Latest-batch execution behavior

- Default execution/listing flows now target the latest generated batch when explicit IDs are not provided.
- Reduced accidental cross-batch mixing.

### 5) Debug artifact trail (current milestone)

- Added persistent generation artifacts in SQLite (`generation_artifacts` table).
- Added `generation_id` in `/api/generate` response for traceability.
- Captured high-signal generation internals:
  - Validated planner plan
  - Per-case executor outcomes (success/failure, fallback used, error message)
  - Final materialized suites and load scenarios
  - Generation metadata and queue counters
- Added optional raw LLM output capture with redaction:
  - `GEN_CAPTURE_RAW_LLM=false` by default
  - Sensitive fields (`authorization`, `token`, `api_key`, `password`, `cookie`, etc.) are redacted before persistence
- Added debug retrieval APIs:
  - `GET /api/generations`
  - `GET /api/generations/{generation_id}?include_raw=false`
- Added structured backend logs for parse/generate/execute/loadtest lifecycles.

## Current Architecture (High Level)

1. **Parse**: OpenAPI source is parsed into normalized `ParsedAPI`.
2. **Generate**:
   - Planner creates a strict `PlannerTestPlan`
   - HTTP drafts become executor jobs in an async worker queue
   - Per-case failures fall back to deterministic draft conversion
   - Generated artifacts and diagnostics are persisted
3. **Execute**: Generated suites are executed (latest batch by default) and results persisted.
4. **Observe**:
   - Dashboard summarizes latest run
   - Suites/results endpoints provide execution details
   - Generations endpoints provide generation internals for debugging

## How To Use These Features

### 1) Start backend with debug settings

From project root:

```bash
source .venv/bin/activate
set -a; source .env; set +a
export GEN_DEBUG_ARTIFACTS=true
export GEN_CAPTURE_RAW_LLM=false
export LOG_LEVEL=INFO
cd backend
uvicorn main:app --reload --port 8000
```

Notes:
- Restart backend after changing env flags.
- `GEN_CAPTURE_RAW_LLM=false` is the safe default.

### 2) Generate tests from UI

1. Open Dashboard in frontend.
2. Enter OpenAPI spec URL/path.
3. Click `Parse`.
4. Click `Generate Tests`.

`/api/generate` now returns a `generation_id` for trace lookup.

### 3) Inspect generation artifacts via API

```bash
# List recent generations
curl -s http://localhost:8000/api/generations | jq

# Inspect one generation artifact
GEN_ID="<generation_id>"
curl -s "http://localhost:8000/api/generations/$GEN_ID" | jq
```

This payload includes:
- `planner_plan`
- `executor_case_outcomes`
- `fallback_case_ids`
- `suites`
- `load_scenarios`
- `generation_meta`

### 4) (Optional) include raw LLM outputs

Enable raw capture:

```bash
export GEN_CAPTURE_RAW_LLM=true
```

Then restart backend, generate again, and call:

```bash
curl -s "http://localhost:8000/api/generations/$GEN_ID?include_raw=true" | jq
```

### 5) Read logs for lifecycle visibility

Backend logs now include lifecycle events:
- `parse.start` / `parse.complete`
- `generate.start` / `generate.complete`
- `execute.start` / `execute.complete`
- `loadtest.start` / `loadtest.complete`

These logs are the fastest way to diagnose where a run failed.

## Known Limitations

- Public demo APIs (e.g., Petstore) can be unstable and produce frequent 5xx responses.
- Full auth/stateful workflow compatibility is still limited.
- Artifact retention is currently indefinite (no cleanup policy yet).
- Debug UI integration is deferred; debug data is currently available via backend APIs.

## Recommended Next Improvements

1. Add frontend debug view for generation artifacts and case-level lineage.
2. Add retention/cleanup policy for generation artifacts and raw outputs.
3. Introduce retry/backoff and flakiness classification for unstable targets.
4. Add auth plugins and stateful dependency handling for broader API compatibility.
5. Add optional strict response mode (full-body validation) for stable environments.

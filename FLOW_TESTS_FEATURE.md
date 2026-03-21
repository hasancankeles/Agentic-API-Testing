# Flow Tests Feature Documentation

## 1. What This Feature Does

The Flow Tests feature generates and executes realistic, stateful API user journeys such as:

- login -> browse items -> open detail -> perform action
- create resource -> read back -> verify state
- transaction create -> transaction lookup

Unlike isolated single-endpoint tests, a flow is an ordered sequence where later steps can reuse values extracted from earlier responses (for example token, id, order_id).

This feature is available through:

- Backend API: `/api/flows/*`
- Frontend page: `/flows` ("Flow Tests" in nav)


## 2. High-Level Architecture

Main modules:

- Parser enrichment: `backend/parser/openapi_parser.py`
- Flow generation: `backend/flows/generator.py`
- Flow execution: `backend/flows/runner.py`
- API routes: `backend/main.py`
- Persistence models: `backend/db/models.py`
- Pydantic contracts: `backend/models/schemas.py`
- Frontend page: `frontend/src/pages/FlowTests.tsx`

Data lifecycle:

1. Parse OpenAPI spec into enriched `ParsedAPI`
2. Generate flow scenarios from API semantics
3. Persist scenarios
4. Run scenarios against target base URL with runtime context
5. Persist run + step traces
6. Display in Flow Tests UI history/detail views


## 3. Data Model

### 3.1 Parsed API enrichment

Each parsed endpoint now contains more semantic hints used by flow generation:

- `security`
- `requires_auth`
- `request_body_required_fields`
- `request_body_example`
- `response_examples`
- response `links` (OpenAPI Links)

These are defined in `ParsedEndpoint` / `ParsedResponse` inside `backend/models/schemas.py`.

### 3.2 Flow domain objects

Core schema models in `backend/models/schemas.py`:

- `FlowScenario`
- `FlowStep`
- `FlowExtractRule`
- `FlowRunRecord`
- `FlowStepResult`
- `FlowGenerateRequest`
- `FlowRunRequest`

Important behavior:

- Step ordering and duplicate `step_id` are validated.
- `FlowExtractRule` supports `from: body | headers | status_code`.
- `initial_context` defaults to `{}`.

### 3.3 Database tables

SQLite tables in `backend/db/models.py`:

- `flow_scenarios`
- `flow_runs`
- `flow_step_results`

Persisted data includes full step-level trace payloads:

- resolved request
- response status/headers/body
- assertion counts
- extracted context delta
- error message


## 4. Flow Generation Pipeline (How It Works Internally)

Entry point: `generate_flows(...)` in `backend/flows/generator.py`

### Step 1: Infer objectives

If `objectives` are not explicitly provided, objectives are inferred from API structure:

- tags
- operationId
- summary/description keywords
- auth patterns
- resource/action patterns

Examples of inferred objectives:

- authentication and session workflow
- browse and discovery workflow
- detail retrieval workflow
- interaction workflow
- transactional lifecycle workflow
- create and verify workflow

### Step 2: Build dependency hints and graph

Dependency hints come from:

- OpenAPI Links (highest signal)
- path params and common id/token patterns (`id`, `*Id`, `token`, `location`)
- producer-consumer variable relationships

These hints are converted to dependency edges and used to build valid step chains.

### Step 3: Build deterministic seed flows

Before LLM refinement, the generator creates executable seed flows with:

- ordered steps
- extraction rules
- context variable reuse
- expected status/assertions defaults

This deterministic layer ensures there is always a runnable baseline.

### Step 4: Optional multi-pass LLM refinement

When mode allows and API key is available, LLM refinement runs as:

1. Scenario planner pass
2. Step composer pass
3. Critic/repair pass

If any stage fails or outputs invalid structures, system falls back safely to deterministic flows.

### Step 5: Quality gates

Before persistence, flows go through quality checks:

- unresolved path placeholders are rejected
- broken/dead variable chains are rejected
- step ordering/dependency coherence enforced
- mutating flows require read-after-write style verification
- mutation policy constraints enforced (`safe | balanced | full_lifecycle`)

Summary metadata records:

- source (`llm_refined` or `deterministic_fallback`)
- fallback reason
- objectives used
- dependency hint counts
- generation mode and mutation policy


## 5. Flow Runner (How Execution Works)

Entry point: `run_flow_scenario(...)` in `backend/flows/runner.py`

### Context behavior

Each run starts with:

- `ctx.run_id`
- `ctx.timestamp`
- merged `initial_context` from request

Template syntax is supported in endpoint/path/query/header/body:

- `{{ctx.some_key}}`
- nested keys supported, for example `{{ctx.auth.token}}`

### Step execution

For each step:

1. Resolve templates from current context
2. Execute HTTP request
3. Validate `expected_status` and custom assertions
4. Run extraction rules (`body`, `headers`, `status_code`)
5. Merge extracted values into context (only if step passes)
6. Save step result trace

### Failure policy

- If a required step fails (`required: true`), execution stops immediately.
- If a non-required step fails (`required: false`), flow continues.
- Final flow status becomes `passed`, `failed`, or `error`.


## 6. Backend API Contract

Routes in `backend/main.py`:

- `POST /api/flows/generate`
- `GET /api/flows`
- `GET /api/flows/{flow_id}`
- `PUT /api/flows/{flow_id}`
- `POST /api/flows/run`
- `GET /api/flows/runs`
- `GET /api/flows/runs/{run_id}`

Important request fields:

- Generation:
  - `max_flows`
  - `max_steps_per_flow`
  - `include_negative`
  - `generation_mode` (`hybrid_auto | llm_first | deterministic_first`)
  - `mutation_policy` (`safe | balanced | full_lifecycle`)
  - `personas`
  - `app_context`
- Run:
  - `flow_ids` (optional; if omitted, latest batch is used)
  - `target_base_url` (optional)
  - `initial_context` (JSON object)


## 7. Frontend Workflow (`/flows`)

Implemented in `frontend/src/pages/FlowTests.tsx`.

The page has 4 sections:

1. Generate Flows
2. Latest Flow Batch (list + selection + detail)
3. Run Flows
4. Flow Run History + Run Detail

Behavior highlights:

- Invalid JSON in `app_context` or `initial_context` blocks submit.
- Backend errors are shown with `detail` when present.
- Run trace JSON blocks are collapsed by default for large payloads.
- You can run either selected flows or latest batch fallback.


## 8. Step-by-Step Usage (Demo)

1. Start backend and frontend.
2. Parse an OpenAPI spec (Dashboard parse, or parse API route).
3. Open `Flow Tests` page.
4. Click `Generate Flows` (defaults are enough for first run).
5. Verify generated list appears in Latest Flow Batch.
6. In Run panel:
   - set `target_base_url`
   - keep `initial_context` as `{}` or add credentials/tokens if needed
7. Select flows and click `Run Selected` (or click `Run Latest Batch`).
8. Verify run-group summary.
9. Open latest entry in Flow Run History.
10. Inspect step-level traces:
    - status/method/endpoint
    - response code
    - resolved request
    - response body
    - extracted context delta


## 9. Validation and Test Coverage

Backend automated tests:

- `backend/tests/test_flow_generator.py`
- `backend/tests/test_flow_runner.py`
- `backend/tests/test_flow_routes.py`

Covered areas include:

- objective inference
- dependency hints (including OpenAPI Links)
- LLM fallback behavior
- quality gates
- template resolution
- extraction from body/headers/status
- fail-fast required-step behavior
- route contracts for generate/run/history/detail

Frontend validation used in this phase:

- `npm run lint`
- `npm run build`
- manual QA on `/flows` page


## 10. Current Limits

- Protocol scope is HTTP flows.
- No dedicated flow edit UI yet (backend `PUT /api/flows/{flow_id}` exists).
- Real execution of auth-protected APIs may require `initial_context` values.
- Public demo APIs may be unstable or return noisy datasets.

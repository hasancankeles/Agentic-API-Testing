# Load Tests and Flow Tests: Brief Technical Overview

## 1) What this system does

This project provides two testing modes for APIs:

- **Load Tests**: Measures performance under traffic (latency, throughput, failures).
- **Flow Tests**: Validates realistic multi-step user journeys (stateful API behavior).

Both are run from the UI and exposed through FastAPI routes.

## 2) System flow at a glance

1. Parse OpenAPI spec (`/api/parse`).
2. Generate test artifacts:
   - General/load artifacts (`/api/generate`)
   - Flow artifacts (`/api/flows/generate`)
3. Execute tests:
   - Load (`/api/loadtest/run`)
   - Flow (`/api/flows/run`)
4. Persist results to SQLite.
5. Show run history and details in the frontend.

---

## 3) Load tests: how they work

### Creation

Load scenarios come from:

- AI planning pipeline in `backend/generator/gemini_generator.py` (during `/api/generate`), or
- Manual CRUD from the Load Tests page (`/api/loadtest/scenarios`).

A load scenario includes:

- target URL + HTTP method
- VUs and duration (or ramp stages)
- thresholds
- headers/query/body
- expected statuses

### Execution

Implemented in:

- `backend/loadtest/k6_generator.py`
- `backend/loadtest/k6_runner.py`

Execution path:

1. Backend converts scenario into a k6 JS script.
2. Runs `k6 run --summary-export ...`.
3. Parses k6 summary JSON.
4. Stores metrics in `load_test_results`.

### Output

Typical output metrics:

- avg/p90/p95/p99 response time
- total requests, requests/sec
- failed requests, error rate
- VU max, data sent/received
- runner status/message/stdout/stderr excerpts

---

## 4) Flow tests: how they work

### Generation

Implemented in `backend/flows/generator.py`.

Generation has two layers:

1. **Deterministic seed generation (rule-based)**
2. **Optional AI refinement (Gemini)**

Deterministic generation is not tied to fixed URLs. It reads OpenAPI structure and uses common API patterns:

- producers (endpoints likely creating id/token)
- consumers (endpoints requiring id/token)
- auth requirements
- path params and response examples

So it can connect different APIs by dependency, for example:

- step 1 extracts `order_id`
- step 2 reuses `{{ctx.order_id}}` in `/orders/{orderId}`

If `GEMINI_API_KEY` is available and mode allows, AI refines the generated flows. If AI fails, deterministic fallback is used.

### Execution

Implemented in `backend/flows/runner.py`.

Execution path:

1. Initialize runtime context (`run_id`, timestamp, optional initial context).
2. Execute steps in order.
3. Resolve templates like `{{ctx.token}}`.
4. Run HTTP request.
5. Validate expected status + assertions.
6. Extract values from body/headers/status and update context.
7. Persist per-step traces.

Fail behavior:

- required step fails -> stop flow
- optional step fails -> continue

### Output

Flow run output includes:

- run-level status (`passed`, `failed`, `error`)
- step-by-step request/response traces
- assertion counts
- extracted context deltas
- final context after journey

---

## 5) Where AI is used

AI is used in generation, not in execution:

- `/api/generate`: AI plans test artifacts (including load scenario drafts)
- `/api/flows/generate`: AI can refine deterministic flow candidates

Execution itself is deterministic runtime code (`k6` and HTTP step runner).

---

## 6) Quick UI demo plan (for presentation)

## Load Tests demo

1. Open `/load-tests`.
2. Create scenario (GET + public endpoint, expected status 200).
3. Apply `smoke` preset.
4. Click **Run Selected**.
5. Show summary cards (`passed/failed/errors`).
6. Open run detail and show p95, RPS, runner message.

## Flow Tests demo

1. Parse an OpenAPI spec first (Dashboard parse).
2. Open `/flows`.
3. Generate flows (`deterministic_first` for stable demo, or `hybrid_auto` if key exists).
4. Run selected flow(s).
5. Show step trace:
   - resolved request
   - response status
   - extracted values
   - final context

---

## 7) Key tables and modules

Core backend modules:

- Load: `backend/loadtest/k6_generator.py`, `backend/loadtest/k6_runner.py`, `backend/loadtest/profiles.py`
- Flow: `backend/flows/generator.py`, `backend/flows/runner.py`
- API routes: `backend/main.py`
- Schema/models: `backend/models/schemas.py`, `backend/db/models.py`

Main database tables:

- `load_test_scenarios`, `load_test_results`
- `flow_scenarios`, `flow_runs`, `flow_step_results`

---

## 8) Known limitations (brief)

- Some flow generation controls exist in request/UI but are lightly used (`include_negative`).
- Long runs use blocking operations inside async routes (can reduce concurrency).
- Base URL override behavior may be surprising for APIs hosted under nested base paths.
- k6 script files are persisted in `k6-scripts/` unless manually cleaned.

---

## 9) One-minute summary for professor

- The system has two complementary modes:
  - **Load tests** answer: "How does API perform under traffic?"
  - **Flow tests** answer: "Does the real multi-step user journey work correctly?"
- Flow creation is **deterministic first**, optionally **AI-refined**.
- Execution is fully runtime code with persisted, auditable traces.

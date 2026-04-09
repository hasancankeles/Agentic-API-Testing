import { useCallback, useEffect, useMemo, useState } from "react";
import {
  createLoadTestScenario,
  deleteLoadTestScenario,
  getLoadTestProfiles,
  getLoadTestResult,
  getLoadTestResults,
  getLoadTestScenarios,
  runLoadTests,
  updateLoadTestScenario,
  type LoadTestPreset,
  type LoadTestProfile,
  type LoadTestResult,
  type LoadTestRunResponse,
  type LoadTestScenario,
  type LoadTestScenarioUpsertRequest,
} from "../api/client";
import StatusBadge, { type TestStatus } from "../components/StatusBadge";

function unwrap<T>(res: { data?: T } | T): T {
  const d = res as { data?: T };
  return (d.data !== undefined ? d.data : res) as T;
}

function toPrettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) {
    return "-";
  }
  return new Date(dateStr).toLocaleString();
}

function extractErrorMessage(error: unknown, fallback: string): string {
  if (typeof error === "object" && error !== null) {
    const maybe = error as {
      message?: unknown;
      response?: {
        data?: {
          detail?: unknown;
        };
      };
    };
    const detail = maybe.response?.data?.detail;
    if (typeof detail === "string" && detail.trim()) {
      return detail;
    }
    if (detail !== undefined && detail !== null) {
      return toPrettyJson(detail);
    }
    if (typeof maybe.message === "string" && maybe.message.trim()) {
      return maybe.message;
    }
  }
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return fallback;
}

function toStatus(value: string): TestStatus {
  if (
    value === "passed" ||
    value === "failed" ||
    value === "error" ||
    value === "pending" ||
    value === "running"
  ) {
    return value;
  }
  return "pending";
}

function isSuspiciousLoadRun(result: LoadTestResult): boolean {
  const warnings = result.parse_warnings ?? [];
  if (warnings.length > 0) {
    return true;
  }
  return result.runner_status === "passed" && result.total_requests === 0;
}

function diagnosticInterpretation(result: LoadTestResult): string {
  if (result.runner_status === "error") {
    return "Runner error occurred. Review stderr/stdout excerpts and parser warnings.";
  }
  if (result.runner_status === "failed") {
    if ((result.runner_message || "").toLowerCase().includes("threshold")) {
      return "k6 executed but one or more thresholds failed.";
    }
    return "k6 returned a non-zero exit code with metrics available.";
  }
  if (result.total_requests === 0) {
    return "Run is marked passed but request count is zero. Treat this run as suspicious.";
  }
  return "Metrics look consistent: requests were executed and parsed.";
}

function thresholdInterpretation(result: LoadTestResult): string {
  const message = (result.runner_message || "").toLowerCase();
  if (message.includes("threshold")) {
    return "Threshold breach detected.";
  }
  if (result.runner_status === "passed") {
    return "No threshold breach reported by runner.";
  }
  return "Threshold status unclear; inspect raw metrics and runner output.";
}

function parseJsonObject(
  raw: string,
  fieldName: string
): { value: Record<string, unknown> | null; error: string | null } {
  const trimmed = raw.trim();
  if (!trimmed) {
    return { value: {}, error: null };
  }

  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
      return { value: null, error: `${fieldName} must be a JSON object.` };
    }
    return { value: parsed as Record<string, unknown>, error: null };
  } catch {
    return { value: null, error: `${fieldName} is not valid JSON.` };
  }
}

function parseJsonAny(
  raw: string,
  fieldName: string
): { value: unknown; error: string | null } {
  const trimmed = raw.trim();
  if (!trimmed) {
    return { value: null, error: null };
  }

  try {
    return { value: JSON.parse(trimmed), error: null };
  } catch {
    return { value: null, error: `${fieldName} is not valid JSON.` };
  }
}

function parseExpectedStatuses(raw: string): { value: number[] | null; error: string | null } {
  const trimmed = raw.trim();
  if (!trimmed) {
    return { value: [200], error: null };
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return {
      value: null,
      error: "Expected statuses must be a JSON array, for example [200, 201].",
    };
  }

  if (!Array.isArray(parsed)) {
    return {
      value: null,
      error: "Expected statuses must be a JSON array.",
    };
  }

  const values: number[] = [];
  const seen = new Set<number>();
  for (const item of parsed) {
    const num = Number(item);
    if (!Number.isInteger(num) || num < 100 || num > 599) {
      return {
        value: null,
        error: "Expected statuses must contain valid HTTP status codes (100-599).",
      };
    }
    if (!seen.has(num)) {
      values.push(num);
      seen.add(num);
    }
  }

  if (values.length === 0) {
    values.push(200);
  }

  return { value: values, error: null };
}

const methodStyles: Record<string, string> = {
  GET: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  POST: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  PUT: "bg-amber-500/20 text-amber-400 border-amber-500/30",
  DELETE: "bg-red-500/20 text-red-400 border-red-500/30",
  PATCH: "bg-violet-500/20 text-violet-400 border-violet-500/30",
  OPTIONS: "bg-cyan-500/20 text-cyan-400 border-cyan-500/30",
  HEAD: "bg-zinc-500/20 text-zinc-400 border-zinc-500/30",
};

function MethodBadge({ method }: { method: string }) {
  const normalized = method.toUpperCase();
  const style =
    methodStyles[normalized] ??
    "bg-zinc-500/20 text-zinc-400 border-zinc-500/30";

  return (
    <span className={`inline-flex items-center rounded border px-2 py-0.5 text-xs font-mono ${style}`}>
      {normalized}
    </span>
  );
}

function JsonDetails({ title, value }: { title: string; value: unknown }) {
  return (
    <details className="overflow-hidden rounded-lg border border-zinc-800 bg-zinc-900/60">
      <summary className="cursor-pointer list-none px-3 py-2 text-sm font-medium text-zinc-300 hover:bg-zinc-800/70">
        {title}
      </summary>
      <pre className="max-h-80 overflow-auto border-t border-zinc-800 px-3 py-2 text-xs text-zinc-300">
        {toPrettyJson(value)}
      </pre>
    </details>
  );
}

type ScenarioFormState = {
  name: string;
  description: string;
  target_url: string;
  method: string;
  vus: string;
  duration: string;
  headers: string;
  query_params: string;
  body: string;
  expected_statuses: string;
  ramp_stages: string;
  thresholds: string;
};

const EMPTY_SCENARIO_FORM: ScenarioFormState = {
  name: "",
  description: "",
  target_url: "",
  method: "GET",
  vus: "10",
  duration: "30s",
  headers: "{}",
  query_params: "{}",
  body: "null",
  expected_statuses: "[200]",
  ramp_stages: "[]",
  thresholds: JSON.stringify(
    {
      http_req_duration: ["p(95)<2000"],
      http_req_failed: ["rate<0.05"],
    },
    null,
    2
  ),
};

function scenarioToForm(scenario: LoadTestScenario): ScenarioFormState {
  return {
    name: scenario.name,
    description: scenario.description,
    target_url: scenario.target_url,
    method: scenario.method.toUpperCase(),
    vus: String(scenario.vus),
    duration: scenario.duration,
    headers: toPrettyJson(scenario.headers),
    query_params: toPrettyJson(scenario.query_params),
    body: toPrettyJson(scenario.body),
    expected_statuses: toPrettyJson(scenario.expected_statuses),
    ramp_stages: toPrettyJson(scenario.ramp_stages),
    thresholds: toPrettyJson(scenario.thresholds),
  };
}

function buildScenarioPayload(
  form: ScenarioFormState,
  scenarioId?: string
): { payload: LoadTestScenarioUpsertRequest | null; error: string | null } {
  const name = form.name.trim();
  if (!name) {
    return { payload: null, error: "Scenario name is required." };
  }

  const targetUrl = form.target_url.trim();
  if (!targetUrl) {
    return { payload: null, error: "Target URL is required." };
  }

  const vus = Number(form.vus);
  if (!Number.isInteger(vus) || vus <= 0) {
    return { payload: null, error: "VUs must be a positive integer." };
  }

  const duration = form.duration.trim();
  if (!duration) {
    return { payload: null, error: "Duration is required." };
  }

  const headersParsed = parseJsonObject(form.headers, "Headers");
  if (headersParsed.error || !headersParsed.value) {
    return { payload: null, error: headersParsed.error };
  }

  const queryParsed = parseJsonObject(form.query_params, "Query params");
  if (queryParsed.error || !queryParsed.value) {
    return { payload: null, error: queryParsed.error };
  }

  const thresholdsParsed = parseJsonObject(form.thresholds, "Thresholds");
  if (thresholdsParsed.error || !thresholdsParsed.value) {
    return { payload: null, error: thresholdsParsed.error };
  }

  const rampStagesAny = parseJsonAny(form.ramp_stages, "Ramp stages");
  if (rampStagesAny.error) {
    return { payload: null, error: rampStagesAny.error };
  }
  if (!Array.isArray(rampStagesAny.value)) {
    return { payload: null, error: "Ramp stages must be a JSON array." };
  }

  const bodyParsed = parseJsonAny(form.body, "Body");
  if (bodyParsed.error) {
    return { payload: null, error: bodyParsed.error };
  }

  const statusesParsed = parseExpectedStatuses(form.expected_statuses);
  if (statusesParsed.error || !statusesParsed.value) {
    return { payload: null, error: statusesParsed.error };
  }

  const payload: LoadTestScenarioUpsertRequest = {
    id: scenarioId,
    name,
    description: form.description,
    target_url: targetUrl,
    method: form.method,
    vus,
    duration,
    headers: Object.fromEntries(
      Object.entries(headersParsed.value).map(([key, value]) => [
        String(key),
        String(value),
      ])
    ),
    query_params: queryParsed.value,
    body: bodyParsed.value,
    expected_statuses: statusesParsed.value,
    ramp_stages: rampStagesAny.value as { duration: string; target: number }[],
    thresholds: Object.fromEntries(
      Object.entries(thresholdsParsed.value).map(([key, value]) => {
        if (Array.isArray(value)) {
          return [String(key), value.map((item) => String(item))];
        }
        return [String(key), [String(value)]];
      })
    ),
  };

  return { payload, error: null };
}

export default function LoadTests() {
  const [scenarios, setScenarios] = useState<LoadTestScenario[]>([]);
  const [selectedScenarioIds, setSelectedScenarioIds] = useState<string[]>([]);
  const [editingScenarioId, setEditingScenarioId] = useState<string | null>(null);
  const [form, setForm] = useState<ScenarioFormState>(EMPTY_SCENARIO_FORM);

  const [profiles, setProfiles] = useState<LoadTestProfile[]>([]);
  const [presets, setPresets] = useState<
    Record<
      string,
      {
        vus: number;
        duration: string;
        ramp_stages: { duration: string; target: number }[];
        thresholds: Record<string, string[]>;
      }
    >
  >({});

  const [scenarioLoading, setScenarioLoading] = useState(true);
  const [scenarioActionLoading, setScenarioActionLoading] = useState(false);
  const [scenarioError, setScenarioError] = useState<string | null>(null);

  const [profilesLoading, setProfilesLoading] = useState(true);
  const [profilesError, setProfilesError] = useState<string | null>(null);

  const [runProfileId, setRunProfileId] = useState("");
  const [runTargetBaseUrl, setRunTargetBaseUrl] = useState("");
  const [runHeadersOverrideInput, setRunHeadersOverrideInput] = useState("{}");
  const [runHeadersError, setRunHeadersError] = useState<string | null>(null);
  const [runningSelected, setRunningSelected] = useState(false);
  const [runningLatest, setRunningLatest] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [runSummary, setRunSummary] = useState<LoadTestRunResponse | null>(null);

  const [results, setResults] = useState<LoadTestResult[]>([]);
  const [resultsLoading, setResultsLoading] = useState(true);
  const [resultsError, setResultsError] = useState<string | null>(null);
  const [selectedResultId, setSelectedResultId] = useState<string | null>(null);
  const [selectedResult, setSelectedResult] = useState<LoadTestResult | null>(null);
  const [resultDetailLoading, setResultDetailLoading] = useState(false);
  const [resultDetailError, setResultDetailError] = useState<string | null>(null);

  const selectedScenarioSet = useMemo(
    () => new Set(selectedScenarioIds),
    [selectedScenarioIds]
  );

  const allScenariosSelected =
    scenarios.length > 0 && selectedScenarioIds.length === scenarios.length;

  const refreshScenarios = useCallback(async () => {
    setScenarioLoading(true);
    setScenarioError(null);
    try {
      const res = await getLoadTestScenarios(true);
      const data = unwrap(res.data);
      const loadedScenarios = Array.isArray(data) ? data : [];
      setScenarios(loadedScenarios);
      setSelectedScenarioIds((prev) =>
        prev.filter((id) => loadedScenarios.some((scenario) => scenario.id === id))
      );
      setEditingScenarioId((prev) =>
        prev && loadedScenarios.some((scenario) => scenario.id === prev) ? prev : null
      );
    } catch (error) {
      setScenarios([]);
      setSelectedScenarioIds([]);
      setEditingScenarioId(null);
      setScenarioError(
        extractErrorMessage(error, "Failed to load load-test scenarios.")
      );
    } finally {
      setScenarioLoading(false);
    }
  }, []);

  const refreshProfiles = useCallback(async () => {
    setProfilesLoading(true);
    setProfilesError(null);
    try {
      const res = await getLoadTestProfiles();
      const data = unwrap(res.data);
      setProfiles(Array.isArray(data.profiles) ? data.profiles : []);
      setPresets(data.presets ?? {});
    } catch (error) {
      setProfiles([]);
      setPresets({});
      setProfilesError(
        extractErrorMessage(error, "Failed to load load-test profiles.")
      );
    } finally {
      setProfilesLoading(false);
    }
  }, []);

  const refreshResults = useCallback(async () => {
    setResultsLoading(true);
    setResultsError(null);
    try {
      const res = await getLoadTestResults({ limit: 20 });
      const data = unwrap(res.data);
      const loadedResults = Array.isArray(data) ? data : [];
      setResults(loadedResults);
      setSelectedResultId((prev) =>
        prev && loadedResults.some((result) => result.id === prev) ? prev : null
      );
      setSelectedResult((prev) =>
        prev && loadedResults.some((result) => result.id === prev.id) ? prev : null
      );
    } catch (error) {
      setResults([]);
      setSelectedResultId(null);
      setSelectedResult(null);
      setResultsError(
        extractErrorMessage(error, "Failed to load load-test history.")
      );
    } finally {
      setResultsLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshScenarios();
    void refreshProfiles();
    void refreshResults();
  }, [refreshScenarios, refreshProfiles, refreshResults]);

  const startCreateScenario = () => {
    setEditingScenarioId(null);
    setForm(EMPTY_SCENARIO_FORM);
    setScenarioError(null);
  };

  const editScenario = (scenario: LoadTestScenario) => {
    setEditingScenarioId(scenario.id);
    setForm(scenarioToForm(scenario));
    setScenarioError(null);
  };

  const cloneScenario = () => {
    if (selectedScenarioIds.length !== 1) {
      setScenarioError("Select exactly one scenario to clone.");
      return;
    }

    const source = scenarios.find((item) => item.id === selectedScenarioIds[0]);
    if (!source) {
      setScenarioError("Selected scenario was not found.");
      return;
    }

    setEditingScenarioId(null);
    setForm({
      ...scenarioToForm(source),
      name: `${source.name} (Copy)`,
    });
    setScenarioError(null);
  };

  const applyPreset = (preset: LoadTestPreset) => {
    const config = presets[preset];
    if (!config) {
      setScenarioError(`Preset '${preset}' is not available.`);
      return;
    }

    setForm((prev) => ({
      ...prev,
      vus: String(config.vus),
      duration: config.duration,
      ramp_stages: toPrettyJson(config.ramp_stages),
      thresholds: toPrettyJson(config.thresholds),
    }));
    setScenarioError(null);
  };

  const saveScenario = async () => {
    const built = buildScenarioPayload(form, editingScenarioId ?? undefined);
    if (built.error || !built.payload) {
      setScenarioError(built.error);
      return;
    }

    setScenarioActionLoading(true);
    setScenarioError(null);
    try {
      const res = editingScenarioId
        ? await updateLoadTestScenario(editingScenarioId, built.payload)
        : await createLoadTestScenario(built.payload);
      const saved = unwrap(res.data);
      await refreshScenarios();
      setEditingScenarioId(saved.id);
      setSelectedScenarioIds([saved.id]);
      setForm(scenarioToForm(saved));
    } catch (error) {
      setScenarioError(
        extractErrorMessage(error, "Failed to save load-test scenario.")
      );
    } finally {
      setScenarioActionLoading(false);
    }
  };

  const deleteSelectedScenarios = async () => {
    if (selectedScenarioIds.length === 0) {
      setScenarioError("Select at least one scenario to delete.");
      return;
    }

    setScenarioActionLoading(true);
    setScenarioError(null);
    try {
      await Promise.all(selectedScenarioIds.map((id) => deleteLoadTestScenario(id)));
      await refreshScenarios();
      if (editingScenarioId && selectedScenarioIds.includes(editingScenarioId)) {
        setEditingScenarioId(null);
        setForm(EMPTY_SCENARIO_FORM);
      }
      setSelectedScenarioIds([]);
    } catch (error) {
      setScenarioError(
        extractErrorMessage(error, "Failed to delete selected scenarios.")
      );
    } finally {
      setScenarioActionLoading(false);
    }
  };

  const toggleScenarioSelection = (scenarioId: string) => {
    setSelectedScenarioIds((prev) =>
      prev.includes(scenarioId)
        ? prev.filter((id) => id !== scenarioId)
        : [...prev, scenarioId]
    );
  };

  const selectAllScenarios = () => {
    setSelectedScenarioIds(scenarios.map((item) => item.id));
  };

  const clearSelection = () => {
    setSelectedScenarioIds([]);
  };

  const parseRunHeadersOverride = (): Record<string, string> | null => {
    const parsed = parseJsonObject(runHeadersOverrideInput, "Headers override");
    if (parsed.error || !parsed.value) {
      setRunHeadersError(parsed.error);
      return null;
    }
    setRunHeadersError(null);
    return Object.fromEntries(
      Object.entries(parsed.value).map(([key, value]) => [String(key), String(value)])
    );
  };

  const loadResultDetail = async (resultId: string) => {
    setSelectedResultId(resultId);
    setResultDetailLoading(true);
    setResultDetailError(null);
    try {
      const res = await getLoadTestResult(resultId, true);
      const data = unwrap(res.data);
      setSelectedResult(data);
    } catch (error) {
      setSelectedResult(null);
      setResultDetailError(
        extractErrorMessage(error, "Failed to load result details.")
      );
    } finally {
      setResultDetailLoading(false);
    }
  };

  const executeRun = async (useSelected: boolean) => {
    const headersOverride = parseRunHeadersOverride();
    if (!headersOverride) {
      return;
    }

    if (useSelected && selectedScenarioIds.length === 0) {
      setRunError("Select at least one scenario before running selected.");
      return;
    }

    if (useSelected) {
      setRunningSelected(true);
    } else {
      setRunningLatest(true);
    }
    setRunError(null);

    try {
      const payload = {
        scenario_ids: useSelected ? selectedScenarioIds : undefined,
        profile_id: runProfileId || undefined,
        target_base_url: runTargetBaseUrl.trim() || undefined,
        headers_override: headersOverride,
      };

      const res = await runLoadTests(payload);
      const data = unwrap(res.data);
      setRunSummary(data);
      await refreshResults();
      if (data.results.length > 0) {
        await loadResultDetail(data.results[0].id);
      }
    } catch (error) {
      setRunError(extractErrorMessage(error, "Failed to execute load test run."));
    } finally {
      if (useSelected) {
        setRunningSelected(false);
      } else {
        setRunningLatest(false);
      }
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 p-6 text-zinc-100">
      <div className="mx-auto max-w-7xl space-y-6">
        <h1 className="text-2xl font-semibold text-zinc-50">Load Tests</h1>

        <section className="rounded-xl border border-zinc-800 bg-zinc-900/80 p-4">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <h2 className="text-sm font-medium text-zinc-400">Scenario Library</h2>
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={refreshScenarios}
                disabled={scenarioLoading || scenarioActionLoading}
                className="rounded-lg bg-slate-700 px-3 py-1.5 text-sm font-medium text-zinc-100 hover:bg-slate-600 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Refresh
              </button>
              <button
                type="button"
                onClick={startCreateScenario}
                disabled={scenarioActionLoading}
                className="rounded-lg bg-zinc-800 px-3 py-1.5 text-sm font-medium text-zinc-100 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                New
              </button>
              <button
                type="button"
                onClick={cloneScenario}
                disabled={scenarioActionLoading || selectedScenarioIds.length !== 1}
                className="rounded-lg bg-zinc-800 px-3 py-1.5 text-sm font-medium text-zinc-100 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Clone
              </button>
              <button
                type="button"
                onClick={deleteSelectedScenarios}
                disabled={scenarioActionLoading || selectedScenarioIds.length === 0}
                className="rounded-lg bg-red-700 px-3 py-1.5 text-sm font-medium text-zinc-100 hover:bg-red-600 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Delete Selected
              </button>
            </div>
          </div>

          {scenarioError && (
            <div className="mb-4 rounded-lg border border-red-800 bg-red-950/50 px-4 py-2 text-sm text-red-300">
              {scenarioError}
            </div>
          )}

          <div className="mb-4 grid gap-4 lg:grid-cols-2">
            <label className="space-y-1">
              <span className="text-sm text-zinc-400">Name</span>
              <input
                type="text"
                value={form.name}
                onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))}
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              />
            </label>
            <label className="space-y-1">
              <span className="text-sm text-zinc-400">Target URL</span>
              <input
                type="text"
                value={form.target_url}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, target_url: event.target.value }))
                }
                placeholder="https://api.example.com/orders"
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-zinc-100 placeholder-zinc-500 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              />
            </label>
            <label className="space-y-1 lg:col-span-2">
              <span className="text-sm text-zinc-400">Description</span>
              <input
                type="text"
                value={form.description}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, description: event.target.value }))
                }
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              />
            </label>
          </div>

          <div className="mb-4 grid gap-4 md:grid-cols-2 lg:grid-cols-4">
            <label className="space-y-1">
              <span className="text-sm text-zinc-400">Method</span>
              <select
                value={form.method}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, method: event.target.value }))
                }
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              >
                <option value="GET">GET</option>
                <option value="POST">POST</option>
                <option value="PUT">PUT</option>
                <option value="DELETE">DELETE</option>
                <option value="PATCH">PATCH</option>
                <option value="OPTIONS">OPTIONS</option>
                <option value="HEAD">HEAD</option>
              </select>
            </label>
            <label className="space-y-1">
              <span className="text-sm text-zinc-400">VUs</span>
              <input
                type="number"
                min={1}
                value={form.vus}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, vus: event.target.value }))
                }
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              />
            </label>
            <label className="space-y-1">
              <span className="text-sm text-zinc-400">Duration</span>
              <input
                type="text"
                value={form.duration}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, duration: event.target.value }))
                }
                placeholder="30s"
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-zinc-100 placeholder-zinc-500 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              />
            </label>
            <label className="space-y-1">
              <span className="text-sm text-zinc-400">Expected statuses (JSON)</span>
              <input
                type="text"
                value={form.expected_statuses}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, expected_statuses: event.target.value }))
                }
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 font-mono text-sm text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              />
            </label>
          </div>

          <div className="mb-4 flex flex-wrap items-center gap-2">
            <span className="text-sm text-zinc-400">Apply preset:</span>
            {(["smoke", "load", "stress"] as LoadTestPreset[]).map((preset) => (
              <button
                key={preset}
                type="button"
                onClick={() => applyPreset(preset)}
                disabled={scenarioActionLoading || !presets[preset]}
                className="rounded-lg bg-zinc-800 px-3 py-1 text-xs font-medium uppercase tracking-wide text-zinc-100 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {preset}
              </button>
            ))}
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <label className="space-y-1">
              <span className="text-sm text-zinc-400">Headers (JSON)</span>
              <textarea
                rows={5}
                value={form.headers}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, headers: event.target.value }))
                }
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 font-mono text-sm text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              />
            </label>
            <label className="space-y-1">
              <span className="text-sm text-zinc-400">Query Params (JSON)</span>
              <textarea
                rows={5}
                value={form.query_params}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, query_params: event.target.value }))
                }
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 font-mono text-sm text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              />
            </label>
            <label className="space-y-1">
              <span className="text-sm text-zinc-400">Body (JSON)</span>
              <textarea
                rows={5}
                value={form.body}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, body: event.target.value }))
                }
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 font-mono text-sm text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              />
            </label>
            <label className="space-y-1">
              <span className="text-sm text-zinc-400">Ramp Stages (JSON array)</span>
              <textarea
                rows={5}
                value={form.ramp_stages}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, ramp_stages: event.target.value }))
                }
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 font-mono text-sm text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              />
            </label>
            <label className="space-y-1 lg:col-span-2">
              <span className="text-sm text-zinc-400">Thresholds (JSON)</span>
              <textarea
                rows={5}
                value={form.thresholds}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, thresholds: event.target.value }))
                }
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 font-mono text-sm text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              />
            </label>
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={saveScenario}
              disabled={scenarioActionLoading}
              className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-medium text-zinc-100 hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {scenarioActionLoading ? (
                <span className="inline-flex items-center gap-2">
                  <span className="size-4 animate-spin rounded-full border-2 border-zinc-300 border-t-transparent" />
                  Saving
                </span>
              ) : editingScenarioId ? (
                "Update Scenario"
              ) : (
                "Create Scenario"
              )}
            </button>
            <span className="text-sm text-zinc-400">
              {editingScenarioId
                ? `Editing: ${editingScenarioId}`
                : "Create mode"}
            </span>
          </div>

          <div className="mt-6">
            {scenarioLoading ? (
              <div className="py-8 text-center text-zinc-500">Loading scenarios...</div>
            ) : scenarios.length === 0 ? (
              <div className="py-8 text-center text-zinc-500">No scenarios found.</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-zinc-800">
                      <th className="w-12 px-3 py-3 text-left text-zinc-400">Sel</th>
                      <th className="px-3 py-3 text-left text-zinc-400">Name</th>
                      <th className="px-3 py-3 text-left text-zinc-400">Method</th>
                      <th className="px-3 py-3 text-left text-zinc-400">Target URL</th>
                      <th className="px-3 py-3 text-right text-zinc-400">VUs</th>
                      <th className="px-3 py-3 text-left text-zinc-400">Duration</th>
                      <th className="px-3 py-3 text-left text-zinc-400">Created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {scenarios.map((scenario, index) => (
                      <tr
                        key={scenario.id}
                        onClick={() => editScenario(scenario)}
                        className={`cursor-pointer border-b border-zinc-900/70 transition-colors hover:bg-zinc-800/50 ${
                          editingScenarioId === scenario.id
                            ? "bg-zinc-800/60"
                            : index % 2 === 0
                            ? "bg-zinc-900/30"
                            : "bg-zinc-900/10"
                        }`}
                      >
                        <td className="px-3 py-3">
                          <input
                            type="checkbox"
                            checked={selectedScenarioSet.has(scenario.id)}
                            onClick={(event) => event.stopPropagation()}
                            onChange={(event) => {
                              event.stopPropagation();
                              toggleScenarioSelection(scenario.id);
                            }}
                            className="size-4 rounded border-zinc-700 bg-zinc-800 text-emerald-600 focus:ring-emerald-500/40"
                          />
                        </td>
                        <td className="px-3 py-3 font-medium text-zinc-100">{scenario.name}</td>
                        <td className="px-3 py-3">
                          <MethodBadge method={scenario.method} />
                        </td>
                        <td className="px-3 py-3 font-mono text-xs text-zinc-300">{scenario.target_url}</td>
                        <td className="px-3 py-3 text-right text-zinc-300">{scenario.vus}</td>
                        <td className="px-3 py-3 text-zinc-300">{scenario.duration}</td>
                        <td className="px-3 py-3 text-zinc-400">{formatDate(scenario.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            <div className="mt-3 flex items-center gap-2">
              <button
                type="button"
                onClick={selectAllScenarios}
                disabled={scenarioLoading || scenarios.length === 0 || allScenariosSelected}
                className="rounded-lg bg-zinc-800 px-3 py-1.5 text-xs font-medium text-zinc-100 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Select All
              </button>
              <button
                type="button"
                onClick={clearSelection}
                disabled={scenarioLoading || selectedScenarioIds.length === 0}
                className="rounded-lg bg-zinc-800 px-3 py-1.5 text-xs font-medium text-zinc-100 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Clear Selection
              </button>
              <span className="text-xs text-zinc-500">
                Selected: {selectedScenarioIds.length}
              </span>
            </div>
          </div>
        </section>

        <section className="rounded-xl border border-zinc-800 bg-zinc-900/80 p-4">
          <h2 className="mb-4 text-sm font-medium text-zinc-400">Run Panel</h2>

          {profilesError && (
            <div className="mb-4 rounded-lg border border-red-800 bg-red-950/50 px-4 py-2 text-sm text-red-300">
              {profilesError}
            </div>
          )}

          <div className="grid gap-4 lg:grid-cols-2">
            <label className="space-y-1">
              <span className="text-sm text-zinc-400">Profile</span>
              <select
                value={runProfileId}
                onChange={(event) => setRunProfileId(event.target.value)}
                disabled={profilesLoading || runningSelected || runningLatest}
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40 disabled:opacity-60"
              >
                <option value="">None</option>
                {profiles.map((profile) => (
                  <option key={profile.id} value={profile.id}>
                    {profile.name} ({profile.base_url})
                  </option>
                ))}
              </select>
            </label>

            <label className="space-y-1">
              <span className="text-sm text-zinc-400">Target base URL override</span>
              <input
                type="text"
                value={runTargetBaseUrl}
                onChange={(event) => setRunTargetBaseUrl(event.target.value)}
                placeholder="https://api.example.com"
                disabled={runningSelected || runningLatest}
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-zinc-100 placeholder-zinc-500 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40 disabled:opacity-60"
              />
            </label>
          </div>

          <label className="mt-4 block space-y-1">
            <span className="text-sm text-zinc-400">Headers override (JSON)</span>
            <textarea
              rows={5}
              value={runHeadersOverrideInput}
              onChange={(event) => setRunHeadersOverrideInput(event.target.value)}
              disabled={runningSelected || runningLatest}
              className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 font-mono text-sm text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40 disabled:opacity-60"
            />
          </label>
          {runHeadersError && (
            <p className="mt-1 text-sm text-red-400">{runHeadersError}</p>
          )}

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={() => void executeRun(true)}
              disabled={runningSelected || runningLatest || selectedScenarioIds.length === 0}
              className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-medium text-zinc-100 hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {runningSelected ? (
                <span className="inline-flex items-center gap-2">
                  <span className="size-4 animate-spin rounded-full border-2 border-zinc-300 border-t-transparent" />
                  Running Selected
                </span>
              ) : (
                "Run Selected"
              )}
            </button>
            <button
              type="button"
              onClick={() => void executeRun(false)}
              disabled={runningSelected || runningLatest}
              className="rounded-lg bg-slate-700 px-4 py-2 text-sm font-medium text-zinc-100 hover:bg-slate-600 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {runningLatest ? (
                <span className="inline-flex items-center gap-2">
                  <span className="size-4 animate-spin rounded-full border-2 border-zinc-300 border-t-transparent" />
                  Running Latest Batch
                </span>
              ) : (
                "Run Latest Batch"
              )}
            </button>
            <span className="text-sm text-zinc-400">
              Selected scenarios: {selectedScenarioIds.length}
            </span>
          </div>

          {runError && (
            <div className="mt-4 rounded-lg border border-red-800 bg-red-950/50 px-4 py-2 text-sm text-red-300">
              {runError}
            </div>
          )}

          {runSummary && (
            <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3">
                <p className="text-xs text-zinc-500">Total scenarios</p>
                <p className="text-lg font-semibold text-zinc-100">{runSummary.total_scenarios}</p>
              </div>
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3">
                <p className="text-xs text-zinc-500">Passed</p>
                <p className="text-lg font-semibold text-emerald-400">{runSummary.passed}</p>
              </div>
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3">
                <p className="text-xs text-zinc-500">Failed</p>
                <p className="text-lg font-semibold text-red-400">{runSummary.failed}</p>
              </div>
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3">
                <p className="text-xs text-zinc-500">Errors</p>
                <p className="text-lg font-semibold text-amber-400">{runSummary.errors}</p>
              </div>
            </div>
          )}
        </section>

        <section className="rounded-xl border border-zinc-800 bg-zinc-900/80 p-4">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-sm font-medium text-zinc-400">History & Detail</h2>
            <button
              type="button"
              onClick={refreshResults}
              disabled={resultsLoading}
              className="rounded-lg bg-slate-700 px-3 py-1.5 text-sm font-medium text-zinc-100 hover:bg-slate-600 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Refresh
            </button>
          </div>

          {resultsError && (
            <div className="mb-4 rounded-lg border border-red-800 bg-red-950/50 px-4 py-2 text-sm text-red-300">
              {resultsError}
            </div>
          )}

          {resultsLoading ? (
            <div className="py-10 text-center text-zinc-500">Loading run history...</div>
          ) : results.length === 0 ? (
            <div className="py-10 text-center text-zinc-500">No load test runs yet.</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-zinc-800">
                    <th className="px-3 py-3 text-left text-zinc-400">Scenario</th>
                    <th className="px-3 py-3 text-left text-zinc-400">Status</th>
                    <th className="px-3 py-3 text-right text-zinc-400">Requests</th>
                    <th className="px-3 py-3 text-right text-zinc-400">Failed</th>
                    <th className="px-3 py-3 text-right text-zinc-400">p95 (ms)</th>
                    <th className="px-3 py-3 text-right text-zinc-400">RPS</th>
                    <th className="px-3 py-3 text-left text-zinc-400">Executed</th>
                  </tr>
                </thead>
                <tbody>
                  {results.map((result, index) => (
                    <tr
                      key={result.id}
                      onClick={() => void loadResultDetail(result.id)}
                      className={`cursor-pointer border-b border-zinc-900/70 transition-colors hover:bg-zinc-800/50 ${
                        selectedResultId === result.id
                          ? "bg-zinc-800/60"
                          : index % 2 === 0
                          ? "bg-zinc-900/30"
                          : "bg-zinc-900/10"
                      }`}
                    >
                      <td className="px-3 py-3 text-zinc-200">{result.scenario_name}</td>
                      <td className="px-3 py-3">
                        <div className="flex items-center gap-2">
                          <StatusBadge status={toStatus(result.runner_status)} />
                          {isSuspiciousLoadRun(result) && (
                            <span className="rounded border border-amber-700/60 bg-amber-900/40 px-2 py-0.5 text-xs font-medium text-amber-300">
                              Warning
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="px-3 py-3 text-right text-zinc-300">{result.total_requests}</td>
                      <td className="px-3 py-3 text-right text-zinc-300">{result.failed_requests}</td>
                      <td className="px-3 py-3 text-right text-zinc-300">{result.p95_ms.toFixed(0)}</td>
                      <td className="px-3 py-3 text-right text-zinc-300">{result.requests_per_second.toFixed(2)}</td>
                      <td className="px-3 py-3 text-zinc-400">{formatDate(result.executed_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {resultDetailError && (
            <div className="mt-4 rounded-lg border border-red-800 bg-red-950/50 px-4 py-2 text-sm text-red-300">
              {resultDetailError}
            </div>
          )}

          {resultDetailLoading ? (
            <div className="mt-4 text-sm text-zinc-500">Loading run detail...</div>
          ) : selectedResult ? (
            <div className="mt-4 space-y-4 rounded-lg border border-zinc-800 bg-zinc-950/70 p-4">
              <div className="flex flex-wrap items-center gap-3">
                <h3 className="text-base font-medium text-zinc-100">{selectedResult.scenario_name}</h3>
                <StatusBadge status={toStatus(selectedResult.runner_status)} />
                {isSuspiciousLoadRun(selectedResult) && (
                  <span className="rounded border border-amber-700/60 bg-amber-900/40 px-2 py-0.5 text-xs font-medium text-amber-300">
                    Suspicious run
                  </span>
                )}
                <span className="font-mono text-xs text-zinc-500">{selectedResult.id}</span>
              </div>

              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
                  <p className="text-xs text-zinc-500">Total requests</p>
                  <p className="text-sm font-semibold text-zinc-200">{selectedResult.total_requests}</p>
                </div>
                <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
                  <p className="text-xs text-zinc-500">Failed requests</p>
                  <p className="text-sm font-semibold text-zinc-200">{selectedResult.failed_requests}</p>
                </div>
                <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
                  <p className="text-xs text-zinc-500">Avg response</p>
                  <p className="text-sm font-semibold text-zinc-200">{selectedResult.avg_response_time_ms.toFixed(2)} ms</p>
                </div>
                <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
                  <p className="text-xs text-zinc-500">Error rate</p>
                  <p className="text-sm font-semibold text-zinc-200">{(selectedResult.error_rate * 100).toFixed(2)}%</p>
                </div>
              </div>

              <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 text-sm text-zinc-300">
                <p>
                  <span className="text-zinc-500">Runner message:</span>{" "}
                  {selectedResult.runner_message || "-"}
                </p>
                <p>
                  <span className="text-zinc-500">Runner exit code:</span>{" "}
                  {selectedResult.runner_exit_code ?? "-"}
                </p>
                <p>
                  <span className="text-zinc-500">Executed:</span>{" "}
                  {formatDate(selectedResult.executed_at)}
                </p>
              </div>

              <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
                <h4 className="mb-2 text-sm font-medium text-zinc-300">Run Diagnostics</h4>
                <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                  <div>
                    <p className="text-xs text-zinc-500">Metric shape</p>
                    <p className="text-sm text-zinc-200">{selectedResult.metric_shape ?? "unknown"}</p>
                  </div>
                  <div>
                    <p className="text-xs text-zinc-500">Request count source</p>
                    <p className="text-sm text-zinc-200">
                      {selectedResult.request_count_source ?? "unknown"}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs text-zinc-500">Error-rate source</p>
                    <p className="text-sm text-zinc-200">
                      {selectedResult.error_rate_source ?? "unknown"}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs text-zinc-500">Parse warnings</p>
                    <p className="text-sm text-zinc-200">
                      {(selectedResult.parse_warnings ?? []).length}
                    </p>
                  </div>
                </div>

                <div className="mt-3 rounded-lg border border-zinc-800 bg-zinc-950/70 px-3 py-2 text-sm text-zinc-300">
                  <p>
                    <span className="text-zinc-500">Computed interpretation:</span>{" "}
                    {diagnosticInterpretation(selectedResult)}
                  </p>
                  <p>
                    <span className="text-zinc-500">Threshold/fallback interpretation:</span>{" "}
                    {thresholdInterpretation(selectedResult)}
                  </p>
                </div>

                {(selectedResult.parse_warnings ?? []).length > 0 && (
                  <div className="mt-3 rounded-lg border border-amber-800/60 bg-amber-950/30 px-3 py-2 text-sm text-amber-300">
                    Parse warnings: {(selectedResult.parse_warnings ?? []).join(", ")}
                  </div>
                )}
              </div>

              <div className="grid gap-2">
                <JsonDetails
                  title="Raw metrics"
                  value={selectedResult.raw_metrics ?? {}}
                />
                <JsonDetails
                  title="Runner stdout excerpt"
                  value={selectedResult.runner_stdout_excerpt || ""}
                />
                <JsonDetails
                  title="Runner stderr excerpt"
                  value={selectedResult.runner_stderr_excerpt || ""}
                />
              </div>
            </div>
          ) : null}
        </section>
      </div>
    </div>
  );
}

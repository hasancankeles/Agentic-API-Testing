import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Copy,
  Gauge,
  Play,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
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
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Field,
  InlineAlert,
  Input,
  JsonDisclosure,
  MethodBadge,
  MetricCard,
  PageHeader,
  Panel,
  Select,
  StatusBadge,
  tableCellClass,
  tableHeaderClass,
  Textarea,
} from "../components/ui";
import {
  cn,
  extractErrorMessage,
  formatDate,
  parseJsonAny,
  parseJsonObject,
  toPrettyJson,
  toStatus,
  truncateMiddle,
} from "../lib/ui";

function unwrap<T>(res: { data?: T } | T): T {
  const d = res as { data?: T };
  return (d.data !== undefined ? d.data : res) as T;
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

function parseExpectedStatuses(raw: string): { value: number[] | null; error: string | null } {
  const trimmed = raw.trim();
  if (!trimmed) return { value: [200], error: null };

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
    return { value: null, error: "Expected statuses must be a JSON array." };
  }

  const seen = new Set<number>();
  const values: number[] = [];
  for (const item of parsed) {
    const status = Number(item);
    if (!Number.isInteger(status) || status < 100 || status > 599) {
      return {
        value: null,
        error: "Expected statuses must contain valid HTTP status codes (100-599).",
      };
    }
    if (!seen.has(status)) {
      seen.add(status);
      values.push(status);
    }
  }

  return { value: values.length > 0 ? values : [200], error: null };
}

function buildScenarioPayload(
  form: ScenarioFormState,
  scenarioId?: string
): { payload: LoadTestScenarioUpsertRequest | null; error: string | null } {
  const name = form.name.trim();
  const targetUrl = form.target_url.trim();
  const vus = Number(form.vus);
  const duration = form.duration.trim();

  if (!name) return { payload: null, error: "Scenario name is required." };
  if (!targetUrl) return { payload: null, error: "Target URL is required." };
  if (!Number.isInteger(vus) || vus <= 0) {
    return { payload: null, error: "VUs must be a positive integer." };
  }
  if (!duration) return { payload: null, error: "Duration is required." };

  const headers = parseJsonObject(form.headers, "Headers");
  if (headers.error || !headers.value) return { payload: null, error: headers.error };

  const query = parseJsonObject(form.query_params, "Query params");
  if (query.error || !query.value) return { payload: null, error: query.error };

  const thresholds = parseJsonObject(form.thresholds, "Thresholds");
  if (thresholds.error || !thresholds.value) {
    return { payload: null, error: thresholds.error };
  }

  const rampStages = parseJsonAny(form.ramp_stages, "Ramp stages");
  if (rampStages.error) return { payload: null, error: rampStages.error };
  if (!Array.isArray(rampStages.value)) {
    return { payload: null, error: "Ramp stages must be a JSON array." };
  }

  const body = parseJsonAny(form.body, "Body");
  if (body.error) return { payload: null, error: body.error };

  const statuses = parseExpectedStatuses(form.expected_statuses);
  if (statuses.error || !statuses.value) return { payload: null, error: statuses.error };

  return {
    payload: {
      id: scenarioId,
      name,
      description: form.description,
      target_url: targetUrl,
      method: form.method,
      vus,
      duration,
      headers: Object.fromEntries(
        Object.entries(headers.value).map(([key, value]) => [String(key), String(value)])
      ),
      query_params: query.value,
      body: body.value,
      expected_statuses: statuses.value,
      ramp_stages: rampStages.value as { duration: string; target: number }[],
      thresholds: Object.fromEntries(
        Object.entries(thresholds.value).map(([key, value]) => [
          String(key),
          Array.isArray(value) ? value.map(String) : [String(value)],
        ])
      ),
    },
    error: null,
  };
}

function isSuspiciousLoadRun(result: LoadTestResult): boolean {
  return (
    (result.parse_warnings ?? []).length > 0 ||
    (result.runner_status === "passed" && result.total_requests === 0)
  );
}

function diagnosticInterpretation(result: LoadTestResult): string {
  if (result.runner_status === "error") {
    return "Runner error occurred. Review stderr/stdout excerpts and parser warnings.";
  }
  if (result.runner_status === "failed") {
    return (result.runner_message || "").toLowerCase().includes("threshold")
      ? "k6 executed but one or more thresholds failed."
      : "k6 returned a non-zero exit code with metrics available.";
  }
  if (result.total_requests === 0) {
    return "Run is marked passed but request count is zero. Treat this run as suspicious.";
  }
  return "Metrics look consistent: requests were executed and parsed.";
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
      const loaded = Array.isArray(data) ? data : [];
      setScenarios(loaded);
      setSelectedScenarioIds((prev) =>
        prev.filter((id) => loaded.some((scenario) => scenario.id === id))
      );
      setEditingScenarioId((prev) =>
        prev && loaded.some((scenario) => scenario.id === prev) ? prev : null
      );
    } catch (err) {
      setScenarios([]);
      setSelectedScenarioIds([]);
      setEditingScenarioId(null);
      setScenarioError(extractErrorMessage(err, "Failed to load load-test scenarios."));
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
    } catch (err) {
      setProfiles([]);
      setPresets({});
      setProfilesError(extractErrorMessage(err, "Failed to load load-test profiles."));
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
      const loaded = Array.isArray(data) ? data : [];
      setResults(loaded);
      setSelectedResultId((prev) =>
        prev && loaded.some((result) => result.id === prev) ? prev : null
      );
      setSelectedResult((prev) =>
        prev && loaded.some((result) => result.id === prev.id) ? prev : null
      );
    } catch (err) {
      setResults([]);
      setSelectedResultId(null);
      setSelectedResult(null);
      setResultsError(extractErrorMessage(err, "Failed to load load-test history."));
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
    setForm({ ...scenarioToForm(source), name: `${source.name} (Copy)` });
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
    } catch (err) {
      setScenarioError(extractErrorMessage(err, "Failed to save load-test scenario."));
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
    } catch (err) {
      setScenarioError(extractErrorMessage(err, "Failed to delete selected scenarios."));
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
      setSelectedResult(unwrap(res.data));
    } catch (err) {
      setSelectedResult(null);
      setResultDetailError(extractErrorMessage(err, "Failed to load result details."));
    } finally {
      setResultDetailLoading(false);
    }
  };

  const executeRun = async (useSelected: boolean) => {
    const headersOverride = parseRunHeadersOverride();
    if (!headersOverride) return;

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
      const res = await runLoadTests({
        scenario_ids: useSelected ? selectedScenarioIds : undefined,
        profile_id: runProfileId || undefined,
        target_base_url: runTargetBaseUrl.trim() || undefined,
        headers_override: headersOverride,
      });
      const data = unwrap(res.data);
      setRunSummary(data);
      await refreshResults();
      if (data.results.length > 0) {
        await loadResultDetail(data.results[0].id);
      }
    } catch (err) {
      setRunError(extractErrorMessage(err, "Failed to execute load test run."));
    } finally {
      setRunningSelected(false);
      setRunningLatest(false);
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="Load Tests"
        description="Create k6-backed load scenarios, run selected or latest batches, and inspect parsed metrics plus runner diagnostics."
        action={
          <Button
            variant="ghost"
            icon={RefreshCw}
            onClick={() => {
              void refreshScenarios();
              void refreshProfiles();
              void refreshResults();
            }}
            loading={scenarioLoading || profilesLoading || resultsLoading}
          >
            Refresh
          </Button>
        }
      />

      <Panel
        title="Scenario Builder"
        eyebrow={editingScenarioId ? "Edit scenario" : "Create scenario"}
        action={
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="subtle"
              icon={Plus}
              disabled={scenarioActionLoading}
              onClick={startCreateScenario}
            >
              New
            </Button>
            <Button
              size="sm"
              variant="ghost"
              icon={Copy}
              disabled={scenarioActionLoading || selectedScenarioIds.length !== 1}
              onClick={cloneScenario}
            >
              Clone
            </Button>
            <Button
              size="sm"
              variant="danger"
              icon={Trash2}
              disabled={scenarioActionLoading || selectedScenarioIds.length === 0}
              onClick={() => void deleteSelectedScenarios()}
            >
              Delete
            </Button>
          </div>
        }
      >
        {scenarioError && (
          <div className="mb-4">
            <InlineAlert tone="danger" title="Scenario action failed">
              {scenarioError}
            </InlineAlert>
          </div>
        )}

        <div className="grid gap-4 lg:grid-cols-2">
          <Field label="Name">
            <Input
              value={form.name}
              onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))}
              placeholder="Checkout smoke load"
            />
          </Field>
          <Field label="Target URL">
            <Input
              value={form.target_url}
              onChange={(event) =>
                setForm((prev) => ({ ...prev, target_url: event.target.value }))
              }
              placeholder="https://api.example.com/orders"
            />
          </Field>
          <Field label="Description" className="lg:col-span-2">
            <Input
              value={form.description}
              onChange={(event) =>
                setForm((prev) => ({ ...prev, description: event.target.value }))
              }
              placeholder="Optional note for this load scenario"
            />
          </Field>
        </div>

        <div className="mt-4 grid gap-4 md:grid-cols-2 lg:grid-cols-4">
          <Field label="Method">
            <Select
              value={form.method}
              onChange={(event) => setForm((prev) => ({ ...prev, method: event.target.value }))}
            >
              {["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"].map((method) => (
                <option key={method} value={method}>
                  {method}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="VUs">
            <Input
              type="number"
              min={1}
              value={form.vus}
              onChange={(event) => setForm((prev) => ({ ...prev, vus: event.target.value }))}
            />
          </Field>
          <Field label="Duration">
            <Input
              value={form.duration}
              onChange={(event) =>
                setForm((prev) => ({ ...prev, duration: event.target.value }))
              }
              placeholder="30s"
            />
          </Field>
          <Field label="Expected statuses">
            <Input
              value={form.expected_statuses}
              onChange={(event) =>
                setForm((prev) => ({ ...prev, expected_statuses: event.target.value }))
              }
            />
          </Field>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <span className="text-sm text-slate-400">Preset:</span>
          {(["smoke", "load", "stress"] as LoadTestPreset[]).map((preset) => (
            <Button
              key={preset}
              size="sm"
              variant="ghost"
              disabled={scenarioActionLoading || !presets[preset]}
              onClick={() => applyPreset(preset)}
            >
              {preset}
            </Button>
          ))}
        </div>

        <details className="mt-4 rounded-lg border border-slate-800 bg-slate-950/55 p-3">
          <summary className="cursor-pointer text-sm font-medium text-slate-300">
            Advanced request and threshold JSON
          </summary>
          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <Field label="Headers">
              <Textarea
                rows={5}
                value={form.headers}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, headers: event.target.value }))
                }
              />
            </Field>
            <Field label="Query params">
              <Textarea
                rows={5}
                value={form.query_params}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, query_params: event.target.value }))
                }
              />
            </Field>
            <Field label="Body">
              <Textarea
                rows={5}
                value={form.body}
                onChange={(event) => setForm((prev) => ({ ...prev, body: event.target.value }))}
              />
            </Field>
            <Field label="Ramp stages">
              <Textarea
                rows={5}
                value={form.ramp_stages}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, ramp_stages: event.target.value }))
                }
              />
            </Field>
            <Field label="Thresholds" className="lg:col-span-2">
              <Textarea
                rows={5}
                value={form.thresholds}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, thresholds: event.target.value }))
                }
              />
            </Field>
          </div>
        </details>

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Button
            variant="primary"
            icon={Gauge}
            loading={scenarioActionLoading}
            onClick={() => void saveScenario()}
          >
            {editingScenarioId ? "Update Scenario" : "Create Scenario"}
          </Button>
          <span className="text-sm text-slate-500">
            {editingScenarioId
              ? `Editing ${truncateMiddle(editingScenarioId, 18)}`
              : "Create mode"}
          </span>
        </div>
      </Panel>

      <div className="grid gap-4 xl:grid-cols-[1.1fr_0.9fr]">
        <Panel
          title="Scenario Library"
          action={
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="ghost"
                disabled={scenarioLoading || scenarios.length === 0 || allScenariosSelected}
                onClick={() => setSelectedScenarioIds(scenarios.map((item) => item.id))}
              >
                Select All
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={scenarioLoading || selectedScenarioIds.length === 0}
                onClick={() => setSelectedScenarioIds([])}
              >
                Clear
              </Button>
            </div>
          }
        >
          {scenarioLoading ? (
            <EmptyState title="Loading scenarios…" />
          ) : scenarios.length === 0 ? (
            <EmptyState
              title="No scenarios found"
              description="Create a scenario above, then select it here for execution."
            />
          ) : (
            <DataTable>
              <thead className={tableHeaderClass}>
                <tr>
                  <th className={`${tableCellClass} w-12`}>Sel</th>
                  <th className={tableCellClass}>Name</th>
                  <th className={tableCellClass}>Method</th>
                  <th className={tableCellClass}>Target</th>
                  <th className={`${tableCellClass} text-right`}>VUs</th>
                  <th className={tableCellClass}>Duration</th>
                </tr>
              </thead>
              <tbody>
                {scenarios.map((scenario) => (
                  <tr
                    key={scenario.id}
                    onClick={() => editScenario(scenario)}
                    className={cn(
                      "cursor-pointer border-b border-slate-800/70 transition-colors hover:bg-slate-800/45",
                      editingScenarioId === scenario.id && "bg-emerald-500/8"
                    )}
                  >
                    <td className={tableCellClass}>
                      <input
                        type="checkbox"
                        checked={selectedScenarioSet.has(scenario.id)}
                        onClick={(event) => event.stopPropagation()}
                        onChange={(event) => {
                          event.stopPropagation();
                          toggleScenarioSelection(scenario.id);
                        }}
                        className="size-4 rounded border-slate-700 bg-slate-950 text-emerald-500 focus:ring-emerald-400/30"
                      />
                    </td>
                    <td className={tableCellClass}>
                      <p className="font-medium text-slate-100">{scenario.name}</p>
                      <p className="mt-1 font-mono text-xs text-slate-500">
                        {truncateMiddle(scenario.id, 18)}
                      </p>
                    </td>
                    <td className={tableCellClass}>
                      <MethodBadge method={scenario.method} />
                    </td>
                    <td className={`${tableCellClass} max-w-[340px]`}>
                      <p className="truncate font-mono text-xs text-slate-300">
                        {scenario.target_url}
                      </p>
                    </td>
                    <td className={`${tableCellClass} text-right tabular-nums text-slate-300`}>
                      {scenario.vus}
                    </td>
                    <td className={`${tableCellClass} text-slate-300`}>
                      {scenario.duration}
                    </td>
                  </tr>
                ))}
              </tbody>
            </DataTable>
          )}
          <div className="mt-3">
            <Badge tone={selectedScenarioIds.length > 0 ? "info" : "neutral"}>
              {selectedScenarioIds.length} selected
            </Badge>
          </div>
        </Panel>

        <Panel title="Run Panel" eyebrow="Execution">
          {profilesError && (
            <div className="mb-4">
              <InlineAlert tone="warning" title="Profiles unavailable">
                {profilesError}
              </InlineAlert>
            </div>
          )}

          <div className="grid gap-4">
            <Field label="Profile">
              <Select
                value={runProfileId}
                onChange={(event) => setRunProfileId(event.target.value)}
                disabled={profilesLoading || runningSelected || runningLatest}
              >
                <option value="">None</option>
                {profiles.map((profile) => (
                  <option key={profile.id} value={profile.id}>
                    {profile.name} ({profile.base_url})
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Target base URL override">
              <Input
                value={runTargetBaseUrl}
                onChange={(event) => setRunTargetBaseUrl(event.target.value)}
                placeholder="https://api.example.com"
                disabled={runningSelected || runningLatest}
              />
            </Field>
          </div>

          <details className="mt-4 rounded-lg border border-slate-800 bg-slate-950/55 p-3">
            <summary className="cursor-pointer text-sm font-medium text-slate-300">
              Advanced headers override JSON
            </summary>
            <Field label="Headers override" error={runHeadersError} className="mt-3">
              <Textarea
                rows={5}
                value={runHeadersOverrideInput}
                onChange={(event) => setRunHeadersOverrideInput(event.target.value)}
                disabled={runningSelected || runningLatest}
              />
            </Field>
          </details>

          <div className="mt-4 flex flex-wrap gap-3">
            <Button
              variant="primary"
              icon={Play}
              disabled={selectedScenarioIds.length === 0}
              loading={runningSelected}
              onClick={() => void executeRun(true)}
            >
              Run Selected
            </Button>
            <Button
              variant="secondary"
              icon={Play}
              loading={runningLatest}
              onClick={() => void executeRun(false)}
            >
              Run Latest Batch
            </Button>
          </div>

          {runError && (
            <div className="mt-4">
              <InlineAlert tone="danger" title="Run failed">
                {runError}
              </InlineAlert>
            </div>
          )}

          {runSummary && (
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <MetricCard label="Total" value={runSummary.total_scenarios} />
              <MetricCard label="Passed" value={runSummary.passed} tone="good" />
              <MetricCard label="Failed" value={runSummary.failed} tone="bad" />
              <MetricCard label="Errors" value={runSummary.errors} tone="warn" />
            </div>
          )}
        </Panel>
      </div>

      <div className="grid gap-4 xl:grid-cols-[0.9fr_1.1fr]">
        <Panel
          title="History"
          action={
            <Button
              size="sm"
              variant="ghost"
              icon={RefreshCw}
              onClick={() => void refreshResults()}
              loading={resultsLoading}
            >
              Refresh
            </Button>
          }
        >
          {resultsError && (
            <InlineAlert tone="danger" title="History failed">
              {resultsError}
            </InlineAlert>
          )}
          {resultsLoading ? (
            <EmptyState title="Loading run history…" />
          ) : results.length === 0 ? (
            <EmptyState
              title="No load test runs yet"
              description="Run a scenario to see metrics and diagnostics here."
            />
          ) : (
            <DataTable>
              <thead className={tableHeaderClass}>
                <tr>
                  <th className={tableCellClass}>Scenario</th>
                  <th className={tableCellClass}>Status</th>
                  <th className={`${tableCellClass} text-right`}>Requests</th>
                  <th className={`${tableCellClass} text-right`}>p95</th>
                  <th className={`${tableCellClass} text-right`}>RPS</th>
                </tr>
              </thead>
              <tbody>
                {results.map((result) => (
                  <tr
                    key={result.id}
                    onClick={() => void loadResultDetail(result.id)}
                    className={cn(
                      "cursor-pointer border-b border-slate-800/70 transition-colors hover:bg-slate-800/45",
                      selectedResultId === result.id && "bg-emerald-500/8"
                    )}
                  >
                    <td className={tableCellClass}>
                      <p className="font-medium text-slate-100">{result.scenario_name}</p>
                      <p className="mt-1 font-mono text-xs text-slate-500">
                        {truncateMiddle(result.id, 18)}
                      </p>
                    </td>
                    <td className={tableCellClass}>
                      <div className="flex flex-wrap items-center gap-2">
                        <StatusBadge status={toStatus(result.runner_status)} />
                        {isSuspiciousLoadRun(result) && <Badge tone="warn">warning</Badge>}
                      </div>
                    </td>
                    <td className={`${tableCellClass} text-right tabular-nums text-slate-300`}>
                      {result.total_requests}
                    </td>
                    <td className={`${tableCellClass} text-right tabular-nums text-slate-300`}>
                      {result.p95_ms.toFixed(0)}
                    </td>
                    <td className={`${tableCellClass} text-right tabular-nums text-slate-300`}>
                      {result.requests_per_second.toFixed(2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </DataTable>
          )}
        </Panel>

        <Panel title="Run Detail">
          {resultDetailError && (
            <InlineAlert tone="danger" title="Detail failed">
              {resultDetailError}
            </InlineAlert>
          )}
          {resultDetailLoading ? (
            <EmptyState title="Loading run detail…" />
          ) : selectedResult ? (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="text-base font-semibold text-slate-100">
                  {selectedResult.scenario_name}
                </h3>
                <StatusBadge status={toStatus(selectedResult.runner_status)} />
                {isSuspiciousLoadRun(selectedResult) && <Badge tone="warn">suspicious</Badge>}
              </div>

              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <MetricCard label="Requests" value={selectedResult.total_requests} />
                <MetricCard label="Failed" value={selectedResult.failed_requests} tone="bad" />
                <MetricCard
                  label="Avg response"
                  value={`${selectedResult.avg_response_time_ms.toFixed(1)} ms`}
                  tone="info"
                />
                <MetricCard
                  label="Error rate"
                  value={`${(selectedResult.error_rate * 100).toFixed(2)}%`}
                  tone={selectedResult.error_rate > 0 ? "warn" : "good"}
                />
              </div>

              <InlineAlert tone={isSuspiciousLoadRun(selectedResult) ? "warning" : "success"}>
                {diagnosticInterpretation(selectedResult)}
              </InlineAlert>

              <div className="rounded-lg border border-slate-800 bg-slate-950/55 p-3 text-sm text-slate-300">
                <p>
                  <span className="text-slate-500">Runner message:</span>{" "}
                  {selectedResult.runner_message || "—"}
                </p>
                <p>
                  <span className="text-slate-500">Exit code:</span>{" "}
                  {selectedResult.runner_exit_code ?? "—"}
                </p>
                <p>
                  <span className="text-slate-500">Executed:</span>{" "}
                  {formatDate(selectedResult.executed_at)}
                </p>
              </div>

              <div className="grid gap-2">
                <JsonDisclosure title="Raw metrics" value={selectedResult.raw_metrics ?? {}} />
                <JsonDisclosure
                  title="Runner stdout excerpt"
                  value={selectedResult.runner_stdout_excerpt || ""}
                />
                <JsonDisclosure
                  title="Runner stderr excerpt"
                  value={selectedResult.runner_stderr_excerpt || ""}
                />
              </div>
            </div>
          ) : (
            <EmptyState
              title="Select a run"
              description="Click a history row to inspect metrics, runner status, warnings, and raw excerpts."
            />
          )}
        </Panel>
      </div>
    </div>
  );
}

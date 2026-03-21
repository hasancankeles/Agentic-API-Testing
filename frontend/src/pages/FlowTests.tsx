import { useCallback, useEffect, useMemo, useState } from "react";
import {
  generateFlows,
  getFlow,
  getFlowRun,
  listFlowRuns,
  listFlows,
  runFlows,
  type FlowGenerateRequest,
  type FlowGenerationSummary,
  type FlowListItem,
  type FlowMutationPolicy,
  type FlowRunGroupResponse,
  type FlowRunListItem,
  type FlowRunRecord,
  type FlowScenario,
  type FlowGenerationMode,
} from "../api/client";
import StatusBadge, { type TestStatus } from "../components/StatusBadge";

function unwrap<T>(res: { data?: T } | T): T {
  const d = res as { data?: T };
  return (d.data !== undefined ? d.data : res) as T;
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleString();
}

function toPrettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function clampInt(
  value: number,
  min: number,
  max: number,
  fallback: number
): number {
  if (!Number.isFinite(value)) return fallback;
  return Math.max(min, Math.min(max, Math.floor(value)));
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
      return {
        value: null,
        error: `${fieldName} must be a JSON object.`,
      };
    }
    return { value: parsed as Record<string, unknown>, error: null };
  } catch {
    return {
      value: null,
      error: `${fieldName} is not valid JSON.`,
    };
  }
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

function toTestStatus(status: string): TestStatus {
  if (
    status === "passed" ||
    status === "failed" ||
    status === "error" ||
    status === "pending" ||
    status === "running"
  ) {
    return status;
  }
  return "pending";
}

const methodStyles: Record<string, string> = {
  GET: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  POST: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  PUT: "bg-amber-500/20 text-amber-400 border-amber-500/30",
  DELETE: "bg-red-500/20 text-red-400 border-red-500/30",
  PATCH: "bg-violet-500/20 text-violet-400 border-violet-500/30",
};

function MethodBadge({ method }: { method: string }) {
  const normalized = method.toUpperCase();
  const style =
    methodStyles[normalized] ??
    "bg-zinc-500/20 text-zinc-400 border-zinc-500/30";
  return (
    <span
      className={`inline-flex items-center rounded border px-2 py-0.5 text-xs font-mono font-medium ${style}`}
    >
      {normalized}
    </span>
  );
}

function JsonDetails({ title, value }: { title: string; value: unknown }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="overflow-hidden rounded-lg border border-zinc-800 bg-zinc-900/60"
      onToggle={(event) => {
        setOpen((event.currentTarget as HTMLDetailsElement).open);
      }}
    >
      <summary className="cursor-pointer list-none px-3 py-2 text-sm font-medium text-zinc-300 hover:bg-zinc-800/70">
        {title}
      </summary>
      {open && (
        <pre className="max-h-80 overflow-auto border-t border-zinc-800 px-3 py-2 text-xs text-zinc-300">
          {toPrettyJson(value)}
        </pre>
      )}
    </details>
  );
}

export default function FlowTests() {
  const [maxFlows, setMaxFlows] = useState(5);
  const [maxStepsPerFlow, setMaxStepsPerFlow] = useState(8);
  const [generationMode, setGenerationMode] =
    useState<FlowGenerationMode>("hybrid_auto");
  const [mutationPolicy, setMutationPolicy] =
    useState<FlowMutationPolicy>("safe");
  const [personasInput, setPersonasInput] = useState("");
  const [appContextInput, setAppContextInput] = useState("{}");
  const [appContextError, setAppContextError] = useState<string | null>(null);
  const [includeNegative, setIncludeNegative] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [generateError, setGenerateError] = useState<string | null>(null);
  const [generationSummary, setGenerationSummary] =
    useState<FlowGenerationSummary | null>(null);

  const [flows, setFlows] = useState<FlowListItem[]>([]);
  const [flowsLoading, setFlowsLoading] = useState(true);
  const [flowsError, setFlowsError] = useState<string | null>(null);
  const [selectedFlowIds, setSelectedFlowIds] = useState<string[]>([]);
  const [selectedFlowId, setSelectedFlowId] = useState<string | null>(null);
  const [selectedFlow, setSelectedFlow] = useState<FlowScenario | null>(null);
  const [flowDetailLoading, setFlowDetailLoading] = useState(false);
  const [flowDetailError, setFlowDetailError] = useState<string | null>(null);

  const [initialContextInput, setInitialContextInput] = useState("{}");
  const [initialContextError, setInitialContextError] = useState<string | null>(
    null
  );
  const [targetBaseUrl, setTargetBaseUrl] = useState("");
  const [runSelectedLoading, setRunSelectedLoading] = useState(false);
  const [runLatestLoading, setRunLatestLoading] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [runGroupSummary, setRunGroupSummary] =
    useState<FlowRunGroupResponse | null>(null);

  const [runs, setRuns] = useState<FlowRunListItem[]>([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<FlowRunRecord | null>(null);
  const [runDetailLoading, setRunDetailLoading] = useState(false);
  const [runDetailError, setRunDetailError] = useState<string | null>(null);

  const selectedFlowSet = useMemo(
    () => new Set(selectedFlowIds),
    [selectedFlowIds]
  );
  const allFlowsSelected =
    flows.length > 0 && selectedFlowIds.length === flows.length;

  const fetchFlows = useCallback(async () => {
    setFlowsLoading(true);
    setFlowsError(null);
    try {
      const res = await listFlows(true);
      const data = unwrap(res.data);
      const fetchedFlows = Array.isArray(data) ? data : [];
      setFlows(fetchedFlows);
      setSelectedFlowIds((prev) =>
        prev.filter((id) => fetchedFlows.some((flow) => flow.id === id))
      );
      setSelectedFlowId((prev) =>
        prev && fetchedFlows.some((flow) => flow.id === prev) ? prev : null
      );
      setSelectedFlow((prev) =>
        prev && fetchedFlows.some((flow) => flow.id === prev.id) ? prev : null
      );
    } catch (error) {
      setFlows([]);
      setFlowsError(extractErrorMessage(error, "Failed to load flows."));
      setSelectedFlowIds([]);
      setSelectedFlowId(null);
      setSelectedFlow(null);
    } finally {
      setFlowsLoading(false);
    }
  }, []);

  const fetchRuns = useCallback(async () => {
    setRunsLoading(true);
    setRunsError(null);
    try {
      const res = await listFlowRuns(20);
      const data = unwrap(res.data);
      const fetchedRuns = Array.isArray(data) ? data : [];
      setRuns(fetchedRuns);
      setSelectedRunId((prev) =>
        prev && fetchedRuns.some((run) => run.id === prev) ? prev : null
      );
      setSelectedRun((prev) =>
        prev && fetchedRuns.some((run) => run.id === prev.id) ? prev : null
      );
    } catch (error) {
      setRuns([]);
      setRunsError(extractErrorMessage(error, "Failed to load flow run history."));
      setSelectedRunId(null);
      setSelectedRun(null);
    } finally {
      setRunsLoading(false);
    }
  }, []);

  const loadFlowDetail = useCallback(async (flowId: string) => {
    setFlowDetailLoading(true);
    setFlowDetailError(null);
    setSelectedFlowId(flowId);
    try {
      const res = await getFlow(flowId);
      const data = unwrap(res.data);
      setSelectedFlow(data);
    } catch (error) {
      setSelectedFlow(null);
      setFlowDetailError(extractErrorMessage(error, "Failed to load flow detail."));
    } finally {
      setFlowDetailLoading(false);
    }
  }, []);

  const loadRunDetail = useCallback(async (runId: string) => {
    setRunDetailLoading(true);
    setRunDetailError(null);
    setSelectedRunId(runId);
    try {
      const res = await getFlowRun(runId);
      const data = unwrap(res.data);
      setSelectedRun(data);
    } catch (error) {
      setSelectedRun(null);
      setRunDetailError(extractErrorMessage(error, "Failed to load run detail."));
    } finally {
      setRunDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchFlows();
    void fetchRuns();
  }, [fetchFlows, fetchRuns]);

  const handleGenerateFlows = async () => {
    const parsedAppContext = parseJsonObject(appContextInput, "App context");
    if (parsedAppContext.error || !parsedAppContext.value) {
      setAppContextError(parsedAppContext.error);
      return;
    }
    setAppContextError(null);

    const payload: FlowGenerateRequest = {
      max_flows: clampInt(maxFlows, 1, 20, 5),
      max_steps_per_flow: clampInt(maxStepsPerFlow, 2, 20, 8),
      include_negative: includeNegative,
      generation_mode: generationMode,
      mutation_policy: mutationPolicy,
      personas: personasInput
        .split(",")
        .map((item) => item.trim())
        .filter((item) => item.length > 0),
      app_context: parsedAppContext.value,
    };

    setGenerating(true);
    setGenerateError(null);
    try {
      const res = await generateFlows(payload);
      const data = unwrap(res.data);
      setGenerationSummary(data.summary);
      await fetchFlows();
      if (data.flows.length > 0) {
        const firstFlow = data.flows[0];
        setSelectedFlowId(firstFlow.id);
        setSelectedFlow(firstFlow);
      }
    } catch (error) {
      setGenerateError(extractErrorMessage(error, "Failed to generate flows."));
    } finally {
      setGenerating(false);
    }
  };

  const parseInitialContextOrFail = (): Record<string, unknown> | null => {
    const parsedInitialContext = parseJsonObject(
      initialContextInput,
      "Initial context"
    );
    if (parsedInitialContext.error || !parsedInitialContext.value) {
      setInitialContextError(parsedInitialContext.error);
      return null;
    }
    setInitialContextError(null);
    return parsedInitialContext.value;
  };

  const handleRunSelected = async () => {
    if (selectedFlowIds.length === 0) {
      setRunError("Select at least one flow before running selected.");
      return;
    }

    const initialContext = parseInitialContextOrFail();
    if (!initialContext) {
      return;
    }

    setRunSelectedLoading(true);
    setRunError(null);
    try {
      const res = await runFlows({
        flow_ids: selectedFlowIds,
        target_base_url: targetBaseUrl.trim() || undefined,
        initial_context: initialContext,
      });
      const data = unwrap(res.data);
      setRunGroupSummary(data);
      await fetchRuns();
      if (data.flow_runs.length > 0) {
        await loadRunDetail(data.flow_runs[0].id);
      }
    } catch (error) {
      setRunError(extractErrorMessage(error, "Failed to run selected flows."));
    } finally {
      setRunSelectedLoading(false);
    }
  };

  const handleRunLatestBatch = async () => {
    const initialContext = parseInitialContextOrFail();
    if (!initialContext) {
      return;
    }

    setRunLatestLoading(true);
    setRunError(null);
    try {
      const res = await runFlows({
        target_base_url: targetBaseUrl.trim() || undefined,
        initial_context: initialContext,
      });
      const data = unwrap(res.data);
      setRunGroupSummary(data);
      await fetchRuns();
      if (data.flow_runs.length > 0) {
        await loadRunDetail(data.flow_runs[0].id);
      }
    } catch (error) {
      setRunError(extractErrorMessage(error, "Failed to run latest flow batch."));
    } finally {
      setRunLatestLoading(false);
    }
  };

  const toggleFlowSelection = (flowId: string) => {
    setSelectedFlowIds((prev) =>
      prev.includes(flowId)
        ? prev.filter((id) => id !== flowId)
        : [...prev, flowId]
    );
  };

  const handleSelectAllFlows = () => {
    setSelectedFlowIds(flows.map((flow) => flow.id));
  };

  const handleClearFlowSelection = () => {
    setSelectedFlowIds([]);
  };

  return (
    <div className="min-h-screen bg-slate-950 p-6 text-zinc-100">
      <div className="mx-auto max-w-7xl space-y-6">
        <h1 className="text-2xl font-semibold text-zinc-50">Flow Tests</h1>

        {/* Generate panel */}
        <section className="rounded-xl border border-zinc-800 bg-zinc-900/80 p-4">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-sm font-medium text-zinc-400">Generate Flows</h2>
            <button
              type="button"
              onClick={handleGenerateFlows}
              disabled={generating}
              className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-medium text-zinc-100 hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {generating ? (
                <span className="inline-flex items-center gap-2">
                  <span className="size-4 animate-spin rounded-full border-2 border-zinc-300 border-t-transparent" />
                  Generating
                </span>
              ) : (
                "Generate Flows"
              )}
            </button>
          </div>

          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            <label className="space-y-1">
              <span className="text-sm text-zinc-400">Max flows</span>
              <input
                type="number"
                min={1}
                max={20}
                value={maxFlows}
                onChange={(event) => setMaxFlows(Number(event.target.value))}
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              />
            </label>

            <label className="space-y-1">
              <span className="text-sm text-zinc-400">Max steps per flow</span>
              <input
                type="number"
                min={2}
                max={20}
                value={maxStepsPerFlow}
                onChange={(event) =>
                  setMaxStepsPerFlow(Number(event.target.value))
                }
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              />
            </label>

            <label className="space-y-1">
              <span className="text-sm text-zinc-400">Generation mode</span>
              <select
                value={generationMode}
                onChange={(event) =>
                  setGenerationMode(event.target.value as FlowGenerationMode)
                }
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              >
                <option value="hybrid_auto">hybrid_auto</option>
                <option value="llm_first">llm_first</option>
                <option value="deterministic_first">deterministic_first</option>
              </select>
            </label>

            <label className="space-y-1">
              <span className="text-sm text-zinc-400">Mutation policy</span>
              <select
                value={mutationPolicy}
                onChange={(event) =>
                  setMutationPolicy(event.target.value as FlowMutationPolicy)
                }
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              >
                <option value="safe">safe</option>
                <option value="balanced">balanced</option>
                <option value="full_lifecycle">full_lifecycle</option>
              </select>
            </label>

            <label className="space-y-1 lg:col-span-2">
              <span className="text-sm text-zinc-400">
                Personas (comma separated)
              </span>
              <input
                type="text"
                value={personasInput}
                onChange={(event) => setPersonasInput(event.target.value)}
                placeholder="guest_user, registered_user"
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-zinc-100 placeholder-zinc-500 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              />
            </label>
          </div>

          <div className="mt-4 space-y-2">
            <label className="space-y-1">
              <span className="text-sm text-zinc-400">App context (JSON)</span>
              <textarea
                value={appContextInput}
                onChange={(event) => setAppContextInput(event.target.value)}
                rows={5}
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 font-mono text-sm text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              />
            </label>
            {appContextError && (
              <p className="text-sm text-red-400">{appContextError}</p>
            )}
          </div>

          <label className="mt-4 inline-flex items-center gap-2 text-sm text-zinc-300">
            <input
              type="checkbox"
              checked={includeNegative}
              onChange={(event) => setIncludeNegative(event.target.checked)}
              className="size-4 rounded border-zinc-700 bg-zinc-800 text-emerald-600 focus:ring-emerald-500/40"
            />
            Include negative flows
          </label>

          {generateError && (
            <div className="mt-4 rounded-lg border border-red-800 bg-red-950/50 px-4 py-2 text-sm text-red-300">
              {generateError}
            </div>
          )}

          {generationSummary && (
            <div className="mt-4 rounded-lg border border-zinc-800 bg-zinc-950/70 p-4">
              <h3 className="mb-3 text-sm font-medium text-zinc-300">
                Last generation summary
              </h3>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <div>
                  <p className="text-xs text-zinc-500">Source</p>
                  <p className="text-sm text-zinc-200">{generationSummary.source}</p>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">Fallback used</p>
                  <p className="text-sm text-zinc-200">
                    {generationSummary.fallback_used ? "yes" : "no"}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">Flows generated</p>
                  <p className="text-sm text-zinc-200">
                    {generationSummary.flows_generated}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">Dependency hints</p>
                  <p className="text-sm text-zinc-200">
                    {generationSummary.dependency_hints_count}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">OpenAPI link hints</p>
                  <p className="text-sm text-zinc-200">
                    {generationSummary.openapi_link_hints_count}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">Generation mode</p>
                  <p className="text-sm text-zinc-200">
                    {generationSummary.generation_mode ?? "—"}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">Mutation policy</p>
                  <p className="text-sm text-zinc-200">
                    {generationSummary.mutation_policy ?? "—"}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">Generated at</p>
                  <p className="text-sm text-zinc-200">
                    {formatDate(generationSummary.batch_created_at)}
                  </p>
                </div>
              </div>
              {generationSummary.objectives_used &&
                generationSummary.objectives_used.length > 0 && (
                  <p className="mt-3 text-sm text-zinc-400">
                    Objectives: {generationSummary.objectives_used.join(", ")}
                  </p>
                )}
              {generationSummary.fallback_reason && (
                <p className="mt-2 text-sm text-amber-400">
                  Fallback reason: {generationSummary.fallback_reason}
                </p>
              )}
            </div>
          )}
        </section>

        {/* Flow list + selection */}
        <section className="rounded-xl border border-zinc-800 bg-zinc-900/80 p-4">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <h2 className="text-sm font-medium text-zinc-400">
              Latest Flow Batch
            </h2>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={fetchFlows}
                disabled={flowsLoading}
                className="rounded-lg bg-slate-700 px-3 py-1.5 text-sm font-medium text-zinc-100 hover:bg-slate-600 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Refresh
              </button>
              <button
                type="button"
                onClick={handleSelectAllFlows}
                disabled={flowsLoading || flows.length === 0 || allFlowsSelected}
                className="rounded-lg bg-zinc-800 px-3 py-1.5 text-sm font-medium text-zinc-100 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Select All
              </button>
              <button
                type="button"
                onClick={handleClearFlowSelection}
                disabled={flowsLoading || selectedFlowIds.length === 0}
                className="rounded-lg bg-zinc-800 px-3 py-1.5 text-sm font-medium text-zinc-100 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Clear
              </button>
            </div>
          </div>

          {flowsError && (
            <div className="mb-4 rounded-lg border border-red-800 bg-red-950/50 px-4 py-2 text-sm text-red-300">
              {flowsError}
            </div>
          )}

          {flowsLoading ? (
            <div className="py-10 text-center text-zinc-500">Loading flows…</div>
          ) : flows.length === 0 ? (
            <div className="py-10 text-center text-zinc-500">
              No flows found in latest batch.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-zinc-800">
                    <th className="w-12 px-3 py-3 text-left text-zinc-400">Sel</th>
                    <th className="px-3 py-3 text-left text-zinc-400">Name</th>
                    <th className="px-3 py-3 text-left text-zinc-400">Persona</th>
                    <th className="px-3 py-3 text-left text-zinc-400">Tags</th>
                    <th className="px-3 py-3 text-right text-zinc-400">Steps</th>
                    <th className="px-3 py-3 text-left text-zinc-400">Created</th>
                  </tr>
                </thead>
                <tbody>
                  {flows.map((flow, index) => (
                    <tr
                      key={flow.id}
                      onClick={() => void loadFlowDetail(flow.id)}
                      className={`cursor-pointer border-b border-zinc-900/70 transition-colors hover:bg-zinc-800/50 ${
                        selectedFlowId === flow.id
                          ? "bg-zinc-800/60"
                          : index % 2 === 0
                          ? "bg-zinc-900/30"
                          : "bg-zinc-900/10"
                      }`}
                    >
                      <td className="px-3 py-3">
                        <input
                          type="checkbox"
                          checked={selectedFlowSet.has(flow.id)}
                          onClick={(event) => event.stopPropagation()}
                          onChange={(event) => {
                            event.stopPropagation();
                            toggleFlowSelection(flow.id);
                          }}
                          className="size-4 rounded border-zinc-700 bg-zinc-800 text-emerald-600 focus:ring-emerald-500/40"
                        />
                      </td>
                      <td className="px-3 py-3 font-medium text-zinc-100">
                        {flow.name}
                      </td>
                      <td className="px-3 py-3 text-zinc-300">
                        {flow.persona || "—"}
                      </td>
                      <td className="px-3 py-3 text-zinc-400">
                        {flow.tags.length > 0 ? flow.tags.join(", ") : "—"}
                      </td>
                      <td className="px-3 py-3 text-right text-zinc-300">
                        {flow.step_count}
                      </td>
                      <td className="px-3 py-3 text-zinc-400">
                        {formatDate(flow.created_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {flowDetailError && (
            <div className="mt-4 rounded-lg border border-red-800 bg-red-950/50 px-4 py-2 text-sm text-red-300">
              {flowDetailError}
            </div>
          )}

          {flowDetailLoading ? (
            <div className="mt-4 text-sm text-zinc-500">Loading flow detail…</div>
          ) : selectedFlow ? (
            <div className="mt-4 rounded-lg border border-zinc-800 bg-zinc-950/70 p-4">
              <div className="mb-3 flex flex-wrap items-center gap-3">
                <h3 className="text-base font-medium text-zinc-100">
                  {selectedFlow.name}
                </h3>
                <span className="rounded border border-zinc-700 bg-zinc-900 px-2 py-0.5 text-xs text-zinc-400">
                  {selectedFlow.persona || "no persona"}
                </span>
              </div>
              {selectedFlow.description && (
                <p className="mb-3 text-sm text-zinc-400">
                  {selectedFlow.description}
                </p>
              )}
              <div className="space-y-2">
                {selectedFlow.steps
                  .slice()
                  .sort((a, b) => a.order - b.order)
                  .map((step) => (
                    <div
                      key={step.step_id}
                      className="rounded-lg border border-zinc-800 bg-zinc-900/60 px-3 py-2"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-xs text-zinc-500">
                          #{step.order}
                        </span>
                        <MethodBadge method={step.method} />
                        <span className="text-sm font-medium text-zinc-200">
                          {step.name}
                        </span>
                        <span className="font-mono text-xs text-zinc-400">
                          {step.endpoint}
                        </span>
                      </div>
                      <div className="mt-1 text-xs text-zinc-500">
                        required: {step.required ? "yes" : "no"} | extracts:{" "}
                        {step.extract.length > 0
                          ? step.extract.map((rule) => rule.var).join(", ")
                          : "none"}
                      </div>
                    </div>
                  ))}
              </div>
            </div>
          ) : null}
        </section>

        {/* Run panel */}
        <section className="rounded-xl border border-zinc-800 bg-zinc-900/80 p-4">
          <h2 className="mb-4 text-sm font-medium text-zinc-400">Run Flows</h2>
          <div className="grid gap-4 lg:grid-cols-2">
            <label className="space-y-1">
              <span className="text-sm text-zinc-400">Target base URL (optional)</span>
              <input
                type="text"
                value={targetBaseUrl}
                onChange={(event) => setTargetBaseUrl(event.target.value)}
                placeholder="https://api.example.com"
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-zinc-100 placeholder-zinc-500 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              />
            </label>
            <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 px-3 py-2 text-sm text-zinc-400">
              Selected flows:{" "}
              <span className="font-semibold text-zinc-200">
                {selectedFlowIds.length}
              </span>
            </div>
          </div>

          <div className="mt-4 space-y-2">
            <label className="space-y-1">
              <span className="text-sm text-zinc-400">Initial context (JSON)</span>
              <textarea
                value={initialContextInput}
                onChange={(event) => setInitialContextInput(event.target.value)}
                rows={5}
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 font-mono text-sm text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              />
            </label>
            {initialContextError && (
              <p className="text-sm text-red-400">{initialContextError}</p>
            )}
          </div>

          <div className="mt-4 flex flex-wrap gap-3">
            <button
              type="button"
              onClick={handleRunSelected}
              disabled={
                runSelectedLoading ||
                runLatestLoading ||
                selectedFlowIds.length === 0
              }
              className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-medium text-zinc-100 hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {runSelectedLoading ? (
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
              onClick={handleRunLatestBatch}
              disabled={runSelectedLoading || runLatestLoading}
              className="rounded-lg bg-slate-700 px-4 py-2 text-sm font-medium text-zinc-100 hover:bg-slate-600 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {runLatestLoading ? (
                <span className="inline-flex items-center gap-2">
                  <span className="size-4 animate-spin rounded-full border-2 border-zinc-300 border-t-transparent" />
                  Running Latest
                </span>
              ) : (
                "Run Latest Batch"
              )}
            </button>
          </div>

          {runError && (
            <div className="mt-4 rounded-lg border border-red-800 bg-red-950/50 px-4 py-2 text-sm text-red-300">
              {runError}
            </div>
          )}

          {runGroupSummary && (
            <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3">
                <p className="text-xs text-zinc-500">Total flows</p>
                <p className="text-lg font-semibold text-zinc-100">
                  {runGroupSummary.total_flows}
                </p>
              </div>
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3">
                <p className="text-xs text-zinc-500">Passed</p>
                <p className="text-lg font-semibold text-emerald-400">
                  {runGroupSummary.passed}
                </p>
              </div>
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3">
                <p className="text-xs text-zinc-500">Failed</p>
                <p className="text-lg font-semibold text-red-400">
                  {runGroupSummary.failed}
                </p>
              </div>
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3">
                <p className="text-xs text-zinc-500">Errors</p>
                <p className="text-lg font-semibold text-amber-400">
                  {runGroupSummary.errors}
                </p>
              </div>
            </div>
          )}
        </section>

        {/* History + run detail */}
        <section className="rounded-xl border border-zinc-800 bg-zinc-900/80 p-4">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-sm font-medium text-zinc-400">Flow Run History</h2>
            <button
              type="button"
              onClick={fetchRuns}
              disabled={runsLoading}
              className="rounded-lg bg-slate-700 px-3 py-1.5 text-sm font-medium text-zinc-100 hover:bg-slate-600 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Refresh
            </button>
          </div>

          {runsError && (
            <div className="mb-4 rounded-lg border border-red-800 bg-red-950/50 px-4 py-2 text-sm text-red-300">
              {runsError}
            </div>
          )}

          {runsLoading ? (
            <div className="py-10 text-center text-zinc-500">Loading runs…</div>
          ) : runs.length === 0 ? (
            <div className="py-10 text-center text-zinc-500">No flow runs yet.</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-zinc-800">
                    <th className="px-3 py-3 text-left text-zinc-400">Run</th>
                    <th className="px-3 py-3 text-left text-zinc-400">Flow</th>
                    <th className="px-3 py-3 text-left text-zinc-400">Status</th>
                    <th className="px-3 py-3 text-left text-zinc-400">Started</th>
                    <th className="px-3 py-3 text-left text-zinc-400">Finished</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((run, index) => (
                    <tr
                      key={run.id}
                      onClick={() => void loadRunDetail(run.id)}
                      className={`cursor-pointer border-b border-zinc-900/70 transition-colors hover:bg-zinc-800/50 ${
                        selectedRunId === run.id
                          ? "bg-zinc-800/60"
                          : index % 2 === 0
                          ? "bg-zinc-900/30"
                          : "bg-zinc-900/10"
                      }`}
                    >
                      <td className="px-3 py-3 font-mono text-xs text-zinc-400">
                        {run.id}
                      </td>
                      <td className="px-3 py-3 text-zinc-200">{run.flow_name}</td>
                      <td className="px-3 py-3">
                        <StatusBadge status={toTestStatus(run.status)} />
                      </td>
                      <td className="px-3 py-3 text-zinc-400">
                        {formatDate(run.started_at)}
                      </td>
                      <td className="px-3 py-3 text-zinc-400">
                        {formatDate(run.finished_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {runDetailError && (
            <div className="mt-4 rounded-lg border border-red-800 bg-red-950/50 px-4 py-2 text-sm text-red-300">
              {runDetailError}
            </div>
          )}

          {runDetailLoading ? (
            <div className="mt-4 text-sm text-zinc-500">Loading run detail…</div>
          ) : selectedRun ? (
            <div className="mt-4 space-y-4 rounded-lg border border-zinc-800 bg-zinc-950/70 p-4">
              <div className="flex flex-wrap items-center gap-3">
                <h3 className="text-base font-medium text-zinc-100">
                  {selectedRun.flow_name}
                </h3>
                <StatusBadge status={toTestStatus(selectedRun.status)} />
                <span className="font-mono text-xs text-zinc-500">
                  {selectedRun.id}
                </span>
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
                  <p className="text-xs text-zinc-500">Target base URL</p>
                  <p className="font-mono text-xs text-zinc-300">
                    {selectedRun.target_base_url}
                  </p>
                </div>
                <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
                  <p className="text-xs text-zinc-500">Window</p>
                  <p className="text-sm text-zinc-300">
                    {formatDate(selectedRun.started_at)} →{" "}
                    {formatDate(selectedRun.finished_at)}
                  </p>
                </div>
              </div>

              <div className="grid gap-2">
                <JsonDetails
                  title="Initial context"
                  value={selectedRun.initial_context}
                />
                <JsonDetails title="Final context" value={selectedRun.final_context} />
              </div>

              <div className="space-y-3">
                <h4 className="text-sm font-medium text-zinc-300">Step Trace</h4>
                {selectedRun.step_results.length === 0 ? (
                  <p className="text-sm text-zinc-500">No step results recorded.</p>
                ) : (
                  selectedRun.step_results.map((step) => {
                    const methodCandidate = step.resolved_request["method"];
                    const endpointCandidate = step.resolved_request["endpoint"];
                    const urlCandidate = step.resolved_request["url"];
                    const method =
                      typeof methodCandidate === "string"
                        ? methodCandidate
                        : "UNKNOWN";
                    const endpoint =
                      typeof endpointCandidate === "string"
                        ? endpointCandidate
                        : typeof urlCandidate === "string"
                        ? urlCandidate
                        : step.step_id;

                    return (
                      <article
                        key={step.id}
                        className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3"
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-mono text-xs text-zinc-500">
                            #{step.order}
                          </span>
                          <StatusBadge status={toTestStatus(step.status)} />
                          <MethodBadge method={method} />
                          <span className="font-mono text-xs text-zinc-400">
                            {endpoint}
                          </span>
                        </div>
                        <div className="mt-2 flex flex-wrap gap-4 text-xs text-zinc-400">
                          <span>
                            response:{" "}
                            <span className="font-mono text-zinc-200">
                              {step.response_status ?? "—"}
                            </span>
                          </span>
                          <span>
                            assertions:{" "}
                            <span className="font-mono text-zinc-200">
                              {step.assertions_passed}/{step.assertions_total}
                            </span>
                          </span>
                          <span>executed: {formatDate(step.executed_at)}</span>
                        </div>
                        {step.error_message && (
                          <p className="mt-2 rounded border border-red-800 bg-red-950/40 px-2 py-1 text-xs text-red-300">
                            {step.error_message}
                          </p>
                        )}
                        <div className="mt-3 grid gap-2">
                          <JsonDetails
                            title="Resolved request"
                            value={step.resolved_request}
                          />
                          <JsonDetails
                            title="Response body"
                            value={step.response_body}
                          />
                          <JsonDetails
                            title="Extracted context delta"
                            value={step.extracted_context_delta}
                          />
                        </div>
                      </article>
                    );
                  })
                )}
              </div>
            </div>
          ) : null}
        </section>
      </div>
    </div>
  );
}

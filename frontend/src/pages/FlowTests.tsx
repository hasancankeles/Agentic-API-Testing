import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CheckCircle2,
  History,
  Play,
  RefreshCw,
  Route,
  Sparkles,
  Wand2,
  XCircle,
} from "lucide-react";
import {
  generateFlows,
  getFlow,
  getFlowRun,
  listFlowRuns,
  listFlows,
  runFlows,
  type EliminatedFlowCandidate,
  type FlowGenerateRequest,
  type FlowGenerationMode,
  type FlowGenerationSummary,
  type FlowListItem,
  type FlowMutationPolicy,
  type FlowRunGroupResponse,
  type FlowRunListItem,
  type FlowRunRecord,
  type FlowScenario,
  type FlowStep,
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
  clampInt,
  cn,
  extractErrorMessage,
  formatDate,
  parseJsonObject,
  toStatus,
  truncateMiddle,
} from "../lib/ui";

function unwrap<T>(res: { data?: T } | T): T {
  const d = res as { data?: T };
  return (d.data !== undefined ? d.data : res) as T;
}

type FlowTab = "generate" | "review" | "run" | "history";

type SuggestionChip = {
  label: string;
  action: "deterministic_first" | "safe_policy" | "reduce_steps" | "disable_negative" | "retry_hybrid";
};

type FallbackDiagnostics = {
  category: string;
  detail: string;
  suggestions: SuggestionChip[];
};

function analyzeFallbackReason(reason: string): FallbackDiagnostics {
  const detail = reason.trim();
  if (!detail) return { category: "none", detail: "", suggestions: [] };

  const lowered = detail.toLowerCase();
  if (lowered.includes("api key") || lowered.includes("missing_gemini_api_key")) {
    return {
      category: "configuration",
      detail,
      suggestions: [
        { label: "Use deterministic mode", action: "deterministic_first" },
        { label: "Retry hybrid", action: "retry_hybrid" },
      ],
    };
  }
  if (lowered.includes("validation") || lowered.includes("pydantic") || lowered.includes("missing")) {
    return {
      category: "schema mismatch",
      detail,
      suggestions: [
        { label: "Use deterministic mode", action: "deterministic_first" },
        { label: "Safe mutation policy", action: "safe_policy" },
        { label: "Reduce steps", action: "reduce_steps" },
      ],
    };
  }
  if (lowered.includes("quality")) {
    return {
      category: "quality gate",
      detail,
      suggestions: [
        { label: "Safe mutation policy", action: "safe_policy" },
        { label: "Reduce steps", action: "reduce_steps" },
        { label: "Disable negatives", action: "disable_negative" },
      ],
    };
  }
  if (lowered.includes("timeout") || lowered.includes("upstream") || lowered.includes("server error")) {
    return {
      category: "upstream",
      detail,
      suggestions: [
        { label: "Retry hybrid", action: "retry_hybrid" },
        { label: "Use deterministic mode", action: "deterministic_first" },
      ],
    };
  }
  return {
    category: "other",
    detail,
    suggestions: [
      { label: "Safe mutation policy", action: "safe_policy" },
      { label: "Use deterministic mode", action: "deterministic_first" },
    ],
  };
}

function groupEliminated(items: EliminatedFlowCandidate[] = []) {
  return items.reduce<Record<string, EliminatedFlowCandidate[]>>((acc, item) => {
    const key = item.reason_code || "other";
    acc[key] = [...(acc[key] ?? []), item];
    return acc;
  }, {});
}

function StepTimeline({ steps }: { steps: FlowStep[] }) {
  const sorted = steps.slice().sort((a, b) => a.order - b.order);
  return (
    <div className="space-y-3">
      {sorted.map((step) => (
        <article
          key={step.step_id}
          className="rounded-lg border border-slate-800 bg-slate-950/55 p-3"
        >
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone="neutral" className="font-mono">
              #{step.order}
            </Badge>
            <MethodBadge method={step.method} />
            <span className="min-w-0 flex-1 text-sm font-medium text-slate-100">
              {step.name}
            </span>
            <Badge tone={step.required ? "info" : "neutral"}>
              {step.required ? "required" : "optional"}
            </Badge>
          </div>
          <p className="mt-2 break-all font-mono text-xs text-slate-400">
            {step.endpoint}
          </p>
          <div className="mt-3 grid gap-2 text-xs text-slate-400 sm:grid-cols-3">
            <span>
              Expected:{" "}
              <span className="font-mono text-slate-200">
                {step.expected_status ?? "any"}
              </span>
            </span>
            <span>
              Extracts:{" "}
              <span className="text-slate-200">
                {step.extract.length > 0
                  ? step.extract.map((rule) => rule.var).join(", ")
                  : "none"}
              </span>
            </span>
            <span>
              Assertions:{" "}
              <span className="font-mono text-slate-200">
                {step.assertions.length}
              </span>
            </span>
          </div>
        </article>
      ))}
    </div>
  );
}

function GenerationDecision({
  summary,
  onSuggestion,
}: {
  summary: FlowGenerationSummary;
  onSuggestion: (action: SuggestionChip["action"]) => void;
}) {
  const diagnostics = analyzeFallbackReason(summary.fallback_reason ?? "");
  const rejectedGroups = groupEliminated(summary.eliminated_flows ?? []);
  const rejectedEntries = Object.entries(rejectedGroups);

  return (
    <div className="space-y-4">
      <div className="grid gap-3 md:grid-cols-4">
        <MetricCard
          label="Accepted"
          value={summary.flows_generated}
          tone={summary.flows_generated > 0 ? "good" : "warn"}
          icon={CheckCircle2}
        />
        <MetricCard
          label="Rejected"
          value={summary.eliminated_flows_count ?? 0}
          tone={(summary.eliminated_flows_count ?? 0) > 0 ? "warn" : "neutral"}
          icon={XCircle}
        />
        <MetricCard
          label="Final Source"
          value={summary.source}
          tone="info"
          icon={Wand2}
        />
        <MetricCard
          label="Reviewer"
          value={summary.reviewer_applied ? "applied" : "not used"}
          tone={summary.reviewer_applied ? "good" : "neutral"}
        />
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <div className="rounded-lg border border-slate-800 bg-slate-950/55 p-4">
          <h3 className="text-sm font-semibold text-slate-100">Decision Path</h3>
          <div className="mt-3 space-y-3">
            {[
              ["Requested mode", summary.generation_mode ?? "—"],
              ["Mutation policy", summary.mutation_policy ?? "—"],
              ["LLM attempted", summary.llm_attempted ? "yes" : "no"],
              ["Normalizations", summary.llm_normalizations_applied ?? 0],
              ["Candidates reviewed", summary.candidate_flows_reviewed ?? 0],
              ["Negative steps", summary.negative_flows_added ?? 0],
              ["Generated at", formatDate(summary.batch_created_at)],
            ].map(([label, value]) => (
              <div key={label} className="flex items-center justify-between gap-4">
                <span className="text-sm text-slate-500">{label}</span>
                <span className="text-right text-sm font-medium text-slate-200">
                  {value}
                </span>
              </div>
            ))}
          </div>
        </div>

        <div className="rounded-lg border border-slate-800 bg-slate-950/55 p-4">
          <h3 className="text-sm font-semibold text-slate-100">Fallback & Hints</h3>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <MetricCard
              label="Dependency hints"
              value={summary.dependency_hints_count}
              tone="neutral"
            />
            <MetricCard
              label="OpenAPI links"
              value={summary.openapi_link_hints_count}
              tone="neutral"
            />
          </div>
          {(summary.fallback_used || diagnostics.detail) && (
            <div className="mt-3 rounded-lg border border-amber-500/25 bg-amber-500/10 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone="warn">{diagnostics.category}</Badge>
                <span className="text-sm text-amber-100">
                  {summary.fallback_used ? "Fallback was used." : "Fallback detail available."}
                </span>
              </div>
              {diagnostics.detail && (
                <p className="mt-2 text-sm text-amber-100/80">{diagnostics.detail}</p>
              )}
              {diagnostics.suggestions.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-2">
                  {diagnostics.suggestions.map((chip) => (
                    <Button
                      key={`${chip.action}:${chip.label}`}
                      size="sm"
                      variant="ghost"
                      onClick={() => onSuggestion(chip.action)}
                    >
                      {chip.label}
                    </Button>
                  ))}
                </div>
              )}
            </div>
          )}
          {summary.negative_generation_skipped_reason && (
            <InlineAlert tone="warning">
              Negative generation skipped: {summary.negative_generation_skipped_reason}
            </InlineAlert>
          )}
        </div>
      </div>

      {rejectedEntries.length > 0 && (
        <div className="rounded-lg border border-slate-800 bg-slate-950/55 p-4">
          <h3 className="text-sm font-semibold text-slate-100">Rejected Candidates</h3>
          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            {rejectedEntries.map(([reason, items]) => (
              <details
                key={reason}
                className="rounded-lg border border-slate-800 bg-slate-900/70 p-3"
              >
                <summary className="cursor-pointer text-sm font-medium text-slate-200">
                  {reason} ({items.length})
                </summary>
                <div className="mt-3 space-y-2">
                  {items.map((item, index) => (
                    <div
                      key={`${item.name}:${index}`}
                      className="rounded-md border border-slate-800 bg-slate-950/70 px-3 py-2"
                    >
                      <p className="text-sm text-slate-100">{item.name}</p>
                      <p className="mt-1 text-xs text-slate-500">{item.reason}</p>
                    </div>
                  ))}
                </div>
              </details>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default function FlowTests() {
  const [activeTab, setActiveTab] = useState<FlowTab>("generate");
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
      const fetched = Array.isArray(data) ? data : [];
      setFlows(fetched);
      setSelectedFlowIds((prev) =>
        prev.filter((id) => fetched.some((flow) => flow.id === id))
      );
      setSelectedFlowId((prev) =>
        prev && fetched.some((flow) => flow.id === prev) ? prev : null
      );
      setSelectedFlow((prev) =>
        prev && fetched.some((flow) => flow.id === prev.id) ? prev : null
      );
    } catch (err) {
      setFlows([]);
      setSelectedFlowIds([]);
      setSelectedFlowId(null);
      setSelectedFlow(null);
      setFlowsError(extractErrorMessage(err, "Failed to load flows."));
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
      const fetched = Array.isArray(data) ? data : [];
      setRuns(fetched);
      setSelectedRunId((prev) =>
        prev && fetched.some((run) => run.id === prev) ? prev : null
      );
      setSelectedRun((prev) =>
        prev && fetched.some((run) => run.id === prev.id) ? prev : null
      );
    } catch (err) {
      setRuns([]);
      setSelectedRunId(null);
      setSelectedRun(null);
      setRunsError(extractErrorMessage(err, "Failed to load flow run history."));
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
      setSelectedFlow(unwrap(res.data));
    } catch (err) {
      setSelectedFlow(null);
      setFlowDetailError(extractErrorMessage(err, "Failed to load flow detail."));
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
      setSelectedRun(unwrap(res.data));
    } catch (err) {
      setSelectedRun(null);
      setRunDetailError(extractErrorMessage(err, "Failed to load run detail."));
    } finally {
      setRunDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchFlows();
    void fetchRuns();
  }, [fetchFlows, fetchRuns]);

  const applySuggestionChip = (action: SuggestionChip["action"]) => {
    if (action === "deterministic_first") {
      setGenerationMode("deterministic_first");
    } else if (action === "safe_policy") {
      setMutationPolicy("safe");
    } else if (action === "reduce_steps") {
      setMaxStepsPerFlow((current) => Math.max(2, Math.min(current, 6)));
    } else if (action === "disable_negative") {
      setIncludeNegative(false);
    } else {
      setGenerationMode("hybrid_auto");
    }
    setActiveTab("generate");
  };

  const handleGenerateFlows = async () => {
    const parsed = parseJsonObject(appContextInput, "App context");
    if (parsed.error || !parsed.value) {
      setAppContextError(parsed.error);
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
        .filter(Boolean),
      app_context: parsed.value,
    };

    setGenerating(true);
    setGenerateError(null);
    try {
      const res = await generateFlows(payload);
      const data = unwrap(res.data);
      setGenerationSummary(data.summary);
      if (data.flows.length > 0) {
        await fetchFlows();
        setSelectedFlowId(data.flows[0].id);
        setSelectedFlow(data.flows[0]);
        setSelectedFlowIds(data.flows.map((flow) => flow.id));
        setActiveTab("review");
      } else {
        setSelectedFlowId(null);
        setSelectedFlow(null);
        setSelectedFlowIds([]);
        setActiveTab("generate");
      }
    } catch (err) {
      setGenerateError(extractErrorMessage(err, "Failed to generate flows."));
    } finally {
      setGenerating(false);
    }
  };

  const parseInitialContextOrFail = (): Record<string, unknown> | null => {
    const parsed = parseJsonObject(initialContextInput, "Initial context");
    if (parsed.error || !parsed.value) {
      setInitialContextError(parsed.error);
      return null;
    }
    setInitialContextError(null);
    return parsed.value;
  };

  const handleRun = async (useSelected: boolean) => {
    if (useSelected && selectedFlowIds.length === 0) {
      setRunError("Select at least one flow before running selected.");
      return;
    }

    const initialContext = parseInitialContextOrFail();
    if (!initialContext) return;

    if (useSelected) {
      setRunSelectedLoading(true);
    } else {
      setRunLatestLoading(true);
    }
    setRunError(null);

    try {
      const res = await runFlows({
        flow_ids: useSelected ? selectedFlowIds : undefined,
        target_base_url: targetBaseUrl.trim() || undefined,
        initial_context: initialContext,
      });
      const data = unwrap(res.data);
      setRunGroupSummary(data);
      await fetchRuns();
      if (data.flow_runs.length > 0) {
        await loadRunDetail(data.flow_runs[0].id);
      }
      setActiveTab("history");
    } catch (err) {
      setRunError(
        extractErrorMessage(
          err,
          useSelected ? "Failed to run selected flows." : "Failed to run latest flow batch."
        )
      );
      setActiveTab("run");
    } finally {
      setRunSelectedLoading(false);
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

  const tabs: Array<{ id: FlowTab; label: string; icon: typeof Sparkles }> = [
    { id: "generate", label: "Generate", icon: Sparkles },
    { id: "review", label: "Review Flows", icon: Route },
    { id: "run", label: "Run", icon: Play },
    { id: "history", label: "History", icon: History },
  ];

  return (
    <div className="space-y-6">
      <PageHeader
        title="Flow Tests"
        description="Generate multi-step API journeys, review why candidates survived, run selected flows, and inspect step-level traces."
        action={
          <Button
            variant="ghost"
            icon={RefreshCw}
            onClick={() => {
              void fetchFlows();
              void fetchRuns();
            }}
            loading={flowsLoading || runsLoading}
          >
            Refresh
          </Button>
        }
      />

      <div className="sticky top-0 z-30 -mx-4 border-b border-slate-800 bg-slate-950/92 px-4 py-3 backdrop-blur lg:top-0">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex gap-2 overflow-x-auto pb-1">
            {tabs.map((tab) => (
              <button
                key={tab.id}
                type="button"
                onClick={() => setActiveTab(tab.id)}
                className={cn(
                  "inline-flex shrink-0 items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                  activeTab === tab.id
                    ? "bg-emerald-500/12 text-emerald-200 ring-1 ring-emerald-500/25"
                    : "bg-slate-900 text-slate-400 hover:text-slate-100"
                )}
              >
                <tab.icon className="size-4" />
                {tab.label}
              </button>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-2 text-sm text-slate-400">
            <Badge tone={selectedFlowIds.length > 0 ? "info" : "neutral"}>
              {selectedFlowIds.length} selected
            </Badge>
            <Button
              size="sm"
              variant="primary"
              icon={Play}
              disabled={selectedFlowIds.length === 0}
              loading={runSelectedLoading}
              onClick={() => void handleRun(true)}
            >
              Run Selected
            </Button>
          </div>
        </div>
      </div>

      {activeTab === "generate" && (
        <div className="space-y-4">
          <Panel title="Generate Flows" eyebrow="Configuration">
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
              <Field label="Generation mode">
                <Select
                  value={generationMode}
                  onChange={(event) =>
                    setGenerationMode(event.target.value as FlowGenerationMode)
                  }
                >
                  <option value="hybrid_auto">Hybrid auto</option>
                  <option value="llm_first">LLM first</option>
                  <option value="deterministic_first">Deterministic first</option>
                  <option value="pure_llm">Pure LLM</option>
                </Select>
              </Field>
              <Field label="Mutation policy">
                <Select
                  value={mutationPolicy}
                  onChange={(event) =>
                    setMutationPolicy(event.target.value as FlowMutationPolicy)
                  }
                >
                  <option value="safe">Safe</option>
                  <option value="balanced">Balanced</option>
                  <option value="full_lifecycle">Full lifecycle</option>
                </Select>
              </Field>
              <Field label="Max flows">
                <Input
                  type="number"
                  min={1}
                  max={20}
                  value={maxFlows}
                  onChange={(event) => setMaxFlows(Number(event.target.value))}
                />
              </Field>
              <Field label="Max steps">
                <Input
                  type="number"
                  min={2}
                  max={20}
                  value={maxStepsPerFlow}
                  onChange={(event) => setMaxStepsPerFlow(Number(event.target.value))}
                />
              </Field>
            </div>

            <div className="mt-4 grid gap-4 lg:grid-cols-[1fr_220px]">
              <Field
                label="Personas"
                hint="Optional comma-separated names, for example guest_user, admin_user."
              >
                <Input
                  value={personasInput}
                  onChange={(event) => setPersonasInput(event.target.value)}
                  placeholder="guest_user, registered_user"
                />
              </Field>
              <label className="flex items-center gap-3 rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2 text-sm text-slate-300">
                <input
                  type="checkbox"
                  checked={includeNegative}
                  onChange={(event) => setIncludeNegative(event.target.checked)}
                  className="size-4 rounded border-slate-700 bg-slate-950 text-emerald-500 focus:ring-emerald-400/30"
                />
                Include negative steps
              </label>
            </div>

            <details className="mt-4 rounded-lg border border-slate-800 bg-slate-950/55 p-3">
              <summary className="cursor-pointer text-sm font-medium text-slate-300">
                Advanced app context JSON
              </summary>
              <Field
                label="App context"
                error={appContextError}
                className="mt-3"
              >
                <Textarea
                  rows={5}
                  value={appContextInput}
                  onChange={(event) => setAppContextInput(event.target.value)}
                />
              </Field>
            </details>

            <div className="mt-4 flex flex-wrap items-center gap-3">
              <Button
                variant="primary"
                icon={Sparkles}
                loading={generating}
                onClick={() => void handleGenerateFlows()}
              >
                Generate Flows
              </Button>
              <span className="text-sm text-slate-500">
                Accepted flows become runnable artifacts.
              </span>
            </div>

            {generateError && (
              <div className="mt-4">
                <InlineAlert tone="danger" title="Generation failed">
                  {generateError}
                </InlineAlert>
              </div>
            )}
          </Panel>

          {generationSummary && (
            <Panel title="Last Generation Decision" eyebrow="Reviewer gate">
              <GenerationDecision
                summary={generationSummary}
                onSuggestion={applySuggestionChip}
              />
            </Panel>
          )}
        </div>
      )}

      {activeTab === "review" && (
        <div className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
          <Panel
            title="Latest Flow Batch"
            action={
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  variant="ghost"
                  icon={RefreshCw}
                  onClick={() => void fetchFlows()}
                  loading={flowsLoading}
                >
                  Refresh
                </Button>
                <Button
                  size="sm"
                  variant="subtle"
                  disabled={flows.length === 0 || allFlowsSelected}
                  onClick={() => setSelectedFlowIds(flows.map((flow) => flow.id))}
                >
                  Select All
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={selectedFlowIds.length === 0}
                  onClick={() => setSelectedFlowIds([])}
                >
                  Clear
                </Button>
              </div>
            }
          >
            {flowsError && (
              <InlineAlert tone="danger" title="Could not load flows">
                {flowsError}
              </InlineAlert>
            )}

            {flowsLoading ? (
              <EmptyState title="Loading flows…" />
            ) : flows.length === 0 ? (
              <EmptyState
                title="No flows in the latest batch"
                description="Generate flows first, then return here to review the accepted scenarios."
                action={
                  <Button
                    variant="primary"
                    icon={Sparkles}
                    onClick={() => setActiveTab("generate")}
                  >
                    Open Generator
                  </Button>
                }
              />
            ) : (
              <DataTable>
                <thead className={tableHeaderClass}>
                  <tr>
                    <th className={`${tableCellClass} w-12`}>Sel</th>
                    <th className={tableCellClass}>Flow</th>
                    <th className={tableCellClass}>Persona</th>
                    <th className={tableCellClass}>Tags</th>
                    <th className={`${tableCellClass} text-right`}>Steps</th>
                    <th className={tableCellClass}>Created</th>
                  </tr>
                </thead>
                <tbody>
                  {flows.map((flow) => (
                    <tr
                      key={flow.id}
                      onClick={() => void loadFlowDetail(flow.id)}
                      className={cn(
                        "cursor-pointer border-b border-slate-800/70 transition-colors hover:bg-slate-800/45",
                        selectedFlowId === flow.id && "bg-emerald-500/8"
                      )}
                    >
                      <td className={tableCellClass}>
                        <input
                          type="checkbox"
                          checked={selectedFlowSet.has(flow.id)}
                          onClick={(event) => event.stopPropagation()}
                          onChange={(event) => {
                            event.stopPropagation();
                            toggleFlowSelection(flow.id);
                          }}
                          className="size-4 rounded border-slate-700 bg-slate-950 text-emerald-500 focus:ring-emerald-400/30"
                        />
                      </td>
                      <td className={`${tableCellClass} max-w-[280px]`}>
                        <p className="truncate font-medium text-slate-100">{flow.name}</p>
                        <p className="mt-1 font-mono text-xs text-slate-500">
                          {truncateMiddle(flow.id, 18)}
                        </p>
                      </td>
                      <td className={`${tableCellClass} text-slate-300`}>
                        {flow.persona || "—"}
                      </td>
                      <td className={`${tableCellClass} text-slate-400`}>
                        <div className="flex max-w-[260px] flex-wrap gap-1">
                          {flow.tags.length > 0
                            ? flow.tags.slice(0, 3).map((tag) => (
                                <Badge key={tag} tone="neutral">
                                  {tag}
                                </Badge>
                              ))
                            : "—"}
                        </div>
                      </td>
                      <td className={`${tableCellClass} text-right text-slate-300`}>
                        {flow.step_count}
                      </td>
                      <td className={`${tableCellClass} text-slate-400`}>
                        {formatDate(flow.created_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </DataTable>
            )}
          </Panel>

          <Panel title="Selected Flow Detail">
            {flowDetailError && (
              <InlineAlert tone="danger" title="Detail failed">
                {flowDetailError}
              </InlineAlert>
            )}
            {flowDetailLoading ? (
              <EmptyState title="Loading flow detail…" />
            ) : selectedFlow ? (
              <div className="space-y-4">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-base font-semibold text-slate-100">
                      {selectedFlow.name}
                    </h3>
                    <Badge tone="info">{selectedFlow.persona || "no persona"}</Badge>
                  </div>
                  {selectedFlow.description && (
                    <p className="mt-2 text-sm leading-6 text-slate-400">
                      {selectedFlow.description}
                    </p>
                  )}
                </div>
                <StepTimeline steps={selectedFlow.steps} />
                <JsonDisclosure title="Preconditions" value={selectedFlow.preconditions} />
              </div>
            ) : (
              <EmptyState
                title="Select a flow"
                description="Click a row in the latest batch to inspect ordered steps, extracts, and assertions."
              />
            )}
          </Panel>
        </div>
      )}

      {activeTab === "run" && (
        <div className="space-y-4">
          <Panel title="Run Flows" eyebrow="Execution">
            <div className="grid gap-4 lg:grid-cols-[1fr_260px]">
              <Field
                label="Target base URL"
                hint="Required when the spec has no server URL. Booker target: https://restful-booker.herokuapp.com"
              >
                <Input
                  value={targetBaseUrl}
                  onChange={(event) => setTargetBaseUrl(event.target.value)}
                  placeholder="https://restful-booker.herokuapp.com"
                />
                <div className="mt-2">
                  <Button
                    type="button"
                    size="sm"
                    variant="subtle"
                    onClick={() => setTargetBaseUrl("https://restful-booker.herokuapp.com")}
                  >
                    Use Booker target
                  </Button>
                </div>
              </Field>
              <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-3">
                <p className="text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                  Selected Flows
                </p>
                <p className="mt-2 text-2xl font-semibold text-slate-50">
                  {selectedFlowIds.length}
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  Latest batch fallback is available if none are selected.
                </p>
              </div>
            </div>

            <details className="mt-4 rounded-lg border border-slate-800 bg-slate-950/55 p-3">
              <summary className="cursor-pointer text-sm font-medium text-slate-300">
                Advanced initial context JSON
              </summary>
              <Field
                label="Initial context"
                error={initialContextError}
                className="mt-3"
              >
                <Textarea
                  rows={5}
                  value={initialContextInput}
                  onChange={(event) => setInitialContextInput(event.target.value)}
                />
              </Field>
            </details>

            <div className="mt-4 flex flex-wrap gap-3">
              <Button
                variant="primary"
                icon={Play}
                disabled={selectedFlowIds.length === 0}
                loading={runSelectedLoading}
                onClick={() => void handleRun(true)}
              >
                Run Selected
              </Button>
              <Button
                variant="secondary"
                icon={Play}
                loading={runLatestLoading}
                onClick={() => void handleRun(false)}
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
          </Panel>

          {runGroupSummary && (
            <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <MetricCard label="Total flows" value={runGroupSummary.total_flows} />
              <MetricCard label="Passed" value={runGroupSummary.passed} tone="good" />
              <MetricCard label="Failed" value={runGroupSummary.failed} tone="bad" />
              <MetricCard label="Errors" value={runGroupSummary.errors} tone="warn" />
            </section>
          )}
        </div>
      )}

      {activeTab === "history" && (
        <div className="grid gap-4 xl:grid-cols-[0.9fr_1.1fr]">
          <Panel
            title="Flow Run History"
            action={
              <Button
                size="sm"
                variant="ghost"
                icon={RefreshCw}
                onClick={() => void fetchRuns()}
                loading={runsLoading}
              >
                Refresh
              </Button>
            }
          >
            {runsError && (
              <InlineAlert tone="danger" title="Could not load runs">
                {runsError}
              </InlineAlert>
            )}
            {runsLoading ? (
              <EmptyState title="Loading runs…" />
            ) : runs.length === 0 ? (
              <EmptyState
                title="No flow runs yet"
                description="Run selected flows or the latest batch to populate execution history."
                action={
                  <Button variant="primary" icon={Play} onClick={() => setActiveTab("run")}>
                    Open Run Panel
                  </Button>
                }
              />
            ) : (
              <DataTable>
                <thead className={tableHeaderClass}>
                  <tr>
                    <th className={tableCellClass}>Run</th>
                    <th className={tableCellClass}>Flow</th>
                    <th className={tableCellClass}>Status</th>
                    <th className={tableCellClass}>Started</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((run) => (
                    <tr
                      key={run.id}
                      onClick={() => void loadRunDetail(run.id)}
                      className={cn(
                        "cursor-pointer border-b border-slate-800/70 transition-colors hover:bg-slate-800/45",
                        selectedRunId === run.id && "bg-emerald-500/8"
                      )}
                    >
                      <td className={`${tableCellClass} font-mono text-xs text-slate-400`}>
                        {truncateMiddle(run.id, 18)}
                      </td>
                      <td className={`${tableCellClass} text-slate-200`}>
                        {run.flow_name}
                      </td>
                      <td className={tableCellClass}>
                        <StatusBadge status={toStatus(run.status)} />
                      </td>
                      <td className={`${tableCellClass} text-slate-400`}>
                        {formatDate(run.started_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </DataTable>
            )}
          </Panel>

          <Panel title="Run Detail">
            {runDetailError && (
              <InlineAlert tone="danger" title="Detail failed">
                {runDetailError}
              </InlineAlert>
            )}
            {runDetailLoading ? (
              <EmptyState title="Loading run detail…" />
            ) : selectedRun ? (
              <div className="space-y-4">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-base font-semibold text-slate-100">
                    {selectedRun.flow_name}
                  </h3>
                  <StatusBadge status={toStatus(selectedRun.status)} />
                  <Badge tone="neutral" className="font-mono">
                    {truncateMiddle(selectedRun.id, 20)}
                  </Badge>
                </div>

                <div className="grid gap-3 md:grid-cols-2">
                  <div className="rounded-lg border border-slate-800 bg-slate-950/55 p-3">
                    <p className="text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                      Target base URL
                    </p>
                    <p className="mt-2 break-all font-mono text-xs text-slate-300">
                      {selectedRun.target_base_url}
                    </p>
                  </div>
                  <div className="rounded-lg border border-slate-800 bg-slate-950/55 p-3">
                    <p className="text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                      Run Window
                    </p>
                    <p className="mt-2 text-sm text-slate-300">
                      {formatDate(selectedRun.started_at)} →{" "}
                      {formatDate(selectedRun.finished_at)}
                    </p>
                  </div>
                </div>

                <div className="grid gap-2 md:grid-cols-2">
                  <JsonDisclosure title="Initial context" value={selectedRun.initial_context} />
                  <JsonDisclosure title="Final context" value={selectedRun.final_context} />
                </div>

                <div className="space-y-3">
                  <h4 className="text-sm font-semibold text-slate-100">Step Trace</h4>
                  {selectedRun.step_results.length === 0 ? (
                    <EmptyState title="No step results recorded" />
                  ) : (
                    selectedRun.step_results.map((step) => {
                      const method =
                        typeof step.resolved_request.method === "string"
                          ? step.resolved_request.method
                          : "UNKNOWN";
                      const endpoint =
                        typeof step.resolved_request.endpoint === "string"
                          ? step.resolved_request.endpoint
                          : typeof step.resolved_request.url === "string"
                            ? step.resolved_request.url
                            : step.step_id;

                      return (
                        <article
                          key={step.id}
                          className="rounded-lg border border-slate-800 bg-slate-950/55 p-3"
                        >
                          <div className="flex flex-wrap items-center gap-2">
                            <Badge tone="neutral" className="font-mono">
                              #{step.order}
                            </Badge>
                            <StatusBadge status={toStatus(step.status)} />
                            <MethodBadge method={method} />
                            <span className="break-all font-mono text-xs text-slate-400">
                              {endpoint}
                            </span>
                          </div>
                          <div className="mt-3 flex flex-wrap gap-4 text-xs text-slate-400">
                            <span>
                              response:{" "}
                              <span className="font-mono text-slate-200">
                                {step.response_status ?? "—"}
                              </span>
                            </span>
                            <span>
                              assertions:{" "}
                              <span className="font-mono text-slate-200">
                                {step.assertions_passed}/{step.assertions_total}
                              </span>
                            </span>
                            <span>executed: {formatDate(step.executed_at)}</span>
                          </div>
                          {step.error_message && (
                            <div className="mt-3">
                              <InlineAlert tone="danger">{step.error_message}</InlineAlert>
                            </div>
                          )}
                          <div className="mt-3 grid gap-2">
                            <JsonDisclosure title="Resolved request" value={step.resolved_request} />
                            <JsonDisclosure title="Response body" value={step.response_body} />
                            <JsonDisclosure
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
            ) : (
              <EmptyState
                title="Select a run"
                description="Click a history row to inspect request resolution, response bodies, assertions, and extracted context."
              />
            )}
          </Panel>
        </div>
      )}
    </div>
  );
}

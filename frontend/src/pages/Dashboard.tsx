import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  CheckCircle2,
  FileSearch,
  Gauge,
  Play,
  Route,
  Sparkles,
  XCircle,
} from "lucide-react";
import { Cell, Pie, PieChart, ResponsiveContainer } from "recharts";
import {
  executeTests,
  generateTests,
  getDashboard,
  parseSpec,
  type DashboardSummary,
  type TestRun,
} from "../api/client";
import {
  Button,
  DataTable,
  EmptyState,
  Field,
  InlineAlert,
  Input,
  MetricCard,
  PageHeader,
  Panel,
  tableCellClass,
  tableHeaderClass,
} from "../components/ui";
import {
  extractErrorMessage,
  formatDate,
  formatMs,
  truncateMiddle,
} from "../lib/ui";

const emptySummary: DashboardSummary = {
  total_tests: 0,
  passed: 0,
  failed: 0,
  errors: 0,
  pass_rate: 0,
  avg_response_time_ms: 0,
  functional_summary: {},
  suite_summary: {},
  load_summary: {},
  recent_runs: [],
};

export default function Dashboard() {
  const [specUrl, setSpecUrl] = useState("");
  const [targetBaseUrl, setTargetBaseUrl] = useState("");
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [parseLoading, setParseLoading] = useState(false);
  const [generateLoading, setGenerateLoading] = useState(false);
  const [executeLoading, setExecuteLoading] = useState(false);

  const fetchDashboard = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await getDashboard();
      const data = (res.data as { data?: DashboardSummary }).data ?? res.data;
      setSummary(data as DashboardSummary);
    } catch (err) {
      setError(extractErrorMessage(err, "Failed to load dashboard."));
      setSummary(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchDashboard();
  }, [fetchDashboard]);

  const handleParse = async () => {
    setParseLoading(true);
    setError(null);
    try {
      const trimmed = specUrl.trim();
      const isUrl = trimmed.startsWith("http://") || trimmed.startsWith("https://");
      await parseSpec(
        trimmed ? (isUrl ? { spec_url: trimmed } : { spec_path: trimmed }) : {}
      );
      await fetchDashboard();
    } catch (err) {
      setError(extractErrorMessage(err, "Parse failed."));
    } finally {
      setParseLoading(false);
    }
  };

  const handleGenerate = async () => {
    setGenerateLoading(true);
    setError(null);
    try {
      await generateTests();
      await fetchDashboard();
    } catch (err) {
      setError(extractErrorMessage(err, "Generate failed."));
    } finally {
      setGenerateLoading(false);
    }
  };

  const handleExecute = async () => {
    setExecuteLoading(true);
    setError(null);
    try {
      await executeTests({ target_base_url: targetBaseUrl.trim() || undefined });
      await fetchDashboard();
    } catch (err) {
      setError(extractErrorMessage(err, "Execute failed."));
    } finally {
      setExecuteLoading(false);
    }
  };

  const s = summary ?? emptySummary;
  const recentRuns = s.recent_runs ?? [];
  const failedTotal = s.failed + s.errors;
  const passRateData = useMemo(() => {
    const data = [
      { name: "passed", value: s.passed, color: "#34d399" },
      { name: "failed", value: failedTotal, color: "#f87171" },
    ].filter((item) => item.value > 0);
    return data.length > 0 ? data : [{ name: "empty", value: 1, color: "#334155" }];
  }, [failedTotal, s.passed]);

  const workflowSteps = [
    {
      label: "Parse",
      detail: "Load OpenAPI",
      icon: FileSearch,
      tone: "info" as const,
    },
    {
      label: "Generate",
      detail: "Build tests",
      icon: Sparkles,
      tone: "violet" as const,
    },
    {
      label: "Run",
      detail: "Execute checks",
      icon: Play,
      tone: "good" as const,
    },
    {
      label: "Review",
      detail: "Inspect traces",
      icon: Activity,
      tone: "warn" as const,
    },
  ];

  if (loading && !summary) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <div className="text-sm text-slate-500">Loading dashboard…</div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Demo Command Center"
        description="Run the complete path from spec ingestion to generated tests, execution, and result review."
        action={
          <Button
            variant="ghost"
            icon={Activity}
            onClick={() => void fetchDashboard()}
            loading={loading}
          >
            Refresh
          </Button>
        }
      />

      {error && (
        <InlineAlert tone="danger" title="Action failed">
          {error}
        </InlineAlert>
      )}

      <Panel>
        <div className="grid gap-3 md:grid-cols-4">
          {workflowSteps.map((step, index) => (
            <div
              key={step.label}
              className="relative rounded-lg border border-slate-800 bg-slate-950/60 p-4"
            >
              <div className="flex items-center gap-3">
                <span className="flex size-9 items-center justify-center rounded-lg border border-slate-700 bg-slate-900 text-emerald-300">
                  <step.icon className="size-4" />
                </span>
                <div>
                  <p className="text-sm font-semibold text-slate-100">
                    {index + 1}. {step.label}
                  </p>
                  <p className="text-xs text-slate-500">{step.detail}</p>
                </div>
              </div>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="Live Demo Controls" eyebrow="Workflow">
        <div className="grid gap-4 lg:grid-cols-[1.3fr_0.9fr]">
          <div className="space-y-3 rounded-lg border border-slate-800 bg-slate-950/50 p-4">
            <Field
              label="OpenAPI spec URL or path"
              hint="Use the Booker demo spec or a local JSON/YAML path."
            >
              <Input
                value={specUrl}
                onChange={(event) => setSpecUrl(event.target.value)}
                placeholder="https://www.davidmello.com/specs/restful-booker.swagger.json"
              />
            </Field>
            <div className="flex flex-wrap gap-2">
              <Button
                variant="secondary"
                icon={FileSearch}
                onClick={() => void handleParse()}
                loading={parseLoading}
              >
                Parse Spec
              </Button>
              <Button
                variant="subtle"
                onClick={() => {
                  setSpecUrl("https://www.davidmello.com/specs/restful-booker.swagger.json");
                  setTargetBaseUrl("https://restful-booker.herokuapp.com");
                }}
              >
                Use Booker Demo
              </Button>
            </div>
          </div>

          <div className="space-y-3 rounded-lg border border-slate-800 bg-slate-950/50 p-4">
            <Field
              label="Target base URL"
              hint="Required when the spec has no server URL. Booker target: https://restful-booker.herokuapp.com"
            >
              <Input
                value={targetBaseUrl}
                onChange={(event) => setTargetBaseUrl(event.target.value)}
                placeholder="https://restful-booker.herokuapp.com"
              />
            </Field>
            <div className="flex flex-wrap gap-2">
              <Button
                variant="secondary"
                icon={Sparkles}
                onClick={() => void handleGenerate()}
                loading={generateLoading}
              >
                Generate Tests
              </Button>
              <Button
                variant="primary"
                icon={Play}
                onClick={() => void handleExecute()}
                loading={executeLoading}
              >
                Execute All
              </Button>
            </div>
          </div>
        </div>
      </Panel>

      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
        <MetricCard label="Total Tests" value={s.total_tests} icon={Gauge} />
        <MetricCard label="Passed" value={s.passed} tone="good" icon={CheckCircle2} />
        <MetricCard label="Failed" value={failedTotal} tone="bad" icon={XCircle} />
        <MetricCard label="Avg Response" value={formatMs(s.avg_response_time_ms)} />
        <div className="rounded-lg border border-slate-800 bg-slate-900/72 p-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-xs font-medium uppercase tracking-[0.16em] text-slate-500">
                Pass Rate
              </p>
              <p className="mt-3 text-2xl font-semibold tabular-nums text-slate-50">
                {s.pass_rate.toFixed(1)}%
              </p>
            </div>
            <div className="size-16">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={passRateData}
                    dataKey="value"
                    innerRadius={19}
                    outerRadius={30}
                    cx="50%"
                    cy="50%"
                  >
                    {passRateData.map((entry) => (
                      <Cell key={entry.name} fill={entry.color} />
                    ))}
                  </Pie>
                </PieChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      </section>

      <section className="grid gap-4 lg:grid-cols-3">
        <Panel title="Review Generated Assets">
          <div className="grid gap-3">
            <Link
              to="/flows"
              className="rounded-lg border border-slate-800 bg-slate-950/60 p-4 transition-colors hover:border-emerald-500/35 hover:bg-slate-900"
            >
              <div className="flex items-center gap-3">
                <Route className="size-5 text-emerald-300" />
                <div>
                  <p className="font-medium text-slate-100">Flow Tests</p>
                  <p className="text-sm text-slate-500">
                    Generate multi-step journeys and inspect step traces.
                  </p>
                </div>
              </div>
            </Link>
            <Link
              to="/suites"
              className="rounded-lg border border-slate-800 bg-slate-950/60 p-4 transition-colors hover:border-sky-500/35 hover:bg-slate-900"
            >
              <div className="flex items-center gap-3">
                <CheckCircle2 className="size-5 text-sky-300" />
                <div>
                  <p className="font-medium text-slate-100">Test Suites</p>
                  <p className="text-sm text-slate-500">
                    Run grouped functional and WebSocket tests.
                  </p>
                </div>
              </div>
            </Link>
            <Link
              to="/load-tests"
              className="rounded-lg border border-slate-800 bg-slate-950/60 p-4 transition-colors hover:border-amber-500/35 hover:bg-slate-900"
            >
              <div className="flex items-center gap-3">
                <Gauge className="size-5 text-amber-300" />
                <div>
                  <p className="font-medium text-slate-100">Load Tests</p>
                  <p className="text-sm text-slate-500">
                    Create scenarios and review k6 diagnostics.
                  </p>
                </div>
              </div>
            </Link>
          </div>
        </Panel>

        <Panel title="Functional Summary">
          <div className="grid gap-3">
            <MetricCard
              label="Individual"
              value={s.functional_summary.passed ?? 0}
              tone="good"
              detail={`${s.functional_summary.failed ?? 0} failed`}
            />
            <MetricCard
              label="Suites"
              value={s.suite_summary.passed ?? 0}
              tone="info"
              detail={`${s.suite_summary.failed ?? 0} failed`}
            />
            <MetricCard
              label="Load"
              value={s.load_summary.passed ?? 0}
              tone="warn"
              detail={`${s.load_summary.failed ?? 0} failed`}
            />
          </div>
        </Panel>

        <Panel title="Recent Runs">
          {recentRuns.length === 0 ? (
            <EmptyState
              title="No runs yet"
              description="Parse a spec, generate tests, then execute them to populate run history."
            />
          ) : (
            <DataTable>
              <thead className={tableHeaderClass}>
                <tr>
                  <th className={tableCellClass}>Run</th>
                  <th className={tableCellClass}>Total</th>
                  <th className={tableCellClass}>Passed</th>
                  <th className={tableCellClass}>Date</th>
                </tr>
              </thead>
              <tbody>
                {recentRuns.slice(0, 6).map((run: TestRun) => (
                  <tr key={run.id} className="border-b border-slate-800/70">
                    <td className={`${tableCellClass} font-mono text-xs text-slate-300`}>
                      {truncateMiddle(run.id, 14)}
                    </td>
                    <td className={`${tableCellClass} tabular-nums text-slate-300`}>
                      {run.total_tests}
                    </td>
                    <td className={`${tableCellClass} tabular-nums text-emerald-300`}>
                      {run.passed}
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
      </section>
    </div>
  );
}

import { useEffect, useState } from 'react';
import { PieChart, Pie, Cell, ResponsiveContainer } from 'recharts';
import {
  getDashboard,
  parseSpec,
  generateTests,
  executeTests,
  type DashboardSummary,
  type TestRun,
} from '../api/client';

const truncateId = (id: string, len = 8) =>
  id.length > len ? `${id.slice(0, len)}…` : id;

const formatDate = (dateStr: string) => {
  const d = new Date(dateStr);
  return d.toLocaleString();
};

const formatMs = (ms: number) => `${Math.round(ms)} ms`;

export default function Dashboard() {
  const [specUrl, setSpecUrl] = useState('');
  const [targetBaseUrl, setTargetBaseUrl] = useState('');
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [parseLoading, setParseLoading] = useState(false);
  const [generateLoading, setGenerateLoading] = useState(false);
  const [executeLoading, setExecuteLoading] = useState(false);

  const fetchDashboard = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await getDashboard();
      const data = (res.data as { data?: DashboardSummary }).data ?? res.data;
      setSummary(data as DashboardSummary);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load dashboard');
      setSummary(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchDashboard();
  }, []);

  const handleParse = async () => {
    setParseLoading(true);
    try {
      const isUrl = specUrl.startsWith('http://') || specUrl.startsWith('https://');
      await parseSpec(
        specUrl
          ? isUrl
            ? { spec_url: specUrl }
            : { spec_path: specUrl }
          : {}
      );
      await fetchDashboard();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Parse failed');
    } finally {
      setParseLoading(false);
    }
  };

  const handleGenerate = async () => {
    setGenerateLoading(true);
    try {
      await generateTests();
      await fetchDashboard();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Generate failed');
    } finally {
      setGenerateLoading(false);
    }
  };

  const handleExecute = async () => {
    setExecuteLoading(true);
    try {
      await executeTests({ target_base_url: targetBaseUrl || undefined });
      await fetchDashboard();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Execute failed');
    } finally {
      setExecuteLoading(false);
    }
  };

  if (loading && !summary) {
    return (
      <div className="min-h-screen bg-zinc-950 text-zinc-100 flex items-center justify-center">
        <div className="animate-pulse text-zinc-400">Loading dashboard…</div>
      </div>
    );
  }

  const s = summary ?? {
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

  const passRateData = [
    { name: 'passed', value: s.passed, color: '#22c55e' },
    { name: 'failed', value: s.failed + s.errors, color: '#ef4444' },
  ].filter((d) => d.value > 0);

  if (passRateData.length === 0) {
    passRateData.push({ name: 'empty', value: 1, color: '#3f3f46' });
  }

  const functionalPassed =
    s.functional_summary?.passed ?? s.functional_summary?.['passed'] ?? 0;
  const functionalFailed =
    s.functional_summary?.failed ?? s.functional_summary?.['failed'] ?? 0;
  const suitePassed =
    s.suite_summary?.passed ?? s.suite_summary?.['passed'] ?? 0;
  const suiteFailed =
    s.suite_summary?.failed ?? s.suite_summary?.['failed'] ?? 0;

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100 p-6">
      <div className="max-w-7xl mx-auto space-y-6">
        <h1 className="text-2xl font-semibold text-zinc-50">Agentic API Testing</h1>

        {/* Action Panel */}
        <section className="rounded-xl bg-zinc-900/80 border border-zinc-800 p-4">
          <div className="flex flex-wrap items-end gap-4">
            <div className="flex-1 min-w-[200px]">
              <label className="block text-sm text-zinc-400 mb-1">
                OpenAPI Spec URL or Path
              </label>
              <input
                type="text"
                value={specUrl}
                onChange={(e) => setSpecUrl(e.target.value)}
                placeholder="https://example.com/openapi.json or /path/to/spec.yaml"
                className="w-full rounded-lg bg-zinc-800 border border-zinc-700 px-3 py-2 text-zinc-100 placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/50 focus:border-emerald-500"
              />
            </div>
            <button
              onClick={handleParse}
              disabled={parseLoading}
              className="rounded-lg bg-slate-700 hover:bg-slate-600 px-4 py-2 text-sm font-medium text-zinc-100 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
            >
              {parseLoading ? (
                <>
                  <span className="size-4 animate-spin rounded-full border-2 border-zinc-400 border-t-transparent" />
                  Parse
                </>
              ) : (
                'Parse'
              )}
            </button>

            <button
              onClick={handleGenerate}
              disabled={generateLoading}
              className="rounded-lg bg-slate-700 hover:bg-slate-600 px-4 py-2 text-sm font-medium text-zinc-100 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
            >
              {generateLoading ? (
                <>
                  <span className="size-4 animate-spin rounded-full border-2 border-zinc-400 border-t-transparent" />
                  Generate Tests
                </>
              ) : (
                'Generate Tests'
              )}
            </button>

            <div className="flex items-end gap-2">
              <div>
                <label className="block text-sm text-zinc-400 mb-1">
                  Target Base URL
                </label>
                <input
                  type="text"
                  value={targetBaseUrl}
                  onChange={(e) => setTargetBaseUrl(e.target.value)}
                  placeholder="https://api.example.com"
                  className="w-48 rounded-lg bg-zinc-800 border border-zinc-700 px-3 py-2 text-zinc-100 placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/50 focus:border-emerald-500"
                />
              </div>
              <button
                onClick={handleExecute}
                disabled={executeLoading}
                className="rounded-lg bg-emerald-700 hover:bg-emerald-600 px-4 py-2 text-sm font-medium text-zinc-100 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
              >
                {executeLoading ? (
                  <>
                    <span className="size-4 animate-spin rounded-full border-2 border-zinc-400 border-t-transparent" />
                    Execute All Tests
                  </>
                ) : (
                  'Execute All Tests'
                )}
              </button>
            </div>
          </div>
        </section>

        {error && (
          <div className="rounded-lg bg-red-950/50 border border-red-800 text-red-300 px-4 py-2">
            {error}
          </div>
        )}

        {/* Stats Cards Row */}
        <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <div className="rounded-xl bg-zinc-900/80 border border-zinc-800 p-5">
            <div className="text-sm text-zinc-400 mb-1">Total Tests</div>
            <div className="text-2xl font-semibold text-zinc-50 tabular-nums">
              {s.total_tests}
            </div>
          </div>
          <div className="rounded-xl bg-zinc-900/80 border border-zinc-800 p-5">
            <div className="text-sm text-zinc-400 mb-1">Passed</div>
            <div className="text-2xl font-semibold text-emerald-400 tabular-nums">
              {s.passed}
            </div>
          </div>
          <div className="rounded-xl bg-zinc-900/80 border border-zinc-800 p-5">
            <div className="text-sm text-zinc-400 mb-1">Failed</div>
            <div className="text-2xl font-semibold text-red-400 tabular-nums">
              {s.failed + s.errors}
            </div>
          </div>
          <div className="rounded-xl bg-zinc-900/80 border border-zinc-800 p-5 flex items-center gap-4">
            <div className="flex-1">
              <div className="text-sm text-zinc-400 mb-1">Pass Rate</div>
              <div className="text-2xl font-semibold text-zinc-50 tabular-nums">
                {s.pass_rate.toFixed(1)}%
              </div>
            </div>
            <div className="size-16 shrink-0">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={passRateData}
                    cx="50%"
                    cy="50%"
                    innerRadius={18}
                    outerRadius={28}
                    paddingAngle={0}
                    dataKey="value"
                  >
                    {passRateData.map((entry, i) => (
                      <Cell key={i} fill={entry.color} />
                    ))}
                  </Pie>
                </PieChart>
              </ResponsiveContainer>
            </div>
          </div>
        </section>

        {/* Summary Row */}
        <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="rounded-xl bg-zinc-900/80 border border-zinc-800 p-5">
            <h2 className="text-sm font-medium text-zinc-400 mb-3">
              Functional Tests
            </h2>
            <div className="flex gap-4">
              <div>
                <span className="text-emerald-400 font-medium">
                  {functionalPassed}
                </span>
                <span className="text-zinc-500 ml-1">passed</span>
              </div>
              <div>
                <span className="text-red-400 font-medium">
                  {functionalFailed}
                </span>
                <span className="text-zinc-500 ml-1">failed</span>
              </div>
            </div>
          </div>
          <div className="rounded-xl bg-zinc-900/80 border border-zinc-800 p-5">
            <h2 className="text-sm font-medium text-zinc-400 mb-3">
              Suite Tests
            </h2>
            <div className="flex gap-4">
              <div>
                <span className="text-emerald-400 font-medium">{suitePassed}</span>
                <span className="text-zinc-500 ml-1">passed</span>
              </div>
              <div>
                <span className="text-red-400 font-medium">
                  {suiteFailed}
                </span>
                <span className="text-zinc-500 ml-1">failed</span>
              </div>
            </div>
          </div>
        </section>

        {/* Average Response Time */}
        <section>
          <div className="rounded-xl bg-zinc-900/80 border border-zinc-800 p-5">
            <div className="text-sm text-zinc-400 mb-1">Average Response Time</div>
            <div className="text-2xl font-semibold text-zinc-50 tabular-nums">
              {formatMs(s.avg_response_time_ms)}
            </div>
          </div>
        </section>

        {/* Recent Runs Table */}
        <section className="rounded-xl bg-zinc-900/80 border border-zinc-800 overflow-hidden">
          <h2 className="text-sm font-medium text-zinc-400 px-5 pt-5 pb-2">
            Recent Runs
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-800">
                  <th className="text-left py-3 px-5 text-zinc-400 font-medium">
                    Run ID
                  </th>
                  <th className="text-right py-3 px-5 text-zinc-400 font-medium">
                    Total
                  </th>
                  <th className="text-right py-3 px-5 text-zinc-400 font-medium">
                    Passed
                  </th>
                  <th className="text-right py-3 px-5 text-zinc-400 font-medium">
                    Failed
                  </th>
                  <th className="text-right py-3 px-5 text-zinc-400 font-medium">
                    Errors
                  </th>
                  <th className="text-right py-3 px-5 text-zinc-400 font-medium">
                    Avg Response Time
                  </th>
                  <th className="text-left py-3 px-5 text-zinc-400 font-medium">
                    Date
                  </th>
                </tr>
              </thead>
              <tbody>
                {(s.recent_runs ?? []).length === 0 ? (
                  <tr>
                    <td
                      colSpan={7}
                      className="py-8 px-5 text-center text-zinc-500"
                    >
                      No recent runs
                    </td>
                  </tr>
                ) : (
                  (s.recent_runs ?? []).map((run: TestRun, i: number) => (
                    <tr
                      key={run.id}
                      className={
                        i % 2 === 0
                          ? 'bg-zinc-900/50'
                          : 'bg-zinc-800/30'
                      }
                    >
                      <td className="py-3 px-5 font-mono text-zinc-300">
                        {truncateId(run.id)}
                      </td>
                      <td className="py-3 px-5 text-right tabular-nums text-zinc-300">
                        {run.total_tests}
                      </td>
                      <td className="py-3 px-5 text-right tabular-nums text-emerald-400">
                        {run.passed}
                      </td>
                      <td className="py-3 px-5 text-right tabular-nums text-red-400">
                        {run.failed}
                      </td>
                      <td className="py-3 px-5 text-right tabular-nums text-amber-400">
                        {run.errors}
                      </td>
                      <td className="py-3 px-5 text-right tabular-nums text-zinc-300">
                        {formatMs(run.avg_response_time_ms)}
                      </td>
                      <td className="py-3 px-5 text-zinc-400">
                        {formatDate(run.started_at)}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </div>
  );
}

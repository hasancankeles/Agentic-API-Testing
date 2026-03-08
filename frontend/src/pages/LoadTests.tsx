import { useEffect, useState, useCallback } from "react";
import {
  getLoadTestScenarios,
  getLoadTestResults,
  runLoadTests,
  type LoadTestScenario,
  type LoadTestResult,
} from "../api/client";
import MetricsChart, { type PercentileDataPoint } from "../components/MetricsChart";

function unwrap<T>(res: { data?: T } | T): T {
  const d = res as { data?: T };
  return (d.data !== undefined ? d.data : res) as T;
}

const formatDate = (dateStr: string) =>
  new Date(dateStr).toLocaleString();

function PercentileBar({
  label,
  value,
  max,
}: {
  label: string;
  value: number;
  max: number;
}) {
  const pct = max > 0 ? (value / max) * 100 : 0;
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs">
        <span className="text-zinc-400">{label}</span>
        <span className="font-mono text-zinc-300">{value}ms</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-zinc-800">
        <div
          className="h-full rounded-full bg-emerald-500/80"
          style={{ width: `${Math.min(pct, 100)}%` }}
        />
      </div>
    </div>
  );
}

export default function LoadTests() {
  const [scenarios, setScenarios] = useState<LoadTestScenario[]>([]);
  const [results, setResults] = useState<LoadTestResult[]>([]);
  const [loading, setLoading] = useState(true);
  const [runLoading, setRunLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [scenariosRes, resultsRes] = await Promise.all([
        getLoadTestScenarios(),
        getLoadTestResults(),
      ]);
      const scenariosData = unwrap(scenariosRes.data);
      const resultsData = unwrap(resultsRes.data);
      setScenarios(Array.isArray(scenariosData) ? scenariosData : []);
      setResults(Array.isArray(resultsData) ? resultsData : []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load data");
      setScenarios([]);
      setResults([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const handleRun = async (scenarioId: string) => {
    setRunLoading(true);
    setError(null);
    try {
      await runLoadTests({ scenario_ids: [scenarioId] });
      await fetchData();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to run load test");
    } finally {
      setRunLoading(false);
    }
  };

  const handleRunAll = async () => {
    setRunLoading(true);
    setError(null);
    try {
      await runLoadTests({
        scenario_ids: scenarios.map((s) => s.id),
      });
      await fetchData();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to run load tests");
    } finally {
      setRunLoading(false);
    }
  };

  const chartData: PercentileDataPoint[] = results.map((r) => ({
    name: r.scenario_name,
    p50: r.p50_ms,
    p90: r.p90_ms,
    p95: r.p95_ms,
    p99: r.p99_ms,
  }));

  return (
    <div className="min-h-screen bg-slate-950 text-zinc-100 p-6">
      <div className="mx-auto max-w-7xl space-y-6">
        <h1 className="text-2xl font-semibold text-zinc-50">Load Tests</h1>

        {error && (
          <div className="rounded-lg border border-red-800 bg-red-950/50 px-4 py-2 text-red-300">
            {error}
          </div>
        )}

        {loading ? (
          <div className="flex items-center justify-center py-16">
            <span className="animate-pulse text-zinc-400">Loading…</span>
          </div>
        ) : (
          <>
            {/* Scenarios section */}
            <section className="rounded-xl border border-zinc-800 bg-zinc-900/80 p-4">
              <h2 className="mb-4 text-sm font-medium text-zinc-400">
                Load Test Scenarios
              </h2>
              <div className="flex flex-wrap items-center gap-3">
                <button
                  onClick={handleRunAll}
                  disabled={runLoading || scenarios.length === 0}
                  className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-medium text-zinc-100 hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {runLoading ? (
                    <span className="inline-flex items-center gap-2">
                      <span className="size-4 animate-spin rounded-full border-2 border-zinc-400 border-t-transparent" />
                      Running…
                    </span>
                  ) : (
                    "Run All"
                  )}
                </button>
                {scenarios.map((s) => (
                  <div
                    key={s.id}
                    className="flex items-center gap-2 rounded-lg border border-zinc-700/50 bg-zinc-800/50 px-3 py-2"
                  >
                    <span className="text-sm text-zinc-200">{s.name}</span>
                    <button
                      onClick={() => handleRun(s.id)}
                      disabled={runLoading}
                      className="rounded bg-slate-600 px-2 py-1 text-xs font-medium text-zinc-100 hover:bg-slate-500 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Run
                    </button>
                  </div>
                ))}
                {scenarios.length === 0 && (
                  <span className="text-sm text-zinc-500">
                    No scenarios available
                  </span>
                )}
              </div>
            </section>

            {/* Results section */}
            <section className="space-y-4">
              <h2 className="text-sm font-medium text-zinc-400">
                Load Test Results
              </h2>
              <div className="grid gap-4 sm:grid-cols-1 lg:grid-cols-2">
                {results.map((r) => {
                  const maxP = Math.max(
                    r.p50_ms,
                    r.p90_ms,
                    r.p95_ms,
                    r.p99_ms,
                    1
                  );
                  return (
                    <article
                      key={r.id}
                      className="rounded-xl border border-zinc-800 bg-zinc-900/80 p-4"
                    >
                      <div className="mb-4 flex items-start justify-between">
                        <div>
                          <h3 className="font-medium text-zinc-100">
                            {r.scenario_name}
                          </h3>
                          <p className="text-xs text-zinc-500">
                            {formatDate(r.executed_at)}
                          </p>
                        </div>
                      </div>

                      <div className="mb-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
                        <div>
                          <span className="text-zinc-500">Total Requests</span>
                          <p className="font-mono text-zinc-200">
                            {r.total_requests}
                          </p>
                        </div>
                        <div>
                          <span className="text-zinc-500">Failed Requests</span>
                          <p className="font-mono text-red-400">
                            {r.failed_requests}
                          </p>
                        </div>
                        <div>
                          <span className="text-zinc-500">Avg Response</span>
                          <p className="font-mono text-zinc-200">
                            {r.avg_response_time_ms.toFixed(0)}ms
                          </p>
                        </div>
                        <div>
                          <span className="text-zinc-500">RPS</span>
                          <p className="font-mono text-zinc-200">
                            {r.requests_per_second.toFixed(1)}
                          </p>
                        </div>
                        <div>
                          <span className="text-zinc-500">Error Rate</span>
                          <p className="font-mono text-zinc-200">
                            {(r.error_rate * 100).toFixed(2)}%
                          </p>
                        </div>
                      </div>

                      <div className="mb-4 space-y-3">
                        <span className="text-xs font-medium text-zinc-400">
                          Response Time Percentiles
                        </span>
                        <div className="space-y-2">
                          <PercentileBar
                            label="p50"
                            value={r.p50_ms}
                            max={maxP}
                          />
                          <PercentileBar
                            label="p90"
                            value={r.p90_ms}
                            max={maxP}
                          />
                          <PercentileBar
                            label="p95"
                            value={r.p95_ms}
                            max={maxP}
                          />
                          <PercentileBar
                            label="p99"
                            value={r.p99_ms}
                            max={maxP}
                          />
                        </div>
                      </div>

                      <div className="flex flex-wrap gap-4 border-t border-zinc-800 pt-3 text-xs">
                        <span className="text-zinc-500">
                          Min:{" "}
                          <span className="font-mono text-zinc-300">
                            {r.min_response_time_ms}ms
                          </span>
                        </span>
                        <span className="text-zinc-500">
                          Max:{" "}
                          <span className="font-mono text-zinc-300">
                            {r.max_response_time_ms}ms
                          </span>
                        </span>
                        <span className="text-zinc-500">
                          Data sent:{" "}
                          <span className="font-mono text-zinc-300">
                            {r.data_sent_kb.toFixed(2)} KB
                          </span>
                        </span>
                        <span className="text-zinc-500">
                          Data received:{" "}
                          <span className="font-mono text-zinc-300">
                            {r.data_received_kb.toFixed(2)} KB
                          </span>
                        </span>
                      </div>
                    </article>
                  );
                })}
              </div>

              {results.length === 0 && (
                <p className="py-8 text-center text-zinc-500">
                  No load test results yet. Run a scenario to see results.
                </p>
              )}

              {/* Metrics chart - compare multiple results */}
              {results.length > 1 && chartData.length > 0 && (
                <div className="mt-6">
                  <MetricsChart
                    data={chartData}
                    title="Response Time Percentiles Comparison"
                  />
                </div>
              )}
            </section>
          </>
        )}
      </div>
    </div>
  );
}

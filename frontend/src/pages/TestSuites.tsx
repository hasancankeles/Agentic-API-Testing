import { useEffect, useState, useCallback } from "react";
import {
  getSuites,
  getSuite,
  getSuiteResults,
  executeTests,
  type Suite,
  type TestResult,
} from "../api/client";
import TestCard from "../components/TestCard";
import type { TestStatus } from "../components/StatusBadge";

function unwrap<T>(res: { data?: T } | T): T {
  const d = res as { data?: T };
  return (d.data !== undefined ? d.data : res) as T;
}

const methodStyles: Record<string, string> = {
  GET: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  POST: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  PUT: "bg-amber-500/20 text-amber-400 border-amber-500/30",
  DELETE: "bg-red-500/20 text-red-400 border-red-500/30",
  PATCH: "bg-violet-500/20 text-violet-400 border-violet-500/30",
};

function MethodBadge({ method }: { method: string }) {
  const style =
    methodStyles[method.toUpperCase()] ??
    "bg-zinc-500/20 text-zinc-400 border-zinc-500/30";
  return (
    <span
      className={`inline-flex items-center rounded border px-2 py-0.5 text-xs font-mono font-medium ${style}`}
    >
      {method}
    </span>
  );
}

export default function TestSuites() {
  const [suites, setSuites] = useState<Suite[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [expandedSuite, setExpandedSuite] = useState<Suite | null>(null);
  const [expandedLoading, setExpandedLoading] = useState(false);
  const [suiteResults, setSuiteResults] = useState<Record<string, TestResult[]>>(
    {}
  );
  const [runLoading, setRunLoading] = useState<string | null>(null);

  const fetchSuites = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await getSuites();
      const data = unwrap(res.data);
      setSuites(Array.isArray(data) ? data : []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load suites");
      setSuites([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSuites();
  }, [fetchSuites]);

  const fetchExpandedSuite = useCallback(
    async (id: string) => {
      setExpandedLoading(true);
      setExpandedSuite(null);
      try {
        const [suiteRes, resultsRes] = await Promise.all([
          getSuite(id),
          getSuiteResults(id),
        ]);
        const suiteData = unwrap(suiteRes.data);
        const resultsData = unwrap(resultsRes.data);
        setExpandedSuite(suiteData as Suite);
        setSuiteResults((prev) => ({
          ...prev,
          [id]: Array.isArray(resultsData) ? resultsData : [],
        }));
      } catch {
        setExpandedSuite(null);
        setSuiteResults((prev) => ({ ...prev, [id]: [] }));
      } finally {
        setExpandedLoading(false);
      }
    },
    []
  );

  const toggleExpand = (id: string) => {
    if (expandedId === id) {
      setExpandedId(null);
      setExpandedSuite(null);
    } else {
      setExpandedId(id);
      fetchExpandedSuite(id);
    }
  };

  const handleRunSuite = async (suiteId: string) => {
    setRunLoading(suiteId);
    setError(null);
    try {
      await executeTests({ suite_ids: [suiteId] });
      await fetchSuites();
      if (expandedId === suiteId) {
        await fetchExpandedSuite(suiteId);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to run suite");
    } finally {
      setRunLoading(null);
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 text-zinc-100 p-6">
      <div className="mx-auto max-w-7xl space-y-6">
        <h1 className="text-2xl font-semibold text-zinc-50">Test Suites</h1>

        {error && (
          <div className="rounded-lg border border-red-800 bg-red-950/50 px-4 py-2 text-red-300">
            {error}
          </div>
        )}

        {loading ? (
          <div className="flex items-center justify-center py-16">
            <span className="animate-pulse text-zinc-400">Loading suites…</span>
          </div>
        ) : (
          <div className="space-y-4">
            {suites.length === 0 ? (
              <p className="py-8 text-center text-zinc-500">No test suites found</p>
            ) : (
              suites.map((suite) => (
                <article
                  key={suite.id}
                  className="overflow-hidden rounded-xl border border-zinc-800 bg-zinc-900/80"
                >
                  <button
                    type="button"
                    onClick={() => toggleExpand(suite.id)}
                    className="flex w-full items-center justify-between p-4 text-left transition-colors hover:bg-zinc-800/50"
                  >
                    <div className="flex flex-wrap items-center gap-3">
                      <h2 className="font-medium text-zinc-100">{suite.name}</h2>
                      <span className="rounded border border-zinc-600 bg-zinc-800/50 px-2 py-0.5 text-xs text-zinc-400">
                        {suite.category}
                      </span>
                      <span className="text-sm text-zinc-500">
                        {suite.test_count} tests
                      </span>
                      <span className="text-sm text-emerald-400">
                        {suite.passed} passed
                      </span>
                      <span className="text-sm text-red-400">
                        {suite.failed} failed
                      </span>
                      <span className="text-sm text-amber-400">
                        {suite.errors} errors
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          handleRunSuite(suite.id);
                        }}
                        disabled={runLoading === suite.id}
                        className="rounded-lg bg-emerald-700 px-3 py-1.5 text-sm font-medium text-zinc-100 hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {runLoading === suite.id ? (
                          <span className="inline-flex items-center gap-1">
                            <span className="size-3 animate-spin rounded-full border-2 border-zinc-400 border-t-transparent" />
                            Running
                          </span>
                        ) : (
                          "Run Suite"
                        )}
                      </button>
                      <span
                        className={`text-zinc-500 transition-transform ${
                          expandedId === suite.id ? "rotate-180" : ""
                        }`}
                      >
                        ▼
                      </span>
                    </div>
                  </button>

                  {expandedId === suite.id && (
                    <div className="border-t border-zinc-800 bg-zinc-950/50 p-4">
                      {expandedLoading ? (
                        <div className="py-4 text-center text-sm text-zinc-500">
                          Loading…
                        </div>
                      ) : expandedSuite?.id === suite.id ? (
                        <>
                      {expandedSuite.description && (
                        <p className="mb-4 text-sm text-zinc-400">
                          {expandedSuite.description}
                        </p>
                      )}

                      {/* Test cases */}
                      {expandedSuite.test_cases &&
                        expandedSuite.test_cases.length > 0 && (
                          <div className="mb-4">
                            <h3 className="mb-2 text-sm font-medium text-zinc-400">
                              HTTP Test Cases
                            </h3>
                            <div className="space-y-2">
                              {expandedSuite.test_cases.map((tc) => (
                                <div
                                  key={tc.id}
                                  className="flex flex-wrap items-center gap-2 rounded-lg border border-zinc-700/50 bg-zinc-800/30 px-3 py-2"
                                >
                                  <span className="font-medium text-zinc-200">
                                    {tc.name}
                                  </span>
                                  <span className="font-mono text-sm text-zinc-500">
                                    {tc.endpoint}
                                  </span>
                                  <MethodBadge method={tc.method} />
                                  <span className="text-xs text-zinc-500">
                                    Expected: {tc.expected_status}
                                  </span>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}

                      {/* WebSocket test cases */}
                      {expandedSuite.ws_test_cases &&
                        expandedSuite.ws_test_cases.length > 0 && (
                          <div className="mb-4">
                            <h3 className="mb-2 text-sm font-medium text-zinc-400">
                              WebSocket Test Cases
                            </h3>
                            <div className="space-y-2">
                              {expandedSuite.ws_test_cases.map((ws) => (
                                <div
                                  key={ws.id}
                                  className="rounded-lg border border-zinc-700/50 bg-zinc-800/30 px-3 py-2"
                                >
                                  <span className="font-medium text-zinc-200">
                                    {ws.name}
                                  </span>
                                  {ws.description && (
                                    <p className="mt-1 text-sm text-zinc-500">
                                      {ws.description}
                                    </p>
                                  )}
                                  <span className="text-xs text-zinc-500">
                                    {ws.steps?.length ?? 0} steps
                                  </span>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}

                      {/* Suite results */}
                      {suiteResults[suite.id] &&
                        suiteResults[suite.id].length > 0 && (
                          <div>
                            <h3 className="mb-2 text-sm font-medium text-zinc-400">
                              Suite Results
                            </h3>
                            <div className="grid gap-2 sm:grid-cols-2">
                              {suiteResults[suite.id].map((r) => (
                                <TestCard
                                  key={r.id}
                                  name={r.test_case_name}
                                  endpoint={r.endpoint}
                                  method={r.method}
                                  status={(r.status as TestStatus) || "pending"}
                                  responseTime={r.response_time_ms}
                                  expectedStatus={r.expected_status}
                                  actualStatus={r.actual_status}
                                />
                              ))}
                            </div>
                          </div>
                        )}

                      {(!suiteResults[suite.id] ||
                        suiteResults[suite.id].length === 0) && (
                        <p className="text-sm text-zinc-500">
                          No results for this suite yet. Run the suite to see
                          results.
                        </p>
                      )}
                        </>
                      ) : null}
                    </div>
                  )}
                </article>
              ))
            )}
          </div>
        )}
      </div>
    </div>
  );
}

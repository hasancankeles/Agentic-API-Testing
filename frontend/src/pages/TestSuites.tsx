import { useCallback, useEffect, useState } from "react";
import { ChevronDown, Play, RefreshCw } from "lucide-react";
import {
  executeTests,
  getSuite,
  getSuiteResults,
  getSuites,
  type Suite,
  type TestResult,
} from "../api/client";
import {
  Badge,
  Button,
  EmptyState,
  InlineAlert,
  MethodBadge,
  PageHeader,
  Panel,
  StatusBadge,
} from "../components/ui";
import { cn, extractErrorMessage, toStatus } from "../lib/ui";

function unwrap<T>(res: { data?: T } | T): T {
  const d = res as { data?: T };
  return (d.data !== undefined ? d.data : res) as T;
}

export default function TestSuites() {
  const [suites, setSuites] = useState<Suite[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [expandedSuite, setExpandedSuite] = useState<Suite | null>(null);
  const [expandedLoading, setExpandedLoading] = useState(false);
  const [suiteResults, setSuiteResults] = useState<Record<string, TestResult[]>>({});
  const [runLoading, setRunLoading] = useState<string | null>(null);

  const fetchSuites = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await getSuites();
      const data = unwrap(res.data);
      setSuites(Array.isArray(data) ? data : []);
    } catch (err) {
      setError(extractErrorMessage(err, "Failed to load suites."));
      setSuites([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchSuites();
  }, [fetchSuites]);

  const fetchExpandedSuite = useCallback(async (id: string) => {
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
  }, []);

  const toggleExpand = (id: string) => {
    if (expandedId === id) {
      setExpandedId(null);
      setExpandedSuite(null);
      return;
    }
    setExpandedId(id);
    void fetchExpandedSuite(id);
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
    } catch (err) {
      setError(extractErrorMessage(err, "Failed to run suite."));
    } finally {
      setRunLoading(null);
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="Test Suites"
        description="Review generated functional and WebSocket suites, run focused groups, and inspect their latest case results."
        action={
          <Button
            variant="ghost"
            icon={RefreshCw}
            onClick={() => void fetchSuites()}
            loading={loading}
          >
            Refresh
          </Button>
        }
      />

      {error && (
        <InlineAlert tone="danger" title="Suite action failed">
          {error}
        </InlineAlert>
      )}

      <Panel title="Generated Suites" eyebrow={`${suites.length} suites`}>
        {loading ? (
          <EmptyState title="Loading suites…" />
        ) : suites.length === 0 ? (
          <EmptyState
            title="No test suites found"
            description="Generate tests from the dashboard first, then return here to inspect and run suites."
          />
        ) : (
          <div className="space-y-3">
            {suites.map((suite) => {
              const isExpanded = expandedId === suite.id;
              const latestResults = suiteResults[suite.id] ?? [];
              return (
                <article
                  key={suite.id}
                  className={cn(
                    "overflow-hidden rounded-lg border border-slate-800 bg-slate-950/45",
                    isExpanded && "border-emerald-500/25"
                  )}
                >
                  <div className="flex flex-col gap-3 p-4 lg:flex-row lg:items-center lg:justify-between">
                    <button
                      type="button"
                      onClick={() => toggleExpand(suite.id)}
                      className="min-w-0 flex-1 text-left"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <h2 className="font-semibold text-slate-100">{suite.name}</h2>
                        <Badge tone="neutral">{suite.category}</Badge>
                        <Badge tone="info">{suite.test_count} tests</Badge>
                      </div>
                      {suite.description && (
                        <p className="mt-2 max-w-4xl text-sm leading-6 text-slate-500">
                          {suite.description}
                        </p>
                      )}
                      <div className="mt-3 flex flex-wrap gap-3 text-sm">
                        <span className="text-emerald-300">{suite.passed} passed</span>
                        <span className="text-red-300">{suite.failed} failed</span>
                        <span className="text-amber-300">{suite.errors} errors</span>
                      </div>
                    </button>

                    <div className="flex shrink-0 items-center gap-2">
                      <Button
                        size="sm"
                        variant="primary"
                        icon={Play}
                        loading={runLoading === suite.id}
                        onClick={() => void handleRunSuite(suite.id)}
                      >
                        Run Suite
                      </Button>
                      <button
                        type="button"
                        onClick={() => toggleExpand(suite.id)}
                        aria-label={isExpanded ? "Collapse suite" : "Expand suite"}
                        className="flex size-9 items-center justify-center rounded-lg text-slate-500 transition-colors hover:bg-slate-800 hover:text-slate-100"
                      >
                        <ChevronDown
                          className={cn(
                            "size-4 transition-transform",
                            isExpanded && "rotate-180"
                          )}
                        />
                      </button>
                    </div>
                  </div>

                  {isExpanded && (
                    <div className="border-t border-slate-800 bg-slate-950/55 p-4">
                      {expandedLoading ? (
                        <EmptyState title="Loading suite detail…" />
                      ) : expandedSuite?.id === suite.id ? (
                        <div className="grid gap-4 xl:grid-cols-[1fr_0.9fr]">
                          <div className="space-y-4">
                            {(expandedSuite.test_cases?.length ?? 0) > 0 && (
                              <div>
                                <h3 className="text-sm font-semibold text-slate-100">
                                  HTTP Test Cases
                                </h3>
                                <div className="mt-3 space-y-2">
                                  {expandedSuite.test_cases?.map((testCase) => (
                                    <div
                                      key={testCase.id}
                                      className="rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2"
                                    >
                                      <div className="flex flex-wrap items-center gap-2">
                                        <span className="font-medium text-slate-100">
                                          {testCase.name}
                                        </span>
                                        <MethodBadge method={testCase.method} />
                                        <Badge tone="neutral">
                                          expected {testCase.expected_status}
                                        </Badge>
                                      </div>
                                      <p className="mt-2 break-all font-mono text-xs text-slate-500">
                                        {testCase.endpoint}
                                      </p>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}

                            {(expandedSuite.ws_test_cases?.length ?? 0) > 0 && (
                              <div>
                                <h3 className="text-sm font-semibold text-slate-100">
                                  WebSocket Test Cases
                                </h3>
                                <div className="mt-3 space-y-2">
                                  {expandedSuite.ws_test_cases?.map((testCase) => (
                                    <div
                                      key={testCase.id}
                                      className="rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2"
                                    >
                                      <p className="font-medium text-slate-100">
                                        {testCase.name}
                                      </p>
                                      <p className="mt-2 break-all font-mono text-xs text-slate-500">
                                        {testCase.url}
                                      </p>
                                      <Badge tone="neutral">
                                        {testCase.steps.length} steps
                                      </Badge>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}
                          </div>

                          <div>
                            <h3 className="text-sm font-semibold text-slate-100">
                              Latest Results
                            </h3>
                            {latestResults.length === 0 ? (
                              <div className="mt-3">
                                <EmptyState
                                  title="No result history for this suite"
                                  description="Run the suite to populate latest case statuses."
                                />
                              </div>
                            ) : (
                              <div className="mt-3 space-y-2">
                                {latestResults.map((result) => (
                                  <div
                                    key={result.id}
                                    className="rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2"
                                  >
                                    <div className="flex flex-wrap items-center justify-between gap-2">
                                      <span className="font-medium text-slate-100">
                                        {result.test_case_name}
                                      </span>
                                      <StatusBadge status={toStatus(result.status)} />
                                    </div>
                                    <p className="mt-2 text-xs text-slate-500">
                                      {result.assertions_passed}/{result.assertions_total} assertions,
                                      status {result.actual_status ?? "—"}
                                    </p>
                                  </div>
                                ))}
                              </div>
                            )}
                          </div>
                        </div>
                      ) : (
                        <EmptyState title="Could not load suite detail" />
                      )}
                    </div>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </Panel>
    </div>
  );
}

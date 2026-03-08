import { useEffect, useState, useCallback, Fragment } from "react";
import {
  getResults,
  type TestResult,
} from "../api/client";
import StatusBadge from "../components/StatusBadge";
import ResponseDiff from "../components/ResponseDiff";
import type { TestStatus } from "../components/StatusBadge";

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

function unwrap<T>(res: { data?: T } | T): T {
  const d = res as { data?: T };
  return (d.data !== undefined ? d.data : res) as T;
}

export default function TestResults() {
  const [results, setResults] = useState<TestResult[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [categoryFilter, setCategoryFilter] = useState<string>("all");
  const [endpointSearch, setEndpointSearch] = useState("");

  const fetchResults = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await getResults({
        status: statusFilter === "all" ? undefined : statusFilter,
        category: categoryFilter === "all" ? undefined : categoryFilter,
        endpoint: endpointSearch.trim() || undefined,
      });
      const data = unwrap(res.data);
      setResults(Array.isArray(data) ? data : []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load results");
      setResults([]);
    } finally {
      setLoading(false);
    }
  }, [statusFilter, categoryFilter, endpointSearch]);

  useEffect(() => {
    fetchResults();
  }, [fetchResults]);

  const toggleExpand = (id: string) => {
    setExpandedId((prev) => (prev === id ? null : id));
  };

  return (
    <div className="min-h-screen bg-slate-950 text-zinc-100 p-6">
      <div className="mx-auto max-w-7xl space-y-6">
        <h1 className="text-2xl font-semibold text-zinc-50">Test Results</h1>

        {/* Filter bar */}
        <section className="flex flex-wrap items-center gap-4 rounded-xl border border-zinc-800 bg-zinc-900/80 p-4">
          <div className="flex items-center gap-2">
            <label className="text-sm text-zinc-400">Status</label>
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/50"
            >
              <option value="all">All</option>
              <option value="passed">Passed</option>
              <option value="failed">Failed</option>
              <option value="error">Error</option>
            </select>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-sm text-zinc-400">Category</label>
            <select
              value={categoryFilter}
              onChange={(e) => setCategoryFilter(e.target.value)}
              className="rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/50"
            >
              <option value="all">All</option>
              <option value="individual">Individual</option>
              <option value="suite">Suite</option>
            </select>
          </div>
          <div className="flex flex-1 min-w-[200px] items-center gap-2">
            <label className="text-sm text-zinc-400">Endpoint</label>
            <input
              type="text"
              value={endpointSearch}
              onChange={(e) => setEndpointSearch(e.target.value)}
              placeholder="Search endpoint..."
              className="flex-1 rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/50"
            />
          </div>
        </section>

        {error && (
          <div className="rounded-lg border border-red-800 bg-red-950/50 px-4 py-2 text-red-300">
            {error}
          </div>
        )}

        {/* Results table */}
        <section className="overflow-hidden rounded-xl border border-zinc-800 bg-zinc-900/80">
          {loading ? (
            <div className="flex items-center justify-center py-16">
              <span className="animate-pulse text-zinc-400">Loading results…</span>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-zinc-800">
                    <th className="px-5 py-3 text-left font-medium text-zinc-400">
                      Test Name
                    </th>
                    <th className="px-5 py-3 text-left font-medium text-zinc-400">
                      Endpoint
                    </th>
                    <th className="px-5 py-3 text-left font-medium text-zinc-400">
                      Method
                    </th>
                    <th className="px-5 py-3 text-left font-medium text-zinc-400">
                      Status
                    </th>
                    <th className="px-5 py-3 text-right font-medium text-zinc-400">
                      Expected
                    </th>
                    <th className="px-5 py-3 text-right font-medium text-zinc-400">
                      Actual
                    </th>
                    <th className="px-5 py-3 text-right font-medium text-zinc-400">
                      Response Time (ms)
                    </th>
                    <th className="px-5 py-3 text-right font-medium text-zinc-400">
                      Assertions
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {results.length === 0 ? (
                    <tr>
                      <td
                        colSpan={8}
                        className="px-5 py-8 text-center text-zinc-500"
                      >
                        No results found
                      </td>
                    </tr>
                  ) : (
                    results.map((row, i) => (
                      <Fragment key={row.id}>
                        <tr
                          key={row.id}
                          onClick={() => toggleExpand(row.id)}
                          className={`cursor-pointer transition-colors hover:bg-zinc-800/50 ${
                            i % 2 === 0 ? "bg-zinc-900/50" : "bg-zinc-800/30"
                          }`}
                        >
                          <td className="px-5 py-3 font-medium text-zinc-100">
                            {row.test_case_name}
                          </td>
                          <td className="max-w-[200px] truncate px-5 py-3 font-mono text-zinc-400">
                            {row.endpoint}
                          </td>
                          <td className="px-5 py-3">
                            <MethodBadge method={row.method} />
                          </td>
                          <td className="px-5 py-3">
                            <StatusBadge
                              status={(row.status as TestStatus) || "pending"}
                            />
                          </td>
                          <td className="px-5 py-3 text-right tabular-nums text-zinc-300">
                            {row.expected_status}
                          </td>
                          <td className="px-5 py-3 text-right tabular-nums text-zinc-300">
                            {row.actual_status ?? "—"}
                          </td>
                          <td className="px-5 py-3 text-right tabular-nums text-zinc-300">
                            {row.response_time_ms}
                          </td>
                          <td className="px-5 py-3 text-right tabular-nums text-zinc-300">
                            {row.assertions_passed}/{row.assertions_total}
                          </td>
                        </tr>
                        {expandedId === row.id && (
                          <tr key={`${row.id}-expanded`}>
                            <td
                              colSpan={8}
                              className="border-t border-zinc-800 bg-zinc-950/80 px-5 py-4"
                            >
                              <ResponseDiff
                                expectedBody={row.expected_body}
                                actualBody={row.actual_body}
                                expectedStatus={row.expected_status}
                                actualStatus={row.actual_status}
                              />
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

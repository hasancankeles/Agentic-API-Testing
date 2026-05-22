import { Fragment, useCallback, useEffect, useState } from "react";
import { Activity, RefreshCw, Search } from "lucide-react";
import { getResults, type TestResult } from "../api/client";
import ResponseDiff from "../components/ResponseDiff";
import {
  Button,
  DataTable,
  EmptyState,
  Field,
  InlineAlert,
  Input,
  MethodBadge,
  PageHeader,
  Panel,
  Select,
  StatusBadge,
  tableCellClass,
  tableHeaderClass,
} from "../components/ui";
import { extractErrorMessage, toStatus } from "../lib/ui";

function unwrap<T>(res: { data?: T } | T): T {
  const d = res as { data?: T };
  return (d.data !== undefined ? d.data : res) as T;
}

export default function TestResults() {
  const [results, setResults] = useState<TestResult[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState("all");
  const [categoryFilter, setCategoryFilter] = useState("all");
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
    } catch (err) {
      setError(extractErrorMessage(err, "Failed to load results."));
      setResults([]);
    } finally {
      setLoading(false);
    }
  }, [statusFilter, categoryFilter, endpointSearch]);

  useEffect(() => {
    void fetchResults();
  }, [fetchResults]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Test Results"
        description="Filter functional execution history and inspect expected-vs-actual response details."
        action={
          <Button
            variant="ghost"
            icon={RefreshCw}
            onClick={() => void fetchResults()}
            loading={loading}
          >
            Refresh
          </Button>
        }
      />

      <Panel title="Filters" eyebrow="Result search">
        <div className="grid gap-4 md:grid-cols-[180px_180px_1fr]">
          <Field label="Status">
            <Select
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value)}
            >
              <option value="all">All</option>
              <option value="passed">Passed</option>
              <option value="failed">Failed</option>
              <option value="error">Error</option>
            </Select>
          </Field>
          <Field label="Category">
            <Select
              value={categoryFilter}
              onChange={(event) => setCategoryFilter(event.target.value)}
            >
              <option value="all">All</option>
              <option value="individual">Individual</option>
              <option value="suite">Suite</option>
            </Select>
          </Field>
          <Field label="Endpoint">
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-2.5 size-4 text-slate-500" />
              <Input
                value={endpointSearch}
                onChange={(event) => setEndpointSearch(event.target.value)}
                placeholder="Search endpoint..."
                className="pl-9"
              />
            </div>
          </Field>
        </div>
      </Panel>

      {error && (
        <InlineAlert tone="danger" title="Results failed">
          {error}
        </InlineAlert>
      )}

      <Panel title="Execution Results" eyebrow={`${results.length} rows`}>
        {loading ? (
          <EmptyState title="Loading results…" />
        ) : results.length === 0 ? (
          <EmptyState
            title="No results found"
            description="Adjust filters or execute tests from the dashboard or suites page."
          />
        ) : (
          <DataTable>
            <thead className={tableHeaderClass}>
              <tr>
                <th className={tableCellClass}>Test</th>
                <th className={tableCellClass}>Endpoint</th>
                <th className={tableCellClass}>Method</th>
                <th className={tableCellClass}>Status</th>
                <th className={`${tableCellClass} text-right`}>Expected</th>
                <th className={`${tableCellClass} text-right`}>Actual</th>
                <th className={`${tableCellClass} text-right`}>Time</th>
                <th className={`${tableCellClass} text-right`}>Assertions</th>
              </tr>
            </thead>
            <tbody>
              {results.map((row) => (
                <Fragment key={row.id}>
                  <tr
                    onClick={() =>
                      setExpandedId((current) => (current === row.id ? null : row.id))
                    }
                    className="cursor-pointer border-b border-slate-800/70 transition-colors hover:bg-slate-800/45"
                  >
                    <td className={`${tableCellClass} max-w-[240px]`}>
                      <p className="truncate font-medium text-slate-100">
                        {row.test_case_name}
                      </p>
                      <p className="mt-1 text-xs text-slate-500">{row.category}</p>
                    </td>
                    <td className={`${tableCellClass} max-w-[280px]`}>
                      <p className="truncate font-mono text-xs text-slate-400">
                        {row.endpoint}
                      </p>
                    </td>
                    <td className={tableCellClass}>
                      <MethodBadge method={row.method} />
                    </td>
                    <td className={tableCellClass}>
                      <StatusBadge status={toStatus(row.status)} />
                    </td>
                    <td className={`${tableCellClass} text-right tabular-nums text-slate-300`}>
                      {row.expected_status}
                    </td>
                    <td className={`${tableCellClass} text-right tabular-nums text-slate-300`}>
                      {row.actual_status ?? "—"}
                    </td>
                    <td className={`${tableCellClass} text-right tabular-nums text-slate-300`}>
                      {row.response_time_ms} ms
                    </td>
                    <td className={`${tableCellClass} text-right tabular-nums text-slate-300`}>
                      {row.assertions_passed}/{row.assertions_total}
                    </td>
                  </tr>
                  {expandedId === row.id && (
                    <tr>
                      <td colSpan={8} className="border-b border-slate-800 bg-slate-950/70 p-4">
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
              ))}
            </tbody>
          </DataTable>
        )}
      </Panel>

      <Panel title="Inspection Notes">
        <div className="flex items-start gap-3 text-sm text-slate-400">
          <Activity className="mt-0.5 size-4 shrink-0 text-emerald-300" />
          <p>
            Click any result row to compare status code and response body. Long
            endpoints stay available in the expanded detail rather than crowding the
            table.
          </p>
        </div>
      </Panel>
    </div>
  );
}

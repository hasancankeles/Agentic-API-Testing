import type { TestStatus } from "./StatusBadge";
import StatusBadge from "./StatusBadge";

export type HttpMethod = "GET" | "POST" | "PUT" | "DELETE" | "PATCH" | string;

interface TestCardProps {
  name: string;
  endpoint: string;
  method: HttpMethod;
  status: TestStatus;
  responseTime: number;
  expectedStatus: number;
  actualStatus: number | null;
  onClick?: () => void;
}

const methodStyles: Record<string, string> = {
  GET: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  POST: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  PUT: "bg-amber-500/20 text-amber-400 border-amber-500/30",
  DELETE: "bg-red-500/20 text-red-400 border-red-500/30",
  PATCH: "bg-violet-500/20 text-violet-400 border-violet-500/30",
};

function MethodBadge({ method }: { method: HttpMethod }) {
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

export default function TestCard({
  name,
  endpoint,
  method,
  status,
  responseTime,
  expectedStatus,
  actualStatus,
  onClick,
}: TestCardProps) {
  const statusMatch = actualStatus !== null && actualStatus === expectedStatus;

  return (
    <article
      role={onClick ? "button" : undefined}
      onClick={onClick}
      className={`rounded-lg border border-zinc-700/50 bg-zinc-900/50 p-4 transition-colors ${
        onClick ? "cursor-pointer hover:border-zinc-600 hover:bg-zinc-800/50" : ""
      }`}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="truncate font-medium text-zinc-100">{name}</h3>
          <p className="mt-1 truncate font-mono text-sm text-zinc-400">
            {endpoint}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <MethodBadge method={method} />
          <StatusBadge status={status} />
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-4 text-sm">
        <span className="text-zinc-500">
          Response:{" "}
          <span className="font-mono text-zinc-300">{responseTime}ms</span>
        </span>
        <span className="text-zinc-500">
          Status:{" "}
          <span
            className={
              statusMatch ? "font-mono text-emerald-400" : "font-mono text-red-400"
            }
          >
            {actualStatus !== null ? actualStatus : "—"} / {expectedStatus}
          </span>
        </span>
      </div>
    </article>
  );
}

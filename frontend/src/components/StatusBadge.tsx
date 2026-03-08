export type TestStatus = "passed" | "failed" | "error" | "pending" | "running";

interface StatusBadgeProps {
  status: TestStatus;
}

const statusStyles: Record<TestStatus, string> = {
  passed: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  failed: "bg-red-500/20 text-red-400 border-red-500/30",
  error: "bg-amber-500/20 text-amber-400 border-amber-500/30",
  pending: "bg-zinc-500/20 text-zinc-400 border-zinc-500/30",
  running: "bg-blue-500/20 text-blue-400 border-blue-500/30",
};

export default function StatusBadge({ status }: StatusBadgeProps) {
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium capitalize ${statusStyles[status]}`}
    >
      {status}
    </span>
  );
}

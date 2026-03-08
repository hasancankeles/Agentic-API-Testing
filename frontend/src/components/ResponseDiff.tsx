import ReactDiffViewer, { DiffMethod } from "react-diff-viewer-continued";

interface ResponseDiffProps {
  expectedBody: unknown;
  actualBody: unknown;
  expectedStatus: number;
  actualStatus: number | null;
}

function toDiffString(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "string") return value;
  if (typeof value === "object") {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

export default function ResponseDiff({
  expectedBody,
  actualBody,
  expectedStatus,
  actualStatus,
}: ResponseDiffProps) {
  const expectedStr = toDiffString(expectedBody);
  const actualStr = toDiffString(actualBody);
  const statusMatch = actualStatus !== null && actualStatus === expectedStatus;

  return (
    <div className="overflow-hidden rounded-lg border border-zinc-700/50 bg-zinc-900/50">
      <div className="flex items-center justify-between border-b border-zinc-700/50 bg-zinc-800/50 px-4 py-3">
        <span className="text-sm font-medium text-zinc-400">Status Code</span>
        <div className="flex items-center gap-4">
          <span className="text-sm text-zinc-500">
            Expected:{" "}
            <span className="font-mono font-medium text-zinc-300">
              {expectedStatus}
            </span>
          </span>
          <span className="text-zinc-600">→</span>
          <span className="text-sm text-zinc-500">
            Actual:{" "}
            <span
              className={`font-mono font-medium ${
                statusMatch ? "text-emerald-400" : "text-red-400"
              }`}
            >
              {actualStatus ?? "—"}
            </span>
          </span>
          {statusMatch ? (
            <span className="rounded bg-emerald-500/20 px-2 py-0.5 text-xs text-emerald-400">
              Match
            </span>
          ) : (
            <span className="rounded bg-red-500/20 px-2 py-0.5 text-xs text-red-400">
              Mismatch
            </span>
          )}
        </div>
      </div>
      <div className="[&_.diff-viewer]:!rounded-none [&_.diff-viewer]:!border-0">
        <ReactDiffViewer
          oldValue={expectedStr}
          newValue={actualStr}
          splitView
          useDarkTheme
          compareMethod={DiffMethod.LINES}
          leftTitle="Expected"
          rightTitle="Actual"
          styles={{
            variables: {
              dark: {
                diffViewerBackground: "#18181b",
                diffViewerColor: "#e4e4e7",
                addedBackground: "#14532d",
                addedColor: "#86efac",
                removedBackground: "#7f1d1d",
                removedColor: "#fca5a5",
                wordAddedBackground: "#166534",
                wordRemovedBackground: "#991b1b",
                gutterBackground: "#27272a",
                gutterBackgroundDark: "#1f1f23",
                gutterColor: "#71717a",
                addedGutterBackground: "#14532d",
                removedGutterBackground: "#7f1d1d",
              },
            },
          }}
        />
      </div>
    </div>
  );
}

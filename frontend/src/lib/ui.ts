export function cn(
  ...classes: Array<string | false | null | undefined>
): string {
  return classes.filter(Boolean).join(" ");
}

export function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "—";
  const date = new Date(dateStr);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString();
}

export function formatMs(ms: number | null | undefined): string {
  if (typeof ms !== "number" || !Number.isFinite(ms)) return "—";
  return `${Math.round(ms)} ms`;
}

export function truncateMiddle(value: string, max = 18): string {
  if (value.length <= max) return value;
  const edge = Math.max(4, Math.floor((max - 1) / 2));
  return `${value.slice(0, edge)}…${value.slice(-edge)}`;
}

export function toPrettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function parseJsonObject(
  raw: string,
  fieldName: string
): { value: Record<string, unknown> | null; error: string | null } {
  const trimmed = raw.trim();
  if (!trimmed) return { value: {}, error: null };

  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
      return { value: null, error: `${fieldName} must be a JSON object.` };
    }
    return { value: parsed as Record<string, unknown>, error: null };
  } catch {
    return { value: null, error: `${fieldName} is not valid JSON.` };
  }
}

export function parseJsonAny(
  raw: string,
  fieldName: string
): { value: unknown; error: string | null } {
  const trimmed = raw.trim();
  if (!trimmed) return { value: null, error: null };

  try {
    return { value: JSON.parse(trimmed), error: null };
  } catch {
    return { value: null, error: `${fieldName} is not valid JSON.` };
  }
}

export function extractErrorMessage(error: unknown, fallback: string): string {
  if (typeof error === "object" && error !== null) {
    const maybe = error as {
      message?: unknown;
      response?: { data?: { detail?: unknown } };
    };
    const detail = maybe.response?.data?.detail;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (detail !== undefined && detail !== null) return toPrettyJson(detail);
    if (typeof maybe.message === "string" && maybe.message.trim()) {
      return maybe.message;
    }
  }
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

export type NormalizedStatus =
  | "passed"
  | "failed"
  | "error"
  | "pending"
  | "running";

export function toStatus(value: string | null | undefined): NormalizedStatus {
  if (
    value === "passed" ||
    value === "failed" ||
    value === "error" ||
    value === "pending" ||
    value === "running"
  ) {
    return value;
  }
  return "pending";
}

export function clampInt(
  value: number,
  min: number,
  max: number,
  fallback: number
): number {
  if (!Number.isFinite(value)) return fallback;
  return Math.max(min, Math.min(max, Math.floor(value)));
}

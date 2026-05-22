import type { ComponentType, ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import { cn, toPrettyJson, type NormalizedStatus } from "../lib/ui";

type IconType = ComponentType<{
  className?: string;
  size?: number | string;
  strokeWidth?: number | string;
}>;

type ButtonVariant = "primary" | "secondary" | "subtle" | "danger" | "ghost";
type ButtonSize = "sm" | "md";

const buttonVariants: Record<ButtonVariant, string> = {
  primary:
    "border-emerald-500/40 bg-emerald-500 text-slate-950 hover:bg-emerald-400 focus-visible:ring-emerald-400/40",
  secondary:
    "border-sky-500/35 bg-sky-500/15 text-sky-100 hover:bg-sky-500/25 focus-visible:ring-sky-400/35",
  subtle:
    "border-slate-700 bg-slate-800 text-slate-100 hover:bg-slate-700 focus-visible:ring-slate-500/40",
  danger:
    "border-red-500/35 bg-red-500/15 text-red-100 hover:bg-red-500/25 focus-visible:ring-red-400/35",
  ghost:
    "border-transparent bg-transparent text-slate-300 hover:bg-slate-800 hover:text-slate-50 focus-visible:ring-slate-500/35",
};

const buttonSizes: Record<ButtonSize, string> = {
  sm: "h-8 px-3 text-xs",
  md: "h-10 px-4 text-sm",
};

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: IconType;
  loading?: boolean;
}

export function Button({
  variant = "subtle",
  size = "md",
  icon: Icon,
  loading,
  className,
  children,
  disabled,
  ...props
}: ButtonProps) {
  return (
    <button
      type="button"
      disabled={disabled || loading}
      className={cn(
        "inline-flex items-center justify-center gap-2 rounded-lg border font-medium transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "focus-visible:outline-none focus-visible:ring-2",
        buttonVariants[variant],
        buttonSizes[size],
        className
      )}
      {...props}
    >
      {loading ? (
        <span className="size-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
      ) : Icon ? (
        <Icon className="size-4" />
      ) : null}
      {children}
    </button>
  );
}

interface IconButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  icon: IconType;
  label: string;
  variant?: ButtonVariant;
}

export function IconButton({
  icon: Icon,
  label,
  variant = "ghost",
  className,
  ...props
}: IconButtonProps) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={cn(
        "inline-flex size-9 items-center justify-center rounded-lg border transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2",
        buttonVariants[variant],
        className
      )}
      {...props}
    >
      <Icon className="size-4" />
    </button>
  );
}

interface PanelProps {
  children: ReactNode;
  className?: string;
  title?: string;
  eyebrow?: string;
  action?: ReactNode;
}

export function Panel({ children, className, title, eyebrow, action }: PanelProps) {
  return (
    <section
      className={cn(
        "rounded-lg border border-slate-800 bg-slate-900/72 shadow-[0_20px_60px_rgba(2,6,23,0.25)]",
        className
      )}
    >
      {(title || eyebrow || action) && (
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-4 py-3">
          <div>
            {eyebrow && (
              <p className="text-xs font-medium uppercase tracking-[0.18em] text-slate-500">
                {eyebrow}
              </p>
            )}
            {title && <h2 className="text-sm font-semibold text-slate-100">{title}</h2>}
          </div>
          {action}
        </div>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

interface PageHeaderProps {
  title: string;
  description?: string;
  action?: ReactNode;
}

export function PageHeader({ title, description, action }: PageHeaderProps) {
  return (
    <header className="flex flex-col gap-4 border-b border-slate-800 pb-5 md:flex-row md:items-end md:justify-between">
      <div>
        <p className="text-xs font-medium uppercase tracking-[0.22em] text-emerald-300/80">
          Agentic API Testing
        </p>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight text-slate-50">
          {title}
        </h1>
        {description && (
          <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-400">
            {description}
          </p>
        )}
      </div>
      {action}
    </header>
  );
}

interface MetricCardProps {
  label: string;
  value: ReactNode;
  tone?: "neutral" | "good" | "bad" | "warn" | "info";
  detail?: ReactNode;
  icon?: IconType;
}

const metricTone: Record<NonNullable<MetricCardProps["tone"]>, string> = {
  neutral: "text-slate-50 border-slate-800 bg-slate-900/72",
  good: "text-emerald-300 border-emerald-500/20 bg-emerald-500/8",
  bad: "text-red-300 border-red-500/20 bg-red-500/8",
  warn: "text-amber-300 border-amber-500/20 bg-amber-500/8",
  info: "text-sky-300 border-sky-500/20 bg-sky-500/8",
};

export function MetricCard({
  label,
  value,
  tone = "neutral",
  detail,
  icon: Icon,
}: MetricCardProps) {
  return (
    <div className={cn("rounded-lg border p-4", metricTone[tone])}>
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs font-medium uppercase tracking-[0.16em] text-slate-500">
          {label}
        </p>
        {Icon && <Icon className="size-4 text-current opacity-80" />}
      </div>
      <div className="mt-3 text-2xl font-semibold tabular-nums text-current">
        {value}
      </div>
      {detail && <div className="mt-2 text-xs text-slate-400">{detail}</div>}
    </div>
  );
}

interface FieldProps {
  label: string;
  children: ReactNode;
  hint?: string;
  error?: string | null;
  className?: string;
}

export function Field({ label, children, hint, error, className }: FieldProps) {
  return (
    <label className={cn("block space-y-1.5", className)}>
      <span className="text-sm font-medium text-slate-300">{label}</span>
      {children}
      {hint && !error && <span className="block text-xs text-slate-500">{hint}</span>}
      {error && <span className="block text-xs text-red-300">{error}</span>}
    </label>
  );
}

const inputBase =
  "w-full rounded-lg border border-slate-700 bg-slate-950/70 px-3 py-2 text-sm text-slate-100 placeholder-slate-500 shadow-inner shadow-slate-950/30 focus:border-emerald-400 focus:outline-none focus:ring-2 focus:ring-emerald-400/25 disabled:cursor-not-allowed disabled:opacity-60";

export function Input({
  className,
  ...props
}: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn(inputBase, className)} {...props} />;
}

export function Select({
  className,
  ...props
}: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={cn(inputBase, className)} {...props} />;
}

export function Textarea({
  className,
  ...props
}: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cn(inputBase, "font-mono", className)} {...props} />;
}

type BadgeTone = "neutral" | "good" | "bad" | "warn" | "info" | "violet";

const badgeTone: Record<BadgeTone, string> = {
  neutral: "border-slate-700 bg-slate-800/80 text-slate-300",
  good: "border-emerald-500/30 bg-emerald-500/12 text-emerald-300",
  bad: "border-red-500/30 bg-red-500/12 text-red-300",
  warn: "border-amber-500/30 bg-amber-500/12 text-amber-300",
  info: "border-sky-500/30 bg-sky-500/12 text-sky-300",
  violet: "border-violet-500/30 bg-violet-500/12 text-violet-300",
};

export function Badge({
  tone = "neutral",
  children,
  className,
}: {
  tone?: BadgeTone;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium",
        badgeTone[tone],
        className
      )}
    >
      {children}
    </span>
  );
}

const statusTone: Record<NormalizedStatus, BadgeTone> = {
  passed: "good",
  failed: "bad",
  error: "warn",
  pending: "neutral",
  running: "info",
};

export function StatusBadge({ status }: { status: NormalizedStatus }) {
  return <Badge tone={statusTone[status]}>{status}</Badge>;
}

const methodTone: Record<string, BadgeTone> = {
  GET: "good",
  POST: "info",
  PUT: "warn",
  DELETE: "bad",
  PATCH: "violet",
  OPTIONS: "info",
  HEAD: "neutral",
};

export function MethodBadge({ method }: { method: string }) {
  const normalized = method.toUpperCase();
  return (
    <Badge tone={methodTone[normalized] ?? "neutral"} className="font-mono">
      {normalized}
    </Badge>
  );
}

type AlertTone = "info" | "success" | "warning" | "danger";

const alertTone: Record<AlertTone, string> = {
  info: "border-sky-500/30 bg-sky-500/10 text-sky-100",
  success: "border-emerald-500/30 bg-emerald-500/10 text-emerald-100",
  warning: "border-amber-500/30 bg-amber-500/10 text-amber-100",
  danger: "border-red-500/30 bg-red-500/10 text-red-100",
};

export function InlineAlert({
  tone = "info",
  title,
  children,
}: {
  tone?: AlertTone;
  title?: string;
  children: ReactNode;
}) {
  return (
    <div className={cn("rounded-lg border px-4 py-3 text-sm", alertTone[tone])}>
      {title && <p className="font-semibold">{title}</p>}
      <div className={cn(title ? "mt-1" : "", "text-current/85")}>{children}</div>
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="rounded-lg border border-dashed border-slate-700 bg-slate-950/50 px-4 py-10 text-center">
      <p className="text-sm font-semibold text-slate-200">{title}</p>
      {description && (
        <p className="mx-auto mt-2 max-w-xl text-sm text-slate-500">{description}</p>
      )}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function JsonDisclosure({
  title,
  value,
  defaultOpen = false,
}: {
  title: string;
  value: unknown;
  defaultOpen?: boolean;
}) {
  return (
    <details
      open={defaultOpen}
      className="group overflow-hidden rounded-lg border border-slate-800 bg-slate-950/60"
    >
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2 text-sm font-medium text-slate-300 hover:bg-slate-800/70">
        {title}
        <ChevronDown className="size-4 text-slate-500 transition-transform group-open:rotate-180" />
      </summary>
      <pre className="max-h-80 overflow-auto border-t border-slate-800 px-3 py-2 text-xs leading-5 text-slate-300">
        {toPrettyJson(value)}
      </pre>
    </details>
  );
}

export function DataTable({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("overflow-x-auto rounded-lg border border-slate-800", className)}>
      <table className="w-full min-w-[760px] text-sm">{children}</table>
    </div>
  );
}

export const tableHeaderClass =
  "border-b border-slate-800 bg-slate-950/70 text-left text-xs font-medium uppercase tracking-[0.14em] text-slate-500";

export const tableCellClass = "px-3 py-3 align-middle";

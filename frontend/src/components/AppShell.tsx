import type { ReactNode } from "react";
import {
  Activity,
  BookOpen,
  Gauge,
  LayoutDashboard,
  ListChecks,
  Route,
} from "lucide-react";
import { NavLink } from "react-router-dom";
import { cn } from "../lib/ui";

const navItems = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard },
  { to: "/flows", label: "Flow Tests", icon: Route },
  { to: "/suites", label: "Test Suites", icon: ListChecks },
  { to: "/results", label: "Results", icon: Activity },
  { to: "/load-tests", label: "Load Tests", icon: Gauge },
  { to: "/how-to-use", label: "How to Use", icon: BookOpen },
];

export default function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <div className="fixed inset-y-0 left-0 z-40 hidden w-64 border-r border-slate-800 bg-slate-950/96 px-4 py-5 lg:block">
        <div className="rounded-lg border border-slate-800 bg-slate-900/70 p-4">
          <p className="text-xs font-medium uppercase tracking-[0.22em] text-emerald-300">
            Agentic
          </p>
          <h1 className="mt-2 text-lg font-semibold tracking-tight text-slate-50">
            API Testing
          </h1>
          <p className="mt-2 text-xs leading-5 text-slate-500">
            Parse specs, generate tests, run flows, inspect traces.
          </p>
        </div>

        <nav className="mt-6 space-y-1">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-emerald-500/12 text-emerald-200 ring-1 ring-emerald-500/25"
                    : "text-slate-400 hover:bg-slate-900 hover:text-slate-100"
                )
              }
            >
              <item.icon className="size-4" />
              {item.label}
            </NavLink>
          ))}
        </nav>
      </div>

      <header className="sticky top-0 z-50 border-b border-slate-800 bg-slate-950/92 backdrop-blur lg:hidden">
        <div className="px-4 py-3">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-xs font-medium uppercase tracking-[0.2em] text-emerald-300">
                Agentic
              </p>
              <p className="text-sm font-semibold text-slate-50">API Testing</p>
            </div>
          </div>
          <nav className="mt-3 flex gap-2 overflow-x-auto pb-1">
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                className={({ isActive }) =>
                  cn(
                    "inline-flex shrink-0 items-center gap-2 rounded-lg px-3 py-2 text-xs font-medium transition-colors",
                    isActive
                      ? "bg-emerald-500/12 text-emerald-200 ring-1 ring-emerald-500/25"
                      : "bg-slate-900 text-slate-400 hover:text-slate-100"
                  )
                }
              >
                <item.icon className="size-3.5" />
                {item.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>

      <main className="lg:pl-64">
        <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
          {children}
        </div>
      </main>
    </div>
  );
}

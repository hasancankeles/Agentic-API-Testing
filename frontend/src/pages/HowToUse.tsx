import {
  Activity,
  BookOpen,
  CheckCircle2,
  FileSearch,
  Gauge,
  ListChecks,
  Play,
  Route,
  Sparkles,
} from "lucide-react";
import {
  Badge,
  InlineAlert,
  PageHeader,
  Panel,
} from "../components/ui";

type GuideSection = {
  title: string;
  icon: typeof BookOpen;
  summary: string;
  items: string[];
};

const guideSections: GuideSection[] = [
  {
    title: "Dashboard",
    icon: Activity,
    summary: "Use this page as the main demo screen.",
    items: [
      "Paste an OpenAPI spec URL or a local spec path.",
      "Click Parse Spec to read the API and save it in the app.",
      "Click Generate Tests to create normal endpoint tests, suites, and load-test ideas.",
      "Click Execute All to run generated functional tests.",
      "Use the cards and recent runs table to see if tests passed or failed.",
    ],
  },
  {
    title: "Flow Tests",
    icon: Route,
    summary: "Use this page for multi-step API journeys.",
    items: [
      "Open Generate to choose how flows are created.",
      "Use hybrid_auto for normal demos. It tries AI and can fall back to safer logic.",
      "Use deterministic_first when you want stable rule-based flows only.",
      "Use Mutation policy to control how much the flow can change data.",
      "Keep Include negative steps on when you want auth or bad-input checks.",
      "Open Review Flows to select flows and inspect their step order.",
      "Open Run to execute selected flows or the latest generated batch.",
      "Open History to inspect each run and every step trace.",
    ],
  },
  {
    title: "Test Suites",
    icon: ListChecks,
    summary: "Use this page to run grouped generated tests.",
    items: [
      "Each suite contains related test cases.",
      "Click a suite to see its HTTP and WebSocket test cases.",
      "Click Run Suite to execute only that suite.",
      "After a run, expand the suite again to see latest case results.",
    ],
  },
  {
    title: "Test Results",
    icon: CheckCircle2,
    summary: "Use this page to inspect normal test results.",
    items: [
      "Filter by status, category, or endpoint.",
      "Click a result row to open expected versus actual details.",
      "Use the diff view to compare response status and response body.",
      "Failed rows show what the API returned differently than expected.",
    ],
  },
  {
    title: "Load Tests",
    icon: Gauge,
    summary: "Use this page for simple k6-style performance tests.",
    items: [
      "Create a scenario with name, target URL, method, virtual users, and duration.",
      "Use smoke, load, or stress presets to fill common settings quickly.",
      "Open advanced JSON only when you need headers, query params, body, ramp stages, or thresholds.",
      "Select scenarios in the library, then run selected scenarios.",
      "Use History and Run Detail to inspect requests, p95 response time, RPS, warnings, and raw runner output.",
    ],
  },
];

const quickStartSteps = [
  {
    title: "1. Parse an API",
    icon: FileSearch,
    text: "Start on Dashboard. Paste a spec URL, then click Parse Spec.",
  },
  {
    title: "2. Generate flows",
    icon: Sparkles,
    text: "Go to Flow Tests, keep hybrid_auto and safe selected, then click Generate Flows.",
  },
  {
    title: "3. Review what was kept",
    icon: Route,
    text: "Open Review Flows. Check which flows were accepted and what each step does.",
  },
  {
    title: "4. Run and inspect",
    icon: Play,
    text: "Run selected flows. Then open History and inspect the step trace.",
  },
];

export default function HowToUse() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="How to Use the App"
        description="A simple guide for each page. Follow this when you want to demo or test the app without guessing what each control does."
      />

      <InlineAlert tone="info" title="Best demo API">
        Use spec URL{" "}
        <span className="font-mono text-sky-100">
          https://www.davidmello.com/specs/restful-booker.swagger.json
        </span>{" "}
        and target base URL{" "}
        <span className="font-mono text-sky-100">
          https://restful-booker.herokuapp.com
        </span>
        .
      </InlineAlert>

      <Panel title="Quick Start" eyebrow="Recommended demo path">
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {quickStartSteps.map((step) => (
            <div
              key={step.title}
              className="rounded-lg border border-slate-800 bg-slate-950/55 p-4"
            >
              <step.icon className="size-5 text-emerald-300" />
              <h2 className="mt-3 text-sm font-semibold text-slate-100">
                {step.title}
              </h2>
              <p className="mt-2 text-sm leading-6 text-slate-400">
                {step.text}
              </p>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="Page Guide" eyebrow="Every main function">
        <div className="grid gap-4 lg:grid-cols-2">
          {guideSections.map((section) => (
            <article
              key={section.title}
              className="rounded-lg border border-slate-800 bg-slate-950/55 p-4"
            >
              <div className="flex items-start gap-3">
                <span className="flex size-9 shrink-0 items-center justify-center rounded-lg border border-slate-700 bg-slate-900 text-emerald-300">
                  <section.icon className="size-4" />
                </span>
                <div>
                  <h2 className="font-semibold text-slate-100">{section.title}</h2>
                  <p className="mt-1 text-sm text-slate-500">{section.summary}</p>
                </div>
              </div>
              <ul className="mt-4 space-y-2">
                {section.items.map((item) => (
                  <li key={item} className="flex gap-2 text-sm leading-6 text-slate-400">
                    <span className="mt-2 size-1.5 shrink-0 rounded-full bg-emerald-300" />
                    <span>{item}</span>
                  </li>
                ))}
              </ul>
            </article>
          ))}
        </div>
      </Panel>

      <Panel title="Common Inputs" eyebrow="What the fields mean">
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <div className="rounded-lg border border-slate-800 bg-slate-950/55 p-4">
            <Badge tone="info">Spec URL</Badge>
            <p className="mt-3 text-sm leading-6 text-slate-400">
              The OpenAPI or Swagger file. The app reads this to learn endpoints,
              methods, request bodies, and response shapes.
            </p>
          </div>
          <div className="rounded-lg border border-slate-800 bg-slate-950/55 p-4">
            <Badge tone="good">Target base URL</Badge>
            <p className="mt-3 text-sm leading-6 text-slate-400">
              The real API host where requests are sent. Use this when the spec is
              hosted somewhere else.
            </p>
          </div>
          <div className="rounded-lg border border-slate-800 bg-slate-950/55 p-4">
            <Badge tone="warn">Initial context</Badge>
            <p className="mt-3 text-sm leading-6 text-slate-400">
              Extra values that flow steps can reuse. Keep it as {"{}"} unless a
              flow needs a known token or ID.
            </p>
          </div>
          <div className="rounded-lg border border-slate-800 bg-slate-950/55 p-4">
            <Badge tone="violet">Advanced JSON</Badge>
            <p className="mt-3 text-sm leading-6 text-slate-400">
              Extra request data for advanced cases. If JSON is invalid, the page
              shows an error before sending the request.
            </p>
          </div>
        </div>
      </Panel>

      <Panel title="Troubleshooting" eyebrow="Simple fixes">
        <div className="space-y-3 text-sm leading-6 text-slate-400">
          <p>
            <span className="font-semibold text-slate-200">No generated tests:</span>{" "}
            parse an API spec first, then generate again.
          </p>
          <p>
            <span className="font-semibold text-slate-200">No generated flows:</span>{" "}
            try deterministic_first, reduce max steps, or use safe mutation policy.
          </p>
          <p>
            <span className="font-semibold text-slate-200">Run failed:</span> check
            the target base URL and inspect the run history step trace.
          </p>
          <p>
            <span className="font-semibold text-slate-200">JSON error:</span> use
            valid JSON objects such as {"{}"} for context and headers.
          </p>
          <p>
            <span className="font-semibold text-slate-200">Auth errors:</span> some
            APIs need tokens. Use Flow Tests history to see whether the auth step
            extracted a token.
          </p>
        </div>
      </Panel>
    </div>
  );
}

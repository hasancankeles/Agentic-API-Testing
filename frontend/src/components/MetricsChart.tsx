import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

export interface PercentileDataPoint {
  name: string;
  p50: number;
  p90: number;
  p95: number;
  p99: number;
}

interface MetricsChartProps {
  data: PercentileDataPoint[];
  title: string;
}

export default function MetricsChart({ data, title }: MetricsChartProps) {
  return (
    <div className="rounded-lg border border-zinc-700/50 bg-zinc-900/50 p-4">
      <h3 className="mb-4 text-sm font-medium text-zinc-300">{title}</h3>
      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 5, right: 20, left: 0, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#3f3f46" />
            <XAxis
              dataKey="name"
              stroke="#71717a"
              tick={{ fill: "#a1a1aa", fontSize: 12 }}
            />
            <YAxis
              stroke="#71717a"
              tick={{ fill: "#a1a1aa", fontSize: 12 }}
              tickFormatter={(v) => `${v}ms`}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: "#27272a",
                border: "1px solid #3f3f46",
                borderRadius: "0.5rem",
              }}
              labelStyle={{ color: "#a1a1aa" }}
              formatter={(value) => [value != null ? `${value}ms` : "—", ""]}
              labelFormatter={(label) => label}
            />
            <Legend
              wrapperStyle={{ fontSize: 12 }}
              formatter={(value) => (
                <span className="text-zinc-400">{value}</span>
              )}
            />
            <Line
              type="monotone"
              dataKey="p50"
              name="p50"
              stroke="#22c55e"
              strokeWidth={2}
              dot={false}
            />
            <Line
              type="monotone"
              dataKey="p90"
              name="p90"
              stroke="#3b82f6"
              strokeWidth={2}
              dot={false}
            />
            <Line
              type="monotone"
              dataKey="p95"
              name="p95"
              stroke="#8b5cf6"
              strokeWidth={2}
              dot={false}
            />
            <Line
              type="monotone"
              dataKey="p99"
              name="p99"
              stroke="#f59e0b"
              strokeWidth={2}
              dot={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

export interface ThroughputDataPoint {
  name: string;
  rps: number;
  errors: number;
}

interface ThroughputChartProps {
  data: ThroughputDataPoint[];
  title?: string;
}

export function ThroughputChart({
  data,
  title = "Throughput",
}: ThroughputChartProps) {
  return (
    <div className="rounded-lg border border-zinc-700/50 bg-zinc-900/50 p-4">
      <h3 className="mb-4 text-sm font-medium text-zinc-300">{title}</h3>
      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 5, right: 20, left: 0, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#3f3f46" />
            <XAxis
              dataKey="name"
              stroke="#71717a"
              tick={{ fill: "#a1a1aa", fontSize: 12 }}
            />
            <YAxis
              yAxisId="left"
              stroke="#71717a"
              tick={{ fill: "#a1a1aa", fontSize: 12 }}
              tickFormatter={(v) => `${v}`}
            />
            <YAxis
              yAxisId="right"
              orientation="right"
              stroke="#71717a"
              tick={{ fill: "#a1a1aa", fontSize: 12 }}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: "#27272a",
                border: "1px solid #3f3f46",
                borderRadius: "0.5rem",
              }}
              labelStyle={{ color: "#a1a1aa" }}
              formatter={(value, name) => [
                value ?? "—",
                name === "rps" ? "RPS" : "Errors",
              ]}
              labelFormatter={(label) => label}
            />
            <Legend
              wrapperStyle={{ fontSize: 12 }}
              formatter={(value) => (
                <span className="text-zinc-400">{value}</span>
              )}
            />
            <Line
              yAxisId="left"
              type="monotone"
              dataKey="rps"
              name="RPS"
              stroke="#22c55e"
              strokeWidth={2}
              dot={false}
            />
            <Line
              yAxisId="right"
              type="monotone"
              dataKey="errors"
              name="Errors"
              stroke="#ef4444"
              strokeWidth={2}
              dot={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

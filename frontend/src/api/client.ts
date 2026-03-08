import axios from 'axios';

const api = axios.create({
  baseURL: '/api',
  headers: { 'Content-Type': 'application/json' },
});

export interface DashboardSummary {
  total_tests: number;
  passed: number;
  failed: number;
  errors: number;
  pass_rate: number;
  avg_response_time_ms: number;
  functional_summary: Record<string, number>;
  suite_summary: Record<string, number>;
  load_summary: Record<string, number>;
  recent_runs: TestRun[];
}

export interface TestRun {
  id: string;
  total_tests: number;
  passed: number;
  failed: number;
  errors: number;
  avg_response_time_ms: number;
  started_at: string;
  finished_at: string | null;
}

export interface Suite {
  id: string;
  name: string;
  description: string;
  category: string;
  test_count: number;
  passed: number;
  failed: number;
  errors: number;
  created_at: string;
  test_cases?: TestCaseData[];
  ws_test_cases?: WsTestCaseData[];
}

export interface TestCaseData {
  id: string;
  name: string;
  description: string;
  endpoint: string;
  method: string;
  expected_status: number;
  assertions: { field: string; operator: string; expected: unknown }[];
}

export interface WsTestCaseData {
  id: string;
  name: string;
  description: string;
  url: string;
  steps: { action: string; message: Record<string, unknown> }[];
}

export interface TestResult {
  id: string;
  run_id: string;
  test_case_id: string;
  test_case_name: string;
  suite_id: string | null;
  suite_name: string | null;
  endpoint: string;
  method: string;
  category: string;
  status: string;
  expected_status: number;
  actual_status: number | null;
  expected_body: unknown;
  actual_body: unknown;
  response_time_ms: number;
  assertions_passed: number;
  assertions_total: number;
  error_message: string | null;
  executed_at: string;
}

export interface LoadTestResult {
  id: string;
  scenario_id: string;
  scenario_name: string;
  total_requests: number;
  failed_requests: number;
  avg_response_time_ms: number;
  min_response_time_ms: number;
  max_response_time_ms: number;
  p50_ms: number;
  p90_ms: number;
  p95_ms: number;
  p99_ms: number;
  requests_per_second: number;
  error_rate: number;
  data_received_kb: number;
  data_sent_kb: number;
  duration_seconds: number;
  vus_max: number;
  executed_at: string;
}

export interface LoadTestScenario {
  id: string;
  name: string;
  description: string;
  target_url: string;
  method: string;
  vus: number;
  duration: string;
  ramp_stages: { duration: string; target: number }[];
  thresholds: Record<string, string[]>;
  created_at: string;
}

export const getDashboard = () => api.get<DashboardSummary>('/dashboard/summary');

export const parseSpec = (data: { spec_url?: string; spec_path?: string; spec_content?: string }) =>
  api.post('/parse', data);

export const generateTests = (categories?: string[]) =>
  api.post('/generate', { categories: categories ?? ['individual', 'suite', 'load'] });

export const executeTests = (data: { suite_ids?: string[]; target_base_url?: string }) =>
  api.post('/execute', data);

export const runLoadTests = (data: { scenario_ids?: string[]; target_base_url?: string }) =>
  api.post('/loadtest/run', data);

export const getSuites = () => api.get<Suite[]>('/suites');

export const getSuite = (id: string) => api.get<Suite>(`/suites/${id}`);

export const getSuiteResults = (id: string) => api.get<TestResult[]>(`/suites/${id}/results`);

export const getResults = (params?: {
  status?: string;
  category?: string;
  endpoint?: string;
  run_id?: string;
  limit?: number;
  offset?: number;
}) => api.get<TestResult[]>('/results', { params });

export const getResult = (id: string) => api.get<TestResult>(`/results/${id}`);

export const getLoadTestResults = (scenarioId?: string) =>
  api.get<LoadTestResult[]>('/loadtest/results', { params: scenarioId ? { scenario_id: scenarioId } : {} });

export const getLoadTestScenarios = () => api.get<LoadTestScenario[]>('/loadtest/scenarios');

export const getRuns = (limit?: number) => api.get<TestRun[]>('/runs', { params: { limit: limit ?? 20 } });

export default api;

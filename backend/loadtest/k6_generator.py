from __future__ import annotations

import json
import os
import uuid

from models.schemas import LoadTestScenario

K6_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "k6-scripts")


def _build_options_block(scenario: LoadTestScenario) -> str:
    """Build the k6 options block from a scenario definition."""
    if scenario.ramp_stages:
        stages_js = json.dumps(scenario.ramp_stages, indent=4)
        options = f"""export const options = {{
  stages: {stages_js},
  thresholds: {json.dumps(scenario.thresholds, indent=4)},
}};"""
    else:
        options = f"""export const options = {{
  vus: {scenario.vus},
  duration: '{scenario.duration}',
  thresholds: {json.dumps(scenario.thresholds, indent=4)},
}};"""
    return options


def generate_k6_script(scenario: LoadTestScenario) -> str:
    """Generate a k6 JavaScript test script from a load test scenario."""
    options_block = _build_options_block(scenario)

    headers_js = json.dumps(scenario.headers) if scenario.headers else "{}"

    method = scenario.method.value
    if method == "GET":
        request_code = f"""  const res = http.get('{scenario.target_url}', {{ headers: {headers_js} }});"""
    elif method == "POST":
        request_code = f"""  const res = http.post('{scenario.target_url}', null, {{ headers: {headers_js} }});"""
    else:
        request_code = f"""  const res = http.request('{method}', '{scenario.target_url}', null, {{ headers: {headers_js} }});"""

    script = f"""import http from 'k6/http';
import {{ check, sleep }} from 'k6';
import {{ Rate, Trend }} from 'k6/metrics';

const errorRate = new Rate('errors');
const responseTrend = new Trend('response_time_trend');

{options_block}

export default function () {{
{request_code}

  check(res, {{
    'status is 200': (r) => r.status === 200,
    'response time < 500ms': (r) => r.timings.duration < 500,
  }});

  errorRate.add(res.status !== 200);
  responseTrend.add(res.timings.duration);

  sleep(0.1);
}}
"""
    return script


def save_k6_script(scenario: LoadTestScenario) -> str:
    """Generate and save a k6 script to disk. Returns the file path."""
    os.makedirs(K6_SCRIPTS_DIR, exist_ok=True)

    script_content = generate_k6_script(scenario)
    safe_name = scenario.name.lower().replace(" ", "_").replace("/", "_")
    filename = f"{safe_name}_{scenario.id[:8]}.js"
    filepath = os.path.join(K6_SCRIPTS_DIR, filename)

    with open(filepath, "w") as f:
        f.write(script_content)

    return filepath

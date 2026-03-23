from __future__ import annotations

import json
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from models.schemas import LoadTestPreset, LoadTestScenario

K6_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "k6-scripts")

DEFAULT_THRESHOLDS: dict[str, list[str]] = {
    "http_req_duration": ["p(95)<2000"],
    "http_req_failed": ["rate<0.05"],
}

_PRESET_CONFIGS: dict[str, dict[str, object]] = {
    "smoke": {
        "vus": 1,
        "duration": "20s",
        "ramp_stages": [],
        "thresholds": {
            "http_req_duration": ["p(95)<1200"],
            "http_req_failed": ["rate<0.01"],
        },
    },
    "load": {
        "vus": 10,
        "duration": "1m",
        "ramp_stages": [],
        "thresholds": {
            "http_req_duration": ["p(95)<1800"],
            "http_req_failed": ["rate<0.03"],
        },
    },
    "stress": {
        "vus": 0,
        "duration": "0s",
        "ramp_stages": [
            {"duration": "30s", "target": 20},
            {"duration": "30s", "target": 50},
            {"duration": "30s", "target": 0},
        ],
        "thresholds": {
            "http_req_duration": ["p(95)<3000"],
            "http_req_failed": ["rate<0.10"],
        },
    },
}


def load_test_preset_config(preset: LoadTestPreset | str) -> dict[str, object]:
    key = preset.value if isinstance(preset, LoadTestPreset) else str(preset).strip().lower()
    if key not in _PRESET_CONFIGS:
        raise ValueError(f"Unknown load test preset: {preset}")
    return json.loads(json.dumps(_PRESET_CONFIGS[key]))


def get_all_load_test_presets() -> dict[str, dict[str, object]]:
    return json.loads(json.dumps(_PRESET_CONFIGS))


def _normalize_thresholds(thresholds: dict[str, list[str]]) -> dict[str, list[str]]:
    if not thresholds:
        return json.loads(json.dumps(DEFAULT_THRESHOLDS))
    normalized: dict[str, list[str]] = {}
    for metric, values in thresholds.items():
        normalized[str(metric)] = [str(v) for v in values]
    for metric, values in DEFAULT_THRESHOLDS.items():
        normalized.setdefault(metric, list(values))
    return normalized


def _normalize_query_params(url: str, query_params: dict[str, object]) -> str:
    if not query_params:
        return url

    split = urlsplit(url)
    merged: dict[str, list[str]] = {}
    for key, value in parse_qsl(split.query, keep_blank_values=True):
        merged.setdefault(key, []).append(value)

    for key, value in query_params.items():
        if isinstance(value, list):
            merged[str(key)] = [str(item) for item in value]
        else:
            merged[str(key)] = [str(value)]

    encoded_query = urlencode(merged, doseq=True)
    return urlunsplit((split.scheme, split.netloc, split.path, encoded_query, split.fragment))


def _build_request_parts(scenario: LoadTestScenario) -> tuple[str, dict[str, str], str]:
    url = _normalize_query_params(scenario.target_url, scenario.query_params)

    headers = {str(k): str(v) for k, v in (scenario.headers or {}).items()}
    body_literal = "null"
    if scenario.body is not None:
        body_literal = json.dumps(scenario.body)
        if not isinstance(scenario.body, str):
            if "content-type" not in {k.lower() for k in headers}:
                headers["Content-Type"] = "application/json"

    return url, headers, body_literal


def _build_options_block(scenario: LoadTestScenario) -> str:
    """Build the k6 options block from a scenario definition."""
    thresholds = _normalize_thresholds(scenario.thresholds)
    if scenario.ramp_stages:
        stages_js = json.dumps(scenario.ramp_stages, indent=4)
        options = f"""export const options = {{
  stages: {stages_js},
  thresholds: {json.dumps(thresholds, indent=4)},
}};"""
    else:
        options = f"""export const options = {{
  vus: {scenario.vus},
  duration: '{scenario.duration}',
  thresholds: {json.dumps(thresholds, indent=4)},
}};"""
    return options


def generate_k6_script(scenario: LoadTestScenario) -> str:
    """Generate a k6 JavaScript test script from a load test scenario."""
    options_block = _build_options_block(scenario)

    target_url, headers, body_literal = _build_request_parts(scenario)
    headers_js = json.dumps(headers, ensure_ascii=True)
    expected_statuses = scenario.expected_statuses or [200]
    expected_statuses_js = json.dumps(expected_statuses, ensure_ascii=True)
    method = scenario.method.value

    script = f"""import http from 'k6/http';
import {{ check, sleep }} from 'k6';
import {{ Rate, Trend }} from 'k6/metrics';

const errorRate = new Rate('errors');
const responseTrend = new Trend('response_time_trend');

{options_block}

export default function () {{
  const expectedStatuses = {expected_statuses_js};
  const method = '{method}';
  const url = {json.dumps(target_url, ensure_ascii=True)};
  const params = {{ headers: {headers_js} }};
  const body = {body_literal};
  const res = http.request(method, url, body, params);

  check(res, {{
    'status is expected': (r) => expectedStatuses.includes(r.status),
    'response time < 5000ms': (r) => r.timings.duration < 5000,
  }});

  errorRate.add(!expectedStatuses.includes(res.status));
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

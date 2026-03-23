from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loadtest.k6_generator import generate_k6_script, load_test_preset_config  # noqa: E402
from models.schemas import HttpMethod, LoadTestScenario  # noqa: E402


class LoadTestK6GeneratorTests(TestCase):
    def test_generate_k6_script_renders_query_body_and_expected_statuses(self) -> None:
        scenario = LoadTestScenario(
            id="scenario-1",
            name="Search scenario",
            description="",
            target_url="https://api.example.com/search",
            method=HttpMethod.POST,
            headers={"Authorization": "Bearer abc"},
            query_params={"q": "cats", "tags": ["a", "b"]},
            body={"includeArchived": False},
            expected_statuses=[200, 201],
            vus=5,
            duration="30s",
            ramp_stages=[],
            thresholds={"http_req_duration": ["p(95)<1500"]},
        )

        script = generate_k6_script(scenario)

        self.assertIn("expectedStatuses = [200, 201]", script)
        self.assertIn("http.request(method, url, body, params)", script)
        self.assertIn("https://api.example.com/search?q=cats&tags=a&tags=b", script)
        self.assertIn('"Content-Type": "application/json"', script)
        self.assertIn('const method = \'POST\';', script)

    def test_generate_k6_script_uses_custom_expected_statuses_without_200(self) -> None:
        scenario = LoadTestScenario(
            id="scenario-2",
            name="Create scenario",
            target_url="https://api.example.com/items",
            method=HttpMethod.POST,
            expected_statuses=[201, 202],
        )

        script = generate_k6_script(scenario)
        self.assertIn("expectedStatuses = [201, 202]", script)
        self.assertNotIn("expectedStatuses = [200]", script)

    def test_preset_expansion_contains_expected_defaults(self) -> None:
        smoke = load_test_preset_config("smoke")
        load = load_test_preset_config("load")
        stress = load_test_preset_config("stress")

        self.assertEqual(smoke["vus"], 1)
        self.assertEqual(load["vus"], 10)
        self.assertTrue(isinstance(stress["ramp_stages"], list))
        self.assertIn("http_req_duration", smoke["thresholds"])
        self.assertIn("http_req_failed", load["thresholds"])

    def test_unknown_preset_raises(self) -> None:
        with self.assertRaises(ValueError):
            load_test_preset_config("unknown")

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from models.schemas import LoadTestMetrics  # noqa: E402


def _scenario_payload(name: str, target_url: str, headers: dict[str, str] | None = None) -> dict:
    return {
        "name": name,
        "description": "route-test",
        "target_url": target_url,
        "method": "GET",
        "vus": 5,
        "duration": "20s",
        "ramp_stages": [],
        "thresholds": {
            "http_req_duration": ["p(95)<1500"],
            "http_req_failed": ["rate<0.05"],
        },
        "headers": headers or {"X-Scenario": "yes"},
        "query_params": {"status": "active"},
        "body": None,
        "expected_statuses": [200],
    }


def _metrics_for(scenario_id: str, scenario_name: str) -> LoadTestMetrics:
    return LoadTestMetrics(
        id=str(uuid.uuid4()),
        scenario_id=scenario_id,
        scenario_name=scenario_name,
        total_requests=100,
        failed_requests=0,
        avg_response_time_ms=120,
        min_response_time_ms=20,
        max_response_time_ms=240,
        p50_ms=90,
        p90_ms=180,
        p95_ms=210,
        p99_ms=230,
        requests_per_second=8.5,
        error_rate=0.0,
        data_received_kb=150,
        data_sent_kb=50,
        duration_seconds=20,
        vus_max=5,
        runner_status="passed",
        runner_message="ok",
        runner_exit_code=0,
        runner_stdout_excerpt="stdout",
        runner_stderr_excerpt="",
        raw_metrics={"summary": True},
    )


class LoadTestRouteTests(TestCase):
    def test_profiles_endpoint_returns_profiles_and_presets(self) -> None:
        profiles_json = json.dumps(
            [
                {
                    "id": "staging",
                    "name": "Staging",
                    "base_url": "https://staging.example.com",
                    "default_headers": {"Authorization": "Bearer ${LOADTEST_TOKEN}"},
                }
            ]
        )
        with patch.dict(
            os.environ,
            {
                "LOADTEST_PROFILES_JSON": profiles_json,
                "LOADTEST_TOKEN": "token",
            },
            clear=False,
        ):
            with TestClient(main.app) as client:
                res = client.get("/api/loadtest/profiles")

        self.assertEqual(res.status_code, 200)
        payload = res.json()
        self.assertIn("profiles", payload)
        self.assertIn("presets", payload)
        self.assertTrue(any(profile["id"] == "staging" for profile in payload["profiles"]))
        self.assertIn("smoke", payload["presets"])
        self.assertIn("load", payload["presets"])
        self.assertIn("stress", payload["presets"])

    def test_loadtest_scenario_crud_run_and_result_contracts(self) -> None:
        scenario_name = f"CRUD Scenario {uuid.uuid4()}"

        with TestClient(main.app) as client:
            create_res = client.post(
                "/api/loadtest/scenarios",
                json=_scenario_payload(scenario_name, "https://scenario.example.com/orders"),
            )
            self.assertEqual(create_res.status_code, 200)
            created = create_res.json()
            scenario_id = created["id"]
            self.assertEqual(created["query_params"], {"status": "active"})
            self.assertEqual(created["expected_statuses"], [200])

            list_res = client.get("/api/loadtest/scenarios?include_history=true")
            self.assertEqual(list_res.status_code, 200)
            self.assertTrue(any(item["id"] == scenario_id for item in list_res.json()))

            mismatched = _scenario_payload(scenario_name, "https://scenario.example.com/orders")
            mismatched["id"] = "different-id"
            bad_update_res = client.put(f"/api/loadtest/scenarios/{scenario_id}", json=mismatched)
            self.assertEqual(bad_update_res.status_code, 400)

            update_payload = _scenario_payload(
                f"{scenario_name} Updated",
                "https://scenario.example.com/orders",
                headers={"X-Scenario": "updated"},
            )
            update_payload["query_params"] = {"status": "inactive", "page": 2}
            update_payload["expected_statuses"] = [200, 204]
            update_res = client.put(f"/api/loadtest/scenarios/{scenario_id}", json=update_payload)
            self.assertEqual(update_res.status_code, 200)
            updated = update_res.json()
            self.assertEqual(updated["name"], f"{scenario_name} Updated")
            self.assertEqual(updated["expected_statuses"], [200, 204])
            self.assertEqual(updated["query_params"], {"status": "inactive", "page": 2})

            with patch(
                "main.run_k6_test",
                side_effect=lambda scenario: _metrics_for(scenario.id, scenario.name),
            ):
                run_res = client.post(
                    "/api/loadtest/run",
                    json={"scenario_ids": [scenario_id]},
                )

            self.assertEqual(run_res.status_code, 200)
            run_payload = run_res.json()
            self.assertEqual(run_payload["total_scenarios"], 1)
            self.assertEqual(run_payload["passed"], 1)
            self.assertEqual(run_payload["failed"], 0)
            self.assertEqual(run_payload["errors"], 0)
            self.assertEqual(run_payload["results"][0]["runner_status"], "passed")
            result_id = run_payload["results"][0]["id"]

            list_results_res = client.get("/api/loadtest/results")
            self.assertEqual(list_results_res.status_code, 200)
            listed = next(
                item for item in list_results_res.json() if item["id"] == result_id
            )
            self.assertEqual(listed["runner_message"], "ok")
            self.assertNotIn("raw_metrics", listed)

            detail_res = client.get(f"/api/loadtest/results/{result_id}")
            self.assertEqual(detail_res.status_code, 200)
            detail = detail_res.json()
            self.assertEqual(detail["runner_status"], "passed")
            self.assertIn("raw_metrics", detail)

            delete_res = client.delete(f"/api/loadtest/scenarios/{scenario_id}")
            self.assertEqual(delete_res.status_code, 200)
            self.assertTrue(delete_res.json()["deleted"])

    def test_run_precedence_profile_and_overrides(self) -> None:
        scenario_name = f"Precedence Scenario {uuid.uuid4()}"
        profiles_json = json.dumps(
            [
                {
                    "id": "staging",
                    "name": "Staging",
                    "base_url": "https://profile.example.com/api",
                    "default_headers": {
                        "Authorization": "Bearer ${LOADTEST_TOKEN}",
                        "X-Profile": "profile",
                    },
                }
            ]
        )

        with TestClient(main.app) as client:
            create_res = client.post(
                "/api/loadtest/scenarios",
                json=_scenario_payload(
                    scenario_name,
                    "https://scenario.example.com/orders",
                    headers={
                        "Authorization": "Bearer scenario",
                        "X-Scenario": "scenario",
                    },
                ),
            )
            self.assertEqual(create_res.status_code, 200)
            scenario_id = create_res.json()["id"]

            captured: dict[str, object] = {}

            def _fake_run(scenario):
                captured["scenario"] = scenario
                return _metrics_for(scenario.id, scenario.name)

            with patch.dict(
                os.environ,
                {
                    "LOADTEST_PROFILES_JSON": profiles_json,
                    "LOADTEST_TOKEN": "profile-token",
                },
                clear=False,
            ):
                with patch("main.run_k6_test", side_effect=_fake_run):
                    run_res = client.post(
                        "/api/loadtest/run",
                        json={
                            "scenario_ids": [scenario_id],
                            "profile_id": "staging",
                            "target_base_url": "https://override.example.com/base",
                            "headers_override": {
                                "Authorization": "Bearer override",
                                "X-Override": "override",
                            },
                        },
                    )

            self.assertEqual(run_res.status_code, 200)
            effective = captured.get("scenario")
            self.assertIsNotNone(effective)
            assert effective is not None
            self.assertEqual(effective.target_url, "https://override.example.com/orders")
            self.assertEqual(effective.headers["Authorization"], "Bearer override")
            self.assertEqual(effective.headers["X-Scenario"], "scenario")
            self.assertEqual(effective.headers["X-Profile"], "profile")
            self.assertEqual(effective.headers["X-Override"], "override")

    def test_run_fails_for_unresolved_profile_placeholders(self) -> None:
        scenario_name = f"Profile Error Scenario {uuid.uuid4()}"
        profiles_json = json.dumps(
            [
                {
                    "id": "broken",
                    "name": "Broken",
                    "base_url": "https://profile.example.com/api",
                    "default_headers": {
                        "Authorization": "Bearer ${MISSING_SECRET}",
                    },
                }
            ]
        )

        with TestClient(main.app) as client:
            create_res = client.post(
                "/api/loadtest/scenarios",
                json=_scenario_payload(scenario_name, "https://scenario.example.com/orders"),
            )
            self.assertEqual(create_res.status_code, 200)
            scenario_id = create_res.json()["id"]

            with patch.dict(
                os.environ,
                {
                    "LOADTEST_PROFILES_JSON": profiles_json,
                },
                clear=False,
            ):
                run_res = client.post(
                    "/api/loadtest/run",
                    json={
                        "scenario_ids": [scenario_id],
                        "profile_id": "broken",
                    },
                )

            self.assertEqual(run_res.status_code, 400)
            detail = run_res.json()["detail"]
            self.assertEqual(detail["profile_id"], "broken")
            self.assertIn("MISSING_SECRET", detail["missing_env_vars"])

    def test_run_latest_batch_fallback_without_scenario_ids(self) -> None:
        scenario_name = f"Latest Batch Scenario {uuid.uuid4()}"

        with TestClient(main.app) as client:
            create_res = client.post(
                "/api/loadtest/scenarios",
                json=_scenario_payload(scenario_name, "https://scenario.example.com/orders"),
            )
            self.assertEqual(create_res.status_code, 200)

            with patch(
                "main.run_k6_test",
                side_effect=lambda scenario: _metrics_for(scenario.id, scenario.name),
            ) as mocked_run:
                run_res = client.post("/api/loadtest/run", json={})

            self.assertEqual(run_res.status_code, 200)
            payload = run_res.json()
            self.assertGreaterEqual(payload["total_scenarios"], 1)
            self.assertGreaterEqual(mocked_run.call_count, 1)

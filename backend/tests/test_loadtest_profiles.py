from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loadtest.profiles import (  # noqa: E402
    LoadProfileResolutionError,
    get_load_test_profiles,
    resolve_profile_headers,
)


class LoadTestProfileTests(TestCase):
    def test_default_profile_when_env_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            profiles = get_load_test_profiles()

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].id, "local")
        self.assertEqual(profiles[0].base_url, "http://localhost:8080")

    def test_resolve_profile_headers_with_env_placeholders(self) -> None:
        profiles_json = json.dumps(
            [
                {
                    "id": "staging",
                    "name": "Staging",
                    "base_url": "https://staging.example.com",
                    "default_headers": {
                        "Authorization": "Bearer ${LOADTEST_TOKEN}",
                        "X-Tenant": "tenant-a",
                    },
                }
            ]
        )
        with patch.dict(
            os.environ,
            {
                "LOADTEST_PROFILES_JSON": profiles_json,
                "LOADTEST_TOKEN": "secret-token",
            },
            clear=True,
        ):
            profiles = get_load_test_profiles()
            resolved = resolve_profile_headers(profiles[0])

        self.assertEqual(resolved["Authorization"], "Bearer secret-token")
        self.assertEqual(resolved["X-Tenant"], "tenant-a")

    def test_resolve_profile_headers_fails_on_unresolved_env(self) -> None:
        profiles_json = json.dumps(
            [
                {
                    "id": "prod",
                    "name": "Prod",
                    "base_url": "https://prod.example.com",
                    "default_headers": {
                        "Authorization": "Bearer ${MISSING_TOKEN}",
                    },
                }
            ]
        )
        with patch.dict(
            os.environ,
            {
                "LOADTEST_PROFILES_JSON": profiles_json,
            },
            clear=True,
        ):
            profiles = get_load_test_profiles()
            with self.assertRaises(LoadProfileResolutionError) as ctx:
                resolve_profile_headers(profiles[0])

        self.assertEqual(ctx.exception.profile_id, "prod")
        self.assertIn("MISSING_TOKEN", ctx.exception.missing_env_vars)

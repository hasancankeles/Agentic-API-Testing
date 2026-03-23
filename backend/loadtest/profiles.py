from __future__ import annotations

import json
import os
import re
from typing import Any

from models.schemas import LoadTestProfile

_PLACEHOLDER_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")
_PROFILES_ENV = "LOADTEST_PROFILES_JSON"


class LoadProfileResolutionError(ValueError):
    def __init__(self, profile_id: str, missing_env_vars: list[str]):
        self.profile_id = profile_id
        self.missing_env_vars = missing_env_vars
        msg = f"Missing environment variables for profile '{profile_id}': {', '.join(missing_env_vars)}"
        super().__init__(msg)


def _default_profile() -> LoadTestProfile:
    return LoadTestProfile(
        id="local",
        name="Local",
        base_url="http://localhost:8080",
        default_headers={},
    )


def get_load_test_profiles() -> list[LoadTestProfile]:
    raw = os.getenv(_PROFILES_ENV, "").strip()
    if not raw:
        return [_default_profile()]

    payload: Any = json.loads(raw)
    profile_items: list[Any]
    if isinstance(payload, dict):
        maybe_profiles = payload.get("profiles")
        if isinstance(maybe_profiles, list):
            profile_items = maybe_profiles
        else:
            profile_items = [payload]
    elif isinstance(payload, list):
        profile_items = payload
    else:
        raise ValueError(f"{_PROFILES_ENV} must be a JSON list or object")

    profiles: list[LoadTestProfile] = []
    seen_ids: set[str] = set()
    for item in profile_items:
        profile = LoadTestProfile.model_validate(item)
        if profile.id in seen_ids:
            raise ValueError(f"Duplicate load profile id: {profile.id}")
        seen_ids.add(profile.id)
        profiles.append(profile)

    if not profiles:
        return [_default_profile()]
    return profiles


def resolve_profile_headers(profile: LoadTestProfile) -> dict[str, str]:
    missing_env_vars: set[str] = set()

    def _resolve_value(value: str) -> str:
        def _replace(match: re.Match[str]) -> str:
            env_name = match.group(1)
            env_val = os.getenv(env_name)
            if env_val is None:
                missing_env_vars.add(env_name)
                return match.group(0)
            return env_val

        return _PLACEHOLDER_RE.sub(_replace, value)

    resolved = {
        key: _resolve_value(str(value))
        for key, value in (profile.default_headers or {}).items()
    }
    if missing_env_vars:
        raise LoadProfileResolutionError(
            profile_id=profile.id,
            missing_env_vars=sorted(missing_env_vars),
        )
    return resolved


__all__ = [
    "LoadProfileResolutionError",
    "get_load_test_profiles",
    "resolve_profile_headers",
]

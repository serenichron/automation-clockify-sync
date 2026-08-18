"""Pure provider environment-file discovery and merge helpers."""
from __future__ import annotations

import os
from pathlib import Path
import pwd
from typing import Any, Mapping, Sequence


def home_candidates() -> list[Path]:
    homes = [Path.home()]
    try:
        homes.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except (KeyError, OSError):
        pass
    return list(dict.fromkeys(homes))


def calendly_env_candidates(
    environment: Mapping[str, str] = os.environ,
) -> list[str]:
    candidates = [environment.get("CALENDLY_ENV_FILE", "")]
    candidates.extend(
        str(home / ".config/serenichron/calendly.env")
        for home in home_candidates()
    )
    return list(dict.fromkeys(candidates))


def load_env_file(
    candidates: Sequence[str], required_keys: Sequence[str],
) -> dict[str, Any]:
    values: dict[str, str] = {}
    used: str | None = None
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            used = candidate
            for raw in Path(candidate).read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
            break
    missing = [key for key in required_keys if not values.get(key)]
    return {"_env_file": used or "missing", "_missing": missing, **values}


def merged_provider_environment(
    file_values: Mapping[str, Any],
    environment: Mapping[str, str],
    required_keys: Sequence[str],
) -> dict[str, Any]:
    merged = {
        str(key): str(value)
        for key, value in file_values.items()
        if key not in {"_env_file", "_missing"} and str(value)
    }
    for key in required_keys:
        if environment.get(key):
            merged[key] = environment[key]
    missing = [key for key in required_keys if not merged.get(key)]
    return {
        "_env_file": file_values.get("_env_file", "missing"),
        "_missing": missing,
        **merged,
    }

"""Runtime toggles persisted on the data PVC (<intake>/.omi-settings.json).

Env vars only provide defaults for keys never set in the file:
  llm_enabled      OMI_LLM_ENABLED      (default true)  qwen title/summary/action items
  publish_enabled  OMI_PUBLISH_ENABLED  (default true)  wiki page + Todoist + index
Changed from /review (POST, SSO + CSRF). Read on every use, so changes apply without restart.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

NAME = ".omi-settings.json"
KEYS = {"llm_enabled": "OMI_LLM_ENABLED", "publish_enabled": "OMI_PUBLISH_ENABLED"}
_lock = threading.Lock()
_dir: Path | None = None


def configure(intake: Path) -> None:
    global _dir
    _dir = Path(intake)


def _env_bool(var: str, default: bool = True) -> bool:
    v = os.environ.get(var)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")


def _read() -> dict:
    if _dir is None:
        return {}
    try:
        return json.loads((_dir / NAME).read_text())
    except (OSError, ValueError):
        return {}


def get(key: str) -> bool:
    d = _read()
    if isinstance(d.get(key), bool):
        return d[key]
    return _env_bool(KEYS[key])


def all_settings() -> dict:
    d = _read()
    return {k: get(k) for k in KEYS} | {"updated_by": d.get("updated_by"), "updated_at": d.get("updated_at")}


def set_values(values: dict, user: str | None) -> dict:
    if _dir is None:
        raise RuntimeError("settings not configured")
    with _lock:
        d = _read()
        for k, v in values.items():
            if k in KEYS and isinstance(v, bool):
                d[k] = v
        d.update(updated_by=user, updated_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        p = _dir / NAME
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2, sort_keys=True))
        tmp.replace(p)
    return all_settings()


def csrf_token() -> str:
    """Per-install secret stored in the settings file; required in the settings form."""
    if _dir is None:
        raise RuntimeError("settings not configured")
    with _lock:
        d = _read()
        if not d.get("csrf"):
            d["csrf"] = secrets.token_urlsafe(24)
            p = _dir / NAME
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=2, sort_keys=True))
            tmp.replace(p)
        return d["csrf"]

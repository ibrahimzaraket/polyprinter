"""Config loading: config.yaml (committed defaults) overlaid by
data/config-overrides.yaml (gitignored, hand-edited today — eventually
written by the Telegram /set command; the file may simply not exist yet,
that's fine).

Overrides live under data/, not the repo root, on purpose (corrected
2026-08-08, before this mechanism had ever actually been exercised): data/
is the one path every service already bind-mounts (docker-compose.yml),
so an edit here takes effect on the very next load_config() call — no
image rebuild, no container recreate. config.yaml itself stays baked into
the image (COPY config.yaml ./ in the Dockerfile) because it's committed
defaults meant to ship with a given version of the code; overrides are
operator state, which belongs in the one place that's actually live.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# Deliberately NOT Path(__file__).resolve().parents[1]: once this package is
# pip-installed, __file__ lives under site-packages, nowhere near
# config.yaml (which ships at the repo/app root, not inside the package).
# POLYPRINTER_HOME is set explicitly in the Dockerfile; falling back to cwd
# matches local dev, where you run things from the repo root.
REPO_ROOT = Path(os.environ.get("POLYPRINTER_HOME", Path.cwd()))
DEFAULTS_PATH = REPO_ROOT / "config.yaml"
OVERRIDES_PATH = REPO_ROOT / "data" / "config-overrides.yaml"


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config() -> dict[str, Any]:
    config: dict[str, Any] = {}
    if DEFAULTS_PATH.exists():
        config = yaml.safe_load(DEFAULTS_PATH.read_text()) or {}
    if OVERRIDES_PATH.exists():
        overrides = yaml.safe_load(OVERRIDES_PATH.read_text()) or {}
        config = _deep_merge(config, overrides)
    return config

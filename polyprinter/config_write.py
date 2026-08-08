"""Programmatic edits to data/config-overrides.yaml — the dashboard's one
write target for "who's tailed" (mirror.pinned_addresses). The dashboard's
new write routes (dashboard/server.py's /traders/<address>/tail etc.) are
this module's only callers; Scout/Mirror only ever read this file via
config.load_config(), never write it.

Round-trips through plain PyYAML, not ruamel — this project already
depends on PyYAML for config.py's read side, and a full rewrite is simple
to reason about. The tradeoff, accepted on purpose: a hand-written comment
in this file doesn't survive a write from here, since the whole file gets
regenerated, not patched. Fine once any address has been added through the
dashboard — from that point on it's operator/dashboard-managed, not a
hand-curated document.
"""

from __future__ import annotations

import yaml

from polyprinter import config as config_module


def _load() -> dict:
    path = config_module.OVERRIDES_PATH
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _save(data: dict) -> None:
    path = config_module.OVERRIDES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def is_pinned(address: str) -> bool:
    address = address.lower()
    pinned = _load().get("mirror", {}).get("pinned_addresses", [])
    return address in {a.lower() for a in pinned}


def add_pinned(address: str) -> None:
    address = address.lower()
    data = _load()
    mirror = data.setdefault("mirror", {})
    pinned = mirror.setdefault("pinned_addresses", [])
    if address not in {a.lower() for a in pinned}:
        pinned.append(address)
    _save(data)


def remove_pinned(address: str) -> None:
    address = address.lower()
    data = _load()
    pinned = data.get("mirror", {}).get("pinned_addresses", [])
    if not pinned:
        return
    data["mirror"]["pinned_addresses"] = [a for a in pinned if a.lower() != address]
    _save(data)

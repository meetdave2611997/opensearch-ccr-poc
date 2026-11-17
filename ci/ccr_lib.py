"""
Shared helpers for the CI scripts.

dr-switchover.py has a hyphen in its name, so it cannot be imported with a normal
`import`. load_drs() loads it with importlib so the CI scripts reuse its config
loading, index/rule parsing and OpenSearch API helpers instead of duplicating them.
"""

import importlib.util
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_drs():
    """Import dr-switchover.py as a module (cached in sys.modules)."""
    if "dr_switchover" in sys.modules:
        return sys.modules["dr_switchover"]
    path = REPO_ROOT / "dr-switchover.py"
    spec = importlib.util.spec_from_file_location("dr_switchover", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["dr_switchover"] = module
    spec.loader.exec_module(module)
    return module


def disable_colour(drs) -> None:
    for attr in ("RED", "GREEN", "YELLOW", "CYAN", "BOLD", "RESET"):
        setattr(drs.C, attr, "")


def load_entries(drs, indices_file: str) -> list[dict]:
    """Raw index entries from indices.json."""
    return drs._load_indices_data(indices_file).get("indices", [])


def wildcard_prefix(pattern: str) -> str:
    """'metrics-idx*' -> 'metrics-idx'; used to build concrete names matching a pattern."""
    return pattern.split("*")[0].split("?")[0].rstrip("-_")


def wait_until(check, timeout: float, interval: float = 5.0, label: str = "") -> bool:
    """Call check() until it returns True or timeout seconds pass."""
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        if label:
            print(f"[WAIT]  {label} — attempt {attempt}, retrying in {interval:.0f}s")
        time.sleep(interval)

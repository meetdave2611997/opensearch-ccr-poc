#!/usr/bin/env python3
"""
OpenSearch DR Switchover Script
================================
Designed for AWS OpenSearch where cross-cluster connections are
pre-configured via the AWS console/API. This script does NOT register
remote clusters — it assumes the connection aliases already exist on
both clusters before running.

Usage:
  python3 dr-switchover.py                        # interactive menu
  python3 dr-switchover.py --operation failover   # run failover non-interactively
  python3 dr-switchover.py --operation failback   # run failback non-interactively
  python3 dr-switchover.py --operation status     # show current replication state

  python3 dr-switchover.py --config path/to/config.json
  python3 dr-switchover.py --dry-run --operation failover

  Auto-follow is config-driven: set "autofollow": true on any index entry in
  indices.json and the script will automatically manage replication rules for
  that index during failover and failback. No separate CLI operation needed.

Configuration precedence (highest → lowest):
  1. CLI flags
  2. Environment variables  (OPENSEARCH_PRIMARY_URL, etc.)
  3. config.json
  4. Built-in defaults (localhost for local Docker testing)
"""

import argparse
import fnmatch
import json
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import requests
    from requests import Response
except ImportError:
    print("ERROR: 'requests' is required.  pip install requests")
    sys.exit(1)


# ── Terminal colours ───────────────────────────────────────────────────────

class _Colour:
    RED    = "\033[0;31m"
    GREEN  = "\033[0;32m"
    YELLOW = "\033[1;33m"
    CYAN   = "\033[0;36m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"

C = _Colour()

def info(msg: str)    -> None: print(f"{C.CYAN}[INFO]{C.RESET}  {msg}")
def ok(msg: str)      -> None: print(f"{C.GREEN}[ OK ]{C.RESET}  {msg}")
def warn(msg: str)    -> None: print(f"{C.YELLOW}[WARN]{C.RESET}  {msg}")
def err(msg: str)     -> None: print(f"{C.RED}[ERR ]{C.RESET}  {msg}", file=sys.stderr)
def dry(msg: str)     -> None: print(f"{C.YELLOW}[DRY-RUN]{C.RESET}  {msg}")
def step(msg: str)    -> None: print(f"\n{C.BOLD}▶  {msg}{C.RESET}")
def header(msg: str)  -> None:
    bar = "═" * 52
    print(f"\n{C.BOLD}{C.CYAN}{bar}\n  {msg}\n{bar}{C.RESET}\n")


# ── Fake HTTP response used during dry-run ─────────────────────────────────

class _FakeResponse:
    """Returned by _request() instead of making a real HTTP call in dry-run mode."""
    status_code: int = 200
    text: str = "dry-run"

    def json(self) -> dict:
        return {"acknowledged": True}


# ── Configuration dataclass ────────────────────────────────────────────────

@dataclass
class Config:
    primary_url: str   = "http://localhost:9200"
    secondary_url: str = "http://localhost:9201"
    indices_file: str  = "./indices.json"

    # AWS pre-configured connection aliases.
    # primary_alias_on_secondary : alias visible from SECONDARY that points to PRIMARY.
    #   Used during FAILBACK to start normal replication (primary → secondary).
    primary_alias_on_secondary: str = "primary-cluster"

    # secondary_alias_on_primary : alias visible from PRIMARY that points to SECONDARY.
    #   Used during FAILOVER to start reverse replication (secondary → primary).
    secondary_alias_on_primary: str = "secondary-cluster"

    # Optional basic-auth credentials (not required for local Docker / security-disabled)
    username: str = ""
    password: str = ""

    # Set to False when using self-signed certs in non-production environments
    verify_ssl: bool = True

    # Retry settings for start_replication (exponential backoff)
    retry_max_attempts: int   = 3
    retry_base_delay: float   = 2.0

    # When True, mutating API calls (POST/PUT/DELETE) are logged but never executed
    dry_run: bool = False

    def auth(self) -> Optional[tuple]:
        if self.username and self.password:
            return (self.username, self.password)
        return None


def load_config(config_path: Optional[str], cli_overrides: dict) -> Config:
    """Build Config from file + env vars + CLI overrides (in priority order)."""
    import os

    cfg = Config()

    if config_path and Path(config_path).exists():
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
        for key, value in data.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)

    env_map = {
        "OPENSEARCH_PRIMARY_URL":     "primary_url",
        "OPENSEARCH_SECONDARY_URL":   "secondary_url",
        "OPENSEARCH_INDICES_FILE":    "indices_file",
        "OPENSEARCH_PRIMARY_ALIAS":   "primary_alias_on_secondary",
        "OPENSEARCH_SECONDARY_ALIAS": "secondary_alias_on_primary",
        "OPENSEARCH_USERNAME":        "username",
        "OPENSEARCH_PASSWORD":        "password",
        "OPENSEARCH_RETRY_MAX":       "retry_max_attempts",
        "OPENSEARCH_RETRY_DELAY":     "retry_base_delay",
    }
    for env_key, attr in env_map.items():
        if env_key in os.environ:
            val = os.environ[env_key]
            # coerce numeric env vars
            if attr == "retry_max_attempts":
                val = int(val)
            elif attr == "retry_base_delay":
                val = float(val)
            setattr(cfg, attr, val)

    for attr, value in cli_overrides.items():
        if value is not None and hasattr(cfg, attr):
            setattr(cfg, attr, value)

    return cfg


def _load_indices_data(indices_file: str) -> dict:
    """Read and return the raw indices.json data dict."""
    path = Path(indices_file)
    if not path.exists():
        err(f"Indices file not found: {indices_file}")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_indices(indices_file: str) -> list[str]:
    """
    Return the list of concrete index names to manage.
    Entries without a "name" field are autofollow-only rules and are skipped here —
    they have no base index on the cluster to stop/delete/replicate explicitly.

    An empty list is valid when all entries are autofollow-only (no "name" field).
    The caller must ensure at least one autofollow rule exists in that case.
    """
    data = _load_indices_data(indices_file)
    return [
        entry["name"]
        for entry in data.get("indices", [])
        if "name" in entry and not entry.get("autofollow", False)
    ]


def load_autofollow_rules(indices_file: str) -> list[dict]:
    """
    Return auto-follow rules derived from index entries with "autofollow": true.

    Rules:
      - "autofollow_pattern" is REQUIRED when "autofollow": true.
        The pattern is never inferred from "name" — they serve different purposes:
          "name"             → OpenSearch rule identifier (no wildcards allowed)
          "autofollow_pattern" → wildcard expression matched against leader index names

    Each rule dict returned contains:
      "name"    — rule identifier sent to OpenSearch (from "name" field, or derived from
                  "autofollow_pattern" by stripping wildcard characters when "name" absent)
      "pattern" — exact value of "autofollow_pattern"

    Two supported entry shapes:
      1. Named index with explicit pattern:
           { "name": "metrics-idx", "autofollow": true, "autofollow_pattern": "metrics-idx*" }

      2. Autofollow-only entry (no concrete base index):
           { "autofollow": true, "autofollow_pattern": "metrics-idx*" }
           Rule name derived as: re.sub(r"[*?]", "", "metrics-idx*").strip("-_") → "metrics-idx"

    Entries with "autofollow": false, or with "autofollow": true but missing/empty
    "autofollow_pattern", are skipped with an error logged.
    """
    data = _load_indices_data(indices_file)
    rules = []
    for entry in data.get("indices", []):
        if not entry.get("autofollow", False):
            continue
        pattern = entry.get("autofollow_pattern", "").strip()
        if not pattern:
            label = entry.get("name", repr(entry))
            err(f"  [{label}] autofollow: true requires 'autofollow_pattern' — skipping. "
                f"(pattern is never inferred from 'name')")
            exit(1)
        # Derive a wildcard-free rule name: prefer explicit "name", otherwise strip
        # wildcard chars from the pattern so the rule name is a valid OS identifier.
        raw_name = entry.get("name") or re.sub(r"[*?]", "", pattern).strip("-_")
        rule_name = raw_name or pattern
        rules.append({"name": rule_name, "pattern": pattern})
    return rules


# ── OpenSearch API helpers ─────────────────────────────────────────────────

def _request(method: str, url: str, cfg: Config, body: Optional[dict] = None) -> Response:
    """
    Execute an HTTP request, or — when cfg.dry_run is True — log the intent
    and return a fake 200 response for any mutating method (POST/PUT/DELETE).
    GET requests are always executed so that status displays work normally.
    """
    if cfg.dry_run and method.upper() in ("POST", "PUT", "DELETE"):
        body_preview = json.dumps(body) if body else ""
        dry(f"{method.upper():6} {url}  {body_preview}")
        return _FakeResponse()  # type: ignore[return-value]

    kwargs: dict = {
        "auth":    cfg.auth(),
        "verify":  cfg.verify_ssl,
        "timeout": 30,
        "headers": {"Content-Type": "application/json"},
    }
    if body is not None:
        kwargs["json"] = body
    return requests.request(method, url, **kwargs)


def cluster_health(base_url: str, cfg: Config) -> str:
    """Return cluster health status string, or 'unreachable'."""
    try:
        r = _request("GET", f"{base_url}/_cluster/health", cfg)
        return r.json().get("status", "unknown")
    except Exception:
        return "unreachable"


def stop_replication(base_url: str, index: str, cfg: Config) -> bool:
    """POST /_plugins/_replication/<index>/_stop — returns True on success."""
    url = f"{base_url}/_plugins/_replication/{index}/_stop"
    try:
        r = _request("POST", url, cfg, body={})
        if r.status_code < 400:
            ok(f"  [{index}] replication stopped  (HTTP {r.status_code})")
            return True
        # 404 — index never existed on this cluster
        if r.status_code == 404:
            warn(f"  [{index}] index not found — replication was never started, skipping.")
            return True
        # 400 with "No replication in progress" means it was already stopped — treat as non-fatal
        if r.status_code == 400 and "No replication in progress" in r.text:
            warn(f"  [{index}] replication already stopped — skipping.")
            return True
        warn(f"  [{index}] stop returned HTTP {r.status_code}: {r.text.strip()}"
             " — may already be stopped, continuing.")
        return False
    except Exception as exc:
        err(f"  [{index}] stop replication failed: {exc}")
        return False


def remove_write_block(base_url: str, index: str, cfg: Config) -> bool:
    """PUT /<index>/_settings to remove all write / read-only blocks."""
    url = f"{base_url}/{index}/_settings"
    body = {
        "index": {
            "blocks.write":                  None,
            "blocks.read_only":              None,
            "blocks.read_only_allow_delete": None,
        }
    }
    try:
        r = _request("PUT", url, cfg, body=body)
        if r.status_code < 400:
            ok(f"  [{index}] write blocks removed  (HTTP {r.status_code})")
            return True
        if r.status_code == 404:
            warn(f"  [{index}] index not found on cluster — no write block to remove, skipping.")
            return True
        warn(f"  [{index}] remove block returned HTTP {r.status_code}: {r.text.strip()}")
        return False
    except Exception as exc:
        err(f"  [{index}] remove write block failed: {exc}")
        return False


def delete_index(base_url: str, index: str, cfg: Config) -> bool:
    """DELETE /<index> — permanently removes an index from the cluster."""
    url = f"{base_url}/{index}"
    try:
        r = _request("DELETE", url, cfg)
        if r.status_code < 400:
            ok(f"  [{index}] index deleted  (HTTP {r.status_code})")
            return True
        if r.status_code == 404:
            warn(f"  [{index}] index not found on cluster — skipping delete.")
            return True
        err(f"  [{index}] delete returned HTTP {r.status_code}: {r.text.strip()}")
        return False
    except Exception as exc:
        err(f"  [{index}] delete index failed: {exc}")
        return False


def start_replication(follower_url: str, leader_alias: str, index: str, cfg: Config) -> bool:
    """PUT /_plugins/_replication/<index>/_start on the follower cluster."""
    url = f"{follower_url}/_plugins/_replication/{index}/_start"
    body = {
        "leader_alias": leader_alias,
        "leader_index": index,
        # use_roles only needed for self-managed with security enabled;
        # AWS OpenSearch ignores it
        "use_roles": {
            "leader_cluster_role":   "all_access",
            "follower_cluster_role": "all_access",
        },
    }
    try:
        r = _request("PUT", url, cfg, body=body)
        if r.status_code < 400:
            ok(f"  [{index}] replication started  (HTTP {r.status_code})")
            return True
        err(f"  [{index}] start returned HTTP {r.status_code}: {r.text.strip()}")
        return False
    except Exception as exc:
        err(f"  [{index}] start replication failed: {exc}")
        return False


def replication_status(base_url: str, index: str, cfg: Config) -> dict:
    url = f"{base_url}/_plugins/_replication/{index}/_status"
    try:
        r = _request("GET", url, cfg)
        if r.status_code < 400:
            return r.json()
        return {"status": f"HTTP_{r.status_code}"}
    except Exception:
        return {}


def index_blocks(base_url: str, index: str, cfg: Config) -> dict:
    try:
        r = _request("GET", f"{base_url}/{index}/_settings", cfg)
        settings = r.json()
        return settings.get(index, {}).get("settings", {}).get("index", {}).get("blocks", {})
    except Exception:
        return {}


# ── Auto-follow API helpers ────────────────────────────────────────────────

def create_autofollow_rule(
    follower_url: str, leader_alias: str, rule_name: str, pattern: str, cfg: Config
) -> bool:
    """POST /_plugins/_replication/_autofollow — creates a pattern-based replication rule."""
    url = f"{follower_url}/_plugins/_replication/_autofollow"
    body = {
        "leader_alias": leader_alias,
        "name":         rule_name,
        "pattern":      pattern,
        "use_roles": {
            "leader_cluster_role":   "all_access",
            "follower_cluster_role": "all_access",
        },
    }
    try:
        r = _request("POST", url, cfg, body=body)
        if r.status_code < 400:
            ok(f"  [{rule_name}] auto-follow rule created  "
               f"(pattern={pattern}, HTTP {r.status_code})")
            return True
        err(f"  [{rule_name}] create auto-follow returned HTTP {r.status_code}: {r.text.strip()}")
        return False
    except Exception as exc:
        err(f"  [{rule_name}] create auto-follow rule failed: {exc}")
        return False


def delete_autofollow_rule(
    follower_url: str, leader_alias: str, rule_name: str, cfg: Config
) -> bool:
    """DELETE /_plugins/_replication/_autofollow — removes a replication rule by name."""
    url = f"{follower_url}/_plugins/_replication/_autofollow"
    body = {
        "leader_alias": leader_alias,
        "name":         rule_name,
    }
    try:
        r = _request("DELETE", url, cfg, body=body)
        if r.status_code < 400:
            ok(f"  [{rule_name}] auto-follow rule deleted  (HTTP {r.status_code})")
            return True
        if r.status_code == 404:
            warn(f"  [{rule_name}] auto-follow rule not found — skipping delete.")
            return True
        err(f"  [{rule_name}] delete auto-follow returned HTTP {r.status_code}: {r.text.strip()}")
        return False
    except Exception as exc:
        err(f"  [{rule_name}] delete auto-follow rule failed: {exc}")
        return False


def get_autofollow_stats(base_url: str, cfg: Config) -> dict:
    """GET /_plugins/_replication/autofollow_stats — returns rule stats or empty dict."""
    url = f"{base_url}/_plugins/_replication/autofollow_stats"
    try:
        r = _request("GET", url, cfg)
        if r.status_code < 400:
            return r.json()
        return {}
    except Exception:
        return {}


def get_existing_autofollow_rule(base_url: str, rule_name: str, cfg: Config) -> Optional[dict]:
    """
    Return the existing auto-follow rule dict for rule_name from autofollow_stats,
    or None if no rule with that name is registered on the cluster.
    The returned dict contains at minimum: {"name": ..., "pattern": ...}.
    """
    stats = get_autofollow_stats(base_url, cfg)
    for rule in stats.get("autofollow_stats", []):
        if rule.get("name") == rule_name:
            return rule
    return None


def discover_wildcard_indices(base_url: str, pattern: str, cfg: Config) -> list[str]:
    """
    Return all index names on the cluster whose name matches the given wildcard pattern.

    Uses _cat/indices with the pattern directly — OpenSearch natively supports wildcard
    index patterns there, making this more reliable than parsing follower_stats which
    may omit indices that are still bootstrapping or have been stopped.

    Returns an empty list when the pattern contains no wildcard characters (the caller
    already manages that index via the named indices list).
    """
    if "*" not in pattern and "?" not in pattern:
        return []

    # Use the _settings API with the wildcard pattern.
    # The response is a dict whose top-level keys are the matching index names —
    # stable and consistent across all OpenSearch versions, no column-parsing needed.
    # (_cat/indices with ?h=index&format=json can return plain strings instead of
    #  objects on some builds, causing silent .get() failures.)
    url = f"{base_url}/{pattern}/_settings"
    try:
        r = _request("GET", url, cfg)
        if r.status_code >= 400:
            return []
        return sorted(
            name for name in r.json().keys()
            if not name.startswith(".")
        )
    except Exception:
        return []


# ── Retry helper ───────────────────────────────────────────────────────────

def _retry_with_backoff(fn, max_attempts: int = 3, base_delay: float = 2.0) -> bool:
    """
    Call fn() up to max_attempts times. On failure, wait with exponential backoff
    plus jitter before retrying. Returns the result of fn() on success, False if
    all attempts are exhausted.
    """
    for attempt in range(max_attempts):
        result = fn()
        if result:
            return True
        if attempt < max_attempts - 1:
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            warn(f"    attempt {attempt + 1}/{max_attempts} failed — "
                 f"retrying in {delay:.1f}s...")
            time.sleep(delay)
    return False


# ── Confirmation prompts ───────────────────────────────────────────────────

def confirm(prompt: str, non_interactive: bool = False, dry_run: bool = False) -> bool:
    if dry_run:
        dry(f"(dry-run) skipping confirmation: {prompt}")
        return True
    if non_interactive:
        info(f"(non-interactive) auto-confirming: {prompt}")
        return True
    print(f"\n{C.YELLOW}?  {prompt}{C.RESET}  [y/N] ", end="", flush=True)
    answer = input().strip().lower()
    return answer in ("y", "yes")


def confirm_destructive(
    cluster_url: str,
    indices: list[str],
    non_interactive: bool = False,
    dry_run: bool = False,
) -> bool:
    """
    Explicit confirmation gate before deleting indices. Requires the operator
    to type DELETE (uppercase) so an accidental Enter can never trigger data loss.
    In dry-run mode the prompt is skipped entirely.
    """
    print(f"\n{C.RED}{C.BOLD}{'!'*54}{C.RESET}")
    print(f"{C.RED}{C.BOLD}  DESTRUCTIVE OPERATION — DATA WILL BE PERMANENTLY LOST{C.RESET}")
    print(f"{C.RED}{C.BOLD}{'!'*54}{C.RESET}")
    print(f"\n  Cluster : {C.BOLD}{cluster_url}{C.RESET}")
    print("  Action  : delete the following indices from that cluster")
    print("  Reason  : CCR replication requires the follower index to not exist\n")
    for idx in indices:
        print(f"    {C.RED}✗  {idx}{C.RESET}")
    print()
    warn("These indices will be PERMANENTLY DELETED before replication starts.")
    warn("Make sure the leader cluster is healthy and has up-to-date data.")

    if dry_run:
        dry("(dry-run) skipping destructive confirmation — no indices will be deleted.")
        return True
    if non_interactive:
        info("(non-interactive) auto-confirming destructive delete.")
        return True

    print(f"\n{C.BOLD}Type  DELETE  (uppercase) to confirm, or anything else to abort: {C.RESET}",
          end="", flush=True)
    answer = input().strip()
    if answer == "DELETE":
        return True
    warn("Aborted — indices were NOT deleted.")
    return False


# ── Core replication helpers ───────────────────────────────────────────────

def delete_and_start_replication(
    follower_url: str,
    leader_alias: str,
    indices: list[str],
    cfg: Config,
    non_interactive: bool = False,
    autofollow_rules: Optional[list[dict]] = None,
) -> bool:
    """
    Delete each index on the follower cluster (with explicit confirmation),
    then start CCR replication for each index with retry backoff.
    Returns True only if all indices were successfully started.

    Smart CCR pre-step: before deletion, each named index is checked via
    replication_status. If an index is an active CCR follower (SYNCING, BOOTSTRAPPING,
    or PAUSED) it is stopped and unblocked. Indices that are not CCR followers (e.g.
    ex-leader indices in a normal failover) are skipped silently with no output.

    Wildcard cleanup: when autofollow_rules is provided, any indices on the follower
    cluster that match a wildcard pattern (e.g. products-1, products-2) are stopped,
    unblocked, and deleted before the auto-follow rule is re-created on this cluster.
    """

    # ── Discover wildcard-managed indices that also need to be cleaned up ──
    wildcard_extra: list[str] = []
    if autofollow_rules:
        named_set = set(indices)
        for rule in autofollow_rules:
            matched = discover_wildcard_indices(follower_url, rule["pattern"], cfg)
            extra = [idx for idx in matched if idx not in named_set]
            if extra:
                wildcard_extra.extend(extra)

    # ── Destructive confirmation (shows named + wildcard indices) ──────────
    all_to_delete = indices + wildcard_extra
    if not confirm_destructive(follower_url, all_to_delete, non_interactive, dry_run=cfg.dry_run):
        info("Skipped. You can delete the indices and start replication manually later.")
        return False

    # ── Delete indices (with CCR-block fallback) ───────────────────────────
    step(f"Deleting indices on follower cluster ({follower_url})...")
    failed: list[str] = []
    for idx in all_to_delete:
        stop_replication(follower_url, idx, cfg)
        remove_write_block(follower_url, idx, cfg)
        if not delete_index(follower_url, idx, cfg):
            failed.append(idx)

    if failed:
        err(f"Delete failed for: {', '.join(failed)}")
        err("Aborting replication start to avoid partial state. "
            "Fix errors above, delete remaining indices manually, then start replication.")
        return False

    # ── Start explicit replication for named indices only ─────────────────
    # Wildcard indices are intentionally excluded here — they will be re-created
    # automatically when the auto-follow rule is applied on the new follower.
    step(f"Starting replication on follower cluster ({follower_url})...")
    start_results = [
        _retry_with_backoff(
            lambda idx=idx: start_replication(follower_url, leader_alias, idx, cfg),
            max_attempts=cfg.retry_max_attempts,
            base_delay=cfg.retry_base_delay,
        )
        for idx in indices
    ]
    if not all(start_results):
        failed = [idx for idx, ok_ in zip(indices, start_results) if not ok_]
        err(f"Replication failed to start for: {', '.join(failed)}")
        err("Check the OpenSearch logs and try starting them manually.")
        return False

    return True


def _apply_autofollow_rules(
    follower_url: str,
    leader_alias: str,
    autofollow_rules: list[dict],
    cfg: Config,
    action: str,  # "create" or "delete"
    non_interactive: bool = False,
) -> None:
    """
    Create or delete all auto-follow rules on a follower cluster.

    action="create":
      Before creating each rule, checks whether a rule with the same name already
      exists on the follower cluster (via autofollow_stats):
        - Not found            → create normally.
        - Found, same pattern  → skip (idempotent, no change needed).
        - Found, diff pattern  → warn and prompt for approval to replace
                                 (delete existing then create new).

    action="delete":
      When deleting a wildcard-pattern rule, OpenSearch only stops new indices from
      being picked up — existing follower indices remain read-only and keep replicating.
      This function therefore discovers all live wildcard-matched followers, stops their
      replication, and removes their write blocks *before* deleting the rule, ensuring a
      clean teardown.
    """
    if not autofollow_rules:
        return

    if action == "create":
        for rule in autofollow_rules:
            existing = get_existing_autofollow_rule(follower_url, rule["name"], cfg)
            if existing is not None:
                existing_pattern = existing.get("pattern", "")
                if existing_pattern == rule["pattern"]:
                    info(f"  [{rule['name']}] auto-follow rule already exists "
                         f"with same pattern '{existing_pattern}' — skipping.")
                    continue
                warn(f"  [{rule['name']}] rule already exists with a different pattern: "
                     f"current='{existing_pattern}'  new='{rule['pattern']}'")
                if not confirm(
                    f"Replace existing auto-follow rule '{rule['name']}' "
                    f"(pattern: '{existing_pattern}' → '{rule['pattern']}')?",
                    non_interactive,
                    dry_run=cfg.dry_run,
                ):
                    warn(f"  [{rule['name']}] skipped — existing rule kept.")
                    continue
                delete_autofollow_rule(follower_url, leader_alias, rule["name"], cfg)
            create_autofollow_rule(
                follower_url, leader_alias,
                rule["name"], rule["pattern"], cfg,
            )

    elif action == "delete":
        for rule in autofollow_rules:
            pattern = rule["pattern"]

            # Discover all wildcard-matched indices on this cluster and stop/unblock them
            # before deleting the rule.  Deleting the rule alone only stops NEW indices
            # from being picked up — existing followers remain read-only and keep
            # replicating until explicitly stopped.
            wildcard_indices = discover_wildcard_indices(follower_url, pattern, cfg)
            if wildcard_indices:
                info(f"  [{rule['name']}] pattern '{pattern}' → "
                     f"{len(wildcard_indices)} index(es) to stop & unblock: "
                     + ", ".join(wildcard_indices))
                for idx in wildcard_indices:
                    stop_replication(follower_url, idx, cfg)
                    remove_write_block(follower_url, idx, cfg)
            else:
                info(f"  [{rule['name']}] pattern '{pattern}' → "
                     f"no existing follower indices found on this cluster")

            delete_autofollow_rule(follower_url, leader_alias, rule["name"], cfg)


# ── High-level operations ──────────────────────────────────────────────────

def do_failover(
    cfg: Config,
    indices: list[str],
    autofollow_rules: list[dict],
    non_interactive: bool = False,
) -> None:
    """
    FAILOVER  (normal → DR active)

    Before: PRIMARY (leader) ──CCR──► SECONDARY (follower, read-only)
    After:  SECONDARY (leader, R/W) ──CCR──► PRIMARY (follower)

    Steps:
      1. Stop replication on SECONDARY + make indices R/W
      2. Delete auto-follow rules on SECONDARY (it was the follower)
      3. Delete indices on PRIMARY + start replication (following SECONDARY)
      4. Create auto-follow rules on PRIMARY (it is now the follower)
    """
    header("FAILOVER — Activating DR site")
    if cfg.dry_run:
        warn("DRY-RUN MODE — no changes will be made to either cluster")

    info("Current state  : PRIMARY (leader) ──► SECONDARY (follower)")
    info("Target state   : SECONDARY (leader, R/W) ──► PRIMARY (follower)")
    info(f"Follower alias : {C.BOLD}{cfg.secondary_alias_on_primary}{C.RESET}"
         "  (pre-configured connection on PRIMARY pointing to SECONDARY)")
    info(f"Indices        : {', '.join(indices) if indices else '(none — autofollow-only)'}")
    if autofollow_rules:
        info(f"Auto-follow    : {len(autofollow_rules)} rule(s) — "
             + ", ".join(f"{r['name']}={r['pattern']}" for r in autofollow_rules))

    # ── Step 1: Break replication on SECONDARY ────────────────────────────
    step("Step 1 — Stop replication on SECONDARY and make indices read/write")
    info(f"Target cluster : {cfg.secondary_url}")

    if indices and not confirm(
        f"Stop replication for {len(indices)} index(es) on SECONDARY ({cfg.secondary_url}) "
        "and remove write blocks?",
        non_interactive,
        dry_run=cfg.dry_run,
    ):
        info("Aborted.")
        return

    stop_results = [stop_replication(cfg.secondary_url, idx, cfg) for idx in indices]
    if not all(stop_results):
        warn("Some replication-stop calls had errors (see above). Continuing to remove write blocks.")

    rw_results = [remove_write_block(cfg.secondary_url, idx, cfg) for idx in indices]
    if not all(rw_results):
        err("Failed to remove write blocks on some indices. Review errors above before proceeding.")
        if not confirm("Proceed anyway?", non_interactive, dry_run=cfg.dry_run):
            return

    # Tear down auto-follow rules before declaring SECONDARY active so no new
    # indices can be picked up by the rule in the window before it is deleted.
    step(f"Step 2 — Delete auto-follow rules on SECONDARY ({cfg.secondary_url})")
    if autofollow_rules and not confirm(
        f"Delete {len(autofollow_rules)} auto-follow rule(s) from SECONDARY ({cfg.secondary_url})?",
        non_interactive,
        dry_run=cfg.dry_run,
    ):
        info("Aborted.")
        return
    _apply_autofollow_rules(
        cfg.secondary_url, cfg.primary_alias_on_secondary,
        autofollow_rules, cfg, action="delete",
    )

    ok("SECONDARY is now active (read/write).")
    warn("DR TESTING CAN NOW BEGIN on the secondary cluster.")

    # ── Step 3: Delete indices on PRIMARY + start reverse replication ─────
    step(f"Step 3 — Delete indices on PRIMARY ({cfg.primary_url}), then start replication (following SECONDARY ({cfg.secondary_url}))")
    info(f"Follower cluster : {cfg.primary_url}")
    info(f"Leader alias     : {cfg.secondary_alias_on_primary}")
    info("")
    warn("NOTE: The connection alias must already exist on the PRIMARY cluster.")
    warn("      In AWS OpenSearch this is the pre-configured outbound connection")
    warn(f"      named '{cfg.secondary_alias_on_primary}' pointing to the secondary domain.")
    info("")
    info("PRIMARY currently holds these indices as the ex-leader.")
    info("They must be deleted before CCR replication can be started.")

    started = delete_and_start_replication(
        follower_url=cfg.primary_url,
        leader_alias=cfg.secondary_alias_on_primary,
        indices=indices,
        cfg=cfg,
        non_interactive=non_interactive,
        autofollow_rules=autofollow_rules,
    )

    # ── Step 4: Create auto-follow rules on PRIMARY (new follower) ────────
    step(f"Step 4 — Create auto-follow rules on PRIMARY ({cfg.primary_url})")
    if started and autofollow_rules:
        _apply_autofollow_rules(
            cfg.primary_url, cfg.secondary_alias_on_primary,
            autofollow_rules, cfg, action="create",
            non_interactive=non_interactive,
        )

    ok("FAILOVER complete.")
    info("")
    info("Summary:")
    info(f"  Active (leader) cluster   : {cfg.secondary_url}")
    info(f"  Standby (follower) cluster: {cfg.primary_url}")
    info("  Run '--operation status' to verify replication health.")


def do_failback(
    cfg: Config,
    indices: list[str],
    autofollow_rules: list[dict],
    non_interactive: bool = False,
) -> None:
    """
    FAILBACK  (DR active → normal)

    Before: SECONDARY (leader) ──CCR──► PRIMARY (follower)
    After:  PRIMARY (leader, R/W) ──CCR──► SECONDARY (follower)

    Steps:
      1. Stop replication on PRIMARY + make indices R/W
      2. Delete auto-follow rules on PRIMARY (it was the follower)
      3. Delete indices on SECONDARY + start replication (following PRIMARY)
      4. Create auto-follow rules on SECONDARY (it is now the follower)
    """
    header("FAILBACK — Restoring normal state")
    if cfg.dry_run:
        warn("DRY-RUN MODE — no changes will be made to either cluster")

    info("Current state  : SECONDARY (leader) ──► PRIMARY (follower)")
    info("Target state   : PRIMARY (leader, R/W) ──► SECONDARY (follower)")
    info(f"Follower alias : {C.BOLD}{cfg.primary_alias_on_secondary}{C.RESET}"
         "  (pre-configured connection on SECONDARY pointing to PRIMARY)")
    info(f"Indices        : {', '.join(indices) if indices else '(none — autofollow-only)'}")
    if autofollow_rules:
        info(f"Auto-follow    : {len(autofollow_rules)} rule(s) — "
             + ", ".join(f"{r['name']}={r['pattern']}" for r in autofollow_rules))

    # ── Step 1: Break replication on PRIMARY ─────────────────────────────
    step("Step 1 — Stop replication on PRIMARY and make indices read/write")
    info(f"Target cluster : {cfg.primary_url}")

    if indices and not confirm(
        f"Stop replication for {len(indices)} index(es) on PRIMARY ({cfg.primary_url}) "
        "and remove write blocks?",
        non_interactive,
        dry_run=cfg.dry_run,
    ):
        info("Aborted.")
        return

    stop_results = [stop_replication(cfg.primary_url, idx, cfg) for idx in indices]
    if not all(stop_results):
        warn("Some replication-stop calls had errors. Continuing to remove write blocks.")

    rw_results = [remove_write_block(cfg.primary_url, idx, cfg) for idx in indices]
    if not all(rw_results):
        err("Failed to remove write blocks on some indices. Review errors above before proceeding.")
        if not confirm("Proceed anyway?", non_interactive, dry_run=cfg.dry_run):
            return

    # Tear down auto-follow rules before declaring PRIMARY active.
    step(f"Step 2 — Delete auto-follow rules on PRIMARY ({cfg.primary_url})")
    if autofollow_rules and not confirm(
        f"Delete {len(autofollow_rules)} auto-follow rule(s) from PRIMARY ({cfg.primary_url})?",
        non_interactive,
        dry_run=cfg.dry_run,
    ):
        info("Aborted.")
        return
    _apply_autofollow_rules(
        cfg.primary_url, cfg.secondary_alias_on_primary,
        autofollow_rules, cfg, action="delete",
    )

    ok("PRIMARY is now active (read/write). DR testing is over.")

    # ── Step 3: Delete indices on SECONDARY + restore forward replication ─
    step(f"Step 3 — Delete indices on SECONDARY ({cfg.secondary_url}), then restore replication (following PRIMARY ({cfg.primary_url}))")
    info(f"Follower cluster : {cfg.secondary_url}")
    info(f"Leader alias     : {cfg.primary_alias_on_secondary}")
    info("")
    warn("NOTE: The connection alias must already exist on the SECONDARY cluster.")
    warn("      In AWS OpenSearch this is the pre-configured outbound connection")
    warn(f"      named '{cfg.primary_alias_on_secondary}' pointing to the primary domain.")
    info("")
    info("SECONDARY currently holds these indices as the ex-leader.")
    info("They must be deleted before CCR replication can be started.")

    started = delete_and_start_replication(
        follower_url=cfg.secondary_url,
        leader_alias=cfg.primary_alias_on_secondary,
        indices=indices,
        cfg=cfg,
        non_interactive=non_interactive,
        autofollow_rules=autofollow_rules,
    )

    # ── Step 4: Create auto-follow rules on SECONDARY (new follower) ──────
    step(f"Step 4 — Create auto-follow rules on SECONDARY ({cfg.secondary_url})")
    if started and autofollow_rules:
        _apply_autofollow_rules(
            cfg.secondary_url, cfg.primary_alias_on_secondary,
            autofollow_rules, cfg, action="create",
            non_interactive=non_interactive,
        )

    ok("FAILBACK complete. Normal replication restored.")
    info("")
    info("Summary:")
    info(f"  Active (leader) cluster   : {cfg.primary_url}")
    info(f"  Standby (follower) cluster: {cfg.secondary_url}")
    info("  Run '--operation status' to verify replication health.")


def do_status(cfg: Config, indices: list[str], autofollow_rules: list[dict]) -> None:
    """Print replication status, index block info, and auto-follow stats for all indices."""
    header("Replication Status")

    # ── Cluster health ────────────────────────────────────────────────────
    for label, base_url in [("PRIMARY", cfg.primary_url), ("SECONDARY", cfg.secondary_url)]:
        health = cluster_health(base_url, cfg)
        colour = C.GREEN if health in ("green", "yellow") else C.RED
        print(f"  {C.BOLD}{label}{C.RESET}  {base_url}  →  {colour}{health}{C.RESET}")
    print()

    # ── Per-index replication status ──────────────────────────────────────
    for label, base_url in [("PRIMARY", cfg.primary_url), ("SECONDARY", cfg.secondary_url)]:
        print(f"{C.BOLD}Replication on {label} ({base_url}):{C.RESET}")
        for index in indices:
            status = replication_status(base_url, index, cfg)
            rep_status = status.get("status", "—")
            leader_idx = status.get("leader_index", "—")
            colour = C.GREEN if rep_status == "SYNCING" else (
                     C.YELLOW if rep_status in ("BOOTSTRAPPING", "PAUSED") else C.RED)
            print(f"  {C.CYAN}{index:<30}{C.RESET}  "
                  f"status={colour}{rep_status:<15}{C.RESET}  leader_index={leader_idx}")
        print()

    # ── Index write blocks ────────────────────────────────────────────────
    for label, base_url in [("PRIMARY", cfg.primary_url), ("SECONDARY", cfg.secondary_url)]:
        print(f"{C.BOLD}Write blocks on {label}:{C.RESET}")
        for index in indices:
            blocks = index_blocks(base_url, index, cfg)
            if blocks:
                block_str = ", ".join(f"{k}={v}" for k, v in blocks.items())
                print(f"  {C.CYAN}{index:<30}{C.RESET}  {C.YELLOW}{block_str}{C.RESET}")
            else:
                print(f"  {C.CYAN}{index:<30}{C.RESET}  {C.GREEN}none{C.RESET}")
        print()

    # ── Auto-follow stats ─────────────────────────────────────────────────
    if autofollow_rules:
        for label, base_url in [("PRIMARY", cfg.primary_url), ("SECONDARY", cfg.secondary_url)]:
            print(f"{C.BOLD}Auto-follow stats on {label} ({base_url}):{C.RESET}")
            stats = get_autofollow_stats(base_url, cfg)
            if not stats:
                print(f"  {C.YELLOW}(no data — cluster may be unreachable or plugin not active){C.RESET}")
                print()
                continue

            total_ok  = stats.get("num_success_start_replication", 0)
            total_err = stats.get("num_failed_start_replication", 0)
            print(f"  total success={C.GREEN}{total_ok}{C.RESET}  "
                  f"total failed={C.RED if total_err else C.GREEN}{total_err}{C.RESET}")

            for rule_stat in stats.get("autofollow_stats", []):
                name    = rule_stat.get("name", "—")
                pattern = rule_stat.get("pattern", "—")
                r_ok    = rule_stat.get("num_success_start_replication", 0)
                r_err   = rule_stat.get("num_failed_start_replication", 0)
                failed  = rule_stat.get("failed_indices", [])
                colour  = C.GREEN if r_err == 0 else C.RED
                print(f"  {C.CYAN}{name:<25}{C.RESET}  pattern={pattern:<20}"
                      f"  ok={colour}{r_ok}{C.RESET}  failed={colour}{r_err}{C.RESET}")
                for fi in failed:
                    print(f"    {C.RED}✗ {fi}{C.RESET}")
            print()


# ── Interactive menu ───────────────────────────────────────────────────────

def print_banner(cfg: Config, indices: list[str], autofollow_rules: list[dict]) -> None:
    af_indices = ", ".join(r["name"] for r in autofollow_rules) if autofollow_rules else "none"
    print(f"""
{C.BOLD}{C.CYAN}
  ╔══════════════════════════════════════════════════════╗
  ║      OpenSearch DR Switchover & Failback Tool        ║
  ╠══════════════════════════════════════════════════════╣
  ║  Primary    : {cfg.primary_url:<38}║
  ║  Secondary  : {cfg.secondary_url:<38}║
  ║  Indices    : {str(len(indices)) + ' index(es) from ' + cfg.indices_file:<38}║
  ║  Auto-follow: {af_indices:<38}║
  ╚══════════════════════════════════════════════════════╝
{C.RESET}""")


def _build_menu(dry_run: bool) -> str:
    suffix = f"  {C.YELLOW}[DRY-RUN]{C.RESET}" if dry_run else ""
    return f"""
{C.BOLD}Select an operation:{C.RESET}{suffix}

  ┌────────────────────────────────────────────────────────────┐
  │  1) FAILOVER  — Activate DR site                           │
  │     · Stop replication on SECONDARY → make R/W             │
  │     · Delete auto-follow rules on SECONDARY (if any)       │
  │     · Start replication SECONDARY ──► PRIMARY              │
  │     · Create auto-follow rules on PRIMARY (if any)         │
  ├────────────────────────────────────────────────────────────┤
  │  2) FAILBACK  — Restore normal state                        │
  │     · Stop replication on PRIMARY → make R/W               │
  │     · Delete auto-follow rules on PRIMARY (if any)         │
  │     · Start replication PRIMARY ──► SECONDARY              │
  │     · Create auto-follow rules on SECONDARY (if any)       │
  ├────────────────────────────────────────────────────────────┤
  │  3) STATUS   — Show replication state for all indices       │
  │  4) QUIT                                                    │
  └────────────────────────────────────────────────────────────┘
"""


def interactive_menu(
    cfg: Config, indices: list[str], autofollow_rules: list[dict]
) -> None:
    print_banner(cfg, indices, autofollow_rules)
    while True:
        print(_build_menu(cfg.dry_run))
        choice = input(f"{C.BOLD}Enter choice [1-4]: {C.RESET}").strip()
        if choice == "1":
            do_failover(cfg, indices, autofollow_rules)
        elif choice == "2":
            do_failback(cfg, indices, autofollow_rules)
        elif choice == "3":
            do_status(cfg, indices, autofollow_rules)
        elif choice == "4":
            info("Goodbye.")
            sys.exit(0)
        else:
            warn("Invalid choice — enter 1, 2, 3, or 4.")


# ── Entry point ────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OpenSearch DR Switchover Script (AWS OpenSearch compatible)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--operation",
        choices=["failover", "failback", "status"],
        help="Run a specific operation directly (skips interactive menu)",
    )
    parser.add_argument("--config",          metavar="FILE",  help="Path to JSON config file")
    parser.add_argument("--primary-url",     metavar="URL",   help="Primary cluster URL")
    parser.add_argument("--secondary-url",   metavar="URL",   help="Secondary cluster URL")
    parser.add_argument("--indices-file",    metavar="PATH",  help="Path to indices.json")
    parser.add_argument(
        "--primary-alias",  metavar="ALIAS",
        help="Connection alias on SECONDARY pointing to PRIMARY (used during failback)",
    )
    parser.add_argument(
        "--secondary-alias", metavar="ALIAS",
        help="Connection alias on PRIMARY pointing to SECONDARY (used during failover)",
    )
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="Auto-confirm all prompts (for CI/automation)",
    )
    parser.add_argument("--no-colour", action="store_true", help="Disable ANSI colours")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Log what would happen without making any mutating API calls",
    )
    parser.add_argument(
        "--retry-max", metavar="N", type=int,
        help="Max replication start attempts per index (default: 3)",
    )
    parser.add_argument(
        "--retry-delay", metavar="SECS", type=float,
        help="Base backoff delay in seconds for replication start retries (default: 2.0)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.no_colour:
        for attr in ("RED", "GREEN", "YELLOW", "CYAN", "BOLD", "RESET"):
            setattr(C, attr, "")

    cli_overrides = {
        "primary_url":                args.primary_url,
        "secondary_url":              args.secondary_url,
        "indices_file":               args.indices_file,
        "primary_alias_on_secondary": args.primary_alias,
        "secondary_alias_on_primary": args.secondary_alias,
        "retry_max_attempts":         args.retry_max,
        "retry_base_delay":           args.retry_delay,
        "dry_run":                    args.dry_run or None,
    }
    cfg             = load_config(args.config, cli_overrides)
    # dry_run comes via cli_overrides (None means "don't override"), set it directly
    if args.dry_run:
        cfg.dry_run = True

    indices          = load_indices(cfg.indices_file)
    autofollow_rules = load_autofollow_rules(cfg.indices_file)

    if not indices and not autofollow_rules:
        err("indices.json has no named indices and no autofollow rules — nothing to do.")
        sys.exit(1)

    if args.operation == "failover":
        do_failover(cfg, indices, autofollow_rules, non_interactive=args.non_interactive)
    elif args.operation == "failback":
        do_failback(cfg, indices, autofollow_rules, non_interactive=args.non_interactive)
    elif args.operation == "status":
        do_status(cfg, indices, autofollow_rules)
    else:
        interactive_menu(cfg, indices, autofollow_rules)


if __name__ == "__main__":
    main()

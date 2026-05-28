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

Configuration precedence (highest → lowest):
  1. CLI flags
  2. Environment variables  (OPENSEARCH_PRIMARY_URL, etc.)
  3. config.json
  4. Built-in defaults (localhost for local Docker testing)
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
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

def info(msg: str)   -> None: print(f"{C.CYAN}[INFO]{C.RESET}  {msg}")
def ok(msg: str)     -> None: print(f"{C.GREEN}[ OK ]{C.RESET}  {msg}")
def warn(msg: str)   -> None: print(f"{C.YELLOW}[WARN]{C.RESET}  {msg}")
def err(msg: str)    -> None: print(f"{C.RED}[ERR ]{C.RESET}  {msg}", file=sys.stderr)
def step(msg: str)   -> None: print(f"\n{C.BOLD}▶  {msg}{C.RESET}")
def header(msg: str) -> None:
    bar = "═" * 52
    print(f"\n{C.BOLD}{C.CYAN}{bar}\n  {msg}\n{bar}{C.RESET}\n")


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

    def auth(self) -> Optional[tuple]:
        if self.username and self.password:
            return (self.username, self.password)
        return None


def load_config(config_path: Optional[str], cli_overrides: dict) -> Config:
    """Build Config from file + env vars + CLI overrides (in priority order)."""
    import os

    cfg = Config()

    if config_path and Path(config_path).exists():
        with open(config_path) as f:
            data = json.load(f)
        for key, value in data.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)

    env_map = {
        "OPENSEARCH_PRIMARY_URL":              "primary_url",
        "OPENSEARCH_SECONDARY_URL":            "secondary_url",
        "OPENSEARCH_INDICES_FILE":             "indices_file",
        "OPENSEARCH_PRIMARY_ALIAS":            "primary_alias_on_secondary",
        "OPENSEARCH_SECONDARY_ALIAS":          "secondary_alias_on_primary",
        "OPENSEARCH_USERNAME":                 "username",
        "OPENSEARCH_PASSWORD":                 "password",
    }
    for env_key, attr in env_map.items():
        if env_key in os.environ:
            setattr(cfg, attr, os.environ[env_key])

    for attr, value in cli_overrides.items():
        if value is not None and hasattr(cfg, attr):
            setattr(cfg, attr, value)

    return cfg


def load_indices(indices_file: str) -> list[str]:
    path = Path(indices_file)
    if not path.exists():
        err(f"Indices file not found: {indices_file}")
        sys.exit(1)
    with open(path) as f:
        data = json.load(f)
    names = [entry["name"] for entry in data.get("indices", [])]
    if not names:
        err("No indices found in the indices file.")
        sys.exit(1)
    return names


# ── OpenSearch API helpers ─────────────────────────────────────────────────

def _request(method: str, url: str, cfg: Config, body: Optional[dict] = None) -> Response:
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
        # 404 or similar likely means it was never started — treat as non-fatal
        warn(f"  [{index}] stop returned HTTP {r.status_code}: {r.text.strip()}"
             " — may already be stopped, continuing.")
        return True
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
        if r.status_code == 200:
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
        # use_roles only needed for self-managed with security enabled
        # included here so it works for both; AWS OpenSearch ignores it
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
        return r.json()
    except Exception:
        return {}


def index_blocks(base_url: str, index: str, cfg: Config) -> dict:
    try:
        r = _request("GET", f"{base_url}/{index}/_settings", cfg)
        settings = r.json()
        return settings.get(index, {}).get("settings", {}).get("index", {}).get("blocks", {})
    except Exception:
        return {}


# ── Confirmation prompts ───────────────────────────────────────────────────

def confirm(prompt: str, non_interactive: bool = False) -> bool:
    if non_interactive:
        info(f"(non-interactive) auto-confirming: {prompt}")
        return True
    print(f"\n{C.YELLOW}?  {prompt}{C.RESET}  [y/N] ", end="", flush=True)
    answer = input().strip().lower()
    return answer in ("y", "yes")


def confirm_destructive(cluster_url: str, indices: list[str], non_interactive: bool = False) -> bool:
    """
    Separate, explicit confirmation gate before deleting indices.
    Requires the user to type the word DELETE (uppercase) so an accidental
    Enter press can never trigger a data-loss operation.
    """
    print(f"\n{C.RED}{C.BOLD}{'!'*54}{C.RESET}")
    print(f"{C.RED}{C.BOLD}  DESTRUCTIVE OPERATION — DATA WILL BE PERMANENTLY LOST{C.RESET}")
    print(f"{C.RED}{C.BOLD}{'!'*54}{C.RESET}")
    print(f"\n  Cluster : {C.BOLD}{cluster_url}{C.RESET}")
    print(f"  Action  : delete the following indices from that cluster")
    print(f"  Reason  : CCR replication requires the follower index to not exist\n")
    for idx in indices:
        print(f"    {C.RED}✗  {idx}{C.RESET}")
    print()
    warn("These indices will be PERMANENTLY DELETED before replication starts.")
    warn("Make sure the leader cluster is healthy and has up-to-date data.")

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


def delete_and_start_replication(
    follower_url: str,
    leader_alias: str,
    indices: list[str],
    cfg: Config,
    non_interactive: bool = False,
) -> None:
    """
    Delete each index on the follower cluster (with explicit confirmation),
    then start CCR replication for each index.
    """
    if not confirm_destructive(follower_url, indices, non_interactive):
        info("Skipped. You can delete the indices and start replication manually later.")
        return

    step("Deleting indices on follower cluster...")
    delete_ok = all(delete_index(follower_url, idx, cfg) for idx in indices)
    if not delete_ok:
        err("One or more deletes failed. Aborting replication start to avoid partial state.")
        err("Fix the errors above, delete remaining indices manually, then start replication.")
        return

    step("Starting replication on follower cluster...")
    all(start_replication(follower_url, leader_alias, idx, cfg) for idx in indices)


# ── High-level operations ──────────────────────────────────────────────────

def do_failover(cfg: Config, indices: list[str], non_interactive: bool = False) -> None:
    """
    FAILOVER  (normal → DR active)

    Before: PRIMARY (leader) ──CCR──► SECONDARY (follower, read-only)
    After:  SECONDARY (leader, R/W) ──CCR──► PRIMARY (follower)

    Steps:
      1. Stop replication on SECONDARY + make indices R/W
      2. Start replication on PRIMARY (following SECONDARY)
    """
    header("FAILOVER — Activating DR site")

    info("Current state  : PRIMARY (leader) ──► SECONDARY (follower)")
    info("Target state   : SECONDARY (leader, R/W) ──► PRIMARY (follower)")
    info(f"Follower alias : {C.BOLD}{cfg.secondary_alias_on_primary}{C.RESET}"
         "  (pre-configured connection on PRIMARY pointing to SECONDARY)")
    info(f"Indices        : {', '.join(indices)}")

    # ── Step 1: Break replication on SECONDARY ─────────────────────────────
    step("Step 1 — Stop replication on SECONDARY and make indices read/write")
    info(f"Target cluster : {cfg.secondary_url}")

    if not confirm(
        f"Stop replication for {len(indices)} index(es) on SECONDARY ({cfg.secondary_url}) "
        "and remove write blocks?",
        non_interactive,
    ):
        info("Aborted.")
        return

    stop_ok = all(stop_replication(cfg.secondary_url, idx, cfg) for idx in indices)
    if not stop_ok:
        warn("Some replication-stop calls had errors (see above). Continuing to remove write blocks.")

    rw_ok = all(remove_write_block(cfg.secondary_url, idx, cfg) for idx in indices)
    if not rw_ok:
        err("Failed to remove write blocks on some indices. Review errors above before proceeding.")
        if not confirm("Proceed anyway?", non_interactive):
            return

    ok("SECONDARY is now active (read/write).")
    warn("DR TESTING CAN NOW BEGIN on the secondary cluster.")

    # ── Step 2: Delete indices on PRIMARY then start reverse replication ─────
    step("Step 2 — Delete indices on PRIMARY, then start replication (following SECONDARY)")
    info(f"Follower cluster : {cfg.primary_url}")
    info(f"Leader alias     : {cfg.secondary_alias_on_primary}")
    info("")
    warn("NOTE: The connection alias must already exist on the PRIMARY cluster.")
    warn("      In AWS OpenSearch this is the pre-configured outbound connection")
    warn(f"      named '{cfg.secondary_alias_on_primary}' pointing to the secondary domain.")
    info("")
    info("PRIMARY currently holds these indices as the ex-leader.")
    info("They must be deleted before CCR replication can be started.")

    delete_and_start_replication(
        follower_url=cfg.primary_url,
        leader_alias=cfg.secondary_alias_on_primary,
        indices=indices,
        cfg=cfg,
        non_interactive=non_interactive,
    )

    ok("FAILOVER complete.")
    info("")
    info("Summary:")
    info(f"  Active (leader) cluster  : {cfg.secondary_url}")
    info(f"  Standby (follower) cluster: {cfg.primary_url}")
    info(f"  Run '--operation status' to verify replication health.")


def do_failback(cfg: Config, indices: list[str], non_interactive: bool = False) -> None:
    """
    FAILBACK  (DR active → normal)

    Before: SECONDARY (leader) ──CCR──► PRIMARY (follower)
    After:  PRIMARY (leader, R/W) ──CCR──► SECONDARY (follower)

    Steps:
      1. Stop replication on PRIMARY + make indices R/W
      2. Start replication on SECONDARY (following PRIMARY)
    """
    header("FAILBACK — Restoring normal state")

    info("Current state  : SECONDARY (leader) ──► PRIMARY (follower)")
    info("Target state   : PRIMARY (leader, R/W) ──► SECONDARY (follower)")
    info(f"Follower alias : {C.BOLD}{cfg.primary_alias_on_secondary}{C.RESET}"
         "  (pre-configured connection on SECONDARY pointing to PRIMARY)")
    info(f"Indices        : {', '.join(indices)}")

    # ── Step 1: Break replication on PRIMARY ──────────────────────────────
    step("Step 1 — Stop replication on PRIMARY and make indices read/write")
    info(f"Target cluster : {cfg.primary_url}")

    if not confirm(
        f"Stop replication for {len(indices)} index(es) on PRIMARY ({cfg.primary_url}) "
        "and remove write blocks?",
        non_interactive,
    ):
        info("Aborted.")
        return

    stop_ok = all(stop_replication(cfg.primary_url, idx, cfg) for idx in indices)
    if not stop_ok:
        warn("Some replication-stop calls had errors. Continuing to remove write blocks.")

    rw_ok = all(remove_write_block(cfg.primary_url, idx, cfg) for idx in indices)
    if not rw_ok:
        err("Failed to remove write blocks on some indices. Review errors above before proceeding.")
        if not confirm("Proceed anyway?", non_interactive):
            return

    ok("PRIMARY is now active (read/write). DR testing is over.")

    # ── Step 2: Delete indices on SECONDARY then restore forward replication ─
    step("Step 2 — Delete indices on SECONDARY, then restore replication (following PRIMARY)")
    info(f"Follower cluster : {cfg.secondary_url}")
    info(f"Leader alias     : {cfg.primary_alias_on_secondary}")
    info("")
    warn("NOTE: The connection alias must already exist on the SECONDARY cluster.")
    warn("      In AWS OpenSearch this is the pre-configured outbound connection")
    warn(f"      named '{cfg.primary_alias_on_secondary}' pointing to the primary domain.")
    info("")
    info("SECONDARY currently holds these indices as the ex-leader.")
    info("They must be deleted before CCR replication can be started.")

    delete_and_start_replication(
        follower_url=cfg.secondary_url,
        leader_alias=cfg.primary_alias_on_secondary,
        indices=indices,
        cfg=cfg,
        non_interactive=non_interactive,
    )

    ok("FAILBACK complete. Normal replication restored.")
    info("")
    info("Summary:")
    info(f"  Active (leader) cluster   : {cfg.primary_url}")
    info(f"  Standby (follower) cluster: {cfg.secondary_url}")
    info(f"  Run '--operation status' to verify replication health.")


def do_status(cfg: Config, indices: list[str]) -> None:
    """Print replication status and index block info for all indices."""
    header("Replication Status")

    for label, base_url in [("PRIMARY", cfg.primary_url), ("SECONDARY", cfg.secondary_url)]:
        health = cluster_health(base_url, cfg)
        colour = C.GREEN if health in ("green", "yellow") else C.RED
        print(f"  {C.BOLD}{label}{C.RESET}  {base_url}  →  {colour}{health}{C.RESET}")
    print()

    for label, base_url in [("PRIMARY", cfg.primary_url), ("SECONDARY", cfg.secondary_url)]:
        print(f"{C.BOLD}Replication on {label} ({base_url}):{C.RESET}")
        for index in indices:
            status = replication_status(base_url, index, cfg)
            rep_status  = status.get("status", "—")
            leader_idx  = status.get("leader_index", "—")
            colour = C.GREEN if rep_status == "SYNCING" else (
                     C.YELLOW if rep_status in ("BOOTSTRAPPING", "PAUSED") else C.RED)
            print(f"  {C.CYAN}{index:<30}{C.RESET}  "
                  f"status={colour}{rep_status:<15}{C.RESET}  leader_index={leader_idx}")
        print()

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


# ── Interactive menu ───────────────────────────────────────────────────────

def print_banner(cfg: Config, indices: list[str]) -> None:
    print(f"""
{C.BOLD}{C.CYAN}
  ╔══════════════════════════════════════════════════════╗
  ║      OpenSearch DR Switchover & Failback Tool        ║
  ╠══════════════════════════════════════════════════════╣
  ║  Primary    : {cfg.primary_url:<38}║
  ║  Secondary  : {cfg.secondary_url:<38}║
  ║  Indices    : {str(len(indices)) + ' index(es) from ' + cfg.indices_file:<38}║
  ╚══════════════════════════════════════════════════════╝
{C.RESET}""")


MENU = f"""
{C.BOLD}Select an operation:{C.RESET}

  ┌────────────────────────────────────────────────────────────┐
  │  1) FAILOVER  — Activate DR site                           │
  │     · Stop replication on SECONDARY → make R/W             │
  │     · Start replication SECONDARY ──► PRIMARY              │
  ├────────────────────────────────────────────────────────────┤
  │  2) FAILBACK  — Restore normal state                        │
  │     · Stop replication on PRIMARY → make R/W               │
  │     · Start replication PRIMARY ──► SECONDARY              │
  ├────────────────────────────────────────────────────────────┤
  │  3) STATUS   — Show replication state for all indices       │
  │  4) QUIT                                                    │
  └────────────────────────────────────────────────────────────┘
"""


def interactive_menu(cfg: Config, indices: list[str]) -> None:
    print_banner(cfg, indices)
    while True:
        print(MENU)
        choice = input(f"{C.BOLD}Enter choice [1-4]: {C.RESET}").strip()
        if choice == "1":
            do_failover(cfg, indices)
        elif choice == "2":
            do_failback(cfg, indices)
        elif choice == "3":
            do_status(cfg, indices)
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
    parser.add_argument("--operation", choices=["failover", "failback", "status"],
                        help="Run a specific operation directly (skips interactive menu)")
    parser.add_argument("--config",         metavar="FILE",  help="Path to JSON config file")
    parser.add_argument("--primary-url",    metavar="URL",   help="Primary cluster URL")
    parser.add_argument("--secondary-url",  metavar="URL",   help="Secondary cluster URL")
    parser.add_argument("--indices-file",   metavar="PATH",  help="Path to indices.json")
    parser.add_argument("--primary-alias",  metavar="ALIAS",
                        help="Connection alias on SECONDARY pointing to PRIMARY "
                             "(used during failback)")
    parser.add_argument("--secondary-alias", metavar="ALIAS",
                        help="Connection alias on PRIMARY pointing to SECONDARY "
                             "(used during failover)")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Auto-confirm all prompts (for CI/automation)")
    parser.add_argument("--no-colour", action="store_true", help="Disable ANSI colours")
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
    }
    cfg     = load_config(args.config, cli_overrides)
    indices = load_indices(cfg.indices_file)

    if args.operation == "failover":
        do_failover(cfg, indices, non_interactive=args.non_interactive)
    elif args.operation == "failback":
        do_failback(cfg, indices, non_interactive=args.non_interactive)
    elif args.operation == "status":
        do_status(cfg, indices)
    else:
        interactive_menu(cfg, indices)


if __name__ == "__main__":
    main()

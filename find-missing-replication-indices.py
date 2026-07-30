#!/usr/bin/env python3
"""
OpenSearch CCR — Missing Replication Indices Finder
=====================================================
Compares indices on a SOURCE (leader) cluster against a DESTINATION
(follower) cluster and reports indices that exist on SOURCE but not on
DESTINATION. System indices (names starting with '.') are excluded.

For every missing index it dumps a ready-to-run curl command that starts
CCR replication for that index on the destination cluster.

Usage:
  python3 find-missing-replication-indices.py
  python3 find-missing-replication-indices.py --primary-url http://localhost:9200 \\
      --secondary-url http://localhost:9201 --direction primary-to-secondary

  python3 find-missing-replication-indices.py --config path/to/config.json

Uses the same config.json / env-var schema as dr-switchover.py
(primary_url, secondary_url, primary_alias_on_secondary,
secondary_alias_on_primary, username, password, verify_ssl).
The --direction flag picks which of primary/secondary is SOURCE (leader)
vs. DESTINATION (follower) for this comparison.

Configuration precedence (highest → lowest):
  1. CLI flags
  2. Environment variables  (OPENSEARCH_PRIMARY_URL, etc.)
  3. config.json
  4. Built-in defaults (localhost for local Docker testing)
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import requests
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

    # AWS pre-configured connection aliases (identical semantics to dr-switchover.py).
    # primary_alias_on_secondary : alias visible from SECONDARY that points to PRIMARY.
    primary_alias_on_secondary: str = "primary-cluster"

    # secondary_alias_on_primary : alias visible from PRIMARY that points to SECONDARY.
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
        "OPENSEARCH_PRIMARY_URL":     "primary_url",
        "OPENSEARCH_SECONDARY_URL":   "secondary_url",
        "OPENSEARCH_PRIMARY_ALIAS":   "primary_alias_on_secondary",
        "OPENSEARCH_SECONDARY_ALIAS": "secondary_alias_on_primary",
        "OPENSEARCH_USERNAME":        "username",
        "OPENSEARCH_PASSWORD":        "password",
    }
    for env_key, attr in env_map.items():
        if env_key in os.environ:
            setattr(cfg, attr, os.environ[env_key])

    for attr, value in cli_overrides.items():
        if value is not None and hasattr(cfg, attr):
            setattr(cfg, attr, value)

    return cfg


# ── OpenSearch API helpers ─────────────────────────────────────────────────

def _request(method: str, url: str, cfg: Config, body: Optional[dict] = None):
    kwargs: dict = {
        "auth":    cfg.auth(),
        "verify":  cfg.verify_ssl,
        "timeout": 30,
        "headers": {"Content-Type": "application/json"},
    }
    if body is not None:
        kwargs["json"] = body
    return requests.request(method, url, **kwargs)


def list_indices(base_url: str, cfg: Config) -> list[str]:
    """
    Return all non-system index names on the cluster using
    GET /_cat/indices?format=json&h=index

    System indices (leading '.') are excluded here.
    """
    url = f"{base_url}/_cat/indices"
    params = {"format": "json", "h": "index"}
    try:
        r = requests.request(
            "GET", url,
            params=params,
            auth=cfg.auth(),
            verify=cfg.verify_ssl,
            timeout=30,
        )
        r.raise_for_status()
        entries = r.json()
        names = [e["index"] for e in entries if not e["index"].startswith(".")]
        return sorted(names)
    except Exception as exc:
        err(f"Failed to list indices on {base_url}: {exc}")
        sys.exit(1)


# ── Curl command generation ────────────────────────────────────────────────

def build_start_replication_curl(
    destination_url: str,
    leader_alias: str,
    index: str,
    cfg: Config,
) -> str:
    """
    Build the curl command that starts CCR replication for `index` on the
    destination (follower) cluster, following the source (leader) cluster
    via the pre-configured connection alias.
    """
    body = {
        "leader_alias": leader_alias,
        "leader_index": index,
        "use_roles": {
            "leader_cluster_role":   "all_access",
            "follower_cluster_role": "all_access",
        },
    }
    payload = json.dumps(body)

    auth_flag = f" -u '{cfg.username}:{cfg.password}'" if cfg.username and cfg.password else ""
    insecure_flag = " -k" if not cfg.verify_ssl else ""

    return (
        f"curl -X PUT{auth_flag}{insecure_flag} "
        f"'{destination_url}/_plugins/_replication/{index}/_start' "
        f"-H 'Content-Type: application/json' "
        f"-d '{payload}'"
    )


# ── Entry point ────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find indices missing on the destination cluster and dump "
                     "curl commands to start replication for them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", metavar="FILE", help="Path to JSON config file")
    parser.add_argument("--primary-url",   metavar="URL",   help="Primary cluster URL")
    parser.add_argument("--secondary-url", metavar="URL",   help="Secondary cluster URL")
    parser.add_argument("--primary-alias",  metavar="ALIAS",
                        help="Connection alias on SECONDARY pointing to PRIMARY")
    parser.add_argument("--secondary-alias", metavar="ALIAS",
                        help="Connection alias on PRIMARY pointing to SECONDARY")
    parser.add_argument("--direction", choices=["primary-to-secondary", "secondary-to-primary"],
                        default="primary-to-secondary",
                        help="Which cluster is SOURCE (leader) vs. DESTINATION (follower). "
                             "Default: primary-to-secondary (primary is SOURCE).")
    parser.add_argument("--output-file", metavar="PATH",
                        help="Optional path to also write the generated curl commands to")
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
        "primary_alias_on_secondary": args.primary_alias,
        "secondary_alias_on_primary": args.secondary_alias,
    }
    cfg = load_config(args.config, cli_overrides)

    if args.direction == "primary-to-secondary":
        source_url, destination_url = cfg.primary_url, cfg.secondary_url
        leader_alias = cfg.secondary_alias_on_primary
    else:
        source_url, destination_url = cfg.secondary_url, cfg.primary_url
        leader_alias = cfg.primary_alias_on_secondary

    header("CCR — Missing Replication Indices Finder")
    info(f"Direction           : {args.direction}")
    info(f"Source cluster      : {source_url}")
    info(f"Destination cluster : {destination_url}")
    info(f"Leader alias        : {leader_alias}")

    step("Fetching indices from SOURCE cluster")
    source_indices = list_indices(source_url, cfg)
    ok(f"Found {len(source_indices)} non-system index(es) on SOURCE.")

    step("Fetching indices from DESTINATION cluster")
    destination_indices = list_indices(destination_url, cfg)
    ok(f"Found {len(destination_indices)} non-system index(es) on DESTINATION.")

    missing = sorted(set(source_indices) - set(destination_indices))

    step("Comparing")
    if not missing:
        ok("No missing indices — DESTINATION already has every SOURCE index.")
        return

    warn(f"{len(missing)} index(es) exist on SOURCE but not on DESTINATION:")
    for idx in missing:
        print(f"    {C.YELLOW}✗  {idx}{C.RESET}")

    step("Curl commands to start replication on DESTINATION")
    commands = []
    for idx in missing:
        cmd = build_start_replication_curl(destination_url, leader_alias, idx, cfg)
        commands.append(cmd)
        print(f"\n{C.CYAN}# start replication for '{idx}'{C.RESET}")
        print(cmd)

    if args.output_file:
        out_path = Path(args.output_file)
        with open(out_path, "w") as f:
            f.write("#!/usr/bin/env bash\n")
            f.write("# Auto-generated by find-missing-replication-indices.py\n")
            f.write("# Starts CCR replication on the destination cluster for indices\n")
            f.write("# that exist on the source cluster but are missing on destination.\n")
            f.write("set -euo pipefail\n\n")
            for idx, cmd in zip(missing, commands):
                f.write(f"# {idx}\n{cmd}\n\n")
        ok(f"Wrote {len(commands)} curl command(s) to {out_path}")


if __name__ == "__main__":
    main()

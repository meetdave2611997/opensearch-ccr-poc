#!/usr/bin/env python3
"""
Bring a fresh primary/secondary cluster pair into the NORMAL DR state:

    PRIMARY (leader, R/W) ──CCR──► SECONDARY (follower, read-only)

Steps
  1. Wait for both clusters to be green/yellow.
  2. Register the remote-cluster aliases in both directions
     (on AWS these are the console-managed connections; dr-switchover.py never creates them).
  3. Create + seed every named index from indices.json on PRIMARY.
  4. For every auto-follow pattern, create a few matching indices on PRIMARY.
  5. Start explicit replication on SECONDARY for named, non-auto-follow indices.
  6. Create the auto-follow rules on SECONDARY.
  7. Wait until every expected follower index on SECONDARY is SYNCING.

Reuses dr-switchover.py (config, indices.json parsing, API helpers) via ci/ccr_lib.py.
Idempotent: safe to re-run against clusters that are already bootstrapped.
"""

import argparse
import json
import random
import sys

from ccr_lib import disable_colour, load_drs, load_entries, wait_until, wildcard_prefix

drs = load_drs()
info, ok, warn, err, step = drs.info, drs.ok, drs.warn, drs.err, drs.step


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="ci/config.ci.json")
    p.add_argument("--primary-seed", default="opensearch-primary:9300",
                   help="Transport address of PRIMARY as seen from SECONDARY")
    p.add_argument("--secondary-seed", default="opensearch-secondary:9300",
                   help="Transport address of SECONDARY as seen from PRIMARY")
    p.add_argument("--docs-per-index", type=int, default=5)
    p.add_argument("--wildcard-indices", type=int, default=2,
                   help="Concrete indices to create per auto-follow pattern")
    p.add_argument("--timeout", type=float, default=300,
                   help="Seconds to wait for clusters and for followers to reach SYNCING")
    p.add_argument("--no-colour", action="store_true")
    return p.parse_args()


def put_json(cfg, url: str, body: dict) -> bool:
    r = drs._request("PUT", url, cfg, body=body)
    if r.status_code < 400:
        return True
    if r.status_code == 400 and "resource_already_exists_exception" in r.text:
        return True
    err(f"PUT {url} → HTTP {r.status_code}: {r.text.strip()}")
    return False


def register_remote(cfg, cluster_url: str, alias: str, seed: str) -> bool:
    body = {"persistent": {"cluster": {"remote": {alias: {"seeds": [seed]}}}}}
    if not put_json(cfg, f"{cluster_url}/_cluster/settings", body):
        return False

    def connected() -> bool:
        r = drs._request("GET", f"{cluster_url}/_remote/info", cfg)
        return r.status_code < 400 and r.json().get(alias, {}).get("connected") is True

    if wait_until(connected, timeout=60, interval=3, label=f"{alias} handshake on {cluster_url}"):
        ok(f"  remote '{alias}' on {cluster_url} → {seed} connected")
        return True
    err(f"  remote '{alias}' on {cluster_url} never connected to {seed}")
    return False


def create_and_seed(cfg, index: str, shards: int, replicas: int, docs: int) -> bool:
    settings = {"settings": {"number_of_shards": shards, "number_of_replicas": replicas}}
    if not put_json(cfg, f"{cfg.primary_url}/{index}", settings):
        return False
    lines = []
    for i in range(1, docs + 1):
        lines.append(json.dumps({"index": {"_id": f"{index}-{i}"}}))
        lines.append(json.dumps({"index_name": index, "seq": i, "value": random.randint(1, 1000)}))
    r = drs.requests.post(
        f"{cfg.primary_url}/{index}/_bulk",
        data="\n".join(lines) + "\n",
        headers={"Content-Type": "application/x-ndjson"},
        auth=cfg.auth(), verify=cfg.verify_ssl, timeout=30,
    )
    if r.status_code >= 400 or r.json().get("errors"):
        err(f"  [{index}] bulk seed failed: HTTP {r.status_code} {r.text[:300]}")
        return False
    ok(f"  [{index}] created and seeded with {docs} docs")
    return True


def main() -> int:
    args = parse_args()
    if args.no_colour:
        disable_colour(drs)

    cfg = drs.load_config(args.config, {})
    entries = load_entries(drs, cfg.indices_file)
    named = drs.load_indices(cfg.indices_file)            # explicit _start indices
    rules = drs.load_autofollow_rules(cfg.indices_file)   # pattern-based rules

    drs.header("CCR bootstrap — building NORMAL state")
    info(f"Primary   : {cfg.primary_url}")
    info(f"Secondary : {cfg.secondary_url}")
    info(f"Named     : {', '.join(named) or '(none)'}")
    info(f"Rules     : {', '.join(r['name'] + '=' + r['pattern'] for r in rules) or '(none)'}")

    # 1 ─ cluster health
    step("Step 1 — Waiting for both clusters")
    for label, url in (("primary", cfg.primary_url), ("secondary", cfg.secondary_url)):
        if not wait_until(lambda u=url: drs.cluster_health(u, cfg) in ("green", "yellow"),
                          timeout=args.timeout, interval=5, label=f"{label} health"):
            err(f"{label} cluster at {url} never became healthy")
            return 1
        ok(f"  {label} is {drs.cluster_health(url, cfg)}")

    # 2 ─ remote cluster aliases
    step("Step 2 — Registering remote-cluster aliases")
    if not (register_remote(cfg, cfg.secondary_url, cfg.primary_alias_on_secondary, args.primary_seed)
            and register_remote(cfg, cfg.primary_url, cfg.secondary_alias_on_primary, args.secondary_seed)):
        return 1

    # 3 ─ named indices (all entries with a "name", auto-follow or not)
    step("Step 3 — Creating and seeding named indices on PRIMARY")
    failed = False
    for e in entries:
        if "name" in e:
            failed |= not create_and_seed(cfg, e["name"], e.get("shards", 1), e.get("replicas", 0),
                                          args.docs_per_index)

    # 4 ─ concrete indices for each wildcard pattern
    step("Step 4 — Creating indices that match auto-follow patterns on PRIMARY")
    for rule in rules:
        if "*" not in rule["pattern"] and "?" not in rule["pattern"]:
            continue
        prefix = wildcard_prefix(rule["pattern"])
        for i in range(1, args.wildcard_indices + 1):
            failed |= not create_and_seed(cfg, f"{prefix}-{i:02d}", 1, 0, args.docs_per_index)
    if failed:
        return 1
    drs._request("POST", f"{cfg.primary_url}/_refresh", cfg)

    # 5 ─ explicit replication
    step("Step 5 — Starting explicit replication on SECONDARY")
    for idx in named:
        status = drs.replication_status(cfg.secondary_url, idx, cfg).get("status")
        if status in ("SYNCING", "BOOTSTRAPPING"):
            info(f"  [{idx}] already {status} — skipping")
            continue
        if not drs._retry_with_backoff(
            lambda i=idx: drs.start_replication(cfg.secondary_url, cfg.primary_alias_on_secondary, i, cfg),
            max_attempts=cfg.retry_max_attempts, base_delay=cfg.retry_base_delay,
        ):
            return 1

    # 6 ─ auto-follow rules
    step("Step 6 — Creating auto-follow rules on SECONDARY")
    if not drs._apply_autofollow_rules(cfg.secondary_url, cfg.primary_alias_on_secondary,
                                       rules, cfg, action="create", non_interactive=True):
        return 1

    # 7 ─ wait for followers
    expected = list(named)
    for rule in rules:
        expected += [i for i in drs.discover_wildcard_indices(cfg.primary_url, rule["pattern"], cfg)
                     if i not in expected]
    step(f"Step 7 — Waiting for {len(expected)} follower index(es) on SECONDARY to reach SYNCING")
    info("  (auto-follow polls the leader every ~30s, so this can take a minute)")

    def all_syncing() -> bool:
        pending = [i for i in expected
                   if drs.replication_status(cfg.secondary_url, i, cfg).get("status") != "SYNCING"]
        if pending:
            info(f"  not yet SYNCING: {', '.join(pending)}")
        return not pending

    if not wait_until(all_syncing, timeout=args.timeout, interval=10):
        err("Followers did not reach SYNCING in time.")
        return 1

    ok(f"NORMAL state ready: {len(expected)} index(es) replicating PRIMARY → SECONDARY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

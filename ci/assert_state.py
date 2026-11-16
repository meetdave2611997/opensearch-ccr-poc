#!/usr/bin/env python3
"""
Check that the cluster pair is in the expected DR state, and exit 1 if it is not.

  --expect normal   PRIMARY is the leader,   SECONDARY follows it
  --expect dr       SECONDARY is the leader, PRIMARY follows it

For every managed index (named, non-auto-follow indices from indices.json plus every
leader index matching an auto-follow pattern):
  - the follower reports replication status SYNCING
  - the leader has no write / read-only blocks
  - the leader and follower have the same doc count
For every auto-follow rule:
  - the rule exists on the follower with the expected pattern and 0 failed starts
  - the rule does NOT exist on the leader

--write-probe also writes a marker doc to the leader and creates a brand-new index
matching each auto-follow pattern, then waits for both to show up on the follower.
--require-probe <tag> checks that the marker doc written by an earlier
`--write-probe --probe-tag <tag>` run is present on both clusters (proves writes made
during the DR window survived failback).

Replication is asynchronous, so all checks are retried until --timeout.
"""

import argparse
import sys
import time

from ccr_lib import disable_colour, load_drs, wait_until, wildcard_prefix

drs = load_drs()
info, ok, warn, err, step = drs.info, drs.ok, drs.warn, drs.err, drs.step


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="ci/config.ci.json")
    p.add_argument("--expect", choices=["normal", "dr"], required=True)
    p.add_argument("--write-probe", action="store_true")
    p.add_argument("--probe-tag", default="", help="Tag used in the probe doc id / index name")
    p.add_argument("--require-probe", metavar="TAG", action="append", default=[],
                   help="Probe tag whose marker doc must exist on both clusters (repeatable)")
    p.add_argument("--timeout", type=float, default=240)
    p.add_argument("--no-colour", action="store_true")
    return p.parse_args()


def doc_count(base_url: str, index: str, cfg) -> int:
    r = drs._request("GET", f"{base_url}/{index}/_count", cfg)
    return r.json().get("count", -1) if r.status_code < 400 else -1


def doc_exists(base_url: str, index: str, doc_id: str, cfg) -> bool:
    r = drs._request("GET", f"{base_url}/{index}/_doc/{doc_id}", cfg)
    return r.status_code == 200 and r.json().get("found") is True


def autofollow_rules_on(base_url: str, cfg) -> dict:
    return {r.get("name"): r for r in drs.get_autofollow_stats(base_url, cfg).get("autofollow_stats", [])}


def managed_indices(leader_url: str, named: list[str], rules: list[dict], cfg) -> list[str]:
    out = list(named)
    for rule in rules:
        for idx in drs.discover_wildcard_indices(leader_url, rule["pattern"], cfg):
            if idx not in out:
                out.append(idx)
    return out


def check_state(cfg, leader_url, follower_url, named, rules, report: list[str]) -> bool:
    report.clear()
    indices = managed_indices(leader_url, named, rules, cfg)
    if not indices:
        report.append("no managed indices found on the leader")
        return False

    for idx in indices:
        status = drs.replication_status(follower_url, idx, cfg).get("status", "—")
        blocks = drs.index_blocks(leader_url, idx, cfg)
        blocked = {k: v for k, v in blocks.items() if str(v).lower() == "true"}
        lc, fc = doc_count(leader_url, idx, cfg), doc_count(follower_url, idx, cfg)
        problems = []
        if status != "SYNCING":
            problems.append(f"follower status={status}")
        if blocked:
            problems.append(f"leader blocks={blocked}")
        if lc != fc:
            problems.append(f"doc count leader={lc} follower={fc}")
        if problems:
            report.append(f"[{idx}] " + "; ".join(problems))

    follower_rules = autofollow_rules_on(follower_url, cfg)
    leader_rules = autofollow_rules_on(leader_url, cfg)
    for rule in rules:
        fr = follower_rules.get(rule["name"])
        if fr is None:
            report.append(f"[rule {rule['name']}] missing on follower")
        elif fr.get("pattern") != rule["pattern"]:
            report.append(f"[rule {rule['name']}] follower pattern={fr.get('pattern')} expected={rule['pattern']}")
        elif fr.get("num_failed_start_replication", 0):
            report.append(f"[rule {rule['name']}] failed starts={fr.get('num_failed_start_replication')} "
                          f"failed_indices={fr.get('failed_indices')}")
        if rule["name"] in leader_rules:
            report.append(f"[rule {rule['name']}] still present on the leader")

    if not report:
        report.append(f"{len(indices)} index(es) SYNCING, doc counts match, "
                      f"{len(rules)} auto-follow rule(s) on follower only")
        return True
    return False


def main() -> int:
    args = parse_args()
    if args.no_colour:
        disable_colour(drs)

    cfg = drs.load_config(args.config, {})
    named = drs.load_indices(cfg.indices_file)
    rules = drs.load_autofollow_rules(cfg.indices_file)

    if args.expect == "normal":
        leader_url, follower_url, leader_name, follower_name = cfg.primary_url, cfg.secondary_url, "PRIMARY", "SECONDARY"
    else:
        leader_url, follower_url, leader_name, follower_name = cfg.secondary_url, cfg.primary_url, "SECONDARY", "PRIMARY"

    drs.header(f"Assert state: {args.expect.upper()}  ({leader_name} → {follower_name})")
    failures = 0

    # ── Probe writes (before the state check so the new indices are included) ──
    tag = (args.probe_tag or args.expect).lower()
    probe_doc = f"ci-probe-{tag}"
    if args.write_probe:
        step(f"Writing probes to the leader ({leader_name})")
        if named:
            r = drs._request("PUT", f"{leader_url}/{named[0]}/_doc/{probe_doc}?refresh=true", cfg,
                             body={"probe": tag, "ts": time.time()})
            if r.status_code >= 400:
                err(f"  probe doc write to {named[0]} failed: HTTP {r.status_code} {r.text.strip()}")
                failures += 1
            else:
                ok(f"  wrote {named[0]}/_doc/{probe_doc}")
        for rule in rules:
            if "*" not in rule["pattern"] and "?" not in rule["pattern"]:
                continue
            new_index = f"{wildcard_prefix(rule['pattern'])}-probe-{tag}"
            drs._request("PUT", f"{leader_url}/{new_index}", cfg,
                         body={"settings": {"number_of_shards": 1, "number_of_replicas": 0}})
            r = drs._request("PUT", f"{leader_url}/{new_index}/_doc/1?refresh=true", cfg,
                             body={"probe": tag, "rule": rule["name"]})
            if r.status_code >= 400:
                err(f"  new index {new_index} failed: HTTP {r.status_code} {r.text.strip()}")
                failures += 1
            else:
                ok(f"  created {new_index} (should be picked up by rule '{rule['name']}')")

    # ── State check (retried; replication and auto-follow are asynchronous) ──
    step(f"Checking replication {leader_name} → {follower_name}")
    report: list[str] = []
    passed = wait_until(
        lambda: check_state(cfg, leader_url, follower_url, named, rules, report),
        timeout=args.timeout, interval=10, label="state not yet as expected",
    )
    for line in report:
        (ok if passed else err)(f"  {line}")
    failures += 0 if passed else 1

    # ── Probe doc reached the follower ──
    if args.write_probe and named:
        if wait_until(lambda: doc_exists(follower_url, named[0], probe_doc, cfg), timeout=60, interval=5):
            ok(f"  probe doc {probe_doc} replicated to {follower_name}")
        else:
            err(f"  probe doc {probe_doc} never reached {follower_name}")
            failures += 1

    # ── Earlier probes survived ──
    for req_tag in args.require_probe:
        doc_id = f"ci-probe-{req_tag.lower()}"
        for name, url in (("PRIMARY", cfg.primary_url), ("SECONDARY", cfg.secondary_url)):
            if named and wait_until(lambda u=url: doc_exists(u, named[0], doc_id, cfg), timeout=60, interval=5):
                ok(f"  {doc_id} present on {name}")
            else:
                err(f"  {doc_id} missing on {name}")
                failures += 1

    if failures:
        err(f"ASSERT {args.expect.upper()} FAILED ({failures} problem group(s)).")
        return 1
    ok(f"ASSERT {args.expect.upper()} PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

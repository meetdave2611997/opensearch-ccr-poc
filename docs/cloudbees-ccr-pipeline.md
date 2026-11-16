# OpenSearch CCR DR switchover on CloudBees CI

How `dr-switchover.py` (OpenSearch Cross-Cluster Replication failover and failback) runs from a CloudBees CI pipeline. The first stage is a proof of concept (POC) on throwaway Docker clusters. This page covers how the script works, the pipeline architecture, prerequisites, how to run the POC, and the plan for moving to real AWS domains.

| | |
|---|---|
| Pipeline | [`ci/Jenkinsfile`](../ci/Jenkinsfile) (declarative, CloudBees CI) |
| Stage logic | [`ci/pipeline.sh`](../ci/pipeline.sh). Runs the same way on a laptop and on CloudBees. |
| Clusters | [`ci/docker-compose.ci.yml`](../ci/docker-compose.ci.yml): OpenSearch 2.13.0 primary + secondary |
| Runner image | [`ci/Dockerfile.runner`](../ci/Dockerfile.runner): python:3.12-slim + repo scripts |
| Checks | [`ci/bootstrap.py`](../ci/bootstrap.py), [`ci/assert_state.py`](../ci/assert_state.py) |

---

## 1. What the existing script does

### Files and dependencies

| File | Role | Dependencies |
|---|---|---|
| `dr-switchover.py` | Failover, failback, status | `requests`; `boto3` only when `credentials_secret_arn` is set |
| `find-missing-replication-indices.py` | Lists leader indices missing on the follower and prints `_start` curl commands | `requests`, `boto3` (optional) |
| `indices.json` | Indices to manage. `autofollow: true` entries need `autofollow_pattern`. | — |
| `config.json` | URLs, connection aliases, secret ARN, `verify_ssl`, retry settings | — |
| `init/setup.sh` + `docker-compose.yml` | Local dev pair with Dashboards; seeds 4 indices | curl image |
| `generate_fake_indices.py` | Seeds `sample-idx-*` on localhost:9200 | `opensearch-py` (not in `requirements.txt`) |

Configuration precedence: **CLI flags > env vars (`OPENSEARCH_*`) > `config.json` > defaults**. Credentials come from AWS Secrets Manager (`{"username","password"}`), or from `OPENSEARCH_USERNAME`/`OPENSEARCH_PASSWORD` when both are set. With neither, there is no auth, which is how the Docker setup runs.

### Index model

- **Named indices** (`name`, `autofollow: false`) are replicated explicitly with `PUT _plugins/_replication/<idx>/_start`.
- **Auto-follow entries** (`autofollow: true` + `autofollow_pattern`) become an auto-follow rule on the follower. Every leader index that matches the pattern is replicated automatically. These entries are not started one by one, and an entry doesn't need a `name`.

### Execution flow

```
FAILOVER  (normal → DR active)                       FAILBACK  (DR active → normal)
PRIMARY ──► SECONDARY   becomes   SECONDARY ──► PRIMARY    SECONDARY ──► PRIMARY  becomes  PRIMARY ──► SECONDARY

1. SECONDARY: _stop replication for named indices     1. PRIMARY: _stop + remove write blocks
              remove write blocks                        2. PRIMARY: for each rule, stop/unblock matching
2. SECONDARY: for each auto-follow rule, stop/unblock       indices, then delete the rule
   matching indices, then delete the rule               3. SECONDARY: DELETE named + matching indices,
   ⇒ SECONDARY is now read/write (DR testing)              _start named indices (retry with backoff)
3. PRIMARY: DELETE named + matching indices,             4. SECONDARY: create auto-follow rules
   _start named indices from 'secondary-cluster'
   (retry with backoff)
4. PRIMARY: create auto-follow rules
```

`status` is read-only: cluster health, per-index replication status, write blocks, and auto-follow stats on both sides.

The script never creates remote-cluster connections. The aliases `primary-cluster` (on SECONDARY → PRIMARY) and `secondary-cluster` (on PRIMARY → SECONDARY) must already exist: AWS console connections in AWS, `init/setup.sh` locally, `ci/bootstrap.py` in CI.

Interactive prompts (`[y/N]`, typing `DELETE`) use `input()`, so CI always passes `--non-interactive --no-colour`.

### Changes made for CI

| Problem | Fix |
|---|---|
| `do_failover`/`do_failback` returned `None` and the process always exited 0, even when replication failed to start. The script also printed "FAILOVER complete" after a failure. | Both now return `bool`. `main()` exits `1` on abort or on any failed stop/start/rule call, and "complete" only prints on success. |
| `products`, `events` and `inventory` had `autofollow: true` but no `autofollow_pattern`, so `load_autofollow_rules` exited 1 before doing anything. | Added `products*`, `events*` and `inventory*`. |
| `docker-compose.yml` fixes `container_name` and host ports, so two builds would collide. | New CI-only compose file with no names and no published ports, and one compose project per build. |
| `setup.sh` only seeds 4 indices, starts auto-follow entries as named indices, and never creates auto-follow rules. | `ci/bootstrap.py` builds the full normal state from `indices.json` in Python. |
| *Found by the POC run:* `find-missing-replication-indices.py` used `_cat/indices`. After a failover/failback cycle, OpenSearch 2.13 reports a negative store size for re-followed indices, and `_cat/indices` then fails on **every** request with HTTP 400 `Values less than -1 bytes are not supported`. | Lists indices with `GET /_settings?filter_path=*.settings.index.uuid` instead, the same approach `dr-switchover.py` already uses. Avoid `_cat/indices` in any future tooling that runs after a switchover. |
| *Found by the POC run:* `find-missing` printed curl commands with the wrong leader alias: `secondary-cluster` for `primary-to-secondary`, an alias that doesn't exist on the destination. | Uses the alias that lives on the destination and points to the source (`primary-cluster` for `primary-to-secondary`). |
| *Found by the POC run:* with `--non-interactive`, if the cluster being promoted was unreachable, the script auto-confirmed "Proceed anyway?" and then **deleted every managed index on the other cluster**, which at that point held the only good copy. | Three guards, described in §1.1 below. |

### 1.1 Safety guards

These checks run in both interactive and `--non-interactive` mode, and also in `--dry-run`, because they only read.

1. **Pre-check: the cluster being promoted must be healthy.** Before anything changes, failover checks that SECONDARY is green or yellow, and failback checks PRIMARY. If it is red or unreachable, the operation stops with exit 1 and nothing is modified.
2. **No auto-continue after a failed step.** If stopping replication or removing write blocks fails, `--non-interactive` now aborts instead of answering "Proceed anyway?" with yes. Interactive runs still get the prompt.
3. **Never delete the only copy.** Before step 3 deletes indices on the cluster being demoted, each index is checked with `HEAD /<index>`. It must also exist on the new leader. If any index exists only on the cluster being demoted, or either cluster can't answer, nothing is deleted and the script exits 1 with the list. At that point steps 1–2 have already run: the new leader is active and its auto-follow rules are gone, but the other cluster is untouched. Fix the listed indices, then re-run the same operation; the earlier steps are idempotent.

Each guard has a negative test (see §4.3), and none of them changes the behaviour of a healthy run.

Local development (`docker compose up`, `init/setup.sh`) is unchanged.

---

## 2. Architecture

```
CloudBees CI controller
  └─ Pipeline job "opensearch-ccr-dr-poc"  (SCM: this repo, Script Path: ci/Jenkinsfile)
       │
       ▼
Docker-capable agent  (agent any — same as infra/Jenkinsfile_EKS)
  ci/pipeline.sh  →  docker compose -p ccr-<BUILD_TAG> -f ci/docker-compose.ci.yml
  ┌──────────────────── network ccr-<build>_opensearch-net (no host ports) ────────────────────┐
  │                                                                                            │
  │  opensearch-primary  :9200/:9300  ◄──── CCR (transport 9300) ────►  opensearch-secondary   │
  │        ▲  remote alias "secondary-cluster"          remote alias "primary-cluster"  ▲      │
  │        └──────────────── HTTP :9200 ────────────────────────────────────────────────┘      │
  │                              runner (ccr-runner:<build>)                                   │
  │     bootstrap.py → assert_state.py → dr-switchover.py → assert_state.py → find-missing     │
  └────────────────────────────────────────────────────────────────────────────────────────────┘
  reports/*.log + reports/clusters.log  →  archiveArtifacts
  post { always }  →  docker compose down -v  +  docker image rm runner
```

Design choices:

- **Scripts are copied into the runner image, not bind-mounted.** Many CloudBees agents are themselves containers using the host's Docker socket. There, `-v $WORKSPACE:/app` would mount a path from the host, not from the agent. Copying the scripts in avoids that, and the build always runs exactly the code it checked out.
- **The runner reaches the clusters by service name.** `ci/config.ci.json` points at `http://opensearch-primary:9200` and `http://opensearch-secondary:9200`. Remote-cluster seeds use `:9300` on the same network.
- **One compose project per build** (`ccr-<BUILD_TAG>`). Containers, volumes, network and runner image are all scoped to the build, so concurrent builds don't collide. `disableConcurrentBuilds()` is still on for the POC.
- **The Jenkinsfile stays thin; `ci/pipeline.sh` holds the logic.** A failing stage can be reproduced locally with the same command.

### Pipeline stages

| # | Stage | Command | Passes when |
|---|---|---|---|
| 1 | Preflight | `pipeline.sh preflight` | Docker and compose respond. Warns if `vm.max_map_count` < 262144. |
| 2 | Build runner image | `pipeline.sh build` | Image builds |
| 3 | Start clusters | `pipeline.sh up` | Both nodes report healthy within 300s (`--wait`) |
| 4 | Bootstrap replication | `pipeline.sh bootstrap` | Aliases connected. Indices seeded on PRIMARY. Named indices `_start`ed and auto-follow rules created on SECONDARY. All followers `SYNCING`. |
| 5 | Assert normal state | `pipeline.sh assert-normal` | Every managed index is `SYNCING` on SECONDARY, with no leader write blocks and equal doc counts. Rules exist on SECONDARY only, with 0 failed starts. |
| 6 | Failover (dry run) | `pipeline.sh dry-run-failover` | Script plans the failover with no mutating calls; exit 0 |
| 7 | Failover | `pipeline.sh failover` | `dr-switchover.py --operation failover --non-interactive` exits 0 |
| 8 | Assert DR state | `pipeline.sh assert-dr` | Same checks with roles reversed. A probe doc written to SECONDARY appears on PRIMARY, and new `*-probe-dr` indices are auto-followed. |
| 9 | DR test window (optional) | `input` | An approver clicks proceed (`PAUSE_FOR_DR_TEST=true`) |
| 10 | Failback | `pipeline.sh failback` | Script exits 0 |
| 11 | Assert restored state | `pipeline.sh assert-restored` | Normal-state checks pass again, a new probe replicates, and the DR-window probe doc is present on both clusters. |
| 12 | Status report | `pipeline.sh status` + `find-missing` | Report generated (informational) |
| post | Always | `pipeline.sh logs` → `archiveArtifacts reports/**` → `pipeline.sh down` | — |

Parameters:

- `OPERATION`: `full-cycle`, `failover-only` or `status-only`.
- `DRY_RUN`: adds `--dry-run` to failover/failback and skips the state checks.
- `OPENSEARCH_VERSION`: image tag to test.
- `PAUSE_FOR_DR_TEST`: adds a manual approval step between failover and failback. Approvers are the same `submitter` list as `infra/Jenkinsfile_EKS`.

---

## 3. Prerequisites

### CloudBees CI

- The agent pool used by `agent any` must have:
  - Docker Engine 20.10+.
  - **docker compose v2.1+.** Needed for `up --wait`. Check with `docker compose version`.
  - bash.
- **Agent resources:** about 4 GB free RAM (2 × 1 GB heap + JVM overhead + runner) and about 3 GB disk for images. With a 512 MB heap, the CCR follower bootstrap after failover intermittently trips the parent circuit breaker (`Data too large … file_metadata`). Seen in the POC: one run passed, the next failed.
- **Kernel:** `vm.max_map_count ≥ 262144` is recommended on the Docker host. Single-node clusters start without it (bootstrap checks only warn), but anything production-like needs it.
- **Image pulls:**
  - `opensearchproject/opensearch:2.13.0` (about 900 MB) and `python:3.12-slim` from Docker Hub.
  - If agents can't reach Docker Hub, or hit its rate limits, mirror both images to ECR and set the image names in `ci/docker-compose.ci.yml` and `ci/Dockerfile.runner`.
- **Plugins:** Pipeline (workflow-aggregator), Pipeline: Declarative, Timestamper, Pipeline: Input Step. Mask Passwords is already in use in `infra` and will be needed in Phase 3.
- **Credentials:** none for the POC. Security is disabled on the throwaway clusters.
- **SCM:** a GitHub credential on the controller that can read this repo.

### Local (same flow)

Docker Desktop or Docker Engine with compose v2.1+, and about 4 GB RAM for Docker.

---

## 4. Running the POC

### 4.1 Locally

```bash
ci/pipeline.sh all                      # whole cycle, then logs + teardown
KEEP_CLUSTERS=true ci/pipeline.sh all   # keep clusters up afterwards for poking around
ci/pipeline.sh down                     # clean up a kept run (same BUILD_TAG)
```

Individual stages: `ci/pipeline.sh up`, `bootstrap`, `failover`, `assert-dr`, and so on. Logs go to `reports/<stage>.log`.

### 4.2 In CloudBees CI

1. **New Item → Pipeline**, named for example `opensearch-ccr-dr-poc`, in the DevOps folder.
2. **Pipeline → Definition:** *Pipeline script from SCM*. Git, this repo, branch `*/main` (or `*/support-auto-follow` while testing). **Script Path:** `ci/Jenkinsfile`.
3. Save, then **Build Now** once. The first build registers the parameters and may stop at the parameter definition.
4. **Build with Parameters:** `OPERATION=full-cycle`, `DRY_RUN=false`. A green build takes about 5–8 minutes, most of it image pulls on a cold agent.
5. Check the archived `reports/`: `failover.log`, `assert-dr.log`, `failback.log`, `assert-restored.log` and `clusters.log`.
6. Optional: validate the Jenkinsfile syntax before committing:
   ```bash
   curl -sS -u "$JENKINS_USER:$JENKINS_TOKEN" -X POST -F "jenkinsfile=<ci/Jenkinsfile" "$JENKINS_URL/pipeline-model-converter/validate"
   ```

### 4.3 POC exit criteria

- [ ] `full-cycle` is green on a CloudBees agent three runs in a row.
- [ ] A forced failure turns the build red at the right stage **and PRIMARY keeps all its indices and docs**. Two guard tests, both passed locally:
  - *Secondary down:* `docker compose -p ccr-<tag> -f ci/docker-compose.ci.yml stop opensearch-secondary`, then `ci/pipeline.sh failover`. Exits 1 at the pre-check; nothing is modified.
  - *Index missing on the new leader:* stop replication for `orders` on SECONDARY and delete it there, then `ci/pipeline.sh failover`. Exits 1 with "Refusing to delete — these indices would be lost"; PRIMARY's `orders` is intact.
- [ ] `post` always tears down: `docker ps -a --filter name=ccr-` is empty after the build.
- [ ] Two builds in parallel (with `disableConcurrentBuilds` temporarily removed) don't interfere.

---

## 5. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `up` times out | Agent short on RAM, or the image pull is slow. Check `reports/clusters.log`. |
| `assert-dr` / `assert-restored`: `follower status=FAILED`, and `clusters.log` shows `CircuitBreakingException … file_metadata` | Heap too small for many indices bootstrapping at once. Keep `OS_HEAP` ≥ `1g`, or reduce the number of indices. |
| `unknown flag: --wait` | Compose older than v2.1. Upgrade the agent's compose plugin. |
| Bootstrap: `remote 'primary-cluster' never connected` | Transport port 9300 is blocked inside the network, or the cluster names are wrong. Check `_remote/info`. |
| Bootstrap stuck on `not yet SYNCING` for pattern indices | Auto-follow polls the leader about every 30s. Raise `--timeout`, and check `autofollow_stats` `failed_indices`. |
| `failover` exits 1 | Look for `[ERR ]` lines in `reports/failover.log`. The script now fails on any failed stop/start/rule call. |
| `assert-*` doc-count mismatch | Replication lag. The check retries for 240s; persistent mismatches mean the follower is `PAUSED` or `FAILED`. Check `_plugins/_replication/<idx>/_status`. |
| Leftover containers after an aborted build | `BUILD_TAG=<tag> ci/pipeline.sh down`, or `docker compose ls` to list `ccr-*` projects. |

---

## 6. Roadmap

| Phase | Goal | Work |
|---|---|---|
| **0. Prerequisites** (week 1) | Agent ready | Check Docker, compose v2.1+, RAM and `vm.max_map_count` on the agent pool. Mirror images to ECR if needed. Create the CloudBees job. |
| **1. POC** (this change) | Full cycle green on throwaway clusters | `ci/` folder, exit-code fix, safety guards, `indices.json` fix, `find-missing` fixes, this doc. Full cycle is already green locally (Docker 29 / compose v5). Meet the exit criteria in §4.3. |
| **2. Hardening** | Pipeline-grade tool | Add `--output json` to `status` (checks stop needing log parsing). Add the guard negative tests (§4.3) as a pipeline stage. Add pytest unit tests for config/rule parsing with a mocked HTTP layer, and ruff, as a PR-check stage. Publish a versioned runner image to ECR. Add an `OPENSEARCH_VERSION` matrix (ties in with `opensearch-upgrade-checker`). Merge the duplicate config/HTTP code in the two scripts into one module. |
| **3. AWS staging ↔ staging-failover** | Same pipeline against real domains | New `TARGET` parameter (`docker` / `staging`). Assume a role per account, using the `aws_accounts_id_mapping` pattern from `infra/Jenkinsfile_EKS` (staging `768619733044`, staging-failover `202047904840`). `OPENSEARCH_CREDENTIALS_SECRET_ARN` from Secrets Manager, with `maskPasswords`. Agents need a network path to the VPC domain endpoints. Cross-cluster connections are set up in AWS (the script never creates them). `DRY_RUN=true` by default, plus an approval gate before the real run. Skip bootstrap; run `status` → dry run → approve → failover → assert → approve → failback → assert. |
| **4. Production DR runbook** | Controlled prod switchover and DR drills | Production and production-failover targets limited to the `main` branch, using the same guard as `Jenkinsfile_EKS`. Two-person approval, reusing the "pipeline starter can't approve" loop from `Jenkinsfile_EKS`. Slack notifications through the existing `slack-token`/`slack-channel` credentials. Keep the archived reports as the audit trail. Scheduled quarterly DR drill against staging. |

### Risks and notes

- **Fixed: `--non-interactive` could delete the only good copy.** The POC found that with the promoted cluster unreachable, `failover --non-interactive` deleted every index on PRIMARY before exiting 1. The guards in §1.1 now stop this. Both scenarios (secondary down; an index missing on the new leader) are covered by negative tests that leave PRIMARY untouched.
- **A real disaster with PRIMARY down.** Failover still promotes SECONDARY (steps 1–2 only touch SECONDARY). Step 3 then fails because PRIMARY can't be reached, so the run exits 1 and nothing is deleted. Re-run failover once PRIMARY is back, to set up reverse replication.
- **Failover and failback delete the indices on the new follower.** On AWS that is real data loss if the leader is stale. Phase 3 keeps a dry run plus approval in front of every real run.
- `use_roles: all_access` in `_start` and auto-follow bodies only matters for self-managed clusters with security enabled. AWS ignores it.
- The untracked `opensearch-ccr-poc/` folder in the repo root is a byte-identical copy of the repo. Delete it, or add it to `.gitignore`, so it isn't committed by mistake.

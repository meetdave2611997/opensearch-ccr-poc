# OpenSearch CCR DR switchover on CloudBees CI

How `dr-switchover.py` (OpenSearch Cross-Cluster Replication failover and failback) runs from a CloudBees CI pipeline. Each build runs **one operation** (`failover`, `failback` or `status`) for **one team**, and a person approves every step. The first stage is a proof of concept (POC) on Docker clusters, one pair per team. This page covers how the script works, the pipeline architecture, prerequisites, how to run the POC, and the plan for moving to real AWS domains.

| | |
|---|---|
| Pipeline | [`ci/Jenkinsfile`](../ci/Jenkinsfile) (declarative, CloudBees CI) |
| Stage logic | [`ci/pipeline.sh`](../ci/pipeline.sh). Runs the same way on a laptop and on CloudBees. |
| Clusters | [`ci/docker-compose.ci.yml`](../ci/docker-compose.ci.yml): OpenSearch 2.13.0 primary + secondary, one pair per team |
| Teams | [`teams/<team>/indices.json`](../teams/README.md): `rit`, `researchdesk`, `eim` |
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

Every confirmation is answered by a person; there is no auto-confirm mode (`--non-interactive` was removed). The prompts are "Stop replication…?", "Delete auto-follow rules…?", "Type DELETE", "Proceed anyway?" and "Replace rule?". They are answered either:

- **at a terminal** (local runs), or
- **through approval files** with `--approval-dir DIR`, which the CloudBees pipeline uses (§2.1). The script writes `DIR/prompt.json` and waits for `DIR/answer-<id>`. An unanswered prompt is treated as "no" after `--approval-timeout` seconds (default 3600).

If stdin is closed and there is no approval directory, a prompt reads as "no", so the script aborts instead of hanging or crashing.

### Changes made for CI

| Problem | Fix |
|---|---|
| `do_failover`/`do_failback` returned `None` and the process always exited 0, even when replication failed to start. The script also printed "FAILOVER complete" after a failure. | Both now return `bool`. `main()` exits `1` on abort or on any failed stop/start/rule call, and "complete" only prints on success. |
| `products`, `events` and `inventory` had `autofollow: true` but no `autofollow_pattern`, so `load_autofollow_rules` exited 1 before doing anything. | Added `products*`, `events*` and `inventory*`. |
| `docker-compose.yml` fixes `container_name` and host ports, so cluster pairs would collide. | New CI-only compose file with no names and no published ports, and one compose project per team (`ccr-<team>`). |
| `setup.sh` only seeds 4 indices, starts auto-follow entries as named indices, and never creates auto-follow rules. | `ci/bootstrap.py` builds the full normal state from `indices.json` in Python. |
| *Found by the POC run:* `find-missing-replication-indices.py` used `_cat/indices`. After a failover/failback cycle, OpenSearch 2.13 reports a negative store size for re-followed indices, and `_cat/indices` then fails on **every** request with HTTP 400 `Values less than -1 bytes are not supported`. | Lists indices with `GET /_settings?filter_path=*.settings.index.uuid` instead, the same approach `dr-switchover.py` already uses. Avoid `_cat/indices` in any future tooling that runs after a switchover. |
| *Found by the POC run:* `find-missing` printed curl commands with the wrong leader alias: `secondary-cluster` for `primary-to-secondary`, an alias that doesn't exist on the destination. | Uses the alias that lives on the destination and points to the source (`primary-cluster` for `primary-to-secondary`). |
| *Found by the POC run:* with the (since removed) `--non-interactive` flag, if the cluster being promoted was unreachable, the script auto-confirmed "Proceed anyway?" and then **deleted every managed index on the other cluster**, which at that point held the only good copy. | Three guards, described in §1.1 below. |

### 1.1 Safety guards

These checks run in every mode (terminal, approval files, `--dry-run`), because they only read.

1. **Pre-check: the cluster being promoted must be healthy.** Before anything changes, failover checks that SECONDARY is green or yellow, and failback checks PRIMARY. If it is red or unreachable, the operation stops with exit 1 and nothing is modified.
2. **No auto-continue after a failed step.** If stopping replication or removing write blocks fails, the script asks "Proceed anyway?", and a person has to answer it. Nothing answers it automatically.
3. **Never delete the only copy.** Before step 3 deletes indices on the cluster being demoted, each index is checked with `HEAD /<index>`. It must also exist on the new leader. If any index exists only on the cluster being demoted, or either cluster can't answer, nothing is deleted and the script exits 1 with the list. At that point steps 1–2 have already run: the new leader is active and its auto-follow rules are gone, but the other cluster is untouched. Fix the listed indices, then re-run the same operation; the earlier steps are idempotent.

Each guard has a negative test (see §4.3), and none of them changes the behaviour of a healthy run.

Local development (`docker compose up`, `init/setup.sh`) is unchanged.

---

## 2. Architecture

```
CloudBees CI controller
  └─ Pipeline job "opensearch-ccr-dr"  (SCM: this repo, Script Path: ci/Jenkinsfile)
       │   parameters: OPERATION = status | failover | failback,  TEAM = rit | researchdesk | eim
       ▼
Docker-capable agent  (agent any — same as infra/Jenkinsfile_EKS)
  ci/pipeline.sh  →  docker compose -p ccr-<team> -f ci/docker-compose.ci.yml        (kept between builds)
  ┌──────────────────── network ccr-<team>_opensearch-net (no host ports) ─────────────────────┐
  │  opensearch-primary  :9200/:9300  ◄──── CCR (transport 9300) ────►  opensearch-secondary   │
  │        ▲  remote alias "secondary-cluster"          remote alias "primary-cluster"  ▲      │
  │        └──────────────── HTTP :9200 ────────────────────────────────────────────────┘      │
  │  ccr-<team>-operation  (runner image: dr-switchover.py --approval-dir /tmp/approvals)      │
  │        writes prompt.json ──► pipeline.sh op-prompt ──► CloudBees `input` ──► approver     │
  │        reads answer-<id>  ◄── pipeline.sh op-answer ◄───────────────────────────┘          │
  └────────────────────────────────────────────────────────────────────────────────────────────┘
  reports/*.log + reports/clusters.log  →  archiveArtifacts
```

Design choices:

- **One operation per build; the clusters persist.** Failover and failback are separate builds, possibly days apart, so each team's cluster pair (`ccr-<team>`) is kept between builds. The first build for a team creates the pair and brings it to the normal state with `ci/bootstrap.py`; later builds reuse it. `ci/pipeline.sh down` removes it.
- **Team = folder.** `TEAM` picks `teams/<team>/indices.json`, passed to every script as `OPENSEARCH_INDICES_FILE`, and the compose project `ccr-<team>`. Phase 3 adds `teams/<team>/config.json` for the team's AWS endpoints.
- **Interactive through approvals.** The script runs in a background container with `--approval-dir`. The pipeline polls for prompts and shows each one as a CloudBees `input`. The approver's answer goes back into the container. The script itself is unchanged apart from where it reads answers.
- **Scripts are copied into the runner image, not bind-mounted.** Many CloudBees agents are themselves containers using the host's Docker socket, where a `$WORKSPACE` bind mount would point at the wrong filesystem. Answers are delivered with `docker exec` for the same reason.
- **The Jenkinsfile stays thin; `ci/pipeline.sh` holds the logic.** Every stage can be run locally with the same command.

### 2.1 How an approval works

1. The script reaches a prompt and atomically writes `/tmp/approvals/prompt.json` in its container: `{"id":"002","kind":"yesno","question":"Delete 5 auto-follow rule(s) from SECONDARY…?"}`. It then waits.
2. The pipeline loop (`ci/pipeline.sh op-prompt`, every 3s) sees the prompt and opens a CloudBees **input** for the `submitter` list (the same as `infra/Jenkinsfile_EKS`):
   - `yesno` → **Yes, proceed** / **Abort**.
   - `delete` → the indices to be deleted are listed, and the approver must type `DELETE` in a text field. Anything else counts as "no".
3. The answer (`yes`, `no` or `DELETE`, never free text) is written to `answer-<id>` with `docker exec`. The script continues or aborts.
4. **Abort** or a build timeout means "no", and the script stops cleanly with exit 1. If the build ends while a prompt is open, `post` answers "no", waits for the script to exit, and removes the container.

### Pipeline stages

| # | Stage | Command | Runs for | Passes when |
|---|---|---|---|---|
| 1 | Preflight | `pipeline.sh preflight` | all | Docker and compose respond. Warns if `vm.max_map_count` < 262144. |
| 2 | Build runner image | `pipeline.sh build` | all | Image builds |
| 3 | Team clusters | `pipeline.sh ensure` | all | The team's pair is healthy. If missing, it is created and bootstrapped: aliases, seeded indices, replication from PRIMARY to SECONDARY, all followers `SYNCING`. |
| 4 | Check current state | `pipeline.sh check-state normal\|dr` | failover, failback | Failover needs the normal state (PRIMARY → SECONDARY); failback needs the DR state (SECONDARY → PRIMARY). Otherwise the build stops and nothing changes. |
| 5 | Plan (dry run) | `pipeline.sh plan <op>` | failover, failback | The dry run lists every call it would make, plus the health and "only copy" safety checks. |
| 6 | Approve plan | `input` | failover, failback | An approver starts the operation after reading the plan |
| 7 | Run with approvals | `op-start` → loop (`op-logs`, `op-prompt`, `input`, `op-answer`) → `op-finish` | failover, failback | Every prompt has been answered by an approver and the script exits 0 |
| 8 | Verify | `pipeline.sh verify <op>` | failover, failback | The state matches the operation: every index `SYNCING` on the new follower, equal doc counts, rules only on the follower. A probe doc and new probe indices written to the new leader reach the follower. |
| 9 | Status report | `pipeline.sh status` + `find-missing <direction>` | all | Report generated |
| post | Always | `op-cleanup` → `logs` → `archiveArtifacts reports/**` | all | Clusters are **kept**; failover and failback are separate builds. |

Parameters:

- `OPERATION`: `status` (read-only, no approvals), `failover`, `failback`.
- `TEAM`: `rit`, `researchdesk`, `eim`. Adding a team is described in [`teams/README.md`](../teams/README.md).

---

## 3. Prerequisites

### CloudBees CI

- The agent pool used by `agent any` must have:
  - Docker Engine 20.10+.
  - **docker compose v2.1+.** Needed for `up --wait`. Check with `docker compose version`.
  - bash.
- **Agent resources:** about 3 GB free RAM **per team pair** (2 × 1 GB heap + JVM overhead), so about 9 GB with all three teams running, plus about 3 GB disk for images. Pairs stay up between builds; `TEAM=<team> ci/pipeline.sh down` frees one and about 3 GB disk for images. With a 512 MB heap, the CCR follower bootstrap after failover intermittently trips the parent circuit breaker (`Data too large … file_metadata`). Seen in the POC: one run passed, the next failed.
- **Kernel:** `vm.max_map_count ≥ 262144` is recommended on the Docker host. Single-node clusters start without it (bootstrap checks only warn), but anything production-like needs it.
- **Image pulls:**
  - `opensearchproject/opensearch:2.13.0` (about 900 MB) and `python:3.12-slim` from Docker Hub.
  - If agents can't reach Docker Hub, or hit its rate limits, mirror both images to ECR and set the image names in `ci/docker-compose.ci.yml` and `ci/Dockerfile.runner`.
- **Plugins:** Pipeline (workflow-aggregator), Pipeline: Declarative, Timestamper, Pipeline: Input Step. The approvers in the `submitter` list need *Job/Build* permission on the job to answer inputs. Mask Passwords is already in use in `infra` and will be needed in Phase 3.
- **Credentials:** none for the POC. Security is disabled on the throwaway clusters.
- **SCM:** a GitHub credential on the controller that can read this repo.

### Local (same flow)

Docker Desktop or Docker Engine with compose v2.1+, and about 3 GB RAM for Docker per team pair.

---

## 4. Running the POC

### 4.1 Locally

Locally, prompts are answered at your terminal. This is the same script and the same prompts as in CloudBees.

```bash
export TEAM=rit
ci/pipeline.sh build && ci/pipeline.sh ensure     # create the rit pair (NORMAL state)
ci/pipeline.sh plan failover                      # dry run
ci/pipeline.sh run failover                       # answer y / y / DELETE at the prompts
ci/pipeline.sh verify failover
ci/pipeline.sh status
ci/pipeline.sh run failback
ci/pipeline.sh verify failback
ci/pipeline.sh down                               # remove the rit pair
```

`ci/pipeline.sh reset` rebuilds a team's pair from scratch. Logs go to `reports/<stage>.log`.

### 4.2 In CloudBees CI

1. **New Item → Pipeline**, named for example `opensearch-ccr-dr`, in the DevOps folder.
2. **Pipeline → Definition:** *Pipeline script from SCM*. Git, this repo, branch `*/main` (or `*/support-auto-follow` while testing). **Script Path:** `ci/Jenkinsfile`.
3. Save, then **Build Now** once. The first build registers the parameters.
4. **Build with Parameters:** `OPERATION=status`, `TEAM=rit`. This creates the rit pair (about 3 minutes on first use) and prints the report. No approvals are involved.
5. **Build with Parameters:** `OPERATION=failover`, `TEAM=rit`.
   - The build stops at **Approve plan**. Open the *Plan (dry run)* stage log, then click **Start failover**.
   - In **Run with approvals**, answer each prompt in the build's input panel (*Paused for Input*, or the stage view): **Yes, proceed** twice, then type `DELETE` for the index-deletion prompt.
   - **Verify** checks the DR state.
6. **Build with Parameters:** `OPERATION=failback`, `TEAM=rit`. Approve it the same way.
7. Check the archived `reports/`: `plan-*.log`, `failover.log`/`failback.log` (the script output, including every approval), `verify-*.log`, `status.log` and `clusters.log`.
8. Optional: validate the Jenkinsfile syntax before committing:
   ```bash
   curl -sS -u "$JENKINS_USER:$JENKINS_TOKEN" -X POST -F "jenkinsfile=<ci/Jenkinsfile" "$JENKINS_URL/pipeline-model-converter/validate"
   ```

### 4.3 POC exit criteria

Each item below also passed locally, using a scripted approver that drives `op-prompt` / `op-answer` exactly like the Jenkinsfile loop.

- [ ] `failover` → `failback` → `status` is green on CloudBees for each team.
- [ ] **Rejecting** the first prompt ends the build red, and the team is still in the normal state.
- [ ] **Not typing `DELETE`** at the deletion prompt ends the build red, with nothing deleted. Steps 1–2 have already promoted SECONDARY, so the team needs `ci/pipeline.sh reset` (POC) or a new failover attempt.
- [ ] **Aborting the build** while a prompt is open answers "no", and `post` removes the operation container.
- [ ] Running `failback` when the team is in the normal state stops at **Check current state** and changes nothing.
- [ ] Guard tests from §1.1 leave PRIMARY untouched:
  - *Secondary down:* `docker compose -p ccr-<team> -f ci/docker-compose.ci.yml stop opensearch-secondary`, then run a failover. It exits 1 at the health check.
  - *Index missing on the new leader:* stop replication for `orders` on SECONDARY and delete it there, then run a failover. It exits 1 with "Refusing to delete".

---

## 5. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `up` times out | Agent short on RAM, or the image pull is slow. Check `reports/clusters.log`. |
| `verify` or `ensure`: `follower status=FAILED`, and `clusters.log` shows `CircuitBreakingException … file_metadata` | Heap too small for many indices bootstrapping at once. Keep `OS_HEAP` ≥ `1g`, or reduce the number of indices. |
| `unknown flag: --wait` | Compose older than v2.1. Upgrade the agent's compose plugin. |
| Bootstrap: `remote 'primary-cluster' never connected` | Transport port 9300 is blocked inside the network, or the cluster names are wrong. Check `_remote/info`. |
| Bootstrap stuck on `not yet SYNCING` for pattern indices | Auto-follow polls the leader about every 30s. Raise `--timeout`, and check `autofollow_stats` `failed_indices`. |
| `failover` exits 1 | Look for `[ERR ]` lines in `reports/failover.log`. The script fails on any failed stop/start/rule call, and on any prompt answered "no". |
| Build stops at **Check current state** | The team isn't in the state the operation needs. Run `status`; after a failover, run failback, not failover again. If the pair is half-switched after a rejected deletion prompt, use `TEAM=<team> ci/pipeline.sh reset` (POC only). |
| `An operation is already running for team …` | A previous build's operation container is still waiting. `TEAM=<team> ci/pipeline.sh op-cleanup` answers "no" and removes it. |
| `verify` / `check-state` doc-count mismatch | Replication lag. The check retries for 240s; persistent mismatches mean the follower is `PAUSED` or `FAILED`. Check `_plugins/_replication/<idx>/_status`. |
| Need to free memory on the agent | `docker compose ls` lists the `ccr-<team>` pairs; `TEAM=<team> ci/pipeline.sh down` removes one. |

---

## 6. Roadmap

| Phase | Goal | Work |
|---|---|---|
| **0. Prerequisites** (week 1) | Agent ready | Check Docker, compose v2.1+, RAM and `vm.max_map_count` on the agent pool. Mirror images to ECR if needed. Create the CloudBees job. |
| **1. POC** (this change) | Approved failover / failback per team on Docker | `ci/` folder, team folders, approval-driven interactive runs, exit-code fix, safety guards, `indices.json` fix, `find-missing` fixes, this doc. Green locally (Docker 29 / compose v5). Meet the exit criteria in §4.3. |
| **2. Hardening** | Pipeline-grade tool | Add `--output json` to `status` (checks stop needing log parsing). Add the guard negative tests (§4.3) as a pipeline stage. Add pytest unit tests for config/rule parsing with a mocked HTTP layer, and ruff, as a PR-check stage. Publish a versioned runner image to ECR. Use Lockable Resources to lock per team instead of `disableConcurrentBuilds()`, so different teams can run at the same time. Merge the duplicate config/HTTP code in the two scripts into one module. |
| **3. AWS staging ↔ staging-failover** | Same pipeline against real domains | `teams/<team>/config.json` per team, with the AWS domain endpoints and secret ARN; `ensure` is skipped on AWS. Assume a role per account, using the `aws_accounts_id_mapping` pattern from `infra/Jenkinsfile_EKS` (staging `768619733044`, staging-failover `202047904840`). `OPENSEARCH_CREDENTIALS_SECRET_ARN` from Secrets Manager, with `maskPasswords`. Agents need a network path to the VPC domain endpoints. Cross-cluster connections are set up in AWS (the script never creates them). Same flow: check state → plan → approve → run with approvals → verify. |
| **4. Production DR runbook** | Controlled prod switchover and DR drills | Production and production-failover targets limited to the `main` branch, using the same guard as `Jenkinsfile_EKS`. Two-person approval, reusing the "pipeline starter can't approve" loop from `Jenkinsfile_EKS`. Slack notifications through the existing `slack-token`/`slack-channel` credentials. Keep the archived reports as the audit trail. Scheduled quarterly DR drill against staging. |

### Risks and notes

- **Fixed: unattended runs could delete the only good copy.** The POC found that with the promoted cluster unreachable, `failover --non-interactive` deleted every index on PRIMARY before exiting 1. The guards in §1.1 now stop this, and the auto-confirm flag has been removed. Both scenarios (secondary down; an index missing on the new leader) are covered by negative tests that leave PRIMARY untouched.
- **A real disaster with PRIMARY down.** Failover still promotes SECONDARY (steps 1–2 only touch SECONDARY). Step 3 then fails because PRIMARY can't be reached, so the run exits 1 and nothing is deleted. Re-run failover once PRIMARY is back, to set up reverse replication.
- **Failover and failback delete the indices on the new follower.** On AWS that is real data loss if the leader is stale. Every run shows a dry-run plan and needs explicit approvals, including typing `DELETE`.
- `use_roles: all_access` in `_start` and auto-follow bodies only matters for self-managed clusters with security enabled. AWS ignores it.
- The untracked `opensearch-ccr-poc/` folder in the repo root is a byte-identical copy of the repo. Delete it, or add it to `.gitignore`, so it isn't committed by mistake.

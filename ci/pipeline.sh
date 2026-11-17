#!/usr/bin/env bash
# CCR DR pipeline driver. ci/Jenkinsfile calls these subcommands; you can run the
# same ones locally. Every command except preflight/build needs TEAM.
#
#   preflight                  check docker / compose / vm.max_map_count
#   build                      build the runner image (python + scripts + teams/)
#   ensure                     start the team's cluster pair + bootstrap it if not running
#   reset                      delete the team's pair and build a fresh one (NORMAL state)
#   check-state normal|dr      fail unless the pair is in that state
#   plan failover|failback     dry run: show what would happen, change nothing
#   run failover|failback      run it here, answering every prompt at this terminal
#   verify failover|failback   check the state after the operation (+ write probes)
#   status                     dr-switchover.py --operation status
#   find-missing [direction]   primary-to-secondary (default) | secondary-to-primary
#   logs                       save cluster logs to reports/
#   down                       remove the team's containers, volumes and network
#
# Used by the CloudBees approval loop (prompts answered through files, see
# --approval-dir in dr-switchover.py):
#   op-start failover|failback  start the operation in a background container
#   op-prompt                   print the pending prompt (id, kind, question, details...)
#   op-answer <id> yes|no|DELETE
#   op-running                  exit 0 while the operation is still running
#   op-logs                     print new output and append it to reports/<operation>.log
#   op-finish                   wait for the operation; exit with its exit code
#   op-cleanup                  answer "no" to anything pending, then remove the container
#
# Environment:
#   TEAM                team folder under teams/ (rit, researchdesk, eim, ...)
#   OPENSEARCH_VERSION  OpenSearch image tag; default 2.13.0
#   OS_HEAP             JVM heap per node; default 1g (512m trips the circuit breaker during CCR bootstrap)
#   REPORTS_DIR         where logs go; default ./reports

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export OPENSEARCH_VERSION="${OPENSEARCH_VERSION:-2.13.0}"
export OS_HEAP="${OS_HEAP:-1g}"
REPORTS_DIR="${REPORTS_DIR:-$ROOT/reports}"
CONFIG="ci/config.ci.json"
APPROVAL_DIR="/tmp/approvals"            # inside the operation container
mkdir -p "$REPORTS_DIR"

TEAM="${TEAM:-}"
if [[ -n "$TEAM" ]]; then
  [[ "$TEAM" =~ ^[a-z0-9_-]+$ ]] || { echo "TEAM must be lowercase [a-z0-9_-]: '$TEAM'" >&2; exit 2; }
  [[ -f "teams/$TEAM/indices.json" ]] || { echo "unknown team '$TEAM': teams/$TEAM/indices.json not found" >&2; exit 2; }
fi
PROJ="ccr-${TEAM:-none}"
export RUNNER_IMAGE="${RUNNER_IMAGE:-ccr-runner:${PROJ}}"
export OPENSEARCH_INDICES_FILE="teams/${TEAM:-none}/indices.json"
COMPOSE=(docker compose -p "$PROJ" -f ci/docker-compose.ci.yml)
OP_CONTAINER="${PROJ}-operation"

log() { printf '\n[pipeline] %s\n' "$*"; }
need_team() { [[ -n "$TEAM" ]] || { echo "set TEAM (one of: $(ls teams | grep -v README | tr '\n' ' '))" >&2; exit 2; }; }

# Run a command, tee its output to reports/<name>.log, keep its exit code.
record() {
  local name="$1"; shift
  log "$name → $REPORTS_DIR/$name.log"
  "$@" 2>&1 | tee "$REPORTS_DIR/$name.log"
}

# Run a script inside a one-off runner container on the team's cluster network.
runner() {
  if [[ -t 0 ]]; then
    "${COMPOSE[@]}" --profile tools run --rm runner "$@"
  else
    "${COMPOSE[@]}" --profile tools run --rm -T runner "$@"
  fi
}

need_op() {
  case "${1:-}" in failover|failback) ;; *) echo "operation must be failover or failback" >&2; exit 2;; esac
}

clusters_healthy() {
  local s
  for svc in opensearch-primary opensearch-secondary; do
    s="$("${COMPOSE[@]}" ps --format '{{.Health}}' "$svc" 2>/dev/null || true)"
    [[ "$s" == "healthy" ]] || return 1
  done
}

# ── Stages ──────────────────────────────────────────────────────────────────

stage_preflight() {
  docker version --format 'docker server {{.Server.Version}}'
  docker compose version
  local mmc_file=/proc/sys/vm/max_map_count
  if [[ -r "$mmc_file" ]]; then
    local mmc; mmc="$(cat "$mmc_file")"
    if (( mmc < 262144 )); then
      echo "WARN: vm.max_map_count=$mmc (< 262144). Single-node clusters still start, but"
      echo "      production-like settings need: sudo sysctl -w vm.max_map_count=262144"
    else
      echo "vm.max_map_count=$mmc"
    fi
  else
    echo "vm.max_map_count not readable here (Docker Desktop / non-Linux host) — skipping check"
  fi
  echo "team=${TEAM:-<unset>} compose project=$PROJ runner image=$RUNNER_IMAGE opensearch=$OPENSEARCH_VERSION heap=$OS_HEAP"
}

stage_build() { docker build -f ci/Dockerfile.runner -t "$RUNNER_IMAGE" .; }

stage_ensure() {
  need_team
  if clusters_healthy; then
    echo "Team '$TEAM' cluster pair ($PROJ) is already running — reusing it."
    return 0
  fi
  echo "Team '$TEAM' cluster pair ($PROJ) is not running — creating it in the NORMAL state."
  "${COMPOSE[@]}" up -d --wait --wait-timeout 300 opensearch-primary opensearch-secondary
  runner ci/bootstrap.py --config "$CONFIG" --no-colour
}

stage_reset() {
  need_team
  stage_down
  stage_ensure
}

stage_check_state() {
  need_team
  local expect="${1:-}"
  [[ "$expect" == normal || "$expect" == dr ]] || { echo "check-state needs normal|dr" >&2; exit 2; }
  if ! runner ci/assert_state.py --config "$CONFIG" --expect "$expect" --timeout 60 --no-colour; then
    echo
    echo "Team '$TEAM' is not in the expected $(tr '[:lower:]' '[:upper:]' <<<"$expect") state, so this operation was not started."
    [[ "$expect" == normal ]] && echo "Hint: if a failover already ran, run FAILBACK instead."
    [[ "$expect" == dr ]]     && echo "Hint: FAILBACK only makes sense after a FAILOVER."
    return 1
  fi
}

stage_plan() {
  need_team; need_op "${1:-}"
  runner dr-switchover.py --config "$CONFIG" --operation "$1" --dry-run --no-colour
}

stage_run() {
  need_team; need_op "${1:-}"
  runner dr-switchover.py --config "$CONFIG" --operation "$1" --no-colour
}

stage_verify() {
  need_team; need_op "${1:-}"
  if [[ "$1" == failover ]]; then
    runner ci/assert_state.py --config "$CONFIG" --expect dr --write-probe --probe-tag dr --no-colour
  else
    runner ci/assert_state.py --config "$CONFIG" --expect normal --write-probe --probe-tag restored --no-colour
  fi
}

stage_status() {
  need_team
  runner dr-switchover.py --config "$CONFIG" --operation status --no-colour
}

stage_find_missing() {
  need_team
  runner find-missing-replication-indices.py --config "$CONFIG" \
    --direction "${1:-primary-to-secondary}" --no-colour
}

stage_logs() {
  need_team
  "${COMPOSE[@]}" logs --no-color --timestamps > "$REPORTS_DIR/clusters.log" 2>&1 || true
  log "cluster logs → $REPORTS_DIR/clusters.log"
}

stage_down() {
  need_team
  docker rm -f "$OP_CONTAINER" >/dev/null 2>&1 || true
  "${COMPOSE[@]}" --profile tools down -v --remove-orphans || true
}

# ── Operation with approvals (CloudBees) ───────────────────────────────────

op_start() {
  need_team; need_op "${1:-}"
  if [[ "$(docker inspect -f '{{.State.Running}}' "$OP_CONTAINER" 2>/dev/null || true)" == "true" ]]; then
    echo "An operation is already running for team '$TEAM' ($OP_CONTAINER). Not starting another." >&2
    exit 1
  fi
  docker rm -f "$OP_CONTAINER" >/dev/null 2>&1 || true
  echo "$1" > "$REPORTS_DIR/.op"
  echo 0 > "$REPORTS_DIR/.op-lines"
  : > "$REPORTS_DIR/$1.log"
  "${COMPOSE[@]}" --profile tools run -d --name "$OP_CONTAINER" runner \
    dr-switchover.py --config "$CONFIG" --operation "$1" --no-colour --approval-dir "$APPROVAL_DIR"
  echo "Started $1 for team '$TEAM' in container $OP_CONTAINER"
}

op_running() {
  [[ "$(docker inspect -f '{{.State.Running}}' "$OP_CONTAINER" 2>/dev/null || true)" == "true" ]]
}

# Prints: id \n kind \n question \n detail lines...   (nothing when no prompt is pending)
op_prompt() {
  op_running || return 0
  docker exec "$OP_CONTAINER" python3 -c '
import json, pathlib, sys
d = pathlib.Path(sys.argv[1]); p = d / "prompt.json"
if not p.exists(): sys.exit(0)
try: q = json.loads(p.read_text())
except ValueError: sys.exit(0)                     # half-written; try again next poll
if (d / ("answer-" + q["id"])).exists(): sys.exit(0)
print(q["id"]); print(q["kind"]); print(q["question"].replace("\n", " "))
for line in q.get("details", []): print(line)
' "$APPROVAL_DIR"
}

op_answer() {
  local id="${1:-}" answer="${2:-}"
  [[ "$id" =~ ^[0-9]+$ ]] || { echo "bad prompt id '$id'" >&2; exit 2; }
  case "$answer" in yes|no|DELETE) ;; *) echo "answer must be yes|no|DELETE" >&2; exit 2;; esac
  docker exec "$OP_CONTAINER" sh -c \
    "printf '%s' '$answer' > '$APPROVAL_DIR/.answer-$id' && mv '$APPROVAL_DIR/.answer-$id' '$APPROVAL_DIR/answer-$id'"
}

op_logs() {
  local op seen total tmp
  op="$(cat "$REPORTS_DIR/.op" 2>/dev/null || echo operation)"
  seen="$(cat "$REPORTS_DIR/.op-lines" 2>/dev/null || echo 0)"
  tmp="$(mktemp)"
  docker logs "$OP_CONTAINER" > "$tmp" 2>&1 || true
  total="$(wc -l < "$tmp" | tr -d ' ')"
  if (( total > seen )); then
    tail -n +"$((seen + 1))" "$tmp" | head -n "$((total - seen))" | tee -a "$REPORTS_DIR/$op.log"
    echo "$total" > "$REPORTS_DIR/.op-lines"
  fi
  rm -f "$tmp"
}

op_finish() {
  local code
  code="$(docker wait "$OP_CONTAINER")"
  op_logs
  docker rm -f "$OP_CONTAINER" >/dev/null 2>&1 || true
  echo "operation exit code: $code"
  return "$code"
}

op_cleanup() {
  need_team
  if op_running; then
    local pending id
    pending="$(op_prompt || true)"
    if [[ -n "$pending" ]]; then
      id="$(head -n1 <<<"$pending")"
      echo "Build ended with approval #$id still open — answering 'no' so the script aborts cleanly."
      op_answer "$id" no || true
    fi
    for _ in $(seq 1 30); do op_running || break; sleep 2; done
    op_logs || true
    if op_running; then
      echo "Operation still running after 60s — stopping it."
      docker stop -t 10 "$OP_CONTAINER" >/dev/null || true
    fi
  fi
  docker rm -f "$OP_CONTAINER" >/dev/null 2>&1 || true
}

# ── Dispatch ───────────────────────────────────────────────────────────────

main() {
  local cmd="${1:-}"; shift || true
  case "$cmd" in
    ""|-h|--help) sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
    preflight|build|ensure|reset|status)
                  record "$cmd" "stage_$cmd" ;;
    check-state)  record "check-state" stage_check_state "$@" ;;
    plan)         record "plan-${1:-}" stage_plan "$@" ;;
    verify)       record "verify-${1:-}" stage_verify "$@" ;;
    find-missing) record "find-missing" stage_find_missing "$@" ;;
    run)
      # Interactive: answers come from this terminal, so don't pipe through tee.
      if [[ -t 0 ]]; then stage_run "$@"; else record "${1:-run}" stage_run "$@"; fi ;;
    logs)         stage_logs ;;
    down)         stage_down ;;
    op-start)     op_start "$@" ;;
    op-prompt)    op_prompt ;;
    op-answer)    op_answer "$@" ;;
    op-running)   op_running ;;
    op-logs)      op_logs ;;
    op-finish)    op_finish ;;
    op-cleanup)   op_cleanup ;;
    *) echo "unknown command: $cmd (run without arguments for help)" >&2; exit 2 ;;
  esac
}

main "$@"

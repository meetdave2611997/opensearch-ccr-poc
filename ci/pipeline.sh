#!/usr/bin/env bash
# CCR DR pipeline driver. ci/Jenkinsfile calls one subcommand per stage; run
# `ci/pipeline.sh all` to do the whole cycle on a laptop with Docker.
#
#   preflight         check docker / compose / vm.max_map_count
#   build             build the runner image (python + repo scripts)
#   up                start the two OpenSearch clusters and wait for health
#   bootstrap         remote aliases + seed data + PRIMARY → SECONDARY replication
#   assert-normal     check PRIMARY → SECONDARY replication
#   dry-run-failover  dr-switchover.py --dry-run --operation failover
#   failover          dr-switchover.py --operation failover
#   assert-dr         check SECONDARY → PRIMARY replication, write DR probe
#   failback          dr-switchover.py --operation failback
#   assert-restored   check PRIMARY → SECONDARY again, DR probe survived
#   status            dr-switchover.py --operation status
#   find-missing      find-missing-replication-indices.py
#   logs              save cluster logs to reports/
#   down              remove containers, volumes, network and runner image
#   all               every stage above in order, then logs + down
#
# Environment:
#   BUILD_TAG           unique build id (set by CloudBees); default "local"
#   OPENSEARCH_VERSION  OpenSearch image tag; default 2.13.0
#   OS_HEAP             JVM heap per node; default 1g (512m trips the circuit breaker during CCR bootstrap)
#   DRY_RUN=true        pass --dry-run to failover/failback
#   KEEP_CLUSTERS=true  `all` leaves the clusters running at the end
#   REPORTS_DIR         where logs go; default ./reports

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Compose project names must be lowercase [a-z0-9_-].
RAW_TAG="${BUILD_TAG:-local}"
PROJ="ccr-$(printf '%s' "$RAW_TAG" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '-' | cut -c1-50)"
export RUNNER_IMAGE="${RUNNER_IMAGE:-ccr-runner:${PROJ}}"
export OPENSEARCH_VERSION="${OPENSEARCH_VERSION:-2.13.0}"
export OS_HEAP="${OS_HEAP:-1g}"
REPORTS_DIR="${REPORTS_DIR:-$ROOT/reports}"
CONFIG="ci/config.ci.json"
COMPOSE=(docker compose -p "$PROJ" -f ci/docker-compose.ci.yml)

mkdir -p "$REPORTS_DIR"

log() { printf '\n[pipeline] %s\n' "$*"; }

# Run a command, tee its output to reports/<name>.log, keep its exit code.
record() {
  local name="$1"; shift
  log "$name → $REPORTS_DIR/$name.log"
  "$@" 2>&1 | tee "$REPORTS_DIR/$name.log"
}

# Run a python script inside the runner container on the cluster network.
runner() {
  "${COMPOSE[@]}" --profile tools run --rm -T runner "$@"
}

dry_run_flag() { [[ "${DRY_RUN:-false}" == "true" ]] && echo "--dry-run" || true; }

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
  echo "compose project=$PROJ runner image=$RUNNER_IMAGE opensearch=$OPENSEARCH_VERSION heap=$OS_HEAP"
}

stage_build()   { docker build -f ci/Dockerfile.runner -t "$RUNNER_IMAGE" .; }
stage_up()      { "${COMPOSE[@]}" up -d --wait --wait-timeout 300 opensearch-primary opensearch-secondary; }
stage_bootstrap()      { runner ci/bootstrap.py --config "$CONFIG" --no-colour; }
stage_assert_normal()  { runner ci/assert_state.py --config "$CONFIG" --expect normal --no-colour; }
stage_assert_dr()      { runner ci/assert_state.py --config "$CONFIG" --expect dr --write-probe --probe-tag dr --no-colour; }
stage_assert_restored() {
  runner ci/assert_state.py --config "$CONFIG" --expect normal --write-probe --probe-tag restored \
    --require-probe dr --no-colour
}
stage_dry_run_failover() {
  runner dr-switchover.py --config "$CONFIG" --operation failover --dry-run --non-interactive --no-colour
}
stage_failover() {
  # shellcheck disable=SC2046
  runner dr-switchover.py --config "$CONFIG" --operation failover --non-interactive --no-colour $(dry_run_flag)
}
stage_failback() {
  # shellcheck disable=SC2046
  runner dr-switchover.py --config "$CONFIG" --operation failback --non-interactive --no-colour $(dry_run_flag)
}
stage_status()  { runner dr-switchover.py --config "$CONFIG" --operation status --no-colour; }
stage_find_missing() {
  runner find-missing-replication-indices.py --config "$CONFIG" --direction primary-to-secondary --no-colour
}
stage_logs() {
  "${COMPOSE[@]}" logs --no-color --timestamps > "$REPORTS_DIR/clusters.log" 2>&1 || true
  log "cluster logs → $REPORTS_DIR/clusters.log"
}
stage_down() {
  "${COMPOSE[@]}" --profile tools down -v --remove-orphans || true
  docker image rm -f "$RUNNER_IMAGE" >/dev/null 2>&1 || true
}

run_stage() {
  local name="$1"
  local fn="stage_${name//-/_}"
  if ! declare -F "$fn" >/dev/null; then
    echo "unknown stage: $name" >&2
    exit 2
  fi
  case "$name" in
    logs|down) "$fn" ;;
    *) record "$name" "$fn" ;;
  esac
}

ALL_STAGES=(preflight build up bootstrap assert-normal dry-run-failover failover assert-dr
            failback assert-restored status find-missing)

main() {
  local stage="${1:-}"
  if [[ -z "$stage" ]]; then
    sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
  fi
  if [[ "$stage" == "all" ]]; then
    if [[ "${KEEP_CLUSTERS:-false}" != "true" ]]; then
      trap 'run_stage logs; run_stage down' EXIT
    fi
    for s in "${ALL_STAGES[@]}"; do run_stage "$s"; done
    log "ALL STAGES PASSED"
  else
    run_stage "$stage"
  fi
}

main "$@"

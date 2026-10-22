#!/usr/bin/env bash
# stage-build-context.sh - assemble a clean, reproducible build context.
#
# The praxis-beyond-routing BuildConfig is a binary build: the context is this
# directory, streamed by `oc start-build --from-dir`. The praxis workspace is
# cloned INSIDE the Containerfile at the exact tag v0.7.5, so the context only
# needs the pieces that live in this repo:
#
#   <context>/router/                              <- the beyond-routing crate
#   <context>/deploy/config/praxis-config.yaml     <- the validate-gate config
#   <context>/deploy/runtime/render-config.sh      <- the initContainer script
#   <context>/Containerfile.praxis                 <- the build recipe
#
# `target/` is excluded: this crate's target/ is ~2 GB of host-arch (arm64)
# artifacts an amd64 in-cluster build cannot use anyway.
#
# stdout is ONLY the context path, so callers can do:
#   CTX="$(hack/stage-build-context.sh)"
# All diagnostics go to stderr.
#
# Usage:
#   hack/stage-build-context.sh [destination-dir]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { echo "$@" >&2; }

command -v rsync >/dev/null 2>&1 || { log "FATAL: rsync not found in PATH"; exit 1; }

[[ -f "${REPO_ROOT}/router/Cargo.toml" ]] || {
  log "FATAL: router crate Cargo.toml not found at ${REPO_ROOT}/router"; exit 1; }
[[ -f "${REPO_ROOT}/Containerfile.praxis" ]] || {
  log "FATAL: Containerfile.praxis not found at ${REPO_ROOT}"; exit 1; }
[[ -f "${REPO_ROOT}/deploy/config/praxis-config.yaml" ]] || {
  log "FATAL: deploy/config/praxis-config.yaml not found at ${REPO_ROOT}"; exit 1; }
[[ -f "${REPO_ROOT}/deploy/runtime/render-config.sh" ]] || {
  log "FATAL: deploy/runtime/render-config.sh not found at ${REPO_ROOT}"; exit 1; }

if [[ $# -ge 1 ]]; then
  CONTEXT="$1"
  mkdir -p "$CONTEXT"
else
  CONTEXT="$(mktemp -d "${TMPDIR:-/tmp/opencode}/praxis-build-context.XXXXXX")"
fi

# Excludes:
#   target/   - build artifacts, host-arch, gigabytes in this crate
#   .env*     - never let a credential file ride along into a build context
# The remaining entries keep the context deterministic (no editor/OS droppings).
EXCLUDES=(
  --exclude='target/'
  --exclude='.env'
  --exclude='.env.*'
  --exclude='.envrc'
  --exclude='.DS_Store'
  --exclude='*.swp'
  --exclude='node_modules/'
)

log "staging build context -> ${CONTEXT}"

# --delete makes re-running against an explicit destination idempotent rather
# than cumulative, so a stale file from a previous staging can never leak in.
rsync -a --delete "${EXCLUDES[@]}" "${REPO_ROOT}/router/" "${CONTEXT}/router/"
mkdir -p "${CONTEXT}/deploy/config" "${CONTEXT}/deploy/runtime"
cp "${REPO_ROOT}/deploy/config/praxis-config.yaml"      "${CONTEXT}/deploy/config/praxis-config.yaml"
cp "${REPO_ROOT}/deploy/runtime/render-config.sh"       "${CONTEXT}/deploy/runtime/render-config.sh"
cp "${REPO_ROOT}/Containerfile.praxis"                  "${CONTEXT}/Containerfile.praxis"

# Sanity: the things whose absence would produce a confusing build failure.
[[ -f "${CONTEXT}/router/src/lib.rs" ]] || { log "FATAL: staged router crate is missing src/lib.rs"; exit 1; }
[[ -f "${CONTEXT}/Containerfile.praxis" ]] || { log "FATAL: Containerfile.praxis missing from the context"; exit 1; }
[[ -f "${CONTEXT}/deploy/config/praxis-config.yaml" ]] || { log "FATAL: praxis-config.yaml missing from the context"; exit 1; }

if [[ -d "${CONTEXT}/router/target" ]]; then
  log "FATAL: target/ leaked into the build context"; exit 1
fi

size="$(du -sh "$CONTEXT" | cut -f1 | tr -d ' ')"
files="$(find "$CONTEXT" -type f | wc -l | tr -d ' ')"
log "context ready: ${files} files, ${size}"

echo "$CONTEXT"

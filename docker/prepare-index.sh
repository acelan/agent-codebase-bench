#!/usr/bin/env bash
# Build the benchmark tool indexes into the image at build time.
# Run in the Dockerfile (cwd = kernel) so `docker run --rm` cells hit a
# pre-built, reproducible index (kernel is pinned to v7.0, indexes don't drift).
#
# Scope notes (from docs/pi-migration.md + code-search-benchmark skill):
#   - codegraph / graft-wiring / codebase-memory-mcp index the FULL tree
#     offline (no key) — reproducible, ~7G + 31M + ~14G.
#   - repowise and graft --deep are LLM-synthesis tools; full-tree standard mode
#     is a measured OOM trap (~16h then SIGKILL), so they are baked FOCUSED to
#     the benchmark's own query scope (drivers/gpu/drm/i915 + drivers/usb/typec)
#     and only when BUILD_OPENROUTER_API_KEY is provided.
#
# Env:
#   KERNEL_DIR      (default /workspace/linux)
#   BUILD_OPENROUTER_API_KEY   set to bake repowise + graft --deep (needs $)
# Exit 0 always logs; nonzero fails the image build (intended).
set -euo pipefail

KERNEL_DIR="${KERNEL_DIR:-/workspace/linux}"
KEY="${BUILD_OPENROUTER_API_KEY:-}"
cd "$KERNEL_DIR"

echo "[index bake] kernel=$(git describe --tags 2>/dev/null || git log -1 --oneline) dir=$KERNEL_DIR"

echo "=== codegraph (full, offline) ==="
codegraph init . --no-color 2>&1 | tail -3 || { echo "codegraph init FAILED"; exit 1; }
du -sh .codegraph 2>/dev/null | tail -1

echo "=== graft wiring (full, offline) ==="
graft build . 2>&1 | tail -3 || { echo "graft build FAILED"; exit 1; }
du -sh graft 2>/dev/null | tail -1

echo "=== codebase-memory-mcp (full, offline) ==="
# Index the whole tree into the per-user cache (~/.cache/codebase-memory-mcp),
# ~3-6 min. Project name derives from the repo path (workspace-linux in-image).
codebase-memory-mcp cli index_repository --repo-path "$KERNEL_DIR" 2>&1 | tail -5 \
    || { echo "codebase-memory index FAILED"; exit 1; }

if [[ -n "$KEY" ]]; then
    export OPENROUTER_API_KEY="$KEY"
    echo "=== repowise (FOCUSED standard, keyed) ==="
    for sub in drivers/gpu/drm/i915 drivers/usb/typec; do
        [ -d "$sub" ] || continue
        echo "--- repowise init --no-prose --mode standard: $sub ---"
        (cd "$sub" && repowise init --no-prose --mode standard . 2>&1 | tail -5) \
            || { echo "repowise init $sub FAILED"; exit 1; }
        echo "--- repowise generate: $sub ---"
        (cd "$sub" && repowise generate --unwritten 2>&1 | tail -5) \
            || { echo "repowise generate $sub FAILED"; exit 1; }
    done

    echo "=== graft --deep (FOCUSED, keyed) ==="
    for sub in drivers/gpu/drm/i915 drivers/usb/typec; do
        [ -d "$sub" ] || continue
        echo "--- graft build --deep: $sub ---"
        (cd "$sub" && graft build --deep . 2>&1 | tail -5) \
            || { echo "graft --deep $sub FAILED"; exit 1; }
    done
else
    echo "=== repowise / graft --deep SKIPPED (no BUILD_OPENROUTER_API_KEY) ==="
fi

echo "[index bake] done"
du -sh .codegraph graft 2>/dev/null | tail -2
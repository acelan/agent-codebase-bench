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
#   Secret /run/secrets/repowise_env (docker build-secret, optional) carrying:
#     OPENROUTER_API_KEY        pays repowise/graft --deep synthesis + embeddings
#     REPOWISE_PROVIDER         LLM provider (default openrouter)
#     REPOWISE_MODEL            model id for page synthesis (default a cheap one)
#     REPOWISE_EMBEDDER         vector embedder (default openrouter -> real dims)
#     REPOWISE_EMBEDDING_MODEL  embedding model on that provider (default
#                               openai/text-embedding-3-small)
#     REPOWISE_EMBEDDING_DIMS   / REPOWISE_EMBEDDING_TIMEOUT (optional)
#   The key is consumed at build time only and scrubbed from the image.
set -euo pipefail

KERNEL_DIR="${KERNEL_DIR:-/workspace/linux}"
cd "$KERNEL_DIR"

# Load optional build-secret (never an ARG, so it doesn't leak into the image).
if [ -f /run/secrets/repowise_env ]; then
    set -a
    # shellcheck disable=SC1091
    . /run/secrets/repowise_env
    set +a
fi
KEY="${OPENROUTER_API_KEY:-}"
REPOWISE_PROVIDER="${REPOWISE_PROVIDER:-openrouter}"
# Default the repowise synthesis model to the pi-agent model (PI_MODEL from
# docker/.env) when the single-model .env is in use; else a cheap fallback.
REPOWISE_MODEL="${REPOWISE_MODEL:-${PI_MODEL:-deepseek/deepseek-v4-flash-0731}}"
# Embedder (semantic-search vectors; SEPARATE from the LLM provider above).
# openrouter gives the baked vector store REAL embeddings via the same key;
# leaving it unset defaults repowise to `mock` (dummy, non-semantic) vectors.
# The embedding model must be one OpenRouter actually serves (e.g.
# openai/text-embedding-3-small). DIMS/TIMEOUT are optional; unset lets
# repowise infer dims from the model and use its default timeout.
REPOWISE_EMBEDDER="${REPOWISE_EMBEDDER:-openrouter}"
REPOWISE_EMBEDDING_MODEL="${REPOWISE_EMBEDDING_MODEL:-openai/text-embedding-3-small}"
REPOWISE_EMBEDDING_DIMS="${REPOWISE_EMBEDDING_DIMS:-}"
REPOWISE_EMBEDDING_TIMEOUT="${REPOWISE_EMBEDDING_TIMEOUT:-}"
# graft is DISABLED in benchmark.yaml (no C parser), so graft --deep is skipped
# unless explicitly asked for (it costs LLM $ with no benchmark benefit today).
BUILD_GRAFT_DEEP="${BUILD_GRAFT_DEEP:-0}"

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
    export OPENROUTER_API_KEY REPOWISE_PROVIDER REPOWISE_MODEL \
        REPOWISE_EMBEDDER REPOWISE_EMBEDDING_MODEL \
        REPOWISE_EMBEDDING_DIMS REPOWISE_EMBEDDING_TIMEOUT
    echo "=== repowise (FOCUSED standard, keyed) ==="
    # repowise scopes to cwd/. (verified: .repowise lands in the subtree), so
    # cd'ing into each benchmark subtree builds a subtree-local index without
    # walking the whole kernel (which OOMs). init --no-prose scaffolds the
    # structural pages (no LLM), then generate synthesizes concept-page prose.
    for sub in drivers/gpu/drm/i915 drivers/usb/typec; do
        [ -d "$sub" ] || continue
        echo "--- repowise init --no-prose --mode standard: $sub ---"
        (cd "$sub" && repowise init --no-prose --mode standard . 2>&1 | tail -5) \
            || { echo "repowise init $sub FAILED"; exit 1; }
        echo "--- repowise generate: $sub ---"
        (cd "$sub" && repowise generate --unwritten 2>&1 | tail -5) \
            || { echo "repowise generate $sub FAILED"; exit 1; }
    done

    echo "=== graft --deep (FOCUSED, keyed, optional) ==="
    if [[ "$BUILD_GRAFT_DEEP" = "1" ]]; then
        for sub in drivers/gpu/drm/i915 drivers/usb/typec; do
            [ -d "$sub" ] || continue
            echo "--- graft build --deep: $sub ---"
            (cd "$sub" && graft build --deep . 2>&1 | tail -5) \
                || { echo "graft --deep $sub FAILED"; exit 1; }
        done
    else
        echo "skipped (graft disabled; set BUILD_GRAFT_DEEP=1 to enable)"
    fi

    # Defense-in-depth: the key is needed only to synthesize the index, never at
    # runtime (repowise search / graft query are offline). Scrub any env file
    # repowise may have written so the secrets are NOT baked into the image.
    echo "=== scrubbing key-bearing env files from the baked index ==="
    find "$KERNEL_DIR" -path '*/.repowise/.env' -delete 2>/dev/null || true
else
    echo "=== repowise / graft --deep SKIPPED (no OPENROUTER_API_KEY secret) ==="
fi

echo "[index bake] done"
du -sh .codegraph graft 2>/dev/null | tail -2
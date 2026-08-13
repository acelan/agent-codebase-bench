#!/usr/bin/env bash
# Build missing indexes into INDEX_CACHE_DIR, which must be outside the image.
set -euo pipefail

KERNEL_DIR="${KERNEL_DIR:-/workspace/linux}"
INDEX_CACHE_DIR="${INDEX_CACHE_DIR:?INDEX_CACHE_DIR is required}"
KEY="${OPENROUTER_API_KEY:-}"
REPOWISE_PROVIDER="${REPOWISE_PROVIDER:-openrouter}"
REPOWISE_MODEL="${REPOWISE_MODEL:-${PI_MODEL:-deepseek/deepseek-v4-flash-0731}}"
REPOWISE_EMBEDDER="${REPOWISE_EMBEDDER:-openrouter}"
REPOWISE_EMBEDDING_MODEL="${REPOWISE_EMBEDDING_MODEL:-openai/text-embedding-3-small}"
REPOWISE_EMBEDDING_DIMS="${REPOWISE_EMBEDDING_DIMS:-}"
REPOWISE_EMBEDDING_TIMEOUT="${REPOWISE_EMBEDDING_TIMEOUT:-}"
BUILD_GRAFT_DEEP="${BUILD_GRAFT_DEEP:-0}"

cd "$KERNEL_DIR"
mkdir -p "$INDEX_CACHE_DIR" "$INDEX_CACHE_DIR/repowise"

link_target() {
    local target="$1" link="$2"
    mkdir -p "$(dirname "$link")"
    rm -rf "$link"
    ln -s "$target" "$link"
}

if [ ! -f "$INDEX_CACHE_DIR/.codegraph.complete" ]; then
    rm -rf "$INDEX_CACHE_DIR/codegraph"
    mkdir -p "$INDEX_CACHE_DIR/codegraph"
    link_target "$INDEX_CACHE_DIR/codegraph" "$KERNEL_DIR/.codegraph"
    echo "=== codegraph (full, offline) ==="
    codegraph init . --no-color 2>&1 | tail -3
    touch "$INDEX_CACHE_DIR/.codegraph.complete"
fi

if [ ! -f "$INDEX_CACHE_DIR/.graft.complete" ]; then
    rm -rf "$INDEX_CACHE_DIR/graft"
    mkdir -p "$INDEX_CACHE_DIR/graft"
    link_target "$INDEX_CACHE_DIR/graft" "$KERNEL_DIR/graft"
    echo "=== graft wiring (full, offline) ==="
    graft build . 2>&1 | tail -3
    touch "$INDEX_CACHE_DIR/.graft.complete"
fi

if [ ! -f "$INDEX_CACHE_DIR/.codebase-memory.complete" ]; then
    if [ ! -d "$INDEX_CACHE_DIR/codebase-memory-mcp" ] || \
       [ -L "$INDEX_CACHE_DIR/codebase-memory-mcp" ]; then
        rm -rf "$INDEX_CACHE_DIR/codebase-memory-mcp"
        mkdir -p "$INDEX_CACHE_DIR/codebase-memory-mcp"
    fi
    chmod 700 "$INDEX_CACHE_DIR/codebase-memory-mcp"
    mkdir -p /root/.cache
    if [ ! -d "/root/.cache/codebase-memory-mcp" ] || \
       [ -L "/root/.cache/codebase-memory-mcp" ]; then
        link_target "$INDEX_CACHE_DIR/codebase-memory-mcp" \
            /root/.cache/codebase-memory-mcp
    fi
    echo "=== codebase-memory-mcp (full, offline) ==="
    codebase-memory-mcp cli index_repository --repo-path "$KERNEL_DIR" 2>&1 | tail -5
    touch "$INDEX_CACHE_DIR/.codebase-memory.complete"
fi

if [ -n "$KEY" ]; then
    export OPENROUTER_API_KEY="$KEY" REPOWISE_PROVIDER REPOWISE_MODEL \
        REPOWISE_EMBEDDER REPOWISE_EMBEDDING_MODEL \
        REPOWISE_EMBEDDING_DIMS REPOWISE_EMBEDDING_TIMEOUT
    for sub in drivers/gpu/drm/i915 drivers/usb/typec; do
        [ -d "$sub" ] || continue
        name="${sub//\//_}"
        marker="$INDEX_CACHE_DIR/.repowise-${name}.complete"
        [ -f "$marker" ] && continue
        rm -rf "$INDEX_CACHE_DIR/repowise/$name"
        mkdir -p "$INDEX_CACHE_DIR/repowise/$name"
        link_target "$INDEX_CACHE_DIR/repowise/$name" "$KERNEL_DIR/$sub/.repowise"
        echo "=== repowise (focused, keyed): $sub ==="
        (cd "$sub" && repowise init --no-prose --mode standard . 2>&1 | tail -5)
        (cd "$sub" && repowise generate --unwritten 2>&1 | tail -5)
        touch "$marker"
    done

    if [ "$BUILD_GRAFT_DEEP" = "1" ] && [ ! -f "$INDEX_CACHE_DIR/.graft-deep.complete" ]; then
        for sub in drivers/gpu/drm/i915 drivers/usb/typec; do
            [ -d "$sub" ] || continue
            name="${sub//\//_}"
            rm -rf "$INDEX_CACHE_DIR/repowise/graft-deep-$name"
            mkdir -p "$INDEX_CACHE_DIR/repowise/graft-deep-$name"
            link_target "$INDEX_CACHE_DIR/repowise/graft-deep-$name" "$KERNEL_DIR/$sub/graft"
            (cd "$sub" && graft build --deep . 2>&1 | tail -5)
        done
        touch "$INDEX_CACHE_DIR/.graft-deep.complete"
    fi
else
    echo "=== repowise skipped: OPENROUTER_API_KEY is not set ==="
fi

cat > "$INDEX_CACHE_DIR/manifest.json" <<EOF
{
  "profile": "${INDEX_PROFILE:-unknown}",
  "kernel_sha": "$(git rev-parse HEAD)",
  "kernel_dir": "${KERNEL_DIR}",
  "indexes": ["codegraph", "graft", "codebase-memory-mcp"],
  "repowise": $([ -n "$KEY" ] && printf true || printf false)
}
EOF
echo "[index cache] prepared $INDEX_CACHE_DIR"
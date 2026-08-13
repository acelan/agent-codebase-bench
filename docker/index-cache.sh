#!/usr/bin/env bash
# Attach or build indexes stored in the host-mounted artifacts directory.
set -euo pipefail

KERNEL_DIR="${KERNEL_DIR:-/workspace/linux}"
ARTIFACTS_DIR="${ARTIFACTS_DIR:-/workspace/artifacts}"
INDEX_PROFILE="${INDEX_PROFILE:?INDEX_PROFILE is not set}"
CACHE_DIR="$ARTIFACTS_DIR/indexes/$INDEX_PROFILE"

link_one() {
    local target="$1" link="$2"
    mkdir -p "$(dirname "$link")"
    if [ -L "$link" ] || [ -e "$link" ]; then
        rm -rf "$link"
    fi
    ln -s "$target" "$link"
}

attach_indexes() {
    [ -f "$CACHE_DIR/manifest.json" ] || {
        echo "[index cache] no cache at $CACHE_DIR; run pi-bench-index build" >&2
        return 1
    }
    link_one "$CACHE_DIR/codegraph" "$KERNEL_DIR/.codegraph"
    link_one "$CACHE_DIR/graft" "$KERNEL_DIR/graft"
    # codebase-memory-mcp rejects symlinked cache paths. When the caller
    # supplies a direct bind mount, leave it in place; otherwise attach the
    # cache for tools that tolerate symlinks.
    if [ ! -d "/root/.cache/codebase-memory-mcp" ] || \
       [ -L "/root/.cache/codebase-memory-mcp" ]; then
        link_one "$CACHE_DIR/codebase-memory-mcp" \
            "/root/.cache/codebase-memory-mcp"
    fi
    for sub in drivers/gpu/drm/i915 drivers/usb/typec; do
        [ -d "$KERNEL_DIR/$sub" ] || continue
        name="${sub//\//_}"
        link_one "$CACHE_DIR/repowise/$name" "$KERNEL_DIR/$sub/.repowise"
    done
    echo "[index cache] attached $INDEX_PROFILE"
}

case "${1:-link}" in
    link)
        # Direct `docker run` without the artifacts mount should still allow
        # grep-only usage; the benchmark runner mounts artifacts and fails if
        # the requested index cache has not been initialized.
        if [ ! -d "$ARTIFACTS_DIR" ]; then
            echo "[index cache] artifacts mount absent; skipping" >&2
            exit 0
        fi
        attach_indexes
        ;;
    build)
        mkdir -p "$CACHE_DIR"
        export INDEX_CACHE_DIR="$CACHE_DIR" INDEX_PROFILE
        /opt/pi-bench/prepare-index.sh
        attach_indexes
        ;;
    status)
        if [ -f "$CACHE_DIR/manifest.json" ]; then
            printf 'ready %s\n' "$CACHE_DIR"
        else
            printf 'missing %s\n' "$CACHE_DIR"
            exit 1
        fi
        ;;
    *)
        echo "usage: pi-bench-index {link|build|status}" >&2
        exit 2
        ;;
esac
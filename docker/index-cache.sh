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
    # codebase-memory-mcp rejects symlinked cache paths (its coordination
    # canonicalizes the symlink to the bind-mounted artifacts dir and fails
    # with "cache-private"). For a bind-mounted cache the caller should mount
    # it directly at /root/.cache/codebase-memory-mcp; when both are absent,
    # fall back to copying (not symlinking) the persisted index into place so
    # the CLI's coordination sees a real on-container path.
    if [ ! -d "/root/.cache/codebase-memory-mcp" ] || \
       [ -L "/root/.cache/codebase-memory-mcp" ]; then
        rm -rf "/root/.cache/codebase-memory-mcp"
        mkdir -p /root/.cache
        cp -a "$CACHE_DIR/codebase-memory-mcp" \
            "/root/.cache/codebase-memory-mcp"
    fi
    # codebase-memory-mcp's "secure CLI coordination" (cache-private) check
    # requires the cache dir to be owned by the invoking user (root in this
    # container). A host bind-mount keeps the HOST uid (e.g. 1000), which
    # fails that ownership check every time -- not a permission-bits issue,
    # an ownership one -- and the CLI dies with the exact "cache-private"
    # error this comment block used to only warn about for the symlink case.
    # Always normalize ownership here, whether the dir was just copied above
    # or arrived as a direct host bind-mount (the "recommended" docker run in
    # README.md / docs/pi-migration.md), so both paths actually work.
    chown -R root:root "/root/.cache/codebase-memory-mcp"
    chmod -R u+rwX "/root/.cache/codebase-memory-mcp"
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
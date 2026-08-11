#!/usr/bin/env bash
# pi-bench entrypoint: the Docker CMD is pi args. Always load the benchmark
# extension (so the index tools are reachable) and run from KERNEL_DIR.
#
# Any explicit -e/--extension passed by the caller wins; we fall back to the
# bundled bench-tools extension when none is given. OPENROUTER_API_KEY (or
# any provider key) is expected in the container environment.
set -euo pipefail

KERNEL_DIR="${KERNEL_DIR:-/workspace/linux}"
cd "$KERNEL_DIR"

EXT="/opt/pi-bench/extensions/bench-tools/index.ts"
if [[ -n "${PI_NO_EXT:-}" ]]; then
    exec pi "$@"
fi

# If the caller already passed an extension flag, don't double-load.
if [[ "$*" != *--extension* && "$*" != *"-e "* ]]; then
    exec pi --extension "$EXT" "$@"
fi
exec pi "$@"

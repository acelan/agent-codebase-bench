#!/usr/bin/env bash
# Run the agent-codebase-bench harness inside the image, writing every run and
# the html report into the mounted /workspace/artifacts dir.
#
# The image bakes in pi v0.84.1, the benchmark tools, and a linux v7.0 clone;
# /workspace/artifacts is the ONLY host path mounted (all outputs land there).
#
# Usage:
#   docker run --rm -it \
#     -e OPENROUTER_API_KEY=$OPENROUTER_API_KEY \
#     -v "$(pwd)/artifacts:/workspace/artifacts" \
#     --entrypoint /usr/local/bin/pi-bench-run \
#     agent-codebase-bench \
#     --model openrouter/deepseek-v4-flash-0731 --backend native [extra args...]
set -euo pipefail

# A matching aggregate-only summary cache needs no provider credentials. Normal
# benchmark runs still fail early with a clear diagnostic rather than starting
# a matrix that cannot contact the configured model.
aggregate_only=false
for arg in "$@"; do
    if [[ "$arg" == "--aggregate-only" ]]; then
        aggregate_only=true
        break
    fi
done
if [[ "$aggregate_only" != true ]]; then
    : "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY is required}"
fi
export KERNEL_DIR=/workspace/linux
export PI_BIN=pi

mkdir -p /workspace/artifacts
cd /workspace
/usr/local/bin/pi-bench-index link
# Default config is the baked-in one; a caller-supplied --config (later arg)
# overrides it. cwd=/workspace so results_dir "artifacts" lands on the mount.
exec /opt/bench-venv/bin/python /opt/pi-bench/bench_pi.py \
    --config /opt/pi-bench/benchmark.yaml --backend native "$@"

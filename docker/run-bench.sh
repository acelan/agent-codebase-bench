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

: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY is required}"
export KERNEL_DIR=/workspace/linux
export PI_BIN=pi

mkdir -p /workspace/artifacts
cd /workspace
# Default config is the baked-in one; a caller-supplied --config (later arg)
# overrides it. cwd=/workspace so results_dir "artifacts" lands on the mount.
exec /opt/bench-venv/bin/python /opt/pi-bench/bench_pi.py \
    --config /opt/pi-bench/benchmark.yaml --backend native "$@"

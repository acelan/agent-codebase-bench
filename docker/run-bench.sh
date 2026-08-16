#!/usr/bin/env bash
# Run the agent-codebase-bench harness inside the image, writing every run and
# the html report into the mounted /workspace/artifacts dir.
#
# The image bakes in pi v0.84.1, the benchmark tools, and a linux v7.0 clone;
# /workspace/artifacts is the ONLY host path mounted (all outputs land there).
#
# Usage (OpenRouter):
#   docker run --rm -it \
#     -e OPENROUTER_API_KEY=$OPENROUTER_API_KEY \
#     -v "$(pwd)/artifacts:/workspace/artifacts" \
#     --entrypoint /usr/local/bin/pi-bench-run \
#     agent-codebase-bench \
#     --model openrouter/deepseek-v4-flash-0731 --backend native [extra args...]
#
# Usage (Ollama/local): no OpenRouter key needed; the container must reach the
# ollama endpoint, so use --network host. OLLAMA_BASE_URL and PI_PROVIDER=ollama
# are passed through and pi-bench-run writes pi's models.json from them:
#   docker run --rm -it \
#     --network host \
#     -e PI_PROVIDER=ollama \
#     -e PI_MODEL=ollama/qwen2.5-coder:0.5b \
#     -e OLLAMA_BASE_URL=http://127.0.0.1:11434/v1 \
#     -e OPENROUTER_API_KEY=... \   # only if repowise/analysis also needs it
#     -v "$(pwd)/artifacts:/workspace/artifacts" \
#     --entrypoint /usr/local/bin/pi-bench-run \
#     agent-codebase-bench \
#     --model-preset ollama --backend native [extra args...]
set -euo pipefail

# A matching aggregate-only summary cache needs no provider credentials. Normal
# benchmark runs still fail early with a clear diagnostic rather than starting
# a matrix that cannot contact the configured model. Ollama runs also need no
# OpenRouter key — PI_PROVIDER=ollama (or OLLAMA_BASE_URL) selects the local
# provider explicitly.
aggregate_only=false
for arg in "$@"; do
    if [[ "$arg" == "--aggregate-only" ]]; then
        aggregate_only=true
        break
    fi
done
if [[ "$aggregate_only" != true \
      && "${PI_PROVIDER:-}" != "ollama" && -z "${OLLAMA_BASE_URL:-}" ]]; then
    : "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY is required (or set PI_PROVIDER=ollama / OLLAMA_BASE_URL for the ollama provider)}"
fi
export KERNEL_DIR=/workspace/linux
export PI_BIN=pi

# Ollama runs: the image entrypoint is NOT used when --entrypoint pi-bench-run
# is passed, so generate pi's models.json here (from PI_MODEL / OLLAMA_BASE_URL)
# before the harness starts. Without it, native pi cannot resolve the ollama
# provider and every cell fails.
if [[ "${PI_PROVIDER:-}" == "ollama" || -n "${OLLAMA_BASE_URL:-}" ]]; then
    /usr/local/bin/pi-ollama-models
fi

# Ollama on localhost: inside a bridge-network container, 127.0.0.1 is the
# container itself, NOT the host. The outer docker run must pass --network host
# (or OLLAMA_BASE_URL must point at a reachable host/LAN IP). Fail early with a
# clear message instead of burning a matrix of connection-error cells.
if [[ "${PI_PROVIDER:-}" == "ollama" || -n "${OLLAMA_BASE_URL:-}" ]]; then
    ou="${OLLAMA_BASE_URL:-http://127.0.0.1:11434/v1}"
    case "$ou" in
        http://127.0.0.1*|http://localhost*)
            echo "[ollama] OLLAMA_BASE_URL=$ou points at localhost — add " \
                 "--network host to the outer docker run, or set " \
                 "OLLAMA_BASE_URL to a reachable host/LAN IP (e.g. " \
                 "http://<host-ip>:11434/v1)." >&2
            ;;
    esac
fi

mkdir -p /workspace/artifacts
cd /workspace
/usr/local/bin/pi-bench-index link
# Default config is the baked-in one; a caller-supplied --config (later arg)
# overrides it. cwd=/workspace so results_dir "artifacts" lands on the mount.
exec /opt/bench-venv/bin/python /opt/pi-bench/bench_pi.py \
    --config /opt/pi-bench/benchmark.yaml --backend native "$@"

# syntax=docker/dockerfile:1
#
# agent-codebase-bench Docker image: pi (pinned) + all code-query benchmark
# tools + a cloned Linux kernel + baked indexes, so a (tool x prompt) cell can
# run fully headless inside one self-contained container.
#
#   docker build -t agent-codebase-bench -f docker/Dockerfile .
#
# LAYERING RULE (why the cheap stuff is after the index bake):
#   The index bake (prepare-index.sh) is a ~21GB / ~1h step whose layers are
#   only invalidated by changes to (base, tools, kernel clone, prepare-index.sh).
#   Everything that changes often — the pi extension (index.ts), the harness
#   python, the rtk binary, run-bench.sh — sits AFTER the index bake so adding
#   or tweaking a tool reuses the cached index layers instead of re-indexing.
#
# Design (see docs/pi-migration.md):
#   - pi pins @earendil-works/pi-coding-agent@0.84.1.
#   - pi has NO native MCP integration, so code-query tools are exposed as
#     custom extension tools that shell out to each tool's CLI against
#     KERNEL_DIR. `-t <tool>` is the hard isolation control (excluded tools are
#     physically unavailable to the model).
#   - Kernel pinned to v7.0 at build time so the baked indexes never drift.

FROM node:24-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV KERNEL_DIR=/workspace/linux
ENV PI_CODING_AGENT_DIR=/root/.pi/agent

# --- base deps: git, ripgrep, python (for repowise), curl -----------------
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates curl ripgrep \
      python3 python3-venv python3-pip pipx \
 && rm -rf /var/lib/apt/lists/*

# --- pi, pinned to v0.84.1 -------------------------------------------------
RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.84.1

# --- benchmark tools (index-backed), pinned -------------------------------
# codegraph (npm), graft (npm), codebase-memory-mcp (npm: static binary),
# repowise (PyPI -> venv; not on root PATH, invoke via its venv bin).
# codebase-memory-mcp@latest (0.10.2) OOM-kills its index worker (signal 9) on
# the full v7.0 kernel index; pin 0.9.0, the known-good version for it.
RUN npm install -g \
      @colbymchenry/codegraph@1.5.0 \
      @nanonets/graft@0.9.0 \
      codebase-memory-mcp@0.9.0
RUN python3 -m venv /opt/repowise-venv \
 && /opt/repowise-venv/bin/pip install --no-cache-dir 'repowise==0.41.0' \
 && ln -s /opt/repowise-venv/bin/repowise /usr/local/bin/repowise

# --- clone the Linux kernel at build time (self-contained image) ----------
# Shallow clone of the v7.0 tag, pinned to match the benchmark prompts (they
# reference 6.17/7.0-era i915_drv.c registration and usb/typec). The bench
# queries the *working tree* (grep + each index tool), so a shallow checkout
# is all that is needed. Override the tag/repo with --build-arg if needed.
ARG KERNEL_REPO=https://github.com/torvalds/linux.git
ARG KERNEL_TAG=v7.0
RUN git clone --depth 1 --branch "$KERNEL_TAG" "$KERNEL_REPO" "$KERNEL_DIR" \
 || { echo "tag clone failed; retrying master"; \
      git clone --depth 1 "$KERNEL_REPO" "$KERNEL_DIR"; }

# --- index-bake script (a dependency of the bake below) -------------------
COPY docker/prepare-index.sh /opt/pi-bench/prepare-index.sh
RUN chmod +x /opt/pi-bench/prepare-index.sh

# --- bake tool indexes at build time (kernel pinned v7.0 -> reproducible) -
# BUILD_INDEXES=1 (default) builds the OFFLINE full indexes (codegraph, graft
# wiring, codebase-memory-mcp; ~21GB, no key). Repowise (LLM concept-page
# synthesis + openrouter embeddings) and graft --deep are baked focused to the
# benchmark scope ONLY when an OPENROUTER_API_KEY is provided via a BUILD
# SECRET (docker/.env), never a build-arg — so the key cannot leak
# into image history.
# Keep THIS step's inputs (above) stable so it stays cached across tool adds.
ARG BUILD_INDEXES=1
RUN --mount=type=secret,id=repowise_env,required=false \
    if [ "${BUILD_INDEXES}" = "1" ]; then \
        /opt/pi-bench/prepare-index.sh; \
    fi

# ===========================================================================
# POST-INDEX: everything below is cheap to rebuild and does NOT touch the
# baked-index layers. Add / tweak tools below this line.
# ===========================================================================

# --- rtk (Rust Token Killer), pinned to a release binary -------------------
# Single static musl binary: wraps bash output (rtk grep / rtk read / rtk rg)
# to cut the tokens the agent reads. Its version is part of the tool id.
ARG RTK_VERSION=0.45.0
RUN set -eux; \
    curl -fsSL -o /tmp/rtk.tgz \
      "https://github.com/rtk-ai/rtk/releases/download/v${RTK_VERSION}/rtk-x86_64-unknown-linux-musl.tar.gz"; \
    tar -xzf /tmp/rtk.tgz -C /tmp; \
    install -m 0755 /tmp/rtk /usr/local/bin/rtk; \
    rm -f /tmp/rtk.tgz /tmp/rtk; \
    rtk --version

# --- pi extension exposing the tools as custom tools -----------------------
# No package.json / npm install: pi bundles @earendil-works/pi-coding-agent and
# typebox internally, so a bare index.ts resolves them at load time.
COPY docker/pi-extensions/bench-tools /opt/pi-bench/extensions/bench-tools

# --- entrypoint shim -------------------------------------------------------
COPY docker/entrypoint.sh /usr/local/bin/pi-bench
RUN chmod +x /usr/local/bin/pi-bench

# --- benchmark harness (runs in-image; all outputs go to a mounted artifacts/) -
# Drives pi in --mode json natively (pi on PATH) and writes every run + html
# report under ./artifacts -> the host mount.
COPY pi_stream.py pi_transcript.py pi_runner.py pi_versions.py pi_aggregate.py \
     pi_report.py bench_pi.py benchmark.yaml \
     /opt/pi-bench/
COPY docker/run-bench.sh /usr/local/bin/pi-bench-run
RUN chmod +x /usr/local/bin/pi-bench-run \
 && python3 -m venv /opt/bench-venv \
 && /opt/bench-venv/bin/pip install --no-cache-dir pyyaml

WORKDIR /workspace/linux

# Default: run pi with whatever args the caller passes (e.g.
#   docker run --rm -e OPENROUTER_API_KEY=$KEY agent-codebase-bench \
#       --model openrouter/deepseek-v4-flash-0731 --mode json -p "...")
ENTRYPOINT ["/usr/local/bin/pi-bench"]

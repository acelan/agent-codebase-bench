# syntax=docker/dockerfile:1
#
# agent-codebase-bench Docker image: pi (pinned) + all code-query benchmark
# tools + a cloned Linux kernel, so a (tool x prompt) cell can run fully
# headless inside one self-contained container. Indexes live in artifacts/.
#
#   docker build -t agent-codebase-bench -f docker/Dockerfile .
#
# Indexes are deliberately kept outside the image under the host-mounted
# artifacts/indexes directory.  This keeps a single image while allowing
# frequent image changes without rebuilding the expensive indexes.
#
# Design (see docs/pi-migration.md):
#   - pi pins @earendil-works/pi-coding-agent@0.84.1.
#   - pi has NO native MCP integration, so code-query tools are exposed as
#     custom extension tools that shell out to each tool's CLI against
#     KERNEL_DIR. `-t <tool>` is the hard isolation control (excluded tools are
#     physically unavailable to the model).
#   - Kernel pinned to v7.0; the external index cache is keyed by this image's
#     kernel/tool identity so indexes do not silently drift.

FROM node:24-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV KERNEL_DIR=/workspace/linux
ENV PI_CODING_AGENT_DIR=/root/.pi/agent

# --- base deps: git, python (for repowise), curl ---------------------------
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates curl \
      python3 python3-venv python3-pip pipx \
 && rm -rf /var/lib/apt/lists/*

# --- ripgrep, pinned to the upstream v15.2.0 release -----------------------
# Do not use Debian's older package: the benchmark identity is the upstream
# ripgrep release, and the raw-rg comparison must be reproducible.
ARG RIPGREP_VERSION=15.2.0
RUN set -eux; \
    curl -fsSL -o /tmp/ripgrep.tar.gz \
      "https://github.com/BurntSushi/ripgrep/releases/download/${RIPGREP_VERSION}/ripgrep-${RIPGREP_VERSION}-x86_64-unknown-linux-musl.tar.gz"; \
    tar -xzf /tmp/ripgrep.tar.gz -C /tmp; \
    install -m 0755 "/tmp/ripgrep-${RIPGREP_VERSION}-x86_64-unknown-linux-musl/rg" /usr/local/bin/rg; \
    rm -rf /tmp/ripgrep.tar.gz "/tmp/ripgrep-${RIPGREP_VERSION}-x86_64-unknown-linux-musl"; \
    rg --version

# --- pi, pinned to v0.84.1 -------------------------------------------------
RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.84.1

# --- benchmark tools (index-backed) ----------------------------------------
# codegraph (npm), graft (npm), codebase-memory-mcp (npm: static binary),
# repowise (PyPI -> venv; not on root PATH, invoke via its venv bin).
# codebase-memory-mcp intentionally follows the npm latest tag; unlike the
# other benchmark tools, its version is not part of the reproducibility pins.
RUN npm install -g \
      @colbymchenry/codegraph@1.5.0 \
      @nanonets/graft@0.9.0 \
      codebase-memory-mcp@0.10.2
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

# Index cache profile. Change this when a tool version or index recipe
# changes; the old cache remains available under artifacts/indexes/.
ARG INDEX_PROFILE=v7.0-codegraph-1.5.0-graft-0.9.0-repowise-0.41.0-codebase-memory-0.10.2
ENV INDEX_PROFILE=${INDEX_PROFILE}

# --- external index-cache manager ------------------------------------------
COPY docker/index-cache.sh docker/prepare-index.sh /opt/pi-bench/
RUN chmod +x /opt/pi-bench/index-cache.sh /opt/pi-bench/prepare-index.sh \
 && ln -s /opt/pi-bench/index-cache.sh /usr/local/bin/pi-bench-index

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
     pi_summary.py pi_report.py bench_pi.py benchmark.yaml \
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

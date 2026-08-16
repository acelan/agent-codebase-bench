# agent-codebase-bench

Benchmark AI coding agents' code-query tools on **token efficiency** — how many
tokens an agent spends to answer a real codebase query with each tool — measured
through a headless **pi** (`--mode json`) run inside a self-contained Docker
image.

The prompt corpus is real Linux-kernel questions (call-graph tracing in
`drivers/gpu/drm/i915`, a `usb/typec` kernfs root-cause, DRM registration
callers), so the measurement reflects genuine open-ended code-query work.

## What's benchmarked

Tools are reached via a pi extension (`docker/pi-extensions/bench-tools/`) that
registers each as a custom tool shelling out to its CLI against the kernel tree;
`-t <tool>` is the hard isolation control (excluded tools are physically
unavailable to the model, so measurements aren't polluted by a ripgrep fallback).

| tool | what it is |
|---|---|
| `grep` | ripgrep-backed built-in search (`-t read,grep,find,ls`) |
| `ripgrep` | raw `rg` via bash (`-t ... ,bash`) |
| `codegraph` | code intelligence / knowledge-graph index |
| `graft` | context graph (linked markdown) — **disabled**: npm CLI has no C tree-sitter grammar, can't index the kernel; re-enable when [PR #69](https://github.com/NanoNets/Graft/pull/69) ships |
| `repowise` | codebase wiki / semantic index |
| `codebase-memory-mcp` | structural graph index |
| `rtk` | Rust Token Killer — wraps grep/rg, compresses output the agent reads |

## The Docker image

`docker/Dockerfile` builds a single image containing pi v0.84.1 (pinned), all
benchmark tools (pinned), and a linux **v7.0** clone. Indexes are persisted
outside the image. Key properties:

- **Only `artifacts/` is mounted.** Every benchmark run, report, and index cache
  lands under the mounted directory; the image remains single and portable.
- **Indexes are external and reusable:** `artifacts/indexes/<INDEX_PROFILE>/`
  stores codegraph, graft, codebase-memory-mcp, and optional repowise indexes.
  `pi-bench-index` builds missing indexes and attaches them by symlink.
- **Cheap tool iteration:** changing the Dockerfile no longer rebuilds indexes.
  Change `INDEX_PROFILE` when a kernel, tool version, or index recipe changes.

## Storage model (versioned + averaging)

A tool's identity includes its **version**; different versions are different tools:

```
artifacts/<model>-<provider>/<tool>@<version>/
  <tool>@<version>-<prompt>-<run_ts>.json            timestamped run usage
  ...-<run_ts>.transcript.json / .jsonl
  runs.json                                          append-only list of runs
  summary.json                                       per-prompt averages
report.md / report.html / versions.json              aggregates at the model root
artifacts/result-summary.json                        cached LLM report analysis at the artifact root
```

- Every run is timestamped, so the same `tool@version` can be benchmarked many
  times with every run kept.
- Version = `tool --version` probe (`pi_versions.py`); fallback = kernel git HEAD
  hash when the tool reports none.
- `pi_aggregate.py` averages across all runs (mean/median/min/max + n) per metric
  (total/in/out/cache/reasoning/api/cost/wall/tool-calls).
- `artifacts/result-summary.json` caches the LLM analysis used by the HTML
  Result summary. Its fingerprint covers the normalized metrics and transcripts,
  analyst model, prompt/schema version, and compaction settings. Report generation
  reuses a matching cache without calling the analyst; changed inputs regenerate it.
- Set `PI_SUMMARY_MODEL` to choose the report analyst. If omitted, it falls back
  to the benchmark `PI_MODEL`. The analyst runs once per testcase so recorded
  iteration traces stay within its context window. Analyst token usage is
  report-generation overhead and is excluded from benchmark totals and tables.

## Run it

```bash
# one source of truth for key + models (gitignored; copy from docker/.env.example)
set -a; . docker/.env; set +a  # exports OPENROUTER_API_KEY, PI_MODEL, optional PI_SUMMARY_MODEL, REPOWISE_*, OLLAMA_*

# build the single image (does not build indexes)
docker build -t agent-codebase-bench -f docker/Dockerfile .

# build missing indexes into the host-mounted artifacts/indexes directory
docker run --rm -it \
  --env-file docker/.env \
  -v "$(pwd)/artifacts:/workspace/artifacts" \
  --entrypoint /usr/local/bin/pi-bench-index \
  agent-codebase-bench build

# recommended: run the harness in-image; the ONLY host mount is artifacts/
# codebase-memory-mcp rejects symlinked cache paths (its coordination fails
# with "cache-private"), so the CBM cache is bind-mounted as a real dir at
# /root/.cache/codebase-memory-mcp; pi-bench-index falls back to copying it
# into place if this mount is omitted.
docker run --rm -it \
  --env-file docker/.env \
  -v "$(pwd)/artifacts:/workspace/artifacts" \
  -v "$(pwd)/artifacts/indexes/v7.0-codegraph-1.5.0-graft-0.9.0-repowise-0.41.0-codebase-memory-0.10.2/codebase-memory-mcp:/root/.cache/codebase-memory-mcp" \
  --entrypoint /usr/local/bin/pi-bench-run \
  agent-codebase-bench \
  --model "$PI_MODEL" --backend native --runs 2

# host-driven, one-shot containers per cell
python3 bench_pi.py --model "$PI_MODEL" --backend docker --runs 2

# === Ollama (local provider) ===
# The model comes from PI_MODEL (e.g. ollama/qwen2.5-coder:0.5b); the harness
# writes models.json from OLLAMA_BASE_URL automatically (see docs/ollama.md).
set -a; . docker/.env; set +a   # exports PI_PROVIDER, PI_MODEL, OLLAMA_BASE_URL (and OPENROUTER_API_KEY if repowise/analysis needs it)

# native backend (pi on the host): docker/ollama-models.sh creates ~/.pi/agent/models.json
docker/ollama-models.sh
python3 bench_pi.py --model-preset ollama --tools grep,rtk --runs 1

# docker backend: --network host is added automatically so the container reaches 127.0.0.1:11434
python3 bench_pi.py --model ollama/qwen2.5-coder:0.5b --backend docker --runs 1

# in-image harness (recommended): pass --network host on the outer docker run
docker run --rm -it \
  --network host \
  -e PI_PROVIDER=ollama \
  -e PI_MODEL=ollama/qwen2.5-coder:0.5b \
  -e OLLAMA_BASE_URL=$OLLAMA_BASE_URL \
  -v "$(pwd)/artifacts:/workspace/artifacts" \
  --entrypoint /usr/local/bin/pi-bench-run \
  agent-codebase-bench \
  --model-preset ollama --backend native --runs 1

# recompute averages/reports from already-saved runs only
python3 bench_pi.py --model "$PI_MODEL" --aggregate-only

# render directly; bypass a matching analysis cache and regenerate it
python3 pi_report.py --artifacts artifacts --out artifacts/report.html --force-summary

# subset / repeat
python3 bench_pi.py --model ... --tools grep,rtk --prompts callers-drm-register --runs 3
```

Both normal aggregation and direct `pi_report.py` rendering create or reuse the
fingerprinted analysis cache automatically. A cache hit needs no provider call or
credentials. If analysis fails and no valid matching cache can be generated, the
report explicitly shows **LLM analysis unavailable**, while its benchmark tables
and recorded iterations still render; the failure reason is written to stderr.

## Layout

```
docker/                    Dockerfile, entrypoint, run-bench, prepare-index, pi extension
pi_stream.py               pi --mode json loader + content flattening
pi_transcript.py           stream -> trace dict (prompt/tool-call/result/final)
pi_runner.py               cell runner (docker or native), versioned+timestamped files
pi_versions.py             per-tool version detection (--version, kernel-hash fallback)
pi_aggregate.py            averages over runs -> summary.json / report.md / report.html
bench_pi.py                matrix driver (per tool@version folders, --runs N)
benchmark.yaml             tools (incl. rtk), prompts, tool_instruction
models.yaml                model presets
docs/pi-migration.md       migration + verification details
```

See `docs/pi-migration.md` for the pi format contract, tool-set mapping, and the
verified baseline.

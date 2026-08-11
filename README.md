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
| `graft` | context graph (linked markdown) |
| `repowise` | codebase wiki / semantic index |
| `codebase-memory-mcp` | structural graph index |
| `rtk` | Rust Token Killer — wraps grep/rg, compresses output the agent reads |

## The Docker image

`docker/Dockerfile` builds a self-contained image: pi v0.84.1 (pinned), all
benchmark tools (pinned), a linux **v7.0** clone, and (optionally) baked tool
indexes. Key properties:

- **Only `artifacts/` is mounted.** Every benchmark run + the html report lands
  under the mounted `artifacts/` dir; the kernel, tools, and indexes are baked
  into the image.
- **Indexes baked at build** (`ARG BUILD_INDEXES=1`, `docker/prepare-index.sh`)
  since the kernel is pinned — reproducible, no per-cell re-index. Offline full
  indexes: codegraph, graft-wiring, codebase-memory-mcp (~21GB). The LLM-synthesis
  tools (repowise, graft `--deep`) are gated behind
  `--build-arg BUILD_OPENROUTER_API_KEY=$KEY` and scoped to the benchmark subtrees
  (full-tree parametrization is a measured OOM trap — see the skill).
- **Cheap tool iteration:** everything that changes often (extension/harness/rtk)
  is layered *after* the index bake, so adding a tool reuses cached index layers.

## Storage model (versioned + averaging)

A tool's identity includes its **version**; different versions are different tools:

```
artifacts/<model>-<provider>/<tool>@<version>/
  <tool>@<version>-<prompt>-<run_ts>.json            timestamped run usage
  ...-<run_ts>.transcript.json / .jsonl
  runs.json                                          append-only list of runs
  summary.json                                       per-prompt averages
report.md / report.html / versions.json              aggregates at the model root
```

- Every run is timestamped, so the same `tool@version` can be benchmarked many
  times with every run kept.
- Version = `tool --version` probe (`pi_versions.py`); fallback = kernel git HEAD
  hash when the tool reports none.
- `pi_aggregate.py` averages across all runs (mean/median/min/max + n) per metric
  (total/in/out/cache/reasoning/api/cost/wall/tool-calls).

## Run it

```bash
# build the image (bakes indexes; ~1h, ~21GB — move to /tmp if using BUILD_INDEXES=1)
docker build -t agent-codebase-bench -f docker/Dockerfile .

# recommended: run the harness in-image; the ONLY host mount is artifacts/
docker run --rm -it \
  -e OPENROUTER_API_KEY=$OPENROUTER_API_KEY \
  -v "$(pwd)/artifacts:/workspace/artifacts" \
  --entrypoint /usr/local/bin/pi-bench-run \
  agent-codebase-bench \
  --model openrouter/deepseek-v4-flash-0731 --backend native --runs 2

# host-driven, one-shot containers per cell
python3 bench_pi.py --model openrouter/deepseek-v4-flash-0731 --backend docker --runs 2

# recompute averages/reports from already-saved runs only
python3 bench_pi.py --model openrouter/deepseek-v4-flash-0731 --aggregate-only

# subset / repeat
python3 bench_pi.py --model ... --tools grep,rtk --prompts callers-drm-register --runs 3
```

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

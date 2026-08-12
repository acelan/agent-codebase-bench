# pi-agent migration for agent-codebase-bench

Status: **PARTIAL / working** — docker image builds, pi runs headless in the
container, and the JSON stream parses to the same trace + usage contract the
hermes driver produced. Remaining: full matrix run + index persistence for the
four index-backed tools.

## Why this is viable (validated 2026-08-11)

Ran pi v0.84.1 headless (`--mode json -p`) against openrouter/deepseek. Two
mechanisms the benchmark depends on were verified on the host binary:

1. **Custom extension tools register by name and are allowlisted via `-t`.**
   A test extension registered `probe_tool`; `pi -t probe_tool,read,grep`
   made the model call it. `docker/pi-extensions/bench-tools/index.ts` is the
   real one (registers `codegraph`, `graft`, `repowise`, `codebase_memory`).
2. **`-t` is a hard exclusion, not a hint.** With `-t probe_tool` the model
   reported it had no bash/shell; `grep -c '"name":"bash"'` on the stream = 0.
   This replaces hermes' `-t <toolset>` isolation control.

## Tool-set mapping (hermes toolset -> pi `-t` allowlist)

| benchmark tool | hermes toolset | pi `-t` allowlist |
|---|---|---|
| `grep` | `file` | `read,grep,find,ls` |
| `ripgrep` | `file,terminal` | `read,write,edit,find,ls,bash` (no `grep` — forces raw `rg` via bash, blocking fallback to the built-in grep tool) |
| `codebase-memory-mcp` | `codebase-memory-mcp` (MCP) | `codebase_memory` (extension tool) |
| `codegraph` | `codegraph` (MCP) | `codegraph` (extension tool) |
| `graft` | `graft` (MCP) | `graft` (extension tool) |
| `repowise` | `repowise` (MCP) | `repowise` (extension tool) |

Key point: **pi has no native MCP integration** (README: "No MCP"), so the four
index tools are re-exposed as custom extension tools that shell out to each
tool's CLI against `$KERNEL_DIR` (`/workspace/linux` in the image). `-t <name>`
restricts the model to exactly that tool — same hard control hermes had.

## Format contract (pi `--mode json` stream -> usage + transcript)

pi streams one JSON object per line. Verified fields:

- Session header: `{"type":"session", "id": <uuid>, "cwd": "/workspace/linux"}`
- Per-request usage on assistant `message_end`: `usage{input, output,
  cacheRead, cacheWrite, reasoning, totalTokens, cost{...}}`, plus
  `model`/`provider`.
- Messages carry `content` as a **part list** (text/thinking/toolCall/
  toolResult). `pi_stream.py` flattens these to strings; `pi_transcript.py`
  reassembles `prompt / assistant_tool_call / tool_result / final_answer`
  events and aggregates per-request usage into the hermes usage-file field
  names (`input_tokens`, `cache_read_tokens`, `reasoning_tokens`, `api_calls`,
  `estimated_cost_usd`, ...). A 10-tool-call grep cell produced:
  `input=29956 output=1920 cache=193088 total=224964 api_calls=10` — matching
  the contract `report.py`/`htmlreport.py` render without changes.

## Files

```
docker/Dockerfile                  node:24-bookworm-slim + pi 0.84.1 + all tools
                                   + linux v7.0 clone + baked indexes + rtk
docker/entrypoint.sh               adds --extension bench-tools, cd KERNEL_DIR
docker/run-bench.sh                run harness in-image; all outputs to mounted artifacts/
docker/prepare-index.sh            build tool indexes at image build time
docker/pi-extensions/bench-tools/index.ts   the tool bridge (TS)
pi_stream.py                       NDJSON loader + content flattening
pi_transcript.py                   stream -> trace dict (drop-in for transcript.py)
pi_runner.py                       cell runner (docker or native), versioned+timestamped files
pi_versions.py                     tool version detection (--version, fallback kernel git hash)
pi_aggregate.py                    averages over all runs -> summary.json/report.md/report.html
pi_report.py                       combined artifacts/report.html (overview + testcase groups,
                                   grep-baseline deltas, min/max coloring, hideable prompt + iterations)
bench_pi.py                        matrix driver (per tool@version folders, --runs N)
benchmark.yaml                     tools (incl. rtk), prompts, tool_instruction
```

Tools benchmarked (via the pi extension tool bridge): grep, ripgrep, codegraph,
repowise, codebase-memory-mcp, and **rtk** (Rust Token Killer — wraps grep/rg
and compresses the output the agent reads, so it directly cuts token usage;
`rtk grep <pattern> <path>`).

**graft is disabled** (`benchmark.yaml` → `graft.enabled: false`): its npm CLI
bundles no `tree-sitter-c` grammar (only Go/Python/TypeScript), so it cannot
index the kernel's C code — builds return 0 C cards and `ask()` returns
unrelated Python matches. Re-enable when the C/C++ parser lands via
[NanoNets/Graft#69](https://github.com/NanoNets/Graft/pull/69).

## Versioned storage & averaging model

A tool's identity **includes its version**: `codegraph@1.5.0` is a different
tool from `codegraph@1.6.0`. Storage layout:

```
artifacts/<model>-<provider>/<tool>@<version>/
  <tool>@<version>-<prompt>-<run_ts>.json            timestamped run usage
  ...-<run_ts>.transcript.json / .jsonl
  runs.json                                          append-only list of runs
  summary.json                                       per-prompt averages over runs
report.md / report.html / versions.json              aggregates, one model root
```

- Every cell run gets a `run_ts` (UTC microsecond token) in its filename, so the
  same `tool@version` can be benchmarked many times with **every run kept** and
  each individually findable.
- Version = `tool --version` probe (`pi_versions.py`); if a tool reports none,
  the **kernel git HEAD short hash** is used (the final-commit identity of what
  is actually measured). The built-in `grep` tool is the pi agent's search tool
  (not a CLI), so its version is the **pi agent version** (`grep@0.84.1`,
  source `agent`) rather than ripgrep's; `ripgrep` reports rg's own version.
- `pi_aggregate.py` loads all runs from `runs.json`, groups by
  (tool@version, prompt), and reports mean/median/min/max + n per metric
  (total, in, out, cache, reas, api, cost, wall, tool-calls).
- Currently verified: `grep@15.1.0` (2 runs) shows `total.mean=1050, n=2` while
  `grep@14.0.0` is a separate row/folder — different versions never mix.

## How to run

```bash
# build the image (installs pi + tools, clone linux @ v7.0, harness venv)
docker build -t agent-codebase-bench -f docker/Dockerfile .

# A) recommended: run the harness IN the image; the ONLY host mount is artifacts/
docker run --rm -it \
  -e OPENROUTER_API_KEY=$OPENROUTER_API_KEY \
  -v "$(pwd)/artifacts:/workspace/artifacts" \
  --entrypoint /usr/local/bin/pi-bench-run \
  agent-codebase-bench \
  --model openrouter/deepseek-v4-flash-0731 --backend native --runs 2

# B) host-driven, each cell as a one-shot docker container
python3 bench_pi.py --model openrouter/deepseek-v4-flash-0731 \
    --backend docker --runs 2

# C) native (host pi binary) for fast iteration
python3 bench_pi.py --model openrouter/deepseek-v4-flash-0731 --backend native

# recompute averages/reports from already-saved runs only
python3 bench_pi.py --model openrouter/deepseek-v4-flash-0731 --aggregate-only
```

## TODO / known gaps

- **Index baking — DONE (offline set).** `docker/prepare-index.sh` (run via
  `ARG BUILD_INDEXES=1`) bakes reproducible full indexes at build time (kernel
  pinned v7.0): codegraph `.codegraph` 4.6G, graft `graft/` 27M, and
  codebase-memory-mcp `~/.cache/codebase-memory-mcp/workspace-linux.db` 12.2G
  (`status: ready`, 8.06M nodes). Verified queryable in-container.
- **Repowise (and graft `--deep`) bake requires an API key — via SECRET, not
  ARG.** They are LLM-synthesis tools; full-tree repowise `--mode standard` is
  a measured ~16h then OOM/SIGKILL trap, so they are baked FOCUSED to the
  benchmark subtrees (`drivers/gpu/drm/i915`, `drivers/usb/typec` — repowise
  scopes `.repowise/` to the subtree). Key + models live in one gitignored
  `docker/.env` (see `docker/.env.example`), consumed at build time and
  scrubbed from the image:
  ```bash
  cp docker/.env.example docker/.env        # fill OPENROUTER_API_KEY, PI_MODEL, ...
  docker build -t agent-codebase-bench -f docker/Dockerfile \
      --secret id=repowise_env,src=docker/.env .
  ```
- **Repowise semantic (vector) search is currently FULL-TEXT-ONLY.** The baked
  vector/embedding index fails to build: `repowise generate` reports
  `Indexed 0 items (N failed)` per subtree. `prepare-index.sh` already exports
  `REPOWISE_EMBEDDER=openrouter` +
  `REPOWISE_EMBEDDING_MODEL=openai/text-embedding-3-small`, yet the vectors
  still fail — strong evidence **repowise 0.41 reads the embedder from
  `.repowise/config.yaml` (the subtree), not process env**, so the env vars are
  ignored during the bake. Until fixed, `repowise search` works on the wiki
  pages (full-text) but not semantically. Fix = set `provider: openrouter` /
  `model: openai/text-embedding-3-small` under an embedding/embedder key in the
  subtree's `.repowise/config.yaml` and confirm OpenRouter actually serves that
  embedding model, then re-bake.
- **Full matrix run** DONE (`deepseek-v4-flash-0731`, 6 tools × 3 prompts,
  18/18 cells after filling 2 timeouts) → `artifacts/report.html`. Two tips:
  deep typec/trace cells can exceed 1800s and time out — raise per-cell via
  `PI_CELL_TIMEOUT`; codebase-memory-mcp@0.10.2 OOM-kills its index worker on
  the full kernel so codebase-memory stays pinned at 0.9.0.
- The image pins linux v7.0 to match the prompt era (i915_drv.c exists there;
  on 7.2-era mainline it was restructured and `i915_drv.c` is gone).

## Resume from here

- **Restart SHA:** `10be4c2` (clean except the untracked new files below).
- **New (untracked) files:** `docker/` (Dockerfile, entrypoint.sh,
  run-bench.sh, pi-extensions/bench-tools/index.ts), `pi_stream.py`,
  `pi_transcript.py`, `pi_runner.py`, `pi_versions.py`, `pi_aggregate.py`,
  `bench_pi.py`, `docs/pi-migration.md`.
- **Verification baseline (proven this session):** docker image
  `agent-codebase-bench:latest` builds; `pi --version` in-container = 0.84.1;
  kernel pinned to v7.0 (shallow clone, `git describe` = v7.0); a full grep cell
  through `bench_pi.py --backend docker` produced usage
  `total=117482 in=14817 out=7945 cache=94720 reas=3275 api_calls=11 #toolcalls=34
  cost=0.00447 wall=40.2s` and rendered report.md/run.json/report.html.
- **Exact resume commands:**
  ```bash
  # recommended: in-image harness; only host mount is artifacts/
  docker run --rm -it -e OPENROUTER_API_KEY=$KEY \
      -v "$(pwd)/artifacts:/workspace/artifacts" \
      --entrypoint /usr/local/bin/pi-bench-run \
      agent-codebase-bench \
      --model openrouter/deepseek-v4-flash-0731 --backend native --runs 2
  # host-driven, one-shot containers
  python3 bench_pi.py --model openrouter/deepseek-v4-flash-0731 --backend docker
  # recompute averages/reports only
  python3 bench_pi.py --model ... --aggregate-only
  # rebuild image after Dockerfile changes
  docker build -t agent-codebase-bench -f docker/Dockerfile .
  ```
- **Unfinished:** full matrix run; index persistence for the 4 index tools in
  docker (see TODO above); `--ensure-index` parity for pi.
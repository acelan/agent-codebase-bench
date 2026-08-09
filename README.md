# agent-codebase-bench

Benchmark AI-agent code-query tools on **token efficiency** against a raw
`grep` / `ripgrep` baseline. Each `(tool, prompt)` cell runs as a headless
[Hermes](https://agent.nousresearch.com) agent and records its exact token
usage, cost, API calls, wall time, and a verbose per-iteration transcript —
the inputs and outputs of every tool call — so you can compare tools not just
on totals but on *how* they reached an answer.

The prompt corpus is real Linux-kernel questions (call-graph tracing in
`drivers/gpu/drm/i915`, a `usb/typec` kernfs root-cause, DRM registration
callers), which are the kind of open-ended, codebase-wide queries where
retrieval tools either shine or quietly fall back to ripgrep.

## Tools benchmarked

| tool | isolation (`-t` toolset) |
|---|---|
| `grep` | `file` (search_files, the ripgrep-backed grep tool) |
| `ripgrep` | `file,terminal` (search_files + raw `rg`) |
| `codebase-memory-mcp` | `codebase-memory-mcp` (MCP toolset only) |
| `codegraph` | `codegraph` (MCP toolset only) |
| `graft` | `graft` (MCP toolset only — [nanonets/graft](https://github.com/NanoNets/Graft)) |
| `repowise` | `repowise` (MCP toolset only — [repowise-dev/repowise](https://github.com/repowise-dev/repowise)) |

The `toolsets` restriction is the *hard* control: the agent physically cannot
fall back to ripgrep/grep when told to use a graph tool, so the measurement
isn't polluted. Omitting `toolsets` runs a tool with the full default toolset
(prompt-only emphasis).

## Repository layout

```
bench.py           # runs the (tool × prompt) matrix through hermes -z
report.py          # generates HTML/markdown reports from saved artifacts
htmlreport.py      # renders the self-contained comparison HTML
transcript.py      # exports/parses the verbose per-iteration transcripts
benchmark.yaml     # tools, prompts, kernel_dir config (model comes from models.yaml)
models.yaml        # named model/provider presets (--model-preset <name>)
artifacts/         # durable per-model run dirs (gitignored)
docs/              # architecture/implementation notes
```

## Artifacts layout (the source of truth)

Every model run keeps its **own** directory under `artifacts/`, so model runs
never clobber each other:

```
artifacts/<model>-<provider>[-tag]/
  run.json                         # raw per-(tool,prompt) rows
  report.md                        # markdown summary for this model run
  <tool>-<prompt>.json             # usage report (tokens, cost, api_calls)
  <tool>-<prompt>.transcript.json  # verbose parsed per-iteration trace
  <tool>-<prompt>.transcript.jsonl # raw Hermes session export
```

Reports are generated **on demand** from these saved dirs via `report.py` —
never by re-running the agents and never by parsing rendered HTML.

## Prerequisites

- `hermes` CLI available on `PATH` (the oneshot runner `hermes -z`).
- For the index-backed tools: the kernel tree indexed, and all registered as
  Hermes MCP servers in `~/.hermes/config.yaml`:
  - `codebase-memory-mcp` via `command: /home/acelan/.local/bin/codebase-memory-mcp`
  - `codegraph` via `command: codegraph, args: [serve, --mcp]`
  - `graft` via `command: graft, args: [mcp, <kernel_dir>]` (built with
    `graft build --deep <kernel_dir>`; see [nanonets/graft](https://github.com/NanoNets/Graft))
  - `repowise` via `command: <path-to-venv>/bin/repowise, args: [mcp, <kernel_dir>]`
    (installed with `uv pip install repowise`, indexed with
    `repowise init <kernel_dir>`; see [repowise-dev/repowise](https://github.com/repowise-dev/repowise))
- `python3` with `PyYAML`.

## Running a benchmark

```bash
cd ~/workspace/agent-codebase-bench

# Model must be given explicitly: --model-preset <name> (see models.yaml)
# or --model <model> --provider <provider>.
python3 bench.py --config benchmark.yaml --model-preset ds4 --kernel-dir ~/workspace/linux

# A named model preset from models.yaml (each writes to its OWN
# artifacts/<model>-<provider>/ dir — presets never clobber each other)
python3 bench.py --model-preset sonnet5
python3 bench.py --model-preset gpt5.6
python3 bench.py --model-preset ds4

# Ad-hoc model without touching models.yaml at all
python3 bench.py --model claude-opus-5 --provider github-copilot

# Useful flags
python3 bench.py --tools grep,ripgrep          # subset of tools
python3 bench.py --prompts callers-drm-register  # subset of prompts
python3 bench.py --jobs 4                      # run cells in parallel
python3 bench.py --resume                      # skip cells whose usage json exists
python3 bench.py --backfill-transcripts        # re-export transcripts only, no re-run
python3 bench.py --no-transcripts              # disable verbose capture
python3 bench.py --ensure-index                # (re)build tool indexes first
python3 bench.py --dry-run                     # print commands, run nothing
```

**Adding a new model to benchmark:** add one entry to `models.yaml` (`name:
{model, provider}`) and run `--model-preset <name>` — no new
`benchmark-<model>.yaml` file needed. `benchmark.yaml` (tools, prompts,
kernel_dir) stays the single source of truth for the query matrix; only
model/provider vary per preset. `--model`/`--provider` still work directly
for a one-off run you don't want to name in `models.yaml`.

**Runtime reality:** each `(tool, prompt)` cell is a full `hermes -z` LLM agent
run and takes roughly 1–26 minutes (a codegraph run once hit ~26 min). A
full matrix under one model can take hours; use `--jobs` and `--resume`. Index
setup is opt-in (`--ensure-index`) — existing indexes are reused and never
rebuilt without the flag.

A benchmark run is interrupt-safe: each cell writes its usage JSON early, and
`--resume` skips any whose file already exists.

## Generating reports

```bash
# List the run dirs you have saved
python3 report.py --artifacts artifacts --list

# Combined HTML from ALL model runs (one table per testcase, tool × model)
python3 report.py --artifacts artifacts --out report-combined.html

# Combined from specific run dirs
python3 report.py \
    --run-dir artifacts/gpt-5.6-sol-github-copilot \
    --run-dir artifacts/deepseek-v4-flash-0731-openrouter \
    --out report-combined.html

# Single-model report for one run dir
python3 report.py --run-dir artifacts/gpt-5.6-sol-github-copilot --out report-gpt.html
```

The combined report is a single self-contained HTML file: one comparison table
per testcase (rows are every tool × model combination, with a `model` column),
then folded per-tool transcripts (click a summary to expand that tool's
inputs/outputs). Tool-section anchors are disambiguated per model so a
multi-model report links correctly.

## Configuring prompts / tools

Edit `benchmark.yaml` (or copy it per model, e.g. `benchmark-gpt5.6.yaml`):

- `model` / `provider` — the backend to benchmark.
- `kernel_dir` — the tree the prompts are answered against.
- `tool_instruction` — one-shot prompt prefix per tool so the agent reaches
  for the tool under test.
- `tools.<name>` — per-tool `toolsets` isolation and optional `index.check` /
  `index.run` commands (only run with `--ensure-index`).
- `prompts` — the query corpus. `id` names the result file(s); `text` is
  appended to the tool instruction to form the full `-z` query.

## How metrics are collected

Each cell runs `hermes -z -m <model> --provider <provider> --usage-file
<path>.json -z "<tool_instruction><prompt text>"`. Token fields are session
totals — correct for `-z` since it's one turn per process. `--usage-file`
is oneshot-only; attached to `chat -q` it is silently ignored, so the harness
always uses `-z`.

## Notes

- Some providers (e.g. gpt-5.6-sol on copilot) report `estimated_cost_usd: 0.0`
  / `cost_status: unknown`. Token counts are the reliable cross-tool signal;
  cost may be under-reported by the backend.
- `artifacts/` and generated `report*.html` are gitignored — the raw run data
  is meant to stay on disk but not in version control.

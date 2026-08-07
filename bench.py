#!/usr/bin/env python3
"""Benchmark code-query tools (grep / ripgrep / codebase-memory-mcp /
codegraph) on per-run token usage via the Hermes oneshot runner.

Each (tool, prompt) pair runs:

    hermes -z -m <model> --provider <provider> \\
        --usage-file <results_dir>/<tool>-<prompt>.json \\
        "<tool_instruction><prompt text>"

The usage JSON (tokens, cost, api_calls, duration_seconds) is then merged
into a summary table + Json report.

Setup before benchmarking:
  - Both index-backed tools must have the kernel tree indexed (codegraph's
    .codegraph/, codebase-memory-mcp's project entry). Pass --ensure-index
    to (re)build them from scratch; by default existing indexes are reused.
  - codegraph and the codebase-memory-mcp MCP server must be registered in
    Hermes config (~/.hermes/config.yaml) so the agent can call them.

Results layout:
  Every run is written to its OWN durable directory under `artifacts_dir`
  (default artifacts/), named <model>-<provider> (optionally + --run-tag).
  This keeps each model's raw data + transcripts intact so a later model run
  never clobbers an earlier one. Each run dir contains:
    run.json            raw per-(tool,prompt) rows (source of truth)
    report.md           markdown summary for this model run
    <tool>-<prompt>.json                   usage report
    <tool>-<prompt>.transcript.json        verbose per-iteration trace
    <tool>-<prompt>.transcript.jsonl       raw session export
  Reports are generated on demand from these dirs with report.py (single or
  combined across models), not by re-querying the agents.

Usage:
  python3 bench.py --config benchmark.yaml --kernel-dir ~/workspace/linux
  python3 bench.py --config benchmark.yaml --tools grep,ripgrep
  python3 bench.py --config benchmark.yaml --ensure-index  # (re)build indexes

Results are written to artifacts/<model>-<provider>/ under results_dir.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import yaml

import htmlreport
import transcript


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise SystemExit(f"config {path} is not a mapping")
    return cfg


def _check(probe, cwd, mode="ready"):
    """Run an index.check probe. mode 'ready': ready if stdout contains
    'ready'; mode 'rc': ready if the command exits 0 (its index exists),
    regardless of warning text like codegraph's 'truncated' status."""
    try:
        r = subprocess.run(probe, cwd=cwd, capture_output=True, text=True, timeout=120)
        if mode == "rc":
            return r.returncode == 0
        return r.returncode == 0 and "ready" in r.stdout.lower()
    except Exception:
        return False


def ensure_indexes(cfg, tools, dry_run):
    """Build/reuse tool indexes. Returns list of tools that have an index."""
    kernel_dir = cfg.get("kernel_dir")
    ready, missing = [], []
    for tool in tools:
        spec = cfg["tools"].get(tool, {})
        idx = spec.get("index")
        if not idx:
            ready.append(tool)  # grep/ripgrep need no index
            continue
        probe = [t.replace("{kernel_dir}", kernel_dir) for t in idx.get("check", [])]
        if _check(probe, kernel_dir, mode=idx.get("check_mode", "ready")):
            print(f"[setup] {tool}: index ready, reusing")
            ready.append(tool)
            continue
        run = [t.replace("{kernel_dir}", kernel_dir) for t in idx.get("run", [])]
        print(f"[setup] {tool}: index not ready -> {shlex.join(run)}")
        if dry_run:
            missing.append(tool)
            continue
        t0 = time.monotonic()
        subprocess.run(run, cwd=kernel_dir, check=True)
        print(f"[setup] {tool}: index built in {time.monotonic()-t0:.1f}s")
        ready.append(tool)
    return ready


def run_one(cfg, tool, prompt, results_dir, dry_run, skip=False):
    os.makedirs(results_dir, exist_ok=True)
    usage_path = os.path.join(
        results_dir, f"{tool}-{prompt['id']}.json"
    )
    if skip and os.path.exists(usage_path):
        usage = read_usage(usage_path)
        print(f"  [{tool} / {prompt['id']}] exists, skipping")
        row = {
            "tool": tool, "prompt": prompt["id"],
            "wall_seconds": None, "usage": usage, "exit": None,
            "stdout_len": 0,
        }
        cap = transcript.capture(
            cfg, usage, results_dir, tool, prompt["id"], dry_run=dry_run
        )
        if cap:
            row["transcript_jsonl"] = cap["transcript_jsonl"]
            row["transcript_json"] = cap["transcript_json"]
            row["transcript_summary"] = cap["trace"].get("summary")
        else:
            row["transcript_jsonl"] = None
            row["transcript_json"] = None
        return row
    instruction = cfg["tool_instruction"].get(tool, "")
    query = instruction + prompt["text"] + " "
    toolsets = cfg["tools"][tool].get("toolsets")
    cmd = [
        "hermes",
        "-m", cfg["model"],
        "--provider", cfg["provider"],
    ]
    if toolsets:
        cmd += ["-t", toolsets]
    cmd += [
        "--usage-file", usage_path,
        "-z", query,
    ]
    print(f"  [{tool} / {prompt['id']}] running ...", flush=True)
    if dry_run:
        print("    " + shlex.join(cmd))
        return None
    t0 = time.monotonic()
    r = subprocess.run(cmd, capture_output=True, text=True)
    wall = time.monotonic() - t0
    usage = read_usage(usage_path)
    if r.returncode != 0:
        print(f"    exit={r.returncode} stderr={r.stderr.strip()[:300]}")
    row = {
        "tool": tool,
        "prompt": prompt["id"],
        "wall_seconds": round(wall, 3),
        "usage": usage,
        "exit": r.returncode,
        "stdout_len": len(r.stdout),
    }
    # Verbose per-iteration capture (prompt + per-tool-call input/output).
    cap = transcript.capture(
        cfg, usage, results_dir, tool, prompt["id"], dry_run=dry_run
    )
    if cap:
        row["transcript_jsonl"] = cap["transcript_jsonl"]
        row["transcript_json"] = cap["transcript_json"]
        row["transcript_summary"] = cap["trace"].get("summary")
    else:
        row["transcript_jsonl"] = None
        row["transcript_json"] = None
    return row


def read_usage(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def summarize(rows):
    """Flatten rows into a per-(tool,prompt) summary using usage-file fields."""
    out = []
    for r in rows:
        u = r["usage"] or {}
        out.append({
            "tool": r["tool"],
            "prompt": r["prompt"],
            "exit": r["exit"],
            "wall_seconds": r["wall_seconds"],
            "duration_seconds": u.get("duration_seconds"),
            "total_tokens": u.get("total_tokens"),
            "input_tokens": u.get("input_tokens"),
            "output_tokens": u.get("output_tokens"),
            "cache_read_tokens": u.get("cache_read_tokens"),
            "reasoning_tokens": u.get("reasoning_tokens"),
            "api_calls": u.get("api_calls"),
            "estimated_cost_usd": u.get("estimated_cost_usd"),
            "model": u.get("model"),
            "provider": u.get("provider"),
        })
        if r.get("transcript_json") or r.get("transcript_summary"):
            out[-1]["transcript_json"] = r.get("transcript_json")
            out[-1]["transcript_jsonl"] = r.get("transcript_jsonl")
            out[-1]["iterations"] = (r.get("transcript_summary") or {}).get("iterations")
            out[-1]["tool_calls_captured"] = (r.get("transcript_summary") or {}).get("tool_calls")
    return out


def write_report(cfg, rows, results_dir):
    os.makedirs(results_dir, exist_ok=True)
    body = []
    for r in rows:
        u = r["usage"] or {}
        missing = [k for k in ("duration_seconds", "total_tokens", "api_calls")
                   if u.get(k) is None]
        if missing:
            body.append(
                f"[warn] {r['tool']}/{r['prompt']}: usage file missing "
                f"fields {missing} — {u}.\n"
                "       Ensure the model+provider resolve and --usage-file "
                "was attached with -z.\n"
            )
    header = (
        "# Tool benchmark — %s\n\n"
        "model=%s provider=%s kernel=%s\n\n"
        "Runs per (tool, prompt). total_tokens/cost are session totals for "
        "the -z run. texec=#iterations in transcript, tcaps=#tool calls "
        "captured. Each cell links to its verbose per-iteration transcript "
        "(`*.transcript.json`, with raw `*.transcript.jsonl` beside it) "
        "containing the prompt, every tool-call input and output, and the "
        "final answer.\n\n"
        "| tool | prompt | exit | wall_s | duration_s | total_tok | "
        "in | out | cache | reas | api | cost_usd | texec | tcaps | transcript |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    ) % (
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        cfg["model"], cfg["provider"], cfg["kernel_dir"],
    )
    lines = [header]
    for r in rows:
        u = r["usage"] or {}

        def g(k, default="—"):
            v = u.get(k)
            return default if v is None else str(v)

        summary = r.get("transcript_summary") or {}
        tlink = r.get("transcript_json")
        tcell = f"[json]({os.path.basename(tlink)})" if tlink else "—"
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |\n"
            % (
                r["tool"], r["prompt"], r["exit"], r["wall_seconds"],
                g("duration_seconds"), g("total_tokens"), g("input_tokens"),
                g("output_tokens"), g("cache_read_tokens"),
                g("reasoning_tokens"), g("api_calls"), g("estimated_cost_usd"),
                summary.get("iterations", "—"),
                summary.get("tool_calls", "—"),
                tcell,
            )
        )
    with open(os.path.join(results_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open(os.path.join(results_dir, "run.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, default=str)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="benchmark.yaml")
    ap.add_argument("--kernel-dir", default=None,
                    help="Override config kernel_dir")
    ap.add_argument("--tools", default=None,
                    help="Comma-separated subset of tools")
    ap.add_argument("--prompts", default=None,
                    help="Comma-separated subset of prompt ids")
    ap.add_argument("--results-dir", default=None,
                    help="Output base dir (default: config results_dir, "
                         "== artifacts/). Each model run lands in a "
                         "<model>-<provider> subdir under it.")
    ap.add_argument("--run-tag", default="",
                    help="Extra suffix appended to the run dir name, e.g. "
                         "--run-tag sol -> artifacts/<model>-<provider>-sol. "
                         "Use to separate repeated runs of the same model "
                         "without clobbering an earlier one (default: the "
                         "run dir is exactly <model>-<provider>).")
    ap.add_argument("--ensure-index", action="store_true",
                    help="(Re)build tool indexes before benchmarking")
    ap.add_argument("--jobs", type=int, default=1,
                    help="Run up to N (tool, prompt) cells in parallel "
                         "(default 1 = sequential)")
    ap.add_argument("--resume", action="store_true",
                    help="Skip (tool, prompt) cells whose usage json already "
                         "exists in results_dir (resume an interrupted run)")
    ap.add_argument("--backfill-transcripts", action="store_true",
                    help="Only export transcripts for (tool, prompt) cells "
                         "whose usage json already exists; do not re-run "
                         "anything. Implies --resume.")
    ap.add_argument("--no-transcripts", action="store_true",
                    help="Disable verbose per-iteration transcript capture")
    ap.add_argument("--html", nargs="?", const="report.html", default=None,
                    metavar="OUT.html",
                    help="Generate a self-contained comparison HTML with every "
                         "iteration folded per tool (default OUT: report.html)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print commands without running")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.no_transcripts:
        cfg["capture_transcripts"] = False
    kernel_dir = args.kernel_dir or cfg.get("kernel_dir")
    # results_dir is the BASE (config, == artifacts/); this run lands in a
    # <model>-<provider>[-tag] subdir so per-model data never collides.
    base_dir = args.results_dir or cfg.get("results_dir", "artifacts")
    model, provider = cfg["model"], cfg["provider"]
    tag = args.run_tag.strip()
    run_dir_name = f"{model}-{provider}" + (f"-{tag}" if tag else "")
    results_dir = os.path.join(base_dir, run_dir_name)
    if args.kernel_dir:
        cfg["kernel_dir"] = args.kernel_dir

    tools = (args.tools or "").split(",") if args.tools else None
    tools = [t.strip() for t in (tools or list(cfg["tools"].keys())) if t.strip()]
    tools = [t for t in tools if cfg["tools"].get(t, {}).get("enabled", True)]
    if not tools:
        raise SystemExit("no tools enabled; check config 'tools' section")

    prompt_ids = [p.strip() for p in (args.prompts or "").split(",") if p.strip()] \
        if args.prompts else None
    prompts = [p for p in cfg["prompts"] if not prompt_ids or p["id"] in prompt_ids]
    if not prompts:
        raise SystemExit("no prompts matched")

    print(f"kernel_dir={kernel_dir}")
    print(f"tools={tools}")
    print(f"prompts={[p['id'] for p in prompts]}")

    if args.dry_run and args.html:
        print(f"[html] would render to {os.path.join(results_dir, args.html)}")

    # --html alone (no re-run intent) and run.json already exists: just
    # re-render the comparison page from existing results/transcripts.
    if (
        args.html
        and not args.backfill_transcripts
        and not args.resume
        and args.tools is None
        and args.prompts is None
        and not args.ensure_index
        and not args.dry_run
        and os.path.exists(os.path.join(results_dir, "run.json"))
    ):
        out = os.path.join(results_dir, args.html)
        htmlreport.render_html(results_dir, out)
        print(f"Rendered {out} from existing results (no benchmark run).")
        sys.exit(0)

    ready = ensure_indexes(cfg, tools, args.dry_run)
    active = [t for t in tools if t in ready]

    # Build the worklist of (tool, prompt) cells.
    tasks = [(tool, prompt) for prompt in prompts for tool in active]

    # --backfill-transcripts: don't run anything; just export transcripts for
    # (tool, prompt) cells whose usage json already exists. Preserve prior
    # wall_seconds/exit/usage from an existing run.json (the skip path doesn't
    # re-measure them).
    if args.backfill_transcripts:
        prior = {}
        if os.path.exists(os.path.join(results_dir, "run.json")):
            try:
                with open(os.path.join(results_dir, "run.json"),
                          encoding="utf-8") as f:
                    for prow in json.load(f):
                        prior[(prow.get("tool"), prow.get("prompt"))] = prow
            except Exception:
                print(f"[backfill] could not read existing run.json, "
                      f"wall/exit not preserved")
        rows = []
        for tool, prompt in tasks:
            usage_path = os.path.join(
                results_dir, f"{tool}-{prompt['id']}.json"
            )
            if not os.path.exists(usage_path):
                continue
            row = run_one(cfg, tool, prompt, results_dir,
                          dry_run=args.dry_run, skip=True)
            p = prior.get((tool, prompt["id"]))
            if p is not None:
                row["wall_seconds"] = p.get("wall_seconds")
                row["exit"] = p.get("exit")
                row["stdout_len"] = p.get("stdout_len")
            rows.append(row)
        write_report(cfg, rows, results_dir)
        print(f"\nBackfilled transcripts into {os.path.join(results_dir, 'report.md')} "
              f"and {os.path.join(results_dir, 'run.json')}")
        sys.exit(0)

    if args.dry_run:
        for tool, prompt in tasks:
            run_one(cfg, tool, prompt, results_dir, dry_run=True, skip=args.resume)
        sys.exit(0)

    rows = []
    if args.jobs <= 1:
        for tool, prompt in tasks:
            rows.append(run_one(cfg, tool, prompt, results_dir,
                                dry_run=False, skip=args.resume))
    else:
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futs = [
                ex.submit(run_one, cfg, tool, prompt, results_dir,
                          False, args.resume)
                for tool, prompt in tasks
            ]
            for fut in as_completed(futs):
                rows.append(fut.result())

    write_report(cfg, rows, results_dir)
    print(f"\nWrote {os.path.join(results_dir, 'report.md')} "
          f"and {os.path.join(results_dir, 'run.json')}")

    if args.html:
        out = os.path.join(results_dir, args.html)
        htmlreport.render_html(results_dir, out)
        print(f"Wrote {out}")


if __name__ == "__main__":
    main()

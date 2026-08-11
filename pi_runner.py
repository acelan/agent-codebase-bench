"""pi-backed cell runner for agent-codebase-bench.

Replaces the `hermes -z --usage-file ...` invocation with a headless pi run in
`--mode json`. For one (tool, prompt) cell it:

  * builds the pi command (native binary or `docker run` of the bench image),
  * passes pi's `-t <allowlist>` tool-isolation control (the pi analogue of
    hermes' `-t <toolset>`), so a cell can ONLY call the tool under test,
  * captures stdout (the NDJSON stream), and
  * aggregates per-request usage into the hermes usage-file field names
    (input_tokens / output_tokens / cache_read_tokens / reasoning_tokens /
    total_tokens / api_calls / estimated_cost_usd / model / provider /
    session_id).

Storage model (see docs/pi-migration.md):
  artifacts/<model>-<provider>/<tool>@<version>/      version is part of tool id
    <tool>@<version>-<prompt>-<run_ts>.json            timestamped run files
    ...-<run_ts>.transcript.json / .jsonl
    runs.json                                          append-only list of runs
Each run is timestamped so the same tool@version can be benchmarked many times
with every result kept; pi_aggregate computes averages over all runs.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time

import pi_transcript

# Optional config via env.
PI_BIN = os.environ.get("PI_BIN", "pi")          # native pi binary path
BENCH_IMAGE = os.environ.get("BENCH_IMAGE", "agent-codebase-bench")
KERNEL_MOUNT = os.environ.get("KERNEL_MOUNT", "")  # "HOSTDIR" -> -v HOSTDIR:/workspace/linux
PI_MODEL_FLAG = "--model"                          # pi uses --model provider/model
# Extension that registers the index tools as custom CLI-backed tools. Used so
# the native backend (pi running directly in the container on PATH) can call
# codegraph/graft/repowise/codebase_memory. The docker-one-shot backend instead
# relies on the image entrypoint to add the extension.
PROBE_EXT = "/opt/pi-bench/extensions/bench-tools/index.ts"

# The pi -t allowlist for each benchmark tool (key = benchmark tool name).
TOOL_SET = {
    "grep": "read,grep,find,ls",
    # ripgrep cell: raw `rg` via bash. `grep` is deliberately NOT allowlisted so
    # the model cannot silently fall back to the built-in grep tool — it must
    # run ripgrep itself, or the token/cost figures are contaminated.
    "ripgrep": "read,write,edit,find,ls,bash",
    "codebase-memory-mcp": "codebase_memory",
    "codegraph": "codegraph",
    "graft": "graft",
    "repowise": "repowise",
    "rtk": "rtk",
}


def _pi_args(model, query, allowlist):
    """The pi argv for a headless one-shot run (provider embedded in model)."""
    args = [PI_MODEL_FLAG, model, "--mode", "json", "-p", "--no-session"]
    if allowlist:
        args += ["--tools", allowlist]
    args += [query]
    return args


def build_cmd(model, query, allowlist, backend="docker"):
    if backend == "native":
        cmd = [PI_BIN]
        if os.path.exists(PROBE_EXT):
            cmd += ["--extension", PROBE_EXT]
        cmd += _pi_args(model, query, allowlist)
        return cmd
    cmd = ["docker", "run", "--rm"]
    if os.environ.get("OPENROUTER_API_KEY"):
        cmd += ["-e", "OPENROUTER_API_KEY"]
    if KERNEL_MOUNT:
        cmd += ["-v", f"{KERNEL_MOUNT}:/workspace/linux"]
    cmd += [BENCH_IMAGE] + _pi_args(model, query, allowlist)
    return cmd


def run_cell(cfg, tool, prompt, tool_version, version_source, run_ts,
             results_dir, backend="docker", dry_run=False):
    """Run one (tool, prompt) cell under pi. Returns a run row dict.

    results_dir = artifacts/<model>-<provider>/<tool>@<version> (caller builds).
    run_ts = a unique per-run timestamp token, included in every filename.
    Writes <tool>@<version>-<prompt>-<run_ts>.{json, transcript.json,
    transcript.jsonl} and appends the run row to runs.json in results_dir.
    """
    v = tool_version or "n/a"
    tool_id = f"{tool}@{v}"
    os.makedirs(results_dir, exist_ok=True)
    run_id = f"{tool_id}-{prompt['id']}-{run_ts}"
    jsonl_path = os.path.join(results_dir, f"{run_id}.transcript.jsonl")
    struct_path = os.path.join(results_dir, f"{run_id}.transcript.json")
    usage_path = os.path.join(results_dir, f"{run_id}.json")
    runs_path = os.path.join(results_dir, "runs.json")

    instruction = cfg["tool_instruction"].get(tool, "")
    query = instruction + prompt["text"] + " "
    allowlist = TOOL_SET.get(tool, "")
    cmd = build_cmd(cfg["model"], query, allowlist, backend=backend)
    print(f"  [{tool_id} / {prompt['id']}] running ...", flush=True)
    if dry_run:
        print("    " + shlex.join(cmd))
        return None

    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        r = None
    wall = time.monotonic() - t0

    if r is None or (r.stdout or "").strip() == "":
        row = _empty_row(tool, v, version_source, prompt, run_ts, None, wall)
    else:
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(r.stdout)
        trace = pi_transcript.parse_stream(jsonl_path)
        usage = _usage_from_trace(trace, cfg, wall, trace.get("session_id"))
        with open(usage_path, "w", encoding="utf-8") as f:
            json.dump(usage, f, indent=2, ensure_ascii=False)
        with open(struct_path, "w", encoding="utf-8") as f:
            json.dump(trace, f, indent=2, ensure_ascii=False)
        row = {
            "tool": tool, "tool_version": v, "tool_id": tool_id,
            "version_source": version_source,
            "model": cfg.get("model"), "provider": cfg.get("provider"),
            "prompt": prompt["id"], "run_ts": run_ts,
            "exit": getattr(r, "returncode", None),
            "wall_seconds": round(wall, 3),
            "usage": usage,
            "transcript_jsonl": os.path.basename(jsonl_path),
            "transcript_json": os.path.basename(struct_path),
            "summary": trace.get("summary"),
        }
        if getattr(r, "returncode", 0) != 0:
            print(f"    exit={r.returncode} stderr={r.stderr.strip()[:300]}")

    # Append-only runs log.
    runs = []
    if os.path.exists(runs_path):
        try:
            with open(runs_path, encoding="utf-8") as f:
                runs = json.load(f)
        except Exception:
            runs = []
    runs.append(row)
    with open(runs_path, "w", encoding="utf-8") as f:
        json.dump(runs, f, indent=2, ensure_ascii=False)
    return row


def _empty_row(tool, v, version_source, prompt, run_ts, rc, wall):
    return {
        "tool": tool, "tool_version": v, "tool_id": f"{tool}@{v}",
        "version_source": version_source, "prompt": prompt["id"],
        "run_ts": run_ts, "exit": rc, "wall_seconds": round(wall, 3),
        "usage": {"error": "empty/no output"},
        "transcript_json": None, "transcript_jsonl": None, "summary": None,
    }


def _usage_from_trace(trace, cfg, wall, session_id):
    if trace.get("error"):
        return {"error": trace["error"]}
    ag = trace.get("aggregate", {})
    return {
        "estimated_cost_usd": ag.get("estimated_cost_usd"),
        "cost_status": "estimated" if ag.get("estimated_cost_usd") is not None
                       else None,
        "cost_source": "pi_stream_aggregate",
        "input_tokens": ag.get("input_tokens"),
        "output_tokens": ag.get("output_tokens"),
        "cache_read_tokens": ag.get("cache_read_tokens"),
        "cache_write_tokens": ag.get("cache_write_tokens"),
        "reasoning_tokens": ag.get("reasoning_tokens"),
        "total_tokens": ag.get("total_tokens"),
        "api_calls": ag.get("api_call_count"),
        "model": trace.get("model") or cfg.get("model"),
        "provider": trace.get("provider") or cfg.get("provider"),
        "session_id": session_id,
        "completed": True, "failed": False, "partial": False,
        "interrupted": False, "turn_exit_reason": "pi_one_shot",
        "duration_seconds": round(wall, 3),
    }

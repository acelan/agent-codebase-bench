"""Verbose per-iteration transcript capture for agent-codebase-bench.

Each `hermes -z` benchmark run is a full Hermes session whose messages live in
the session store. We export that session to JSONL (via
`hermes sessions export`) and rewrite it into a structured, human-readable per-
iteration trace: the initial prompt, every assistant tool-call (name + argument
payload = input), every tool result (output), and the final assistant answer,
each with its token count and timestamp.

This is intentionally verbose: it is what allows comparing tools on *how* they
reached an answer, not just aggregate token totals. The heavy per-iteration
detail is written to dedicated `<tool>-<prompt>.transcript.*` files; run.json /
report.md stay compact and only link to them via `transcript_path`.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess

# Field names present on every exported session message record that carry the
# per-iteration signal we care about. Everything else is transport noise.
_MSG_FIELDS = ("id", "role", "timestamp", "token_count", "finish_reason")

# Runtime cost per invocation of the export CLI (one hermes subprocess per run).
EXPORT_BIN = "hermes"


def export_session_jsonl(session_id, out_path, dry_run=False):
    """Export one Hermes session to a JSONL file. Returns path or None."""
    cmd = [
        EXPORT_BIN, "sessions", "export",
        "--format", "jsonl",
        "--session-id", session_id,
        out_path,
    ]
    if dry_run:
        print("    [transcript] " + shlex.join(cmd))
        return None
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"    [transcript] export failed rc={r.returncode}: "
              f"{r.stderr.strip()[:300]}")
        return None
    if not os.path.exists(out_path):
        print("    [transcript] export reported success but no file")
        return None
    return out_path


def _load_jsonl(path):
    """Yield parsed records from a JSONL file."""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _tool_call_args(args):
    """Best-effort parse of a tool-call arguments string into a dict."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            return json.loads(args)
        except Exception:
            return {"_raw": args}
    return args


def parse_transcript(jsonl_path):
    """Turn an exported session JSONL into a verbose per-iteration trace dict.

    Returns a dict with `prompt`, `events` (chronological list of
    prompt / assistant / tool_result / final_answer events), and per-run
    aggregate counts (iterations, tool_calls, tokens).
    """
    records = list(_load_jsonl(jsonl_path))
    if not records:
        return {"error": "empty transcript"}

    session = records[0]
    trace = {
        "session_id": session.get("session_id"),
        "model": session.get("model"),
        "provider": session.get("provider"),
        "prompt": None,
        "events": [],
        "aggregate": {
            "message_count": session.get("message_count"),
            "tool_call_count": session.get("tool_call_count"),
            "api_call_count": session.get("api_call_count"),
            "input_tokens": session.get("input_tokens"),
            "output_tokens": session.get("output_tokens"),
            "estimated_cost_usd": session.get("estimated_cost_usd"),
        },
    }

    for rec in session.get("messages", []):
        role = rec.get("role")
        tcalls = rec.get("tool_calls") or []
        content = rec.get("content")
        base = {k: rec.get(k) for k in _MSG_FIELDS}

        if role == "user":
            if trace["prompt"] is None:
                trace["prompt"] = content
            trace["events"].append({"type": "prompt", **base, "content": content})
        elif role == "assistant":
            if tcalls:
                trace["events"].append({
                    "type": "assistant_tool_call",
                    **base,
                    "content": content,
                    "tool_calls": [
                        {
                            "tool_call_id": tc.get("call_id") or tc.get("id"),
                            "name": (tc.get("function") or {}).get("name"),
                            "arguments": _tool_call_args(
                                (tc.get("function") or {}).get("arguments")
                            ),
                        }
                        for tc in tcalls
                    ],
                })
            else:
                trace["events"].append({
                    "type": "final_answer" if content else "assistant",
                    **base,
                    "content": content,
                })
        elif role == "tool":
            trace["events"].append({
                "type": "tool_result",
                **base,
                "tool_call_id": rec.get("tool_call_id"),
                "tool_name": rec.get("tool_name"),
                "content": content,
            })

    # A `hermes -z` one-shot run terminates at its first text response, so the
    # benchmark run is semantically "everything up to and including the first
    # final_answer". Truncating there makes capture stable even when the
    # session is still live (still being appended to in the store) — otherwise
    # a backfill of a live session would keep growing / pick up later messages.
    first_final = next(
        (i for i, e in enumerate(trace["events"]) if e["type"] == "final_answer"),
        None,
    )
    if first_final is not None:
        trace["events"] = trace["events"][: first_final + 1]

    # Iteration = one assistant turn (a batch of tool calls or a final answer).
    trace["summary"] = {
        "iterations": sum(
            1 for e in trace["events"]
            if e["type"] in ("assistant_tool_call", "final_answer")
        ),
        "tool_calls": sum(
            len(e["tool_calls"]) for e in trace["events"]
            if e["type"] == "assistant_tool_call"
        ),
    }
    return trace


def capture(cfg, usage, results_dir, tool, prompt, dry_run=False):
    """Capture the verbose transcript for one (tool, prompt) run.

    Uses the session_id recorded in the usage file. Writes two artifacts:
    `<tool>-<prompt>.transcript.jsonl` (raw export) and
    `<tool>-<prompt>.transcript.json` (parsed, verbose trace).
    Returns dict with artifact paths (and parsed `trace`), or None if the
    session could not be exported.
    """
    if not cfg.get("capture_transcripts", True):
        return None
    if not usage:
        return None
    session_id = usage.get("session_id")
    if not session_id:
        return None

    base = os.path.join(results_dir, f"{tool}-{prompt}")
    jsonl_path = f"{base}.transcript.jsonl"
    struct_path = f"{base}.transcript.json"

    raw = export_session_jsonl(session_id, jsonl_path, dry_run=dry_run)
    if not raw:
        return None

    trace = parse_transcript(jsonl_path)
    with open(struct_path, "w", encoding="utf-8") as f:
        json.dump(trace, f, indent=2, ensure_ascii=False)

    return {
        "transcript_jsonl": jsonl_path,
        "transcript_json": struct_path,
        "trace": trace,
    }

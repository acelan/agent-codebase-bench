#!/usr/bin/env python3
"""Render the combined HTML report under artifacts/, aggregating ALL model runs.

Reads every artifacts/<model>-<provider>/<tool>@<version>/summary.json +
folded transcripts and draws one self-contained report.html at the artifacts
root (e.g. artifacts/report.html). New models can be added and the report
re-rendered; it spans whatever models are present.

Layout (each maps to an explicit requirement):
  1. output lives under artifacts/ (aggregates across models)
  2. overview table (tool@version | total tokens | api calls — NO cost column)
     at the top, cheapest -> costliest
  3. results grouped by testcase (prompt)
  4. grep is the baseline: every tool shows -xx% / +xx% token delta vs grep
  5. highest/lowest value per metric is colored (best=green, worst=red)
     within a testcase for tokens / in / out / api / elapsed / tool calls
  6. elapsed (was wall_s) and tool calls (was tcaps) use plain-language names
  7. each testcase has a hideable prompt
  8. each tool has hideable per-iteration transcripts (what actually transferred)

Usage:
  python3 pi_report.py --artifacts artifacts [--out artifacts/report.html]
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re


# Metric columns shown per testcase: (summary key, human label, numeric).
# wall_s / tcaps are renamed to friendly terms (req 6).
BASE_COLS = [
    ("total", "tokens", True),
    ("in", "in", True),
    ("out", "out", True),
    ("api", "api", True),
    ("wall_s", "elapsed (s)", True),
    ("tcaps", "tool calls", True),
]

# Overview columns (req 2 — no cost column).
OVERVIEW_COLS = [("total", "total tokens", True), ("api", "api calls", True)]

BASELINE_TOOL = "grep"

# Short, report-facing descriptions. Keep these deliberately concise: the
# report is a benchmark result, not a tool catalogue. URLs point to the
# upstream project or package page rather than to a local installation.
TOOL_INFO = {
    "grep": {
        "description": "The pi agent's built-in file/search tools, used here as the baseline.",
        "url": "https://github.com/earendil-works/pi",
    },
    "ripgrep": {
        "description": "A fast recursive text-search tool that scans the tree directly.",
        "url": "https://github.com/BurntSushi/ripgrep",
    },
    "codegraph": {
        "description": "An indexed code-intelligence and knowledge-graph tool for symbol exploration.",
        "url": "https://github.com/colbymchenry/codegraph",
    },
    "graft": {
        "description": "A context graph that connects linked Markdown knowledge cards.",
        "url": "https://github.com/NanoNets/Graft",
    },
    "repowise": {
        "description": "A generated codebase wiki and semantic search/indexing tool.",
        "url": "https://github.com/repowise-dev/repowise",
    },
    "codebase-memory-mcp": {
        "description": "A structural code graph exposed through a Model Context Protocol server.",
        "url": "https://github.com/DeusData/codebase-memory-mcp",
    },
    "rtk": {
        "description": "Rust Token Killer, a grep/rg wrapper that compresses command output.",
        "url": "https://github.com/rtk-ai/rtk",
    },
}


# ---------------------------------------------------------------------------
# data loading
# ---------------------------------------------------------------------------

def discover_model_roots(artifacts):
    """Return list of model dirs (artifacts/<model>-<provider>/) that have >=1
    <tool>@<version>/summary.json."""
    roots = []
    if not os.path.isdir(artifacts):
        return roots
    for name in sorted(os.listdir(artifacts)):
        mr = os.path.join(artifacts, name)
        if not os.path.isdir(mr):
            continue
        if any(os.path.exists(os.path.join(mr, t, "summary.json"))
               for t in os.listdir(mr)):
            roots.append(mr)
    return roots


def _mean(stats):
    return (stats or {}).get("mean")


def load_cells(artifacts, instructions=None):
    """Yield dicts describing one (model, tool@version, prompt) run-cell:
    {model, model_root, tool_id, tool, version, prompt, prompt_text, cells}."""
    instructions = instructions or {}
    for mr in discover_model_roots(artifacts):
        model = os.path.basename(mr)
        for tid in sorted(os.listdir(mr)):
            sd = os.path.join(mr, tid)
            sj = os.path.join(sd, "summary.json")
            if not (os.path.isdir(sd) and os.path.exists(sj)):
                continue
            with open(sj, encoding="utf-8") as f:
                s = json.load(f)
            tool = s.get("tool")
            for p in s.get("prompts", []):
                # Keep cache/reasoning available for the narrative summary even
                # though the compact comparison table does not display them.
                cells = {k: _mean(p.get(k)) for k, _, _ in BASE_COLS}
                cells.update({k: _mean(p.get(k)) for k in ("cache", "reas")})
                yield {
                    "model": model,
                    "model_root": mr,
                    "tool_id": tid,
                    "tool": p.get("tool") or tool,
                    "version": s.get("version"),
                    "prompt": p.get("prompt"),
                    "prompt_text": _prompt_text(mr, tid, p, instructions),
                    "cells": cells,
                    "summary": s,
                    "pprompt": p,
                }


def _prompt_text(model_root, tid, p, instructions=None):
    """Best-effort prompt text, with the prepended tool-instruction removed so
    the report shows the generic benchmark prompt (see docs/pi-migration.md)."""
    rl = p.get("run_log") or []
    tpath = ""
    if rl and rl[0].get("transcript"):
        tpath = os.path.join(model_root, tid, rl[0]["transcript"])
    try:
        with open(tpath, encoding="utf-8") as f:
            tr = json.load(f)
        raw = tr.get("prompt")
    except Exception:
        raw = None
    if not raw:
        return None
    return _strip_instruction(raw, p.get("tool"), instructions or {})


def _strip_instruction(prompt, tool, instructions):
    """Drop the per-tool prepended instruction (e.g. 'Use codebase-memory-mcp
    to do any code related query. ') from the displayed prompt."""
    if not prompt:
        return prompt
    instr = (instructions or {}).get(tool)
    if instr and prompt.startswith(instr):
        return prompt[len(instr):]
    # Fallback: strip any leading '<Use ... to do any code related query>' block.
    import re
    m = re.match(r"^Use .*? to do any code related query[.;]?\s*", prompt)
    return prompt[m.end():] if m else prompt


# ---------------------------------------------------------------------------
# transcript -> iterations (req 8)
# ---------------------------------------------------------------------------

def load_iterations(model_root, tid, p):
    """Return list of iterations for one cell, or None if no transcript."""
    rl = p.get("run_log") or []
    if not rl or not rl[0].get("transcript"):
        return None
    tpath = os.path.join(model_root, tid, rl[0]["transcript"])
    try:
        with open(tpath, encoding="utf-8") as f:
            trace = json.load(f)
    except Exception:
        return None
    iters = []
    cur = None
    for e in trace.get("events", []):
        t = e.get("type")
        if t == "assistant_tool_call":
            cur = {"calls": e.get("tool_calls") or [], "results": [], "final": None}
            iters.append(cur)
        elif t == "tool_result":
            if cur is None:
                cur = {"calls": [], "results": [], "final": None}
                iters.append(cur)
            cur["results"].append(e)
        elif t == "final_answer":
            if cur is not None and cur["final"] is None:
                cur["final"] = e
            else:
                iters.append({"calls": [], "results": [], "final": e})
                cur = None
    return iters


# ---------------------------------------------------------------------------
# formatting / HTML
# ---------------------------------------------------------------------------

def fmt_num(v):
    if v is None:
        return "—"
    f = float(v)
    a = abs(f)
    if a >= 1e6:
        return f"{f/1e6:.2f}M"
    if a >= 1e3:
        return f"{f/1e3:.1f}k"
    if f == int(f):
        return str(int(f))
    return f"{f:.1f}"


def _esc(s):
    return html.escape("" if s is None else str(s))


def _best_worst(rows, key):
    vals = [r["cells"].get(key) for r in rows if r["cells"].get(key) is not None]
    if not vals:
        return None, None
    return min(vals), max(vals)


def _delta_pct(tool_total, base_total):
    if base_total in (None, 0) or tool_total is None:
        return None, ""
    pct = (tool_total - base_total) / base_total * 100.0
    cls = "delta-good" if pct < 0 else ("delta-bad" if pct > 0 else "delta-0")
    return pct, cls


def _tool_info(tool):
    return TOOL_INFO.get(tool, {
        "description": "A code-query tool included in this benchmark.",
        "url": "",
    })


def _tool_intro_html(rows):
    """Describe every tool represented in this report, with its project URL."""
    tools = sorted({r["tool"] for r in rows})
    items = []
    for tool in tools:
        info = _tool_info(tool)
        link = (f" <a href='{_esc(info['url'])}' target='_blank' rel='noreferrer'>"
                "project</a>" if info.get("url") else "")
        items.append(f"<li><b>{_esc(tool)}</b> — {_esc(info['description'])}{link}</li>")
    return ("<h1>Tools</h1>"
            "<p>Each tool was isolated and asked the same Linux-kernel questions. "
            "The links below identify the upstream project or package measured.</p>"
            "<ul class='tools'>" + "".join(items) + "</ul>")


def _trace_evidence(row):
    """Extract workflow evidence from the recorded iterations, not usage data."""
    iters = load_iterations(row["model_root"], row["tool_id"], row["pprompt"]) or []
    calls = []
    results = []
    for iteration in iters:
        for call in iteration.get("calls", []):
            args = call.get("arguments")
            if isinstance(args, dict):
                args = json.dumps(args, ensure_ascii=False)
            calls.append(f"{call.get('name', '')} {args or ''}")
        results.extend((r.get("content") or "") for r in iteration.get("results", []))
    call_text = "\n".join(calls)
    result_text = "\n".join(results)
    command_errors = any(re.search(
        r"(?:\[.*? error\]|path not found:|/usr/bin/grep:|no such file|"
        r"command failed|invalid (?:argument|query)|missing required)",
        result, re.I) for result in results)
    search_calls = [c for c in calls if re.search(r"\b(rg|grep|find|search|query)\b", c, re.I)]
    return {
        "calls": calls,
        "searches": len(search_calls),
        "reads": sum(bool(re.search(r"\b(read|sed|awk|cat|get_code_snippet|snippet)\b", c, re.I)) for c in calls),
        "broad_discovery": bool(re.search(r"find /|cd /workspace && ls|ls drivers", call_text)),
        "repeated_search": len(search_calls) >= 3,
        "failures": command_errors,
        "empty_results": any(not result.strip() or result.strip() == "(no output)" for result in results),
        "large_results": any(len(r) > 5000 for r in results),
        "graph_results": bool(re.search(
            r"Found .* symbols|Blast radius|Dynamic-dispatch links|callers_total|"
            r"relationship|\"rows\"\s*:", result_text, re.I)),
        "fulltext_results": "Full-text search:" in result_text,
        "api_setup": bool(re.search(
            r"schema|project|pagination|search_code|get_code_snippet|query shape|"
            r"mem\.init|version_cohort", call_text + "\n" + result_text, re.I)),
        "result_text": result_text,
    }


def _workflow_summary(row):
    """Explain where a tool saves/spends context based on its actual trace."""
    tool = row["tool"]
    evidence = _trace_evidence(row)
    saves = []
    spends = []
    if tool == "rtk":
        saves.append("compressing grep/rg-style results before they reach the agent")
        spends.append("re-running searches when compressed output is insufficient")
    elif tool == "codegraph":
        saves.append("using compact indexed relationship exploration instead of reconstructing every edge with literal searches")
        spends.append("interpreting broad blast-radius and source-excerpt responses and filtering irrelevant global matches")
    elif tool == "codebase-memory-mcp":
        saves.append("using indexed snippets and relationship queries once the project and query shape are understood")
        spends.append("learning the API/schema and assembling the requested path from snippets, rows, and graph results")
    elif tool == "repowise":
        saves.append("avoiding a raw tree-wide source dump through indexed full-text retrieval")
        spends.append("comparing ranked full-text snippets and filling structural gaps the search index does not answer")
    elif tool == "graft":
        saves.append("following pre-built context links instead of repeating unrelated source searches")
        spends.append("following links that do not directly resolve the requested code relationship")
    else:
        saves.append("using literal matches to anchor the investigation in exact source locations")
        spends.append("searching candidate names and reading surrounding definitions and callers")
    if evidence["broad_discovery"]:
        spends.append("initial filesystem or workspace discovery before the real query")
    if evidence["repeated_search"] and tool not in ("repowise", "codebase-memory-mcp"):
        spends.append("repeating or refining searches after the first result was incomplete")
    if evidence["graph_results"]:
        saves.append("getting symbol relationships and relevant source excerpts in the same lookup")
        spends.append("the graph response carrying broad relationship and source context")
    if evidence["fulltext_results"]:
        spends.append("ranked full-text results that must be compared and verified")
    if evidence["api_setup"] and tool == "codebase-memory-mcp":
        spends.append("project/schema setup, malformed query retries, and pagination before useful results arrive")
    if evidence["failures"]:
        spends.append("recovering from empty, missing, or invalid tool results")
    elif evidence["empty_results"]:
        spends.append("checking empty search results before switching to a better-scoped query")
    if evidence["large_results"]:
        spends.append("large returned source or graph context that then remains in the conversation")
    if evidence["reads"] and evidence["searches"] and tool not in ("codegraph", "codebase-memory-mcp"):
        saves.append("moving from candidate matches to focused source windows rather than rereading unrelated files")
    return (f"Saves context by {_join_phrases(saves)}. "
            f"Spends context on {_join_phrases(spends)}.")


def _join_phrases(items):
    items = list(dict.fromkeys(items))
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return "; ".join(items[:-1]) + f"; and {items[-1]}"


def _results_summary_html(rows):
    """Summarize strengths and costs from the recorded interaction traces."""
    prompts = sorted({r["prompt"] for r in rows})
    sections = ["<details class='result-summary'><summary>Result summary</summary>"
                "<h1>Result summary</h1>",
                "<p>These observations come from the recorded interaction traces. "
                "They describe the workflow choices that save context and the "
                "follow-up work that spends it, rather than repeating metric values.</p>"]
    for prompt in prompts:
        group = [r for r in rows if r["prompt"] == prompt]
        valid = [r for r in group if r["cells"].get("total") is not None]
        if not valid:
            continue
        winner = min(valid, key=lambda r: r["cells"]["total"])
        sentences = [f"<b>{_esc(prompt)}</b>: <b>{_esc(winner['tool'])}</b> "
                     f"has the most economical observed workflow. {_esc(_workflow_summary(winner))}"]
        for row in sorted(valid, key=lambda r: r["tool"]):
            if row is winner:
                continue
            sentences.append(f"<br><b>{_esc(row['tool'])}</b>: {_esc(_workflow_summary(row))}")
        sections.append("<p class='finding'>" + " ".join(sentences) + "</p>")
    sections.append("</details>")
    return "".join(sections)


def _cell_html(value, key, best, worst):
    if value is None:
        return "<td class='num'>—</td>"
    style = ""
    # For cost-like metrics, lower is better; for none here, best = min.
    if value == best:
        style = "best"
    elif value == worst and worst != best:
        style = "worst"
    fmt = fmt_num(value)
    if key == "wall_s":
        fmt = fmt_num(value)
    return f"<td class='num {style}'>{fmt}</td>"


def _overview_html(rows):
    """Overview table (req 2): tool@version | total tokens | api calls | Δ vs grep."""
    by_tool = {}
    for r in rows:
        b = by_tool.setdefault(r["tool_id"], {"total": 0.0, "api": 0, "model": set(), "tool": r.get("tool")})
        b["total"] += float(r["cells"].get("total") or 0)
        b["api"] += int(r["cells"].get("api") or 0)
        b["model"].add(r["model"])
    grep_total = by_tool.get(BASELINE_TOOL, {}).get("total") or next(
        (v["total"] for k, v in by_tool.items() if k.startswith(BASELINE_TOOL + "@")), None)
    items = sorted(by_tool.items(), key=lambda kv: kv[1]["total"])
    total_max = max((v["total"] for v in by_tool.values()), default=0)
    api_max = max((v["api"] for v in by_tool.values()), default=0)
    best_t = min(by_tool.values(), key=lambda v: v["total"])["total"] if by_tool else 0
    best_a = min(by_tool.values(), key=lambda v: v["api"])["api"] if by_tool else 0
    thead = "<tr><th>tool@version</th><th>total tokens</th><th>api calls</th><th>Δ vs grep</th></tr>"
    body = []
    for tid, v in items:
        tcl = "best" if v["total"] == best_t else ("worst" if v["total"] == total_max and total_max != best_t else "")
        acl = "best" if v["api"] == best_a else ("worst" if v["api"] == api_max and api_max != best_a else "")
        pct, dcls = _delta_pct(v["total"], grep_total)
        delta = "+0%" if tid == BASELINE_TOOL or v.get("tool") == BASELINE_TOOL else ("" if pct is None else f"{pct:+.0f}%")
        nm = " <span class='mrk'>" + ", ".join(sorted(v["model"])) + "</span>" if len(v["model"]) > 1 else ""
        body.append(
            f"<tr><td><b>{_esc(tid)}</b>{nm}</td>"
            f"<td class='num {tcl}'>{fmt_num(v['total'])}</td>"
            f"<td class='num {acl}'>{v['api']}</td>"
            f"<td class='num {dcls}'>{delta}</td></tr>"
        )
    return ("<h1>Overview</h1>"
            "<p>Aggregated across all testcases and models, cheapest -> costliest "
            "(best green, worst red). Δ vs grep on total tokens.</p>"
            "<table class='ov'><thead>" + thead + "</thead><tbody>"
            + "".join(body) + "</tbody></table>")


def _group_html(prompt, rows):
    """One testcase (prompt) group: header, hideable prompt, metric table with
    grep baseline + min/max coloring, then per-tool hideable iterations."""
    # grep baseline (per-model grep total for req 4)
    prompt_rows = [r for r in rows if r["prompt"] == prompt]
    h = [f"<h2>testcase: {_esc(prompt)}</h2>"]

    # hideable prompt (req 7)
    ptxt = next((r["prompt_text"] for r in prompt_rows if r["prompt_text"]), None)
    if ptxt is not None:
        h.append("<details class='prompt'><summary>prompt</summary><pre>"
                 + _esc(ptxt) + "</pre></details>")

    # table
    thead = "<tr><th>tool@version</th><th>model</th><th>Δ vs grep</th>"
    for _, label, _ in BASE_COLS:
        thead += f"<th>{label}</th>"
    thead += "</tr>"
    trows = []
    for r in prompt_rows:
        base = next((x["cells"].get("total") for x in prompt_rows
                     if x["tool"] == BASELINE_TOOL and x["model"] == r["model"]), None)
        pct, dcls = _delta_pct(r["cells"].get("total"), base)
        delta = "" if pct is None else f"{pct:+.0f}%"
        tds = [f"<td><b>{_esc(r['tool_id'])}</b></td><td>{_esc(r['model'])}</td>",
               f"<td class='num {dcls}'>{delta}</td>"]
        for key, _, _ in BASE_COLS:
            best, worst = _best_worst(prompt_rows, key)
            tds.append(_cell_html(r["cells"].get(key), key, best, worst))
        trows.append("<tr>" + "".join(tds) + "</tr>")
    h.append("<table class='grp'><thead>" + thead + "</thead><tbody>"
             + "".join(trows) + "</tbody></table>")

    # per-tool hideable iterations (req 8)
    for r in prompt_rows:
        iters = load_iterations(r["model_root"], r["tool_id"], r["pprompt"])
        if not iters:
            continue
        inner = []
        for idx, it in enumerate(iters):
            title, inner2 = _iteration_html(idx, it)
            inner.append(f"<details class='iter'><summary>{title}</summary>{inner2}</details>")
        h.append(f"<details class='tool-iters'><summary>{_esc(r['tool_id'])} "
                 f"· {len(iters)} iterations</summary>{''.join(inner)}</details>")
    return "".join(h)


def _iteration_html(idx, it):
    if it["final"] is not None:
        title = "final answer"
        inner = "<div class='answer'><pre>" + _esc(it["final"].get("content")) + "</pre></div>"
        return title, inner
    calls = it["calls"]
    results = it["results"]
    title = f"iteration {idx} — {len(calls)} call(s), {len(results)} result(s)"
    inner = []
    for tc in calls:
        inner.append("<div class='call'><code>" + _esc(tc.get("name")) + "</code>"
                     "<pre>" + _esc(_args(tc.get("arguments"))) + "</pre></div>")
    for tr in results:
        content = tr.get("content") or ""
        preview = content if len(content) <= 6000 else content[:6000] + "…"
        inner.append("<details class='res'><summary>result · "
                     + _esc(tr.get("tool_name")) + " · " + str(len(content))
                     + " chars</summary><pre>" + _esc(preview) + "</pre></details>")
    return title, "".join(inner)


def _args(a):
    if a is None:
        return ""
    if isinstance(a, str):
        return a
    return json.dumps(a, ensure_ascii=False)


def _page(title, body):
    return ("<!doctype html><html><head><meta charset='utf-8'><title>"
            + _esc(title) + "</title><style>"
            "body{font-family:system-ui,sans-serif;margin:2em;max-width:1200px}"
            "table{border-collapse:collapse;width:100%;margin:0.5em 0}"
            "td,th{border:1px solid #ddd;padding:6px 8px;text-align:left;font-size:14px}"
            "th{background:#f5f5f5}td.num{text-align:right;font-variant-numeric:tabular-nums}"
            ".best{background:#d6f5d6}.worst{background:#ffd9d9}"
            ".delta-good{color:#1a7f37;font-weight:600}.delta-bad{color:#d1242f;font-weight:600}"
            ".delta-0{color:#666}.mrk{color:#888;font-size:11px}"
            "details{border:1px solid #eee;border-radius:6px;padding:6px;margin:6px 0}"
            "summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;background:#fafafa;"
            "border-radius:4px;padding:8px;font-size:12px;max-height:340px;overflow:auto}"
            "h1{font-size:22px}h2{font-size:18px;margin-top:1.4em}"
            "ul.tools{padding-left:1.4em}.tools li{margin:0.35em 0}"
            ".finding{background:#f7f9fb;border-left:4px solid #6b8afd;"
            "padding:8px 12px;line-height:1.45}"
            "</style></head><body>" + body + "</body></html>")


def _load_instructions(config=None):
    """Read tool_instruction from benchmark.yaml so the prompt prefix can be
    stripped exactly (falls back to regex stripping if config is unavailable)."""
    config = config or os.environ.get("PI_CONFIG", "benchmark.yaml")
    try:
        import yaml
        cfg = yaml.safe_load(open(config, encoding="utf-8"))
        return (cfg or {}).get("tool_instruction") or {}
    except Exception:
        return {}


def render(artifacts, out_path, instructions=None):
    if instructions is None:
        instructions = _load_instructions()
    rows = list(load_cells(artifacts, instructions))
    if not rows:
        raise SystemExit(f"no benchmark runs found under {artifacts}")
    sections = [_overview_html(rows), _tool_intro_html(rows),
                _results_summary_html(rows)]
    prompts = sorted({r["prompt"] for r in rows})
    for prompt in prompts:
        sections.append(_group_html(prompt, [r for r in rows if r["prompt"] == prompt]))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(_page(f"agent-codebase-bench — aggregated report", "".join(sections)))
    return out_path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--artifacts", default="artifacts")
    ap.add_argument("--out", default="artifacts/report.html")
    ap.add_argument("--config", default=None,
                    help="benchmark.yaml (for tool_instruction prompt stripping)")
    args = ap.parse_args()
    out = render(args.artifacts, args.out, _load_instructions(args.config))
    print(f"Wrote {out} from {len(discover_model_roots(args.artifacts))} model run(s) "
          f"({os.path.basename(os.path.dirname(out))})")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Render the combined HTML report under artifacts/, aggregating ALL model runs.

Reads every artifacts/<model>-<provider>/<tool>@<version>/summary.json +
folded transcripts and draws one self-contained report.html at the artifacts
root (e.g. artifacts/report.html). New models can be added and the report
re-rendered; it spans whatever models are present.

Layout (each maps to an explicit requirement):
  1. output lives under artifacts/ (aggregates across models)
  2. overview table (tool@version | total tokens | api calls | elapsed | Δ vs grep)
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
import sys

import pi_summary

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
                # Skip cells whose every run was a detected failure. aggregate
                # already removes failed runs+folders, but a standalone
                # pi_report run on stale summary.json must not resurrect them.
                rl = p.get("run_log") or []
                if rl and all(not r.get("transcript") for r in rl):
                    continue
                # Keep cache/reasoning available for the narrative summary even
                # though the compact comparison table does not display them.
                cells = {k: _mean(p.get(k)) for k, _, _ in BASE_COLS}
                cells.update({k: _mean(p.get(k)) for k in ("cache", "reas")})
                # Run count each displayed mean rests on (mirrors summary.json's
                # per-metric n) so tables can show "mean (n=N)" like report.md.
                cell_n = {}
                cell_sd = {}
                for k, _, _ in BASE_COLS:
                    stats = p.get(k)
                    if isinstance(stats, dict):
                        cell_n[k] = stats.get("n")
                        cell_sd[k] = stats.get("stdev")
                yield {
                    "model": model,
                    "model_root": mr,
                    "tool_id": tid,
                    "tool": p.get("tool") or tool,
                    "version": s.get("version"),
                    "prompt": p.get("prompt"),
                    "prompt_text": _prompt_text(mr, tid, p, instructions),
                    "cells": cells,
                    "cell_n": cell_n,
                    "cell_sd": cell_sd,
                    "summary": s,
                    "pprompt": p,
                }


def _artifact_file(model_root, tid, filename):
    """Resolve an artifact filename without allowing absolute/parent escapes."""
    base = os.path.realpath(os.path.join(model_root, tid))
    candidate = os.path.realpath(os.path.join(base, str(filename)))
    try:
        if os.path.commonpath((base, candidate)) != base:
            return None
    except ValueError:
        return None
    return candidate


def _prompt_text(model_root, tid, p, instructions=None):
    """Best-effort prompt text, with the prepended tool-instruction removed so
    the report shows the generic benchmark prompt (see docs/pi-migration.md)."""
    rl = p.get("run_log") or []
    tpath = ""
    if rl and rl[0].get("transcript"):
        tpath = _artifact_file(model_root, tid, rl[0]["transcript"]) or ""
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
    tpath = _artifact_file(model_root, tid, rl[0]["transcript"])
    if tpath is None:
        return None
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


def _results_summary_html(findings, unavailable=None):
    """Render validated LLM findings; analyst text is always escaped."""
    sections = [
        ("<details class='result-summary'><summary>Result summary</summary>"
         "<h1>Result summary</h1>"),
        ("<p>These observations were generated by an LLM from the recorded "
         "per-iteration tool calls and results. Tables remain the numeric source of truth.</p>"),
    ]
    if unavailable:
        sections.append(
            "<p class='finding unavailable'><b>LLM analysis unavailable.</b> "
            "See report-generation stderr for diagnostics.</p>"
        )
    for finding in findings or []:
        body = [
            (f"<b>{_esc(finding['testcase'])}</b>: <b>{_esc(finding['winner'])}</b> "
             f"was the most economical observed workflow. {_esc(finding['why_winner'])}")
        ]
        for cost in finding.get("workflow_costs") or []:
            body.append(f"<br><b>{_esc(cost['workflow'])}</b>: {_esc(cost['explanation'])}")
        sections.append("<p class='finding'>" + " ".join(body) + "</p>")
    sections.append("</details>")
    return "".join(sections)


def _cell_html(value, key, best, worst, n=None, sd=None):
    if value is None:
        return "<td class='num'>—</td>"
    style = ""
    # For cost-like metrics, lower is better; for none here, best = min.
    if value == best:
        style = "best"
    elif value == worst and worst != best:
        style = "worst"
    fmt = fmt_num(value)
    if n is not None:
        mark = f"n={n}"
        if sd is not None:
            mark += f", sd={fmt_num(sd)}"
        fmt += f" <span class='mrk'>({mark})</span>"
    return f"<td class='num {style}'>{fmt}</td>"


def _overview_html(rows):
    """Overview table (req 2): tool@version | avg tokens | api calls | Δ vs grep.

    Also shows the mean elapsed (wall_s) per tool so runtime cost is visible
    next to token cost. Tokens/api/elapsed are each averaged across testcases
    (mean of per-testcase means) rather than summed, since different
    testcases/tools don't share the same n runs and a raw sum would let a
    heavily-retried cell dominate. Each metric cell shows (n=N) = how many
    validated runs it rests on (summed across testcases, for context).
    """
    by_tool = {}
    for r in rows:
        b = by_tool.setdefault(r["tool_id"], {
            "total": [], "api": [], "wall": [], "n_runs": 0,
            "model": set(), "tool": r.get("tool"),
        })
        t = r["cells"].get("total")
        if t is not None:
            b["total"].append(float(t))
        a = r["cells"].get("api")
        if a is not None:
            b["api"].append(float(a))
        w = r["cells"].get("wall_s")
        if w is not None:
            b["wall"].append(float(w))
        # Run count behind this cell (mirrors per-metric n in summary.json).
        # All BASE_COLS metrics in a cell share the same run set.
        cell_n = r.get("cell_n") or {}
        b["n_runs"] += cell_n.get("total") or cell_n.get("wall_s") or 0
        b["model"].add(r["model"])

    def _avg(vals):
        return (sum(vals) / len(vals)) if vals else None

    means = {tid: {
        "total": _avg(v["total"]), "api": _avg(v["api"]), "wall": _avg(v["wall"]),
        "model": v["model"], "tool": v["tool"], "n_runs": v["n_runs"],
    } for tid, v in by_tool.items()}
    grep_total = means.get(BASELINE_TOOL, {}).get("total") or next(
        (v["total"] for k, v in means.items() if k.startswith(BASELINE_TOOL + "@")), None)
    items = sorted(means.items(), key=lambda kv: (kv[1]["total"] is None, kv[1]["total"]))
    total_vals = [v["total"] for v in means.values() if v["total"] is not None]
    api_vals = [v["api"] for v in means.values() if v["api"] is not None]
    wall_vals = [v["wall"] for v in means.values() if v["wall"] is not None]
    total_max = max(total_vals, default=0)
    api_max = max(api_vals, default=0)
    wall_max = max(wall_vals, default=0)
    best_t = min(total_vals, default=0)
    best_a = min(api_vals, default=0)
    best_w = min(wall_vals, default=0)
    thead = ("<tr><th>tool@version</th><th>avg tokens</th><th>avg api calls</th>"
             "<th>elapsed (s)</th><th>Δ vs grep</th></tr>")
    body = []
    for tid, v in items:
        tcl = "best" if v["total"] == best_t else ("worst" if v["total"] == total_max and total_max != best_t else "")
        acl = "best" if v["api"] == best_a else ("worst" if v["api"] == api_max and api_max != best_a else "")
        wcl = "best" if v["wall"] is not None and v["wall"] == best_w else ("worst" if v["wall"] is not None and v["wall"] == wall_max and wall_max != best_w else "")
        pct, dcls = _delta_pct(v["total"], grep_total)
        delta = "+0%" if tid == BASELINE_TOOL or v.get("tool") == BASELINE_TOOL else ("" if pct is None else f"{pct:+.0f}%")
        nm = " <span class='mrk'>" + ", ".join(sorted(v["model"])) + "</span>" if len(v["model"]) > 1 else ""
        n_mark = f" <span class='mrk'>(n={v['n_runs']})</span>" if v["n_runs"] else ""
        body.append(
            f"<tr><td><b>{_esc(tid)}</b>{nm}</td>"
            f"<td class='num {tcl}'>{fmt_num(v['total'])}{n_mark}</td>"
            f"<td class='num {acl}'>{fmt_num(v['api'])}{n_mark}</td>"
            f"<td class='num {wcl}'>{fmt_num(v['wall'])}{n_mark}</td>"
            f"<td class='num {dcls}'>{delta}</td></tr>"
        )
    return ("<h1>Overview</h1>"
            "<p>Aggregated across all testcases and models, cheapest -> costliest "
            "(best green, worst red). Tokens/api calls/elapsed are each the mean "
            "of per-testcase means (not a sum, since testcases/tools don't share "
            "the same run counts). Δ vs grep on avg tokens, and (n=N) shows the "
            "total number of validated runs behind each row.</p>"
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
            tds.append(_cell_html(r["cells"].get(key), key, best, worst,
                                  (r.get("cell_n") or {}).get(key),
                                  (r.get("cell_sd") or {}).get(key)))
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


def render(
    artifacts,
    out_path,
    instructions=None,
    *,
    analyst_model=None,
    force_summary=False,
    analyst_runner=None,
):
    if instructions is None:
        instructions = _load_instructions()
    rows = list(load_cells(artifacts, instructions))
    if not rows:
        raise SystemExit(f"no benchmark runs found under {artifacts}")
    cache_path = os.path.join(artifacts, "result-summary.json")
    findings = []
    unavailable = None
    try:
        evidence = pi_summary.normalize_evidence(rows)
        analysis = pi_summary.ensure_summary(
            evidence,
            cache_path,
            analyst_model=analyst_model,
            force=force_summary,
            runner=analyst_runner,
        )
        findings = analysis["findings"]
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        unavailable = pi_summary.redact_secrets(str(exc))
        print(f"LLM result-summary analysis unavailable: {unavailable}", file=sys.stderr)
    sections = [_overview_html(rows), _tool_intro_html(rows),
                _results_summary_html(findings, unavailable)]
    prompts = sorted({r["prompt"] for r in rows})
    for prompt in prompts:
        sections.append(_group_html(prompt, [r for r in rows if r["prompt"] == prompt]))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(_page("agent-codebase-bench — aggregated report", "".join(sections)))
    return out_path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--artifacts", default="artifacts")
    ap.add_argument("--out", default="artifacts/report.html")
    ap.add_argument("--config", default=None,
                    help="benchmark.yaml (for tool_instruction prompt stripping)")
    ap.add_argument("--force-summary", action="store_true",
                    help="ignore a matching result-summary cache and rerun the analyst")
    args = ap.parse_args()
    out = render(
        args.artifacts,
        args.out,
        _load_instructions(args.config),
        force_summary=args.force_summary,
    )
    print(f"Wrote {out} from {len(discover_model_roots(args.artifacts))} model run(s) "
          f"({os.path.basename(os.path.dirname(out))})")


if __name__ == "__main__":
    main()

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
                cells = {k: _mean(p.get(k)) for k, _, _ in BASE_COLS}
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
    sections = [_overview_html(rows)]
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

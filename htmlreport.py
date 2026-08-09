"""Self-contained HTML comparison report for agent-codebase-bench.

Renders the verbose per-iteration transcripts (from results/) into a single
standalone .html file: a compact comparison table across tools/frameworks, then
one foldable (<details>) section per tool with the prompt and every iteration
collapsed by default. Each iteration shows the assistant's tool-call inputs and
the tool outputs that came back.

The file is fully self-contained (CSS/JS inline, no network), so it can be
opened from any browser or shared directly.
"""

from __future__ import annotations

import html
import json
import os


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _esc(value):
    """Escape a value for safe embedding in HTML."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _arg_pretty(args):
    """Format a tool-call arguments dict as readable text."""
    if isinstance(args, dict):
        try:
            return json.dumps(args, indent=2, ensure_ascii=False)
        except Exception:
            return str(args)
    return str(args)


def _content_preview(content, limit=400):
    """Return (preview, full) for a tool result content string."""
    content = content or ""
    if len(content) <= limit:
        return content, None
    return content[:limit] + " …", content


# ---------------------------------------------------------------------------
# iteration grouping
# ---------------------------------------------------------------------------

def _iterations_from_trace(trace):
    """Group a trace's events into iterations.

    An iteration is one assistant turn: its tool-call batch (inputs) followed
    by the tool_result events (outputs) that came back before the next
    assistant event. The final answer (if any) is its own final iteration.
    Returns a list of dicts: {kind, assistant_events, tool_results,
    is_final, tool_calls}.
    """
    iters = []
    current = None
    for e in trace.get("events", []):
        etype = e.get("type")
        if etype == "assistant_tool_call":
            current = {"assistant": e, "tool_results": [], "final": None}
            iters.append(current)
        elif etype == "tool_result":
            if current is not None:
                current["tool_results"].append(e)
            else:  # orphan tool result before any assistant call
                iters.append({"assistant": None, "tool_results": [e], "final": None})
                current = iters[-1] if iters else None
        elif etype == "final_answer":
            # final answer closes the current iteration if one is open, else
            # stands alone
            if current is not None and current.get("final") is None:
                current["final"] = e
            else:
                iters.append({"assistant": None, "tool_results": [], "final": e})
                current = None
        # 'prompt' / 'assistant' events are not displayed as iterations
    return [it for it in iters if it["assistant"] or it["tool_results"] or it["final"]]


# ---------------------------------------------------------------------------
# per-iteration + per-tool HTML
# ---------------------------------------------------------------------------

def _tool_call_input_html(tc):
    args = tc.get("arguments")
    pretty = _arg_pretty(args)
    return (
        "<div class='call'>"
        f"<code class='toolname'>{_esc(tc.get('name'))}</code>"
        "<pre class='args'>" + _esc(pretty) + "</pre>"
        "</div>"
    )


def _tool_result_html(tr):
    content = tr.get("content") or ""
    preview, full = _content_preview(content)
    tool_id = tr.get("tool_call_id") or ""
    name = tr.get("tool_name") or ""
    head = f"<span class='meta'>tool={_esc(name)} call={_esc(tool_id[:12])}</span>" \
        if (name or tool_id) else ""
    if full:
        return (
            "<div class='result'><details><summary>result "
            + (_esc(name) + " " if name else "")
            + f"({len(content)} chars) — {_esc(tool_id[:12])}</summary>"
            + head
            + "<pre>" + _esc(preview) + "</pre>"
            "<details class='full'><summary>full output</summary><pre>"
            + _esc(full) + "</pre></details>"
            "</details></div>"
        )
    return (
        "<div class='result'><details><summary>result "
        + (_esc(name) + " " if name else "")
        + f"({len(content)} chars){(' — ' + _esc(tool_id[:12])) if tool_id else ''}</summary>"
        + head
        + "<pre>" + _esc(content) + "</pre></details></div>"
    )


def _iteration_html(idx, it):
    inner = []
    if it["assistant"]:
        tcs = it["assistant"].get("tool_calls") or []
        inner.append(
            "<div class='iter-head'><b>Input</b> — assistant issued "
            f"{len(tcs)} tool call(s)</div>"
        )
        for tc in tcs:
            inner.append(_tool_call_input_html(tc))
    if it["tool_results"]:
        inner.append("<div class='iter-head'><b>Output</b></div>")
        for tr in it["tool_results"]:
            inner.append(_tool_result_html(tr))
    if it["final"]:
        fa = it["final"].get("content") or ""
        inner.append("<div class='answer'>" + _esc(fa) + "</div>")

    if not inner:
        return ""
    title = "final answer" if it["final"] else f"iteration {idx}"
    n = len(it["assistant"]["tool_calls"]) if it["assistant"] else 0
    nout = len(it["tool_results"])
    summary = f"{title}"
    if it["assistant"]:
        summary += f" — {n} call(s)"
    if nout:
        summary += f", {nout} result(s)"
    return (
        "<details class='iter'>"
        f"<summary>{_esc(summary)}</summary>"
        + "".join(inner)
        + "</details>"
    )


def _tool_section_html(anchor, tool, cprompt, iters, stats):
    body = []
    body.append(f"<h2 id='{_esc(anchor)}'>{_esc(tool)}</h2>")
    body.append("<div class='stats'>" + _esc(stats) + "</div>")
    if cprompt is not None:
        body.append(
            "<details class='prompt'><summary>prompt</summary><pre>"
            + _esc(cprompt) + "</pre></details>"
        )
    body.append("<div class='iters'>")
    if iters:
        for i, it in enumerate(iters, 1):
            body.append(_iteration_html(i, it))
    else:
        body.append("<div class='empty'>no iterations captured</div>")
    body.append("</div>")
    return "".join(body)


# ---------------------------------------------------------------------------
# top-level report
# ---------------------------------------------------------------------------

_PAGE_CSS = """
:root {
  --line:#e2e5e9; --fold:#f6f8fa; --code:#0b1020; --muted:#66707a;
  --accent:#0366d6; --accent-soft:#eaf2fd; --good:#1a7f37; --good-soft:#e8f5e9;
  --shadow: 0 1px 2px rgba(16,24,40,.04), 0 1px 3px rgba(16,24,40,.06);
  --radius: 8px;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { font-family: -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
       margin:0; color:#1c1e21; background:#fafbfc; line-height:1.5; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 0 20px 80px; }
.topbar { position:sticky; top:0; z-index:20; background:rgba(255,255,255,.92);
          backdrop-filter:saturate(180%) blur(6px); border-bottom:1px solid var(--line);
          margin: 0 -20px 20px; padding: 14px 20px; }
h1 { font-size: 21px; margin:0 0 4px; letter-spacing:-.01em; }
h2 { font-size: 17px; margin-top: 40px; padding-bottom: 8px; border-bottom:1px solid var(--line);
     scroll-margin-top: 90px; }
h2::before { content:"#"; color:var(--accent); margin-right:6px; opacity:.55; font-weight:700; }
.meta { color: var(--muted); font-size: 12.5px; }
.stats { color: var(--muted); font-size: 12.5px; margin: 8px 0 12px; background:var(--fold);
         border:1px solid var(--line); border-radius:6px; padding:6px 10px; display:inline-block; }
.toolbar { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.toolbar button { font: inherit; font-size:13px; font-weight:600; cursor:pointer;
  background:#fff; color:var(--accent); border:1px solid var(--accent); border-radius:6px;
  padding:6px 12px; transition: background .12s, color .12s; }
.toolbar button:hover { background:var(--accent); color:#fff; }
.nav-scroll { max-width:100%; overflow-x:auto; white-space:nowrap; padding:4px 0 2px; }
.nav-scroll a.fw { display:inline-block; margin:0 10px 4px 0; padding:3px 9px;
  background:var(--accent-soft); border-radius:999px; font-size:12.5px; }
table.cmp { border-collapse: collapse; width:100%; font-size:13px; margin: 14px 0 6px;
            box-shadow: var(--shadow); border-radius:var(--radius); overflow:hidden; }
table.cmp th, table.cmp td { border-bottom:1px solid var(--line); padding:8px 10px; text-align:left; }
table.cmp th { background:#f2f4f7;
  text-transform:uppercase; font-size:11px; letter-spacing:.04em; color:var(--muted); }
table.cmp td.num { text-align:right; font-variant-numeric: tabular-nums; font-feature-settings:"tnum"; }
table.cmp tbody tr:nth-child(odd) { background:#fbfcfe; }
table.cmp tbody tr:hover { background:var(--accent-soft); }
tr.tool-row td:first-child { font-weight:600; }
td.best { background:var(--good-soft); color:var(--good); font-weight:700; }
td.worst { background:#fdecec; color:#b42318; }
.tool-badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11.5px;
  font-weight:700; color:#fff; background:var(--tc); letter-spacing:.01em; }
section.dashboard { margin: 10px 0 30px; }
.dash-grid { display:grid; grid-template-columns: 1fr 1fr; gap:20px; }
.dash-col h3 { font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted);
  margin:0 0 8px; }
table.cmp.agg { font-size:12.5px; }
@media (max-width: 900px) { .dash-grid { grid-template-columns: 1fr; } }
details { margin: 6px 0; }
details.prompt summary, details.iter summary, details.result summary {
  cursor:pointer; font-size:13px; user-select:none; }
details.prompt summary { background:var(--fold); padding:8px 12px; border:1px solid var(--line);
  border-radius:6px; font-weight:600; }
details.iter { border:1px solid var(--line); border-radius:var(--radius); box-shadow:var(--shadow);
  background:#fff; }
details.iter > summary { padding:8px 12px; background:var(--fold); border-radius:var(--radius);
  font-weight:600; }
details.iter[open] > summary { border-radius: var(--radius) var(--radius) 0 0;
  border-bottom:1px solid var(--line); }
details.iter > *:not(summary) { margin:10px 12px; }
details.result { margin:4px 0 4px 6px; }
details.result > summary { color:var(--accent); font-weight:500; }
details.full > summary { color:var(--muted); }
.iter-head { font-size:11px; color:var(--muted); margin:8px 0 3px; text-transform:uppercase;
  letter-spacing:.05em; font-weight:700; }
.call { margin:2px 0 8px; padding:8px 10px; background:#fafbfc; border-left:3px solid #e6b800;
  border-radius:0 6px 6px 0; }
.toolname { background:var(--accent-soft); color:var(--accent); border-radius:4px;
  padding:2px 7px; font-weight:700; font-size:12px; }
pre { background:#0b1020; color:#e6edf3; padding:10px 12px; border-radius:6px;
      overflow:auto; font-size:12px; white-space:pre-wrap; word-break:break-word;
      max-height: 480px; }
pre.args { background:#f6f8fa; color:#1c1e21; border:1px solid var(--line); max-height:320px; }
.answer { background:var(--good-soft); border-left:4px solid var(--good); padding:12px 14px;
          border-radius:6px; white-space:pre-wrap; font-size:13px; }
.empty { color:var(--muted); font-style:italic; padding:10px; }
a.fw { text-decoration:none; color:var(--accent); }
a.fw:hover { text-decoration:underline; }
::-webkit-scrollbar { height:8px; width:8px; }
::-webkit-scrollbar-thumb { background:#c7ccd1; border-radius:4px; }
@media (max-width: 700px) {
  table.cmp { font-size:12px; }
  .wrap { padding: 0 12px 60px; }
}
"""

_PAGE_JS = """
function foldAll(root){ root.querySelectorAll('details.iter').forEach(d=>d.removeAttribute('open')); }
function expandAll(root){ root.querySelectorAll('details.iter').forEach(d=>d.setAttribute('open','')); }
function toggleFolded(btn){
  const wrap=document.querySelector('.wrap');
  if(btn.dataset.state==='expand'){ expandAll(wrap); btn.textContent='Fold all iterations'; btn.dataset.state='fold'; }
  else { foldAll(wrap); btn.textContent='Expand all iterations'; btn.dataset.state='expand'; }
}
function filterSections(q){
  q = q.trim().toLowerCase();
  document.querySelectorAll('.wrap > h2').forEach(h => {
    let sec = h;
    let show = !q || h.textContent.toLowerCase().includes(q);
    // reveal/hide this heading and everything until the next h2
    let node = h.nextElementSibling;
    h.style.display = show ? '' : 'none';
    while (node && node.tagName !== 'H2') {
      node.style.display = show ? '' : 'none';
      node = node.nextElementSibling;
    }
  });
}
"""


def _tool_anchor(prompt_id, tool, usage):
    """Unique anchor for a tool section, disambiguating across models."""
    model = (usage.get("model") or "unknown").replace("/", "-")
    return f"{prompt_id}-{tool}-{model}"


_TOOL_PALETTE = [
    "#0366d6", "#a23bec", "#e6620d", "#1a7f37", "#c9184a", "#0e7490", "#7c3aed",
]


def _tool_color(tool):
    """Deterministic color for a tool name, so the same tool reads the same
    color everywhere in the report."""
    idx = sum(ord(c) for c in (tool or "?")) % len(_TOOL_PALETTE)
    return _TOOL_PALETTE[idx]


def _tool_badge(tool):
    color = _tool_color(tool)
    return (
        f"<span class='tool-badge' style='--tc:{color}'>{_esc(tool)}</span>"
    )


def _fmt_num(v, digits=None):
    if v is None:
        return "—"
    if digits is not None and isinstance(v, (int, float)):
        return f"{v:,.{digits}f}"
    if isinstance(v, (int, float)):
        return f"{v:,}"
    return _esc(v)


# numeric columns rendered in the per-testcase comparison table, in order.
# (label, extractor(usage,summary,row)->number|None, lower_is_better, digits)
_CMP_COLUMNS = [
    ("total_tok", lambda u, s, r: u.get("total_tokens"), True, None),
    ("in", lambda u, s, r: u.get("input_tokens"), True, None),
    ("out", lambda u, s, r: u.get("output_tokens"), True, None),
    ("cache", lambda u, s, r: u.get("cache_read_tokens"), True, None),
    ("api", lambda u, s, r: u.get("api_calls"), True, None),
    ("iters", lambda u, s, r: s.get("iterations"), True, None),
    ("tool_calls", lambda u, s, r: s.get("tool_calls"), True, None),
    ("wall_s", lambda u, s, r: r.get("wall_seconds"), True, 1),
]


_BASELINE_TOOL = "grep"


def _baseline_totals(prompt_rows):
    """Map model -> baseline (grep) total_tokens for this testcase, so the
    delta column compares apples-to-apples when a report spans models.
    Falls back to the first grep row found for a row whose own model has no
    grep baseline (e.g. only one model ran grep in this report)."""
    per_model = {}
    fallback = None
    for sr in prompt_rows:
        if sr.get("tool") != _BASELINE_TOOL:
            continue
        u = sr.get("usage") or {}
        tok = u.get("total_tokens")
        if not isinstance(tok, (int, float)):
            continue
        model = u.get("model") or "?"
        per_model.setdefault(model, tok)
        if fallback is None:
            fallback = tok
    return per_model, fallback


def _delta_cell(tokens, baseline):
    """Render a +N% / -N% badge for tokens vs. the grep baseline."""
    if not isinstance(tokens, (int, float)) or not isinstance(baseline, (int, float)) or baseline == 0:
        return "<td class='num'>—</td>"
    pct = (tokens - baseline) / baseline * 100.0
    sign = "+" if pct >= 0 else ""
    cls = "num"
    if abs(pct) >= 0.5:
        cls += " worst" if pct > 0 else " best"
    return f"<td class='{cls}'>{sign}{pct:.0f}%</td>"


def _comparison_table_html(prompt_id, prompt_rows):
    """One comparison table for a single testcase (one prompt across tools).

    Numeric columns highlight the best (lowest-cost) value in green and the
    worst (highest-cost) value in a muted red so differences are visible at a
    glance instead of requiring the reader to scan raw numbers. A `vs grep`
    column shows each tool's total-token usage as a +/-% delta against the
    `grep` baseline row (matched by model when the report spans models).
    """
    # Precompute per-column min/max across the rows that ran this testcase.
    col_values = []
    for label, getter, lower_better, digits in _CMP_COLUMNS:
        vals = []
        for sr in prompt_rows:
            u = sr.get("usage") or {}
            s = sr.get("transcript_summary") or {}
            v = getter(u, s, sr)
            vals.append(v if isinstance(v, (int, float)) else None)
        present = [v for v in vals if v is not None]
        col_values.append({
            "vals": vals,
            "best": (min(present) if lower_better else max(present)) if present else None,
            "worst": (max(present) if lower_better else min(present)) if present else None,
        })

    baseline_by_model, baseline_fallback = _baseline_totals(prompt_rows)
    has_baseline = bool(baseline_by_model) or baseline_fallback is not None

    rows = []
    header_cells = "".join(
        f"<th class='num'>{_esc(label)}</th>" for label, *_ in _CMP_COLUMNS
    )
    delta_th = "<th class='num' title='total_tokens vs grep baseline'>vs grep</th>" if has_baseline else ""
    rows.append(
        "<table class='cmp'><thead><tr>"
        "<th>tool</th><th>model</th>" + header_cells + delta_th +
        "</tr></thead><tbody>"
    )
    for i, sr in enumerate(prompt_rows):
        u = sr.get("usage") or {}
        model = u.get("model")
        provider = u.get("provider")
        model_cell = _esc(model) if model else "—"
        if model and provider:
            model_cell += f"<div class='meta'>{_esc(provider)}</div>"
        cells = []
        for ci, (label, getter, lower_better, digits) in enumerate(_CMP_COLUMNS):
            v = col_values[ci]["vals"][i]
            cls = "num"
            if v is not None and col_values[ci]["best"] is not None and len(prompt_rows) > 1:
                if v == col_values[ci]["best"] and col_values[ci]["best"] != col_values[ci]["worst"]:
                    cls += " best"
                elif v == col_values[ci]["worst"] and col_values[ci]["best"] != col_values[ci]["worst"]:
                    cls += " worst"
            cells.append(f"<td class='{cls}'>{_fmt_num(v, digits)}</td>")
        delta_cell = ""
        if has_baseline:
            total_tok = col_values[0]["vals"][i]  # "total_tok" is _CMP_COLUMNS[0]
            baseline = baseline_by_model.get(model or "?", baseline_fallback)
            if sr.get("tool") == _BASELINE_TOOL:
                delta_cell = "<td class='num meta'>baseline</td>"
            else:
                delta_cell = _delta_cell(total_tok, baseline)
        rows.append(
            "<tr class='tool-row'>"
            f"<td>{_tool_badge(sr['tool'])} <a class='fw' href='#{_esc(_tool_anchor(prompt_id, sr['tool'], u))}'>view →</a></td>"
            f"<td>{model_cell}</td>"
            + "".join(cells)
            + delta_cell
            + "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _overview_dashboard_html(grouped):
    """Top-of-report dashboard: aggregate totals by tool and by model across
    every testcase, so the reader sees the headline comparison before diving
    into any single testcase's table."""
    by_tool = {}
    by_model = {}
    testcases_per_tool = {}
    any_known_cost = False
    for prompt, prs in grouped.items():
        for sr in prs:
            u = sr.get("usage") or {}
            s = sr.get("transcript_summary") or {}
            tool = sr.get("tool") or "?"
            model = u.get("model") or "?"
            if u.get("cost_status") == "known":
                any_known_cost = True
            for key, bucket in ((tool, by_tool), (model, by_model)):
                agg = bucket.setdefault(key, {
                    "n": 0, "total_tokens": 0, "cost": 0.0, "cost_n": 0, "wall": 0.0,
                    "api_calls": 0, "tool_calls": 0,
                })
                agg["n"] += 1
                agg["total_tokens"] += u.get("total_tokens") or 0
                if u.get("cost_status") == "known":
                    agg["cost"] += u.get("estimated_cost_usd") or 0.0
                    agg["cost_n"] += 1
                agg["wall"] += sr.get("wall_seconds") or 0.0
                agg["api_calls"] += u.get("api_calls") or 0
                agg["tool_calls"] += s.get("tool_calls") or 0
            testcases_per_tool.setdefault(tool, set()).add(prompt)

    def _agg_table(bucket, name_col, badge=False):
        if not bucket:
            return ""
        cost_th = "<th class='num'>avg_cost_usd</th>" if any_known_cost else ""
        rows = [
            f"<table class='cmp agg'><thead><tr><th>{name_col}</th>"
            "<th class='num'>runs</th><th class='num'>avg_tokens</th>"
            "<th class='num'>total_tokens</th>" + cost_th +
            "<th class='num'>avg_wall_s</th><th class='num'>avg_api_calls</th>"
            "<th class='num'>avg_tool_calls</th></tr></thead><tbody>"
        ]
        best_avg_tok = min(
            (a["total_tokens"] / a["n"] for a in bucket.values() if a["n"]),
            default=None,
        )
        for key in sorted(bucket, key=lambda k: bucket[k]["total_tokens"] / max(bucket[k]["n"], 1)):
            a = bucket[key]
            n = max(a["n"], 1)
            avg_tok = a["total_tokens"] / n
            cls = "num best" if best_avg_tok is not None and avg_tok == best_avg_tok and len(bucket) > 1 else "num"
            label = _tool_badge(key) if badge else _esc(key)
            cost_td = ""
            if any_known_cost:
                cost_td = (
                    f"<td class='num'>{_fmt_num(a['cost']/a['cost_n'], 4)}</td>"
                    if a["cost_n"] else "<td class='num'>—</td>"
                )
            rows.append(
                "<tr>"
                f"<td>{label}</td>"
                f"<td class='num'>{a['n']}</td>"
                f"<td class='{cls}'>{_fmt_num(avg_tok, 0)}</td>"
                f"<td class='num'>{_fmt_num(a['total_tokens'])}</td>"
                + cost_td +
                f"<td class='num'>{_fmt_num(a['wall']/n, 1)}</td>"
                f"<td class='num'>{_fmt_num(a['api_calls']/n, 1)}</td>"
                f"<td class='num'>{_fmt_num(a['tool_calls']/n, 1)}</td>"
                "</tr>"
            )
        rows.append("</tbody></table>")
        return "".join(rows)

    return (
        "<section class='dashboard'>"
        "<h2 id='overview'>Overview</h2>"
        "<p class='meta'>Aggregated across all testcases. Lowest avg_tokens per group is "
        "highlighted — that's the cheapest approach on average. Click a testcase below to "
        "see the per-run breakdown."
        + ("" if any_known_cost else " Cost is omitted: no provider in this run reports a known cost.")
        + "</p>"
        "<div class='dash-grid'>"
        "<div class='dash-col'><h3>By tool</h3>" + _agg_table(by_tool, "tool", badge=True) + "</div>"
        "<div class='dash-col'><h3>By model</h3>" + _agg_table(by_model, "model") + "</div>"
        "</div>"
        "</section>"
    )


def _testcase_section_html(prompt_id, prompt_text, prompt_rows):
    """One <section> per testcase (prompt): heading, prompt text, the
    comparison table across the tools that ran it, then each tool's folded
    transcript."""
    body = []
    body.append(f"<h2 id='{_esc(prompt_id)}'>{_esc(prompt_id)}</h2>")
    if prompt_text:
        body.append(
            "<details class='prompt'><summary>prompt</summary><pre>"
            + _esc(prompt_text) + "</pre></details>"
        )
    body.append(_comparison_table_html(prompt_id, prompt_rows))
    for sr in prompt_rows:
        usage = sr.get("usage") or {}
        body.append(
            _tool_section_html(
                _tool_anchor(prompt_id, sr["tool"], usage), sr["tool"],
                sr.get("prompt_text"), sr["iters"], sr["stats"],
            )
        )
    return "".join(body)


def _load_rows(*results_dirs):
    """Load + merge rows from one or more run dirs (run.json + transcripts).

    Returns list of per-(tool,prompt) cell dicts with all fields needed for
    rendering. Each row is tagged with its owning model/provider (from usage).
    """
    rows = []
    for results_dir in results_dirs:
        rj = os.path.join(results_dir, "run.json")
        if not os.path.exists(rj):
            continue
        with open(rj, encoding="utf-8") as f:
            run_rows = json.load(f)
        for row in run_rows:
            prompt = row.get("prompt")
            if prompt is None:
                continue
            usage = row.get("usage") or {}
            s = row.get("transcript_summary") or {}
            tpath = row.get("transcript_json")
            trace = {}
            if tpath and os.path.exists(tpath):
                try:
                    with open(tpath, encoding="utf-8") as f:
                        trace = json.load(f)
                except Exception:
                    trace = {}
            cost_part = (
                f"cost=${_esc(usage.get('estimated_cost_usd'))} · "
                if usage.get("cost_status") == "known" else ""
            )
            stats = (
                f"model={_esc(usage.get('model')) or '—'} · "
                f"provider={_esc(usage.get('provider')) or '—'} · "
                f"total_tokens={_esc(usage.get('total_tokens'))} · "
                f"input={_esc(usage.get('input_tokens'))} · "
                f"output={_esc(usage.get('output_tokens'))} · "
                f"api_calls={_esc(usage.get('api_calls'))} · "
                + cost_part +
                f"iterations={_esc(s.get('iterations'))} · "
                f"tool_calls={_esc(s.get('tool_calls'))} · "
                f"wall_s={_esc(row.get('wall_seconds'))}"
            )
            rows.append({
                "tool": row.get("tool"),
                "prompt": prompt,
                "prompt_text": trace.get("prompt"),
                "usage": usage,
                "transcript_summary": s,
                "wall_seconds": row.get("wall_seconds"),
                "stats": stats,
                "iters": _iterations_from_trace(trace),
            })
    return rows


def render_html(results_dir, out_path, title=None, *more_dirs):
    """Generate comparison HTML from one or more run dirs.

    Accepts one or more run dirs (artifacts/<model>-<provider>/...). Rows are
    merged across every dir; results group by testcase (prompt), each getting a
    comparison table with rows per (tool, model) plus folded per-tool
    transcripts, and tool-section anchors are disambiguated by model so a
    combined multi-model report links correctly.

    Backwards compatible: a single dir renders like before.
    """
    dirs = (results_dir,) + more_dirs
    rows = _load_rows(*dirs)
    if not rows:
        raise FileNotFoundError(
            f"no run.json found in any of: {', '.join(dirs)}; run bench.py first"
        )

    models = sorted({(r["usage"].get("model") or "?") for r in rows})

    if title is None:
        if len(dirs) > 1 or len(models) > 1:
            title = "Tool comparison — " + " vs ".join(models)
        else:
            title = f"Tool comparison — {os.path.basename(os.path.abspath(results_dir))}"

    # Group results by testcase (prompt); preserve first-seen order.
    from collections import OrderedDict
    grouped = OrderedDict()
    for row in rows:
        grouped.setdefault(row["prompt"], []).append(row)

    # Minimal nav jumping to each testcase.
    nav = "<a class='fw' href='#overview'>Overview</a> " + " · ".join(
        f"<a class='fw' href='#{_esc(p)}'>{_esc(p)}</a>" for p in grouped
    )

    sections = []
    sections.append(_overview_dashboard_html(grouped))
    for prompt, prs in grouped.items():
        sections.append(_testcase_section_html(prompt, prs[0].get("prompt_text"), prs))

    models_txt = ", ".join(_esc(m) for m in models) or "—"
    html_doc = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_esc(title)}</title><style>{_PAGE_CSS}</style></head><body>"
        "<div class='topbar'><div class='wrap' style='padding:0;'>"
        f"<h1>{_esc(title)}</h1>"
        f"<p class='meta'>Self-contained report · models: {models_txt}. "
        "Results are grouped by testcase (prompt); each has its own "
        "comparison table and folded per-tool iterations — click a summary to "
        "open that step's inputs/outputs.</p>"
        "<div class='toolbar'>"
        "<button onclick='toggleFolded(this)' data-state='expand'>Expand all iterations</button>"
        "<input type='search' placeholder='Filter testcases…' "
        "oninput='filterSections(this.value)' "
        "style='flex:1; min-width:160px; padding:6px 10px; font:inherit; font-size:13px; "
        "border:1px solid var(--line); border-radius:6px;'>"
        "</div>"
        + (f"<div class='nav-scroll'>{nav}</div>" if nav else "")
        + "</div></div>"
        "<div class='wrap'>"
        + "".join(sections)
        + "</div><script>" + _PAGE_JS + "</script></body></html>"
    )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    return out_path

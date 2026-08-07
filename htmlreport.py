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
:root { --line:#e2e2e2; --fold:#f6f8fa; --code:#0b1020; --muted:#667; }
* { box-sizing: border-box; }
body { font-family: -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
       margin:0; color:#1c1e21; background:#fff; line-height:1.45; }
.wrap { max-width: 1100px; margin: 0 auto; padding: 24px 20px 80px; }
h1 { font-size: 22px; border-bottom: 2px solid var(--line); padding-bottom: 8px; }
h2 { font-size: 18px; margin-top: 34px; padding-bottom: 6px; border-bottom:1px solid var(--line);}
.meta { color: var(--muted); font-size: 13px; }
.stats { color: var(--muted); font-size: 13px; margin: 6px 0 10px; }
table.cmp { border-collapse: collapse; width:100%; font-size:13px; margin: 14px 0 6px; }
table.cmp th, table.cmp td { border:1px solid var(--line); padding:6px 8px; text-align:left;}
table.cmp th { background:#f2f2f2; }
table.cmp td.num { text-align:right; font-variant-numeric: tabular-nums;}
tr.tool-row td { font-weight:600; }
details { margin: 6px 0; }
details.prompt summary, details.iter summary, details.result summary {
  cursor:pointer; font-size:13px; }
details.prompt summary { background:var(--fold); padding:6px 10px; border:1px solid var(--line); border-radius:4px; }
details.iter { border:1px solid var(--line); border-radius:4px; }
details.iter > summary { padding:6px 10px; background:var(--fold); }
details.iter > *:not(summary) { margin:8px 10px; }
details.result { margin:4px 0 4px 6px; }
details.result > summary { color:#0366d6; }
details.full > summary { color:var(--muted); }
.iter-head { font-size:12px; color:var(--muted); margin:6px 0 2px; text-transform:uppercase; letter-spacing:.03em;}
.call { margin:2px 0 6px; padding:6px 8px; background:#fafbfc; border-left:3px solid #ce5; }
.toolname { background:#eef; border-radius:3px; padding:1px 5px; font-weight:600; }
pre { background:#0b1020; color:#e6edf3; padding:8px 10px; border-radius:4px;
      overflow:auto; font-size:12px; white-space:pre-wrap; word-break:break-word; }
pre.args { background:#f6f8fa; color:#1c1e21; border:1px solid var(--line); }
.answer { background:#e8f5e9; border-left:4px solid #4caf50; padding:10px 12px;
          border-radius:4px; white-space:pre-wrap; font-size:13px; }
.empty { color:var(--muted); font-style:italic; padding:10px; }
a.fw { text-decoration:none; color:#0366d6; }
"""

_PAGE_JS = """
function foldAll(root){ root.querySelectorAll('details.iter').forEach(d=>d.removeAttribute('open')); }
function expandAll(root){ root.querySelectorAll('details.iter').forEach(d=>d.setAttribute('open','')); }
function toggleFolded(btn){
  const wrap=document.querySelector('.wrap');
  if(btn.dataset.state==='expand'){ expandAll(wrap); btn.textContent='Fold all iterations'; btn.dataset.state='fold'; }
  else { foldAll(wrap); btn.textContent='Expand all iterations'; btn.dataset.state='expand'; }
}
"""


def _tool_anchor(prompt_id, tool, usage):
    """Unique anchor for a tool section, disambiguating across models."""
    model = (usage.get("model") or "unknown").replace("/", "-")
    return f"{prompt_id}-{tool}-{model}"


def _comparison_table_html(prompt_id, prompt_rows):
    """One comparison table for a single testcase (one prompt across tools)."""
    rows = []
    rows.append(
        "<table class='cmp'><thead><tr>"
        "<th>tool</th><th>model</th><th class='num'>total_tok</th>"
        "<th class='num'>in</th><th class='num'>out</th>"
        "<th class='num'>cache</th><th class='num'>api</th>"
        "<th class='num'>cost_usd</th><th class='num'>iters</th>"
        "<th class='num'>tool_calls</th><th class='num'>wall_s</th>"
        "</tr></thead><tbody>"
    )
    for sr in prompt_rows:
        u = sr.get("usage") or {}
        s = sr.get("transcript_summary") or {}
        model = u.get("model")
        provider = u.get("provider")
        model_cell = _esc(model) if model else "—"
        if model and provider:
            model_cell += f"<div class='meta'>{_esc(provider)}</div>"
        rows.append(
            "<tr class='tool-row'>"
            f"<td><a class='fw' href='#{_esc(_tool_anchor(prompt_id, sr['tool'], u))}'>{_esc(sr['tool'])}</a></td>"
            f"<td>{model_cell}</td>"
            f"<td class='num'>{_esc(u.get('total_tokens'))}</td>"
            f"<td class='num'>{_esc(u.get('input_tokens'))}</td>"
            f"<td class='num'>{_esc(u.get('output_tokens'))}</td>"
            f"<td class='num'>{_esc(u.get('cache_read_tokens'))}</td>"
            f"<td class='num'>{_esc(u.get('api_calls'))}</td>"
            f"<td class='num'>{_esc(u.get('estimated_cost_usd'))}</td>"
            f"<td class='num'>{_esc(s.get('iterations'))}</td>"
            f"<td class='num'>{_esc(s.get('tool_calls'))}</td>"
            f"<td class='num'>{_esc(sr.get('wall_seconds'))}</td>"
            "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


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
            stats = (
                f"model={_esc(usage.get('model')) or '—'} · "
                f"provider={_esc(usage.get('provider')) or '—'} · "
                f"total_tokens={_esc(usage.get('total_tokens'))} · "
                f"input={_esc(usage.get('input_tokens'))} · "
                f"output={_esc(usage.get('output_tokens'))} · "
                f"api_calls={_esc(usage.get('api_calls'))} · "
                f"cost=${_esc(usage.get('estimated_cost_usd'))} · "
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
    nav = " · ".join(
        f"<a class='fw' href='#{_esc(p)}'>{_esc(p)}</a>" for p in grouped
    )

    sections = []
    for prompt, prs in grouped.items():
        sections.append(_testcase_section_html(prompt, prs[0].get("prompt_text"), prs))

    models_txt = ", ".join(_esc(m) for m in models) or "—"
    html_doc = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{_esc(title)}</title><style>{_PAGE_CSS}</style></head><body><div class='wrap'>"
        f"<h1>{_esc(title)}</h1>"
        f"<p class='meta'>Self-contained report · models: {models_txt}. "
        "Results are grouped by testcase (prompt); each has its own "
        "comparison table and folded per-tool iterations — click a summary to "
        "open that step's inputs/outputs.</p>"
        "<p><button onclick='toggleFolded(this)' data-state='expand'>"
        "Expand all iterations</button></p>"
        + (f"<p class='meta'>testcases: {nav}</p>" if nav else "")
        + "".join(sections)
        + "</div><script>" + _PAGE_JS + "</script></body></html>"
    )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    return out_path

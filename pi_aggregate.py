#!/usr/bin/env python3
"""Aggregate versioned pi benchmark runs into averages + reports.

A benchmark tool's identity is `tool@version` (see pi_versions). Every run of a
cell is stored timestamped under artifacts/<model>-<provider>/<tool>@<version>/
with an append-only runs.json. This module loads ALL runs, groups by
(tool@version, prompt), computes per-metric averages (and min/max/median, n),
and writes:
    <tool@version>/summary.json   full per-prompt summary
    <model-provider>/report.md    markdown averages table
    <model-provider>/report.html  self-contained HTML averages table
    <model-provider>/versions.json  tool@version + probe info used

Usage:
  python3 pi_aggregate.py --artifacts artifacts            # all model runs
  python3 pi_aggregate.py --model-root artifacts/ds4-openrouter   # one model
"""
from __future__ import annotations

import argparse
import html
import json
import os
import statistics
from collections import defaultdict

# Metrics we average across runs (usage-file field -> nice label).
METRICS = [
    ("total_tokens", "total"),
    ("input_tokens", "in"),
    ("output_tokens", "out"),
    ("cache_read_tokens", "cache"),
    ("reasoning_tokens", "reas"),
    ("api_calls", "api"),
    ("estimated_cost_usd", "cost_usd"),
    ("wall_seconds", "wall_s"),
]
SUMMARY_METRICS = [
    ("iterations", "texec"),
    ("tool_calls", "tcaps"),
]


def _num(v):
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _uval(r, field):
    u = r.get("usage")
    if isinstance(u, dict) and field in u:
        return _num(u.get(field))
    # Some metrics live on the run row, not inside usage (e.g. wall_seconds).
    return _num(r.get(field))


def _stats(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0}
    # Sample stdev needs >=2 points; a single run has no spread to report.
    sd = round(statistics.stdev(vals), 4) if len(vals) >= 2 else 0.0
    return {
        "n": len(vals),
        "mean": round(statistics.mean(vals), 4),
        "median": round(statistics.median(vals), 4),
        "min": round(min(vals), 4),
        "max": round(max(vals), 4),
        "stdev": sd,
    }


def aggregate(model_root, artifacts_root=None):
    """Compute per-(tool@version,prompt) averages for one model run dir.

    Runs marked failed (runner-side failure detection: cache-private bridge
    down, empty output, crashes, nonzero exit) are excluded from every metric,
    including n_runs, so a broken cell never inflates an average or a report.
    """
    summaries = []
    if not os.path.isdir(model_root):
        return summaries
    for name in sorted(os.listdir(model_root)):
        d = os.path.join(model_root, name)
        runs_path = os.path.join(d, "runs.json")
        if not (os.path.isdir(d) and os.path.exists(runs_path)):
            continue
        try:
            with open(runs_path, encoding="utf-8") as f:
                runs = json.load(f)
        except Exception:
            continue
        if not runs:
            continue
        # Re-derive failure for pre-detector runs (older runs.json rows have no
        # 'failed' key but their usage.error / transcripts still signal a dud),
        # DELETE their artifact files, and drop them from runs.json so the
        # artifacts tree no longer contains measured-broken runs at all.
        runs, removed = _clean_failed_runs(d, name, runs)
        if not runs:
            # No usable runs remain for this tool@version -> drop the folder.
            _prune_empty_folder(d, name)
            continue
        if removed:
            with open(runs_path, "w", encoding="utf-8") as f:
                json.dump(runs, f, indent=2, ensure_ascii=False)
        summaries.append(_summarize_tool(d, name, runs))
    if summaries:
        _write_report(model_root, summaries)
    return summaries


def _clean_failed_runs(folder, name, runs):
    """Remove failed runs from artifacts (rows + their files).

    Returns (usable_runs, removed_count). For every failed run row the
    transcript/usage files named in the row are deleted. runs.json is NOT
    rewritten here if nothing changed; the caller rewrites it when removed > 0.
    """
    kept = []
    removed = 0
    for r in runs:
        if not _run_is_failed(r, folder):
            kept.append(r)
            continue
        removed += 1
        for key in ("transcript_json", "transcript_jsonl"):
            fn = r.get(key)
            if fn:
                try:
                    os.unlink(os.path.join(folder, fn))
                except OSError:
                    pass
        # <tool_id>-<prompt>-<run_ts>.json usage file (derived filename).
        usage_fn = f"{r.get('tool_id')}-{r.get('prompt')}-{r.get('run_ts')}.json"
        try:
            os.unlink(os.path.join(folder, usage_fn))
        except OSError:
            pass
        print(f"    removed failed run {name}: {r.get('prompt')} "
              f"{r.get('run_ts')} ({r.get('error') or (r.get('usage') or {}).get('error')})")
    return kept, removed


def _run_is_failed(r, folder):
    """True when a run row is a detected failure (or a pre-detector dud)."""
    if r.get("failed"):
        return True
    u = r.get("usage") or {}
    if isinstance(u, dict) and u.get("error"):
        return True
    # Pre-detector rows: a nonzero exit with no final answer, or transcripts
    # whose tool results all carry the hard-failure markers.
    tf = r.get("transcript_json")
    if tf:
        tp = os.path.join(folder, tf)
        try:
            with open(tp, encoding="utf-8") as f:
                trace = json.load(f)
        except Exception:
            trace = None
        if trace:
            import pi_runner
            reason = pi_runner.run_failed_reason(
                trace, exit_code=r.get("exit"), wall=r.get("wall_seconds"))
            if reason:
                return True
    return False


def _prune_empty_folder(folder, name):
    """Remove a tool@version folder left with only failed runs.

    Files with a run_ts token are deleted; summary.json / runs.json are removed
    so the report no longer lists a tool@version with zero usable runs.
    """
    import re
    run_ts_re = re.compile(r"-\d{8}_\d{6}_\d{3}\.")
    for fn in os.listdir(folder):
        if run_ts_re.search(fn):
            try:
                os.unlink(os.path.join(folder, fn))
            except OSError:
                pass
    for stub in ("summary.json", "runs.json"):
        try:
            os.unlink(os.path.join(folder, stub))
        except OSError:
            pass
    try:
        os.rmdir(folder)
    except OSError:
        pass
    print(f"    pruned {name}: no usable runs (all failed)")


def _summarize_tool(folder, name, runs):
    """One tool@version folder -> summary dict + summary.json."""
    first = runs[0]
    tool_id = first.get("tool_id") or name
    per_prompt = defaultdict(list)
    for r in runs:
        per_prompt[r.get("prompt") or "?"].append(r)

    prompts = []
    for prompt in sorted(per_prompt):
        rs = per_prompt[prompt]
        row = {"prompt": prompt, "n_runs": len(rs)}
        for field, label in METRICS:
            row[label] = _stats([_uval(r, field) for r in rs])
        row["texec"] = _stats([(_num((r.get("summary") or {}).get("iterations")))
                              for r in rs])
        row["tcaps"] = _stats([(_num((r.get("summary") or {}).get("tool_calls")))
                             for r in rs])
        row["run_log"] = [{
            "run_ts": r.get("run_ts"), "exit": r.get("exit"),
            "wall_seconds": r.get("wall_seconds"),
            "transcript": r.get("transcript_json"),
            "usage_total": (r.get("usage") or {}).get("total_tokens"),
        } for r in rs]
        prompts.append(row)

    summary = {
        "tool_id": tool_id,
        "tool": first.get("tool"),
        "version": first.get("tool_version"),
        "version_source": first.get("version_source"),
        "model": first.get("model"),
        "provider": first.get("provider"),
        "n_runs": len(runs),
        "prompts": prompts,
    }
    with open(os.path.join(folder, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    return summary


def _fmt_stats(s):
    if not s or s.get("n") == 0:
        return "—"
    return f"{s['mean']:g} (n={s['n']}, sd={s.get('stdev', 0):g})"


def _write_report(model_root, summaries):
    title = f"pi benchmark — {os.path.basename(model_root)}"
    md = [f"# {title}\n",
          "Averages over all runs, by tool@version × prompt "
          "(run notes: mean, n runs).\n"]
    hdr = "| tool@version | prompt | runs | total | in | out | cache | reas | api | cost_usd | wall_s | tcaps |\n"
    sep = "|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    md.append(hdr + sep)

    rows_html = []
    for s in sorted(summaries, key=lambda x: x["tool_id"]):
        tool_id = f"{s['tool']}@{s['version']}"
        for p in s["prompts"]:
            cells = [
                tool_id, p["prompt"], str(p["n_runs"]),
                _fmt_stats(p.get("total")), _fmt_stats(p.get("in")),
                _fmt_stats(p.get("out")), _fmt_stats(p.get("cache")),
                _fmt_stats(p.get("reas")), _fmt_stats(p.get("api")),
                _fmt_stats(p.get("cost_usd")), _fmt_stats(p.get("wall_s")),
                _fmt_stats(p.get("tcaps")),
            ]
            md.append("| " + " | ".join(cells) + " |\n")
            rows_html.append(
                "<tr>" + "".join(f"<td>{html.escape(str(c))}</td>"
                                 for c in cells) + "</tr>")

    with open(os.path.join(model_root, "report.md"), "w", encoding="utf-8") as f:
        f.write("".join(md))

    toc = "<li><a href='#summary'>summary</a></li>"
    body = ("<h1>" + html.escape(title) + "</h1>"
            "<h2 id='summary'>Averages</h2>"
            "<table><thead><tr>" +
            "".join(f"<th>{h}</th>" for h in hdr.strip("|").split("|")) +
            "</tr></thead><tbody>" + "".join(rows_html) + "</tbody></table>")
    with open(os.path.join(model_root, "report.html"), "w", encoding="utf-8") as f:
        f.write(_page(title, toc, body))
    # versions.json: tool identity + probe info for traceability
    with open(os.path.join(model_root, "versions.json"), "w",
              encoding="utf-8") as f:
        json.dump([{k: s.get(k) for k in (
            "tool_id", "tool", "version", "version_source")} for s in summaries],
            f, indent=2)


def _page(title, toc, body):
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>" + html.escape(title) + "</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:2em;max-width:1000px}"
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;"
        "padding:6px;text-align:left}th{background:#f5f5f5}"
        "ul.toc{list-style:none;padding:0}ul.toc li{display:inline;margin-right:1em}"
        "</style></head><body>" + body + "</body></html>"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--artifacts", default="artifacts")
    ap.add_argument("--model-root", default=None,
                    help="a single artifacts/<model>-<provider> dir; else all")
    args = ap.parse_args()
    if args.model_root:
        roots = [args.model_root]
    else:
        roots = [os.path.join(args.artifacts, d)
                 for d in sorted(os.listdir(args.artifacts))
                 if os.path.isdir(os.path.join(args.artifacts, d))]
    total = 0
    for r in roots:
        n = len(aggregate(r, args.artifacts))
        total += n
        if n:
            print(f"  {r}: aggregated {n} tool@version folder(s)")
    print(f"\ntotal {total} tool@version folder(s) aggregated; "
          "report.md / report.html / summary.json written.")


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""Generate HTML reports from saved benchmark run dirs (artifacts/).

Each benchmark run writes its raw data into its own durable dir under
artifacts/ — artifacts/<model>-<provider>[-tag]/ — containing run.json,
per-(tool,prompt) usage JSONs, and verbose transcripts. This tool reads those
saved dirs and renders reports on demand: a single-model report, or a combined
multi-model report with one comparison table per testcase where every row is a
(tool × model) combination. Nothing here re-runs the agents.

Usage:
  # list the run dirs found under artifacts/
  python3 report.py --artifacts artifacts --list

  # combined report from ALL run dirs under artifacts/
  python3 report.py --artifacts artifacts --out report-combined.html

  # combined report from specific run dirs
  python3 report.py \
      --artifacts artifacts/gpt-5.6-sol-copilot \
      --run-dir artifacts/deepseek-v4-flash-0731-openrouter \
      --out report-combined.html

  # single-report for one run dir (same as bench.py --html)
  python3 report.py --run-dir artifacts/gpt-5.6-sol-copilot --out report-gpt.html
"""

from __future__ import annotations

import argparse
import html
import os
import sys

import htmlreport


def discover_dirs(artifacts_root):
    """Return run dirs (containing run.json) directly under artifacts_root."""
    found = []
    if not os.path.isdir(artifacts_root):
        return found
    for name in sorted(os.listdir(artifacts_root)):
        d = os.path.join(artifacts_root, name)
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "run.json")):
            found.append(d)
    return found


def run_label(run_dir):
    """Model + provider label for a run dir (from first usage row or name)."""
    rj = os.path.join(run_dir, "run.json")
    try:
        import json
        with open(rj, encoding="utf-8") as f:
            rows = json.load(f)
        for r in rows:
            u = r.get("usage") or {}
            if u.get("model"):
                prov = f" ({u.get('provider')})" if u.get("provider") else ""
                return f"{u['model']}{prov}"
    except Exception:
        pass
    return os.path.basename(os.path.abspath(run_dir))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--artifacts", default="artifacts",
                    help="Root dir under which run dirs live (default: artifacts/)")
    ap.add_argument("--run-dir", action="append", default=[],
                    help="Explicit run dir(s) to include; may be repeated. "
                         "If absent, all run dirs under --artifacts are used.")
    ap.add_argument("--out", default="report.html",
                    help="Output HTML path (default: report.html)")
    ap.add_argument("--title", default=None, help="Override report title")
    ap.add_argument("--list", action="store_true",
                    help="List discovered run dirs and exit")
    args = ap.parse_args()

    if args.run_dir:
        dirs = args.run_dir
    else:
        dirs = discover_dirs(args.artifacts)
    if not dirs:
        print(f"no run dirs found under {args.artifacts} (look for run.json)",
              file=sys.stderr)
        sys.exit(1)

    if args.list:
        print("Run dirs:")
        for d in dirs:
            print(f"  {d}  [{run_label(d)}]")
        return

    # Validate they all exist + have run.json
    for d in dirs:
        if not os.path.exists(os.path.join(d, "run.json")):
            print(f"warning: no run.json in {d}; skipping", file=sys.stderr)

    labels = ", ".join(run_label(d) for d in dirs)
    print(f"Generating combined report from {len(dirs)} run dir(s): {labels}")
    print(f"  -> {args.out}")

    if args.title:
        out = htmlreport.render_html(dirs[0], args.out, args.title, *dirs[1:])
    else:
        out = htmlreport.render_html(dirs[0], args.out, None, *dirs[1:])
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()

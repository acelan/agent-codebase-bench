#!/usr/bin/env python3
"""Compare per-(tool,prompt) total-token mean across model run dirs.

Reads artifacts/<model>-<provider>/<tool>@<version>/summary.json for each
model and draws a delta table: for every (tool, prompt) it shows each model's
mean total tokens and the % change vs the first (baseline) model.

Usage:
  python3 compare_models.py                    # all model dirs in artifacts/
  python3 compare_models.py --baseline deepseek-v4-flash-0731-openrouter
"""
from __future__ import annotations

import argparse
import json
import os

METRIC = "total"  # total tokens


def _mean(stats):
    return (stats or {}).get("mean")


def load(root):
    out = {}
    if not os.path.isdir(root):
        return out
    for tid in sorted(os.listdir(root)):
        sj = os.path.join(root, tid, "summary.json")
        if not (os.path.isdir(os.path.join(root, tid)) and os.path.isfile(sj)):
            continue
        s = json.load(open(sj, encoding="utf-8"))
        for p in s.get("prompts", []):
            m = _mean(p.get(METRIC))
            n = (p.get(METRIC) or {}).get("n")
            if m is None:
                m = float("nan")
            out[(s.get("tool_id") or tid, p.get("prompt"))] = (m, n)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--artifacts", default="artifacts")
    ap.add_argument("--baseline", default=None,
                    help="model dir name to treat as baseline; default = first sorted")
    args = ap.parse_args()

    roots = sorted(
        d for d in os.listdir(args.artifacts)
        if os.path.isdir(os.path.join(args.artifacts, d))
        and any(
            os.path.exists(os.path.join(args.artifacts, d, t, "summary.json"))
            for t in os.listdir(os.path.join(args.artifacts, d))
        )
    )
    if not roots:
        raise SystemExit(f"no model dirs under {args.artifacts}")
    if args.baseline:
        if args.baseline not in roots:
            raise SystemExit(f"baseline {args.baseline!r} not in {roots}")
        roots = [args.baseline] + [r for r in roots if r != args.baseline]

    data = {r: load(os.path.join(args.artifacts, r)) for r in roots}
    keys = sorted({k for d in data.values() for k in d})

    print(f"metric: mean {METRIC} tokens  |  baseline: {roots[0]}")
    print("tool@version | prompt | " + " | ".join(roots) + " | Δ% vs baseline")

    for (tid, prompt) in keys:
        base_m, _ = data[roots[0]].get((tid, prompt), (float("nan"), None))
        cells = []
        for r in roots:
            m, n = data[r].get((tid, prompt), (float("nan"), None))
            cell = "—" if m != m else f"{m/1000:.1f}k(n={n})"
            cells.append(cell)
        others = " | ".join(cells[i] for i in range(1, len(roots)))
        delta = "—"
        if base_m == base_m:
            # compare the non-baseline model(s) to baseline
            if len(roots) == 2:
                m, _ = data[roots[1]].get((tid, prompt), (float("nan"), None))
                if m == m and base_m:
                    delta = f"{100*(m-base_m)/base_m:+.1f}%"
            elif len(roots) > 2:
                sub = []
                for r in roots[1:]:
                    m, _ = data[r].get((tid, prompt), (float("nan"), None))
                    sub.append("—" if (m != m or not base_m)
                               else f"{100*(m-base_m)/base_m:+.0f}%")
                delta = " | ".join(sub)
        print(f"{tid} | {prompt} | {cells[0]} | {others} | {delta}")

    # Real cross-model delta when exactly 2 models
    if len(roots) == 2:
        r0, r1 = roots
        print("\nΔ% = (model2 - baseline)/baseline on mean totals:")
        for (tid, prompt) in keys:
            b, _ = data[r0].get((tid, prompt), (float("nan"), None))
            m, n = data[r1].get((tid, prompt), (float("nan"), None))
            if b != b or m != m or b == 0:
                print(f"  {tid} {prompt}: —")
                continue
            print(f"  {tid} {prompt}: {100*(m-b)/b:+.1f}%  ({b/1000:.1f}k -> {m/1000:.1f}k)")


if __name__ == "__main__":
    main()

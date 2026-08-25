#!/usr/bin/env python3
"""pi-backed matrix driver for agent-codebase-bench (versioned storage).

Runs the (tool x prompt) matrix through headless pi (`--mode json`) and stores
every run under:
    artifacts/<model>-<provider>/<tool>@<version>/<tool>@<version>-<prompt>-<run_ts>.{json,*.transcript.*}
with an append-only `runs.json` per tool@version folder (so the same tool+version
can be benchmarked many times, all kept). Then pi_aggregate.py computes
averages over all runs and renders summary.json / report.md / report.html.

Tool version comes from pi_versions.tool_version() (a `--version` probe, or the
kernel git HEAD hash when the tool reports none). Different versions of a tool
land in different folders and are treated as distinct tools.

Usage:
  python3 bench_pi.py --model openrouter/deepseek-v4-flash-0731 --backend docker
  python3 bench_pi.py --model-preset ds4 [--tools grep] [--prompts callers-drm-register]
  python3 bench_pi.py --model ... [--runs 3]     # benchmark each cell N times
  python3 bench_pi.py --aggregate-only            # recompute reports from saved runs
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import yaml

import pi_runner
import pi_versions


def load_config(path):
    """Load the benchmark YAML (model/provider resolved at run time)."""
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise SystemExit(f"config {path} is not a mapping")
    return cfg


def now_ts():
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3]


# Harness .py files baked into the Docker image (must match docker/Dockerfile's
# COPY + hash-stamp step, in the same order).
_HARNESS_FILES = [
    "pi_stream.py", "pi_transcript.py", "pi_runner.py", "pi_versions.py",
    "pi_aggregate.py", "pi_summary.py", "pi_report.py", "bench_pi.py",
]


def _local_harness_sha256():
    """Hash the harness .py files exactly like the Dockerfile's stamp step
    (cat file1 file2 ... | sha256sum -- content only, no paths, fixed order)."""
    import hashlib
    here = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256()
    for name in _HARNESS_FILES:
        with open(os.path.join(here, name), "rb") as f:
            h.update(f.read())
    return h.hexdigest()


def check_image_freshness(image, backend):
    """Warn loudly if the docker image predates the local harness source.

    docker/Dockerfile COPYs pi_*.py / bench_pi.py into the image at build
    time, so editing those files on the host has NO effect on a
    already-built image until it is rebuilt. This has silently produced
    stale reports before (a script fix landed, but every benchmark run kept
    executing the OLD baked-in pi_aggregate.py, so e.g. report.html quietly
    missed the new sd= field for days). Comparing the image's baked
    .harness-sha256 against a hash computed the same way from the current
    checkout catches that class of staleness before a run wastes API calls
    on it. Best-effort: any failure here (docker not installed, image not
    built yet, mismatched hash algorithm after a future Dockerfile edit)
    only prints a warning and never blocks the run.
    """
    if backend != "docker":
        return
    import subprocess
    try:
        image_sha = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "cat", image,
             "/opt/pi-bench/.harness-sha256"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except Exception as e:
        print(f"[freshness] could not probe {image}'s baked harness hash "
              f"({e}); skipping staleness check", file=sys.stderr)
        return
    if not image_sha:
        print(f"[freshness] {image} has no .harness-sha256 stamp (built "
              "before this check existed) -- rebuild to enable it: "
              "docker build -t agent-codebase-bench -f docker/Dockerfile .",
              file=sys.stderr)
        return
    local_sha = _local_harness_sha256()
    if image_sha != local_sha:
        print(
            f"[freshness] WARNING: {image}'s baked-in pi_*.py/bench_pi.py "
            f"differ from the local checkout (image={image_sha[:12]} "
            f"local={local_sha[:12]}). The container will run the OLD "
            "harness code -- any fix you made since the image was last "
            "built (e.g. to pi_aggregate.py or pi_report.py) will NOT take "
            "effect until you rebuild:\n"
            "    docker build -t agent-codebase-bench -f docker/Dockerfile .\n"
            "Proceeding anyway; results may not reflect the current source.",
            file=sys.stderr,
        )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="benchmark.yaml")
    ap.add_argument("--model-preset", default=None)
    ap.add_argument("--models-file", default="models.yaml")
    ap.add_argument("--model", default=None)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--backend", default=os.environ.get("PI_BACKEND", "docker"),
                    choices=["docker", "native"])
    ap.add_argument("--results-dir", default=None, help="base dir (default: artifacts/)")
    ap.add_argument("--run-tag", default="")
    ap.add_argument("--tools", default=None)
    ap.add_argument("--prompts", default=None)
    ap.add_argument("--runs", type=int, default=1,
                    help="run each (tool,prompt) cell this many times (default 1)")
    ap.add_argument("--aggregate-only", action="store_true",
                    help="skip running cells; recompute averages/reports from saved runs")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ensure-index", action="store_true")
    args = ap.parse_args()

    import pi_aggregate
    cfg = load_config(args.config)
    if args.model_preset:
        with open(args.models_file) as f:
            presets = yaml.safe_load(f) or {}
        if args.model_preset not in presets:
            raise SystemExit(f"unknown preset '{args.model_preset}' in {args.models_file}")
        cfg["model"] = presets[args.model_preset].get("model", cfg.get("model"))
        cfg["provider"] = presets[args.model_preset].get("provider", cfg.get("provider"))
    if args.model:
        cfg["model"] = args.model
    if args.provider:
        cfg["provider"] = args.provider
    if not cfg.get("model"):
        raise SystemExit("no model: pass --model-preset <name> or --model <provider/model>")
    # PI_PROVIDER in the environment (docker/.env) picks the provider when
    # multiple are configured at once (e.g. both OPENROUTER_API_KEY and
    # OLLAMA_BASE_URL are set). CLI --provider/--model-preset win over it.
    if not args.provider and not args.model_preset:
        env_provider = os.environ.get("PI_PROVIDER", "").strip().lower()
        if env_provider:
            cfg["provider"] = env_provider
    cfg.setdefault("provider", "openrouter")
    cfg.setdefault("kernel_dir", "/workspace/linux")

    if cfg["provider"] == "ollama":
        if not os.environ.get("OLLAMA_BASE_URL"):
            print("[ollama] provider defaults to OLLAMA_BASE_URL "
                  "http://127.0.0.1:11434/v1; set OLLAMA_BASE_URL in "
                  "docker/.env for a remote endpoint", file=sys.stderr)

    if args.ensure_index:
        print("[ensure-index] pi-parity not implemented; index the kernel "
              "manually (codegraph init / graft build / repowise init / "
              "codebase-memory-mcp index_repository).")

    base_dir = os.path.abspath(args.results_dir or cfg.get("results_dir", "artifacts"))
    tools = [t.strip() for t in (args.tools or "").split(",") if t.strip()] \
        if args.tools else list(cfg["tools"].keys())
    tools = [t for t in tools if cfg["tools"].get(t, {}).get("enabled", True)]
    prompt_ids = [p.strip() for p in (args.prompts or "").split(",") if p.strip()] \
        if args.prompts else None
    prompts = [p for p in cfg["prompts"] if not prompt_ids or p["id"] in prompt_ids]

    flat_model = cfg["model"].rsplit("/", 1)[-1]
    if cfg.get("provider") == "ollama":
        # Ollama model ids carry a :tag; keep the artifacts dir name friendly
        # (artifacts/qwen3-coder-30b-ollama/ instead of ...:30b-ollama/).
        flat_model = flat_model.replace(":", "-")
    model_root = os.path.join(base_dir, f"{flat_model}-{cfg['provider']}")

    if not args.aggregate_only:
        check_image_freshness(pi_runner.BENCH_IMAGE, args.backend)
        print(f"backend={args.backend} model={cfg['model']} tools={tools} "
              f"prompts={[p['id'] for p in prompts]} runs={args.runs} "
              f"store={model_root}")
        for t in tools:
            ver, vsrc = pi_versions.tool_version(t, backend=args.backend)
            run_dir = os.path.join(model_root, f"{t}@{ver}")
            print(f"  {t} version={ver} (source={vsrc}) -> {run_dir}")
            for _ in range(args.runs):
                for p in prompts:
                    if args.dry_run:
                        pi_runner.run_cell(cfg, t, p, ver, vsrc, now_ts(),
                                           run_dir, backend=args.backend,
                                           dry_run=True)
                        continue
                    pi_runner.run_cell(cfg, t, p, ver, vsrc, now_ts(),
                                       run_dir, backend=args.backend)

    # Aggregate averages across all saved runs + render per-model reports.
    report = pi_aggregate.aggregate(model_root, base_dir)
    print(f"\naggregated {len(report)} tool@version folders; "
          f"wrote report.md / report.html / summary.json under {model_root}")

    # Combined report at the artifacts root, spanning all model runs.
    import pi_report
    out = pi_report.render(base_dir, os.path.join(base_dir, "report.html"),
                           cfg.get("tool_instruction") or {},
                           analyst_model=(os.environ.get("PI_SUMMARY_MODEL")
                                          or cfg["model"]))
    print(f"combined report: {out}")
    sys.exit(0)


if __name__ == "__main__":
    main()
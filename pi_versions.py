"""Tool version detection for agent-codebase-bench.

A benchmark tool's identity includes its version: `codegraph@1.5.0` is a
different "tool" from `codegraph@1.6.0`, so results live in a versioned
folder and versions never silently mix. Each tool is probed for a version;
when a tool reports none, we fall back to the kernel repo's final-commit
timestamp/hash (the thing actually being measured).

Probing order per tool:
  1. <tool> --version  (parsed for a plausible short version string)
  2. fallback          kernel git HEAD short hash + commit timestamp
Detection runs on the host first (the bench tools are installed there); a
docker backend may set DOCKER_IMAGE to probe inside the image instead.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from functools import lru_cache

# Which host binary provides each benchmark tool's version.
VERSION_BIN = {
    "grep": "rg",                  # search_files is ripgrep-backed
    "ripgrep": "rg",
    "codegraph": "codegraph",
    "graft": "graft",
    "repowise": "repowise",
    "codebase-memory-mcp": "codebase-memory-mcp",
    "rtk": "rtk",
}

_KERNEL_DIR = os.environ.get("KERNEL_DIR", "/workspace/linux")
# Must match pi_runner.BENCH_IMAGE's default so docker-only tools (rtk,
# codegraph, codebase-memory-mcp) can still be version-probed inside the
# image even when BENCH_IMAGE/DOCKER_IMAGE isn't explicitly exported.
DOCKER_IMAGE = (os.environ.get("BENCH_IMAGE") or os.environ.get("DOCKER_IMAGE")
                or "agent-codebase-bench")

# Some tools install off-PATH (repowise lives in a venv); try these too.
CANDIDATE_PATHS = {
    "repowise": [
        shutil.which("repowise"),
        "/opt/repowise-venv/bin/repowise",
        os.path.expanduser("~/.venv/bin/repowise"),
    ],
    "codegraph": [None],
    "graft": [None],
    "codebase-memory-mcp": [None],
    "rg": [None],
    # pi is resolved from PATH (the docker image installs @earendil-works/pi-coding-agent).
    "pi": [None],
}


def _version_flags(bin):
    """A tool returning 'only a number' (codebase-memory-mcp) vs a banner."""
    if bin == "codebase-memory-mcp":
        return ["--version"]
    return ["--version"]


def _parse_version(text):
    """Pull a compact semver-ish token out of `--version` output."""
    if not text:
        return None
    m = re.search(r"\b(\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?)\b", text)
    return m.group(1) if m else None


def _probe_host(bin):
    candidates = CANDIDATE_PATHS.get(bin) or [None]
    seen = set()
    for c in candidates:
        path = c or shutil.which(bin)
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            r = subprocess.run([path, *_version_flags(bin)],
                               capture_output=True, text=True, timeout=30)
            out = (r.stdout or "") + (r.stderr or "")
            if out.strip():
                return out
        except Exception:
            continue
    return None


def _probe_docker(bin):
    if not DOCKER_IMAGE:
        return None
    try:
        # -e entrypoint arg tells docker to run <bin> instead of the pi shim.
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", bin,
             DOCKER_IMAGE, *_version_flags(bin)],
            capture_output=True, text=True, timeout=120)
        return (r.stdout or "") + (r.stderr or "")
    except Exception:
        return None


def _kernel_git_version():
    """Fallback: kernel final-commit short hash + UTC commit timestamp."""
    try:
        tree = _KERNEL_DIR
        if not os.path.isdir(tree) and DOCKER_IMAGE:
            # probe inside the image
            r = subprocess.run(
                ["docker", "run", "--rm", "--entrypoint", "git",
                 DOCKER_IMAGE, "-C", "/workspace/linux",
                 "log", "-1", "--format=%h|%cI"],
                capture_output=True, text=True, timeout=120)
            out = (r.stdout or "").strip()
            if out and "|" in out:
                h, t = out.split("|", 1)
                return h, t
        r = subprocess.run(
            ["git", "-C", tree, "log", "-1", "--format=%h|%cI"],
            capture_output=True, text=True, timeout=60)
        out = (r.stdout or "").strip()
        if not out or "|" not in out:
            return None, None
        h, t = out.split("|", 1)
        return h, t
    except Exception:
        return None, None


@lru_cache(maxsize=None)
def tool_version(tool, backend="native"):
    """Return (version_str, source). source in {'tool','agent','kernel_git','n/a'}.

    'grep' is the pi agent's built-in search tool (allowlist read,grep,find,ls),
    not a standalone CLI, so its version is the PI AGENT version — not ripgrep's.
    'ripgrep' is the raw rg binary and reports rg's own version.
    """
    if tool == "grep":
        text = _probe_docker("pi") if backend == "docker" else _probe_host("pi")
        v = _parse_version(text) if text else None
        if v:
            return v, "agent"
    bin = VERSION_BIN.get(tool, tool)
    text = _probe_docker(bin) if backend == "docker" else None
    if not text:
        text = _probe_host(bin)
    if text:
        v = _parse_version(text)
        if v:
            return v, "tool"
    # Fallback: kernel final commit hash (+ timestamp) as the version.
    h, t = _kernel_git_version()
    if h:
        # A concrete, stable per-tree version: short hash.
        # We surface the commit timestamp too so the exact commit is findable.
        return h, "kernel_git"
    # "n/a" is never allowed here: it's embedded verbatim into directory/file
    # names as f"{tool}@{version}" (e.g. "rtk@n/a"), and the embedded slash
    # silently creates an extra unmade path component, crashing open() later.
    # Use a slash-free sentinel instead.
    return "unknown", "n/a"


def tool_version_meta(tool, backend="native"):
    """Human-friendly version info incl. the raw probe output fragment."""
    if tool == "grep":
        text = (_probe_docker("pi") if backend == "docker" else _probe_host("pi")) or ""
        v, src = tool_version(tool, backend)
        frag = (text.strip().splitlines() or [""])[0][:80]
        return {"tool": tool, "bin": "pi", "version": v, "source": src,
                "probe": frag}
    bin = VERSION_BIN.get(tool, tool)
    text = (_probe_docker(bin) if backend == "docker" else _probe_host(bin)) or ""
    v, src = tool_version(tool, backend)
    frag = (text.strip().splitlines() or [""])[0][:80]
    return {"tool": tool, "bin": bin, "version": v, "source": src,
            "probe": frag}


if __name__ == "__main__":
    import json as _json
    import sys
    for t in sys.argv[1:] or list(VERSION_BIN):
        print(_json.dumps(tool_version_meta(t)))

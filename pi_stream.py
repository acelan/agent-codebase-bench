"""Shared helpers for parsing pi's `--mode json` event stream.

pi emits one JSON object per line (NDJSON). The first line is the session
header; the rest are events. Messages carry content as a list of typed parts
({type: text|thinking|toolCall|toolResult}); we flatten them to plain strings
so the resulting trace is drop-in compatible with transcript.py / htmlreport.py.

Usage:
    recs = list(load_stream(path_or_file))
    for r in recs: print(r.get("type"))
"""
from __future__ import annotations

import json


def load_stream(path):
    """Yield parsed records (dicts) from a pi `--mode json` stream file."""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate a stray log line on stdout


def text_content(parts):
    """Flatten a pi content part-list to a plain string.

    Handles dicts ({type: text, text: ...}), strings, and toolCall/toolResult
    parts (rendered as compact JSON). Used so htmlreport receives strings.
    """
    if parts is None:
        return ""
    if isinstance(parts, str):
        return parts
    if isinstance(parts, list):
        out = []
        for p in parts:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict):
                t = p.get("type")
                if t == "thinking":
                    out.append(p.get("thinking") or "")
                elif t == "toolCall":
                    out.append(json.dumps(p, ensure_ascii=False))
                elif t == "toolResult":
                    out.append(json.dumps(p, ensure_ascii=False))
                else:
                    v = p.get("text") or p.get("content") or ""
                    out.append(v if isinstance(v, str) else json.dumps(v))
        return "\n".join(s for s in out if s)
    return json.dumps(parts, ensure_ascii=False)


def tool_calls_from_content(content):
    """Extract the toolCalls from an assistant message content part-list."""
    calls = []
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict) and p.get("type") == "toolCall":
                calls.append(p)
    return calls

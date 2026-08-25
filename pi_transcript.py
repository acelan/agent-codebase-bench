"""Parse a pi `--mode json` stream into transcript.py's trace dict shape.

Emits the SAME structure as transcript.parse_transcript() so report.py and
htmlreport.py render pi runs unchanged:

    {
      session_id, model, provider,
      prompt,                       # first user message text
      events: [
        {type:"prompt", ...},
        {type:"assistant_tool_call", tool_calls:[{tool_call_id,name,arguments}]},
        {type:"tool_result", tool_call_id, tool_name, content},
        {type:"final_answer", content}, ...
      ],
      aggregate: {...},  summary: {iterations, tool_calls}
    }

Usage metrics are aggregated by summing the per-request `usage` objects on
assistant message_end records (pi does not emit a consolidated session total).

Usage:
    from pi_transcript import parse_stream
    trace = parse_stream("/path/to/run.jsonl")
"""
from __future__ import annotations

import json

from pi_stream import load_stream, text_content, tool_calls_from_content


def _agg_usage(session, records):
    """Aggregate per-request usage from assistant message_end records."""
    a = {k: 0 for k in (
        "input_tokens", "output_tokens", "cache_read_tokens",
        "cache_write_tokens", "reasoning_tokens", "total_tokens",
    )}
    cost = 0.0
    api_calls = 0
    model = provider = None
    for r in records:
        if r.get("type") != "message_end":
            continue
        msg = r.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        u = msg.get("usage") or {}
        if not u:
            continue
        api_calls += 1
        a["input_tokens"] += u.get("input") or 0
        a["output_tokens"] += u.get("output") or 0
        a["cache_read_tokens"] += u.get("cacheRead") or 0
        a["cache_write_tokens"] += u.get("cacheWrite") or 0
        a["reasoning_tokens"] += u.get("reasoning") or 0
        a["total_tokens"] += u.get("totalTokens") or 0
        c = (u.get("cost") or {}).get("total") or 0
        cost += c
        model = msg.get("model") or model
        provider = msg.get("provider") or provider
    model = model or session.get("model")
    provider = provider or session.get("provider")
    return a, round(cost, 10), api_calls, model, provider


def parse_stream(path):
    records = list(load_stream(path))
    if not records:
        return {"error": "empty stream"}

    session = records[0]
    trace = {
        "session_id": session.get("id"),
        "model": None,
        "provider": None,
        "prompt": None,
        "events": [],
        "aggregate": {},
        "summary": {},
    }

    for r in records:
        if r.get("type") != "message_end":
            continue
        msg = r.get("message") or {}
        role = msg.get("role")
        content = msg.get("content")
        ts = msg.get("timestamp")
        base = {
            "id": msg.get("id"),
            "role": role,
            "timestamp": ts,
            "token_count": None,
            "finish_reason": msg.get("stopReason") or msg.get("rawStopReason"),
        }
        if role == "user":
            txt = text_content(content)
            if trace["prompt"] is None:
                trace["prompt"] = txt
            trace["events"].append({"type": "prompt", **base, "content": txt})
        elif role == "assistant":
            # Per-request usage (input/output/cacheRead/cacheWrite/reasoning/
            # totalTokens/cost), so a per-iteration breakdown can be shown in
            # the report without re-parsing the raw jsonl stream.
            u = msg.get("usage") or None
            calls = tool_calls_from_content(content)
            if calls:
                tcs = [{
                    "tool_call_id": c.get("id"),
                    "name": c.get("name"),
                    "arguments": c.get("arguments"),
                } for c in calls]
                trace["events"].append({
                    "type": "assistant_tool_call", **base,
                    "content": text_content(content), "tool_calls": tcs,
                    "usage": u,
                })
            else:
                txt = text_content(content)
                etype = "final_answer" if txt else "assistant"
                trace["events"].append({**base, "type": etype, "content": txt,
                                        "usage": u})
        elif role in ("toolResult", "tool"):
            trace["events"].append({
                "type": "tool_result", **base,
                "tool_call_id": msg.get("toolCallId"),
                "tool_name": msg.get("toolName"),
                "content": text_content(content),
            })

    first_final = next(
        (i for i, e in enumerate(trace["events"]) if e["type"] == "final_answer"),
        None,
    )
    if first_final is not None:
        trace["events"] = trace["events"][: first_final + 1]

    ag, cost, api, model, provider = _agg_usage(session, records)
    trace["model"], trace["provider"] = model, provider
    trace["aggregate"] = {
        "message_count": len(trace["events"]),
        "tool_call_count": sum(
            len(e["tool_calls"]) for e in trace["events"]
            if e["type"] == "assistant_tool_call"
        ),
        "api_call_count": api,
        **ag,
        "estimated_cost_usd": cost,
    }
    trace["summary"] = {
        "iterations": sum(
            1 for e in trace["events"]
            if e["type"] in ("assistant_tool_call", "final_answer")
        ),
        "tool_calls": sum(
            len(e["tool_calls"]) for e in trace["events"]
            if e["type"] == "assistant_tool_call"
        ),
    }
    return trace

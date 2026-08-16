"""Prepare benchmark evidence and manage the tool-free result analyst cache."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from pi_stream import text_content

SCHEMA_VERSION = "1"
PROMPT_VERSION = "1"
COMPACTION_VERSION = "4"

DEFAULT_COMPACTION_SETTINGS = {
    # Keep each recorded interaction small enough that a testcase containing
    # hundreds of interactions remains practical analyst input.
    "max_content_chars": 400,
    "head_chars": 250,
    "tail_chars": 100,
    # The analyst is invoked once per testcase.  This is therefore deliberately
    # a per-testcase guard, not a limit on the aggregate evidence document.
    "max_testcase_chars": 1_000_000,
    # Bound how many iterations per workflow (primary run + additional runs)
    # are expanded for the analyst. Benchmark cells can amass dozens of
    # iterations per run and several runs per cell (rtk/repowise deep cells
    # hit 60-80 iterations), which blows past max_testcase_chars even with
    # per-content compaction. The analyst only needs a representative prefix:
    # the first N iterations capture the tool-call pattern and result shape.
    "max_workflow_iterations": 28,
}

_METRIC_KEYS = (
    ("total", "total"),
    ("in", "input"),
    ("out", "output"),
    ("cache", "cache"),
    ("reas", "reasoning"),
    ("api", "api_calls"),
    ("wall_s", "elapsed_seconds"),
    ("tcaps", "tool_calls"),
)

_SECRET_PATTERNS = (
    (re.compile(r"(?i)(\bauthorization\s*:\s*bearer\s+)\S+"), r"\1[REDACTED]"),
    (re.compile(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)\b"
        r"\s*[:=]\s*[\"']?)[^\s,\"'}]+"
    ), r"\1[REDACTED]"),
    (re.compile(r"\bsk-(?:or-)?[A-Za-z0-9_-]{12,}\b"), "[REDACTED]"),
)


def redact_secrets(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def canonical_json(value: Any) -> str:
    """Serialize JSON data identically regardless of mapping insertion order."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _compaction_settings(settings: Mapping[str, int] | None) -> dict[str, int]:
    resolved = dict(DEFAULT_COMPACTION_SETTINGS)
    if settings is not None:
        unknown = set(settings) - set(resolved)
        if unknown:
            raise ValueError(f"unknown compaction setting(s): {', '.join(sorted(unknown))}")
        resolved.update(settings)

    for name, value in resolved.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if resolved["max_content_chars"] < resolved["head_chars"] + resolved["tail_chars"]:
        raise ValueError("max_content_chars must be at least head_chars + tail_chars")
    return resolved


def compact_content(content: Any, settings: Mapping[str, int] | None = None) -> dict[str, Any]:
    """Represent analyst-facing content with deterministic head/tail compaction."""
    resolved = _compaction_settings(settings)
    if isinstance(content, str):
        text = redact_secrets(content)
    elif content is None:
        text = ""
    else:
        text = canonical_json(content)

    original_length = len(text)
    truncated = original_length > resolved["max_content_chars"]
    if truncated:
        head = text[: resolved["head_chars"]]
        tail_count = resolved["tail_chars"]
        tail = text[-tail_count:] if tail_count else ""
        omitted = original_length - len(head) - len(tail)
        marker = f"\n...[TRUNCATED {omitted} CHARACTERS]...\n"
        compacted = head + marker + tail
    else:
        head = text
        tail = ""
        compacted = text

    return {
        "text": compacted,
        "original_length": original_length,
        "truncated": truncated,
        "empty": original_length == 0,
        "head_length": len(head),
        "tail_length": len(tail),
    }


def _compact_strings(value: Any, settings: Mapping[str, int]) -> Any:
    """Recursively bound retained auxiliary strings without changing small scalars."""
    if isinstance(value, str):
        value = redact_secrets(value)
        if len(value) > settings["max_content_chars"]:
            return compact_content(value, settings)
        return value
    if isinstance(value, Mapping):
        return {key: _compact_strings(value[key], settings) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_compact_strings(item, settings) for item in value]
    return value


def _structured_flag(value: Any, names: set[str]) -> bool:
    """Find an explicit true diagnostic flag in JSON-shaped metadata."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in names and item is True:
                return True
            if _structured_flag(item, names):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_structured_flag(item, names) for item in value)
    return False


def _result_markers(result: Mapping[str, Any], text: str, truncated: bool) -> dict[str, bool]:
    """Derive conservative structural/diagnostic interaction markers."""
    exit_code = result.get("exit_code", result.get("returncode"))
    explicit_failure = (
        result.get("is_error") is True
        or result.get("success") is False
        or (isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0)
        or str(result.get("status", "")).lower() in {"error", "failed", "failure"}
    )
    # Match complete diagnostic lines, not arbitrary occurrences of words such
    # as "error" or "failed" in returned source code.
    diagnostic_failure = bool(re.search(
        r"(?im)^(?:traceback \(most recent call last\):|"
        r"\s*(?:fatal|tool error|command error):\s+\S.*|"
        r"\s*command failed:\s+\S.*|"
        r"\s*enoent:\s+no such file or directory.*|"
        r"[^\n]*:\s+.*(?:no such file or directory|permission denied|command not found)\s*|"
        r"\s*command (?:failed|exited)(?:\s+with)?(?:\s+(?:status|code))?\s*[:=]?\s*[1-9]\d*\s*)$",
        text,
    ))

    structured_pagination = (
        result.get("has_more") is True
        or any(result.get(key) not in (None, "", False) for key in (
            "next_offset", "next_page", "next_cursor", "continuation_token"
        ))
    )
    diagnostic_pagination = bool(re.search(
        r"(?i)(?:\bpage\s+\d+\s+of\s+\d+\b|"
        r"\bshowing\s+\d+\s*[-–]\s*\d+\s+of\s+\d+\b|"
        r"\b(?:use|next)\s+(?:offset|cursor)\s*[=:]?\s*\d+\b|"
        r'\b"?has_more"?\s*:\s*true\b|\b"?next_offset"?\s*:)',
        text,
    ))
    source_truncated = (
        _structured_flag(result, {"truncated", "is_truncated", "stdout_truncated"})
        or bool(re.search(
            r"(?i)(?:\[output truncated\]|\boutput (?:was )?truncated\b|"
            r"\.\.\.\[truncated \d+ characters\]\.\.\.)",
            text,
        ))
    )
    return {
        "empty": not text.strip(),
        "failure": explicit_failure or diagnostic_failure,
        "pagination": structured_pagination or diagnostic_pagination,
        "known_truncated": truncated or source_truncated,
    }


def _normalize_call(call: Mapping[str, Any], settings: Mapping[str, int]) -> dict[str, Any]:
    normalized = {
        key: _compact_strings(call[key], settings)
        for key in sorted(call)
        if key != "arguments"
    }
    # Arguments are one logical analyst-facing field.  Serializing the entire
    # JSON value first both preserves its boundary and makes its limit exact.
    normalized["arguments"] = compact_content(
        canonical_json(call.get("arguments")), settings
    )
    return normalized


def _normalize_result(result: Mapping[str, Any], settings: Mapping[str, int]) -> dict[str, Any]:
    normalized = {
        key: _compact_strings(result[key], settings)
        for key in sorted(result)
        if key != "content"
    }
    content = compact_content(result.get("content"), settings)
    if isinstance(result.get("content"), str):
        text = result["content"]
    elif result.get("content") is None:
        text = ""
    else:
        text = canonical_json(result["content"])
    normalized["content"] = content
    normalized["markers"] = _result_markers(result, text, content["truncated"])
    return normalized


def _normalize_iteration(iteration: Mapping[str, Any], settings: Mapping[str, int]) -> dict[str, Any]:
    calls = [_normalize_call(call, settings) for call in (iteration.get("calls") or [])]
    results = [
        _normalize_result(result, settings)
        for result in (iteration.get("results") or [])
    ]
    final = iteration.get("final")
    return {
        "calls": calls,
        "results": results,
        "final": _normalize_result(final, settings) if final is not None else None,
    }


def _transcript_path(
    row: Mapping[str, Any],
    run: Mapping[str, Any] | None = None,
) -> str | None:
    prompt_row = row.get("pprompt") or {}
    if run is None:
        run_log = prompt_row.get("run_log") or []
        run = run_log[0] if run_log else None
    if not run or not run.get("transcript"):
        return None
    transcript = os.path.normpath(str(run["transcript"]))
    if (
        os.path.isabs(transcript)
        or transcript == os.pardir
        or transcript.startswith(os.pardir + os.sep)
    ):
        return None
    # Keep provenance relative to the artifact root. This is stable when the
    # same artifacts mount is rendered on the host and inside /workspace.
    return os.path.normpath(os.path.join(
        os.path.basename(os.path.normpath(str(row.get("model_root") or ""))),
        str(row.get("tool_id") or ""),
        transcript,
    ))


def _default_iteration_loader(model_root: str, tool_id: str, prompt_row: Mapping[str, Any]):
    # Lazy import keeps this module independent of report rendering and avoids a
    # module cycle when pi_report later imports analyst functionality.
    from pi_report import load_iterations

    return load_iterations(model_root, tool_id, prompt_row)


def normalize_evidence(
    rows: Iterable[Mapping[str, Any]],
    *,
    iteration_loader: Callable[[str, str, Mapping[str, Any]], list[Mapping[str, Any]] | None] | None = None,
    compaction_settings: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Group report rows by testcase and normalize their analyst evidence.

    ``rows`` accepts the dictionaries yielded by :func:`pi_report.load_cells`.
    A ``None`` iteration result is represented explicitly as a missing
    transcript; an empty list means the transcript was read but had no tool
    iterations.
    """
    settings = _compaction_settings(compaction_settings)
    loader = iteration_loader or _default_iteration_loader
    grouped: dict[str, dict[str, Any]] = {}

    for row in rows:
        testcase_value = row.get("prompt")
        testcase = "" if testcase_value is None else str(testcase_value)
        prompt_text = row.get("prompt_text")
        group = grouped.setdefault(testcase, {"prompt_texts": set(), "workflows": []})
        if prompt_text is not None:
            group["prompt_texts"].add(str(prompt_text))

        prompt_row = row.get("pprompt") or {}
        iterations = loader(
            str(row.get("model_root") or ""),
            str(row.get("tool_id") or ""),
            prompt_row,
        )
        path = _transcript_path(row)
        # Bound the analyst input: keep only the first max_workflow_iterations
        # per workflow (combined across the primary run + additional runs) so
        # deep cells (rtk/repowise with 60-80 iterations per run, several runs
        # per cell) stay under max_testcase_chars. The prefix captures the
        # representative tool-call/result pattern; we record how many were
        # omitted.
        wf_cap = settings["max_workflow_iterations"]
        primary_omitted = 0
        if iterations is not None:
            if len(iterations) <= wf_cap:
                kept_count = len(iterations)
            else:
                primary_omitted = len(iterations) - wf_cap
                iterations = iterations[:wf_cap]
                kept_count = wf_cap
        else:
            kept_count = 0
        omitted_count = primary_omitted
        additional_runs = []
        for run_index, run in enumerate((prompt_row.get("run_log") or [])[1:], start=1):
            run_prompt = dict(prompt_row)
            run_prompt["run_log"] = [run]
            run_iterations = loader(
                str(row.get("model_root") or ""),
                str(row.get("tool_id") or ""),
                run_prompt,
            )
            run_omitted = 0
            if run_iterations is not None and kept_count < wf_cap:
                take = run_iterations[: wf_cap - kept_count]
                run_omitted = len(run_iterations) - len(take)
                run_iterations = take
                kept_count += len(take)
            elif run_iterations is not None:
                run_omitted = len(run_iterations)
                run_iterations = []
            omitted_count += run_omitted
            additional_runs.append({
                "run_index": run_index,
                "transcript": {
                    "path": _transcript_path(row, run),
                    "status": "missing" if run_iterations is None else "available",
                },
                "iterations": [] if run_iterations is None else [
                    _normalize_iteration(iteration, settings)
                    for iteration in run_iterations
                ],
                "omitted_iterations": run_omitted,
            })
        metrics_source = row.get("cells") or {}
        metrics = {
            output_name: _compact_strings(metrics_source.get(source_name), settings)
            for source_name, output_name in _METRIC_KEYS
        }
        cost_usd = metrics_source.get("cost_usd")
        if cost_usd is None:
            cost_usd = (prompt_row.get("cost_usd") or {}).get("mean")
        metrics["cost_usd"] = _compact_strings(cost_usd, settings)
        workflow = {
            "tool": _compact_strings(row.get("tool"), settings),
            "version": _compact_strings(row.get("version"), settings),
            "tool_id": _compact_strings(row.get("tool_id"), settings),
            "model": _compact_strings(row.get("model"), settings),
            "metrics": metrics,
            "transcript": {
                "path": path,
                "status": "missing" if iterations is None else "available",
            },
            "iterations": [] if iterations is None else [
                _normalize_iteration(iteration, settings) for iteration in iterations
            ],
        }
        if primary_omitted:
            workflow["omitted_primary_iterations"] = primary_omitted
        if omitted_count:
            workflow["omitted_iterations_total"] = omitted_count
        if additional_runs:
            workflow["additional_runs"] = additional_runs
        group["workflows"].append(workflow)

    testcases = []
    for testcase in sorted(grouped):
        group = grouped[testcase]
        prompt_texts = sorted(group["prompt_texts"])
        workflows = sorted(
            group["workflows"],
            key=lambda workflow: (
                str(workflow.get("tool") or ""),
                str(workflow.get("version") or ""),
                str(workflow.get("tool_id") or ""),
                str(workflow.get("model") or ""),
                canonical_json(workflow),
            ),
        )
        normalized_testcase = {
            "testcase": testcase,
            "prompt_text": _compact_strings(prompt_texts[0], settings) if len(prompt_texts) == 1 else None,
            "prompt_variants": _compact_strings(prompt_texts, settings),
            "workflows": workflows,
        }
        testcase_chars = len(canonical_json(normalized_testcase))
        if testcase_chars > settings["max_testcase_chars"]:
            raise ValueError(
                f"normalized evidence for testcase {testcase!r} is {testcase_chars} characters; "
                f"exceeds max_testcase_chars={settings['max_testcase_chars']}"
            )
        testcases.append(normalized_testcase)

    return {
        "schema_version": SCHEMA_VERSION,
        "compaction": {"version": COMPACTION_VERSION, "settings": settings},
        "testcases": testcases,
    }


def fingerprint_input(
    evidence: Mapping[str, Any],
    analyst_model: str,
    *,
    compaction_settings: Mapping[str, int] | None = None,
) -> str:
    """Return a content hash for evidence and every analyst contract input."""
    if compaction_settings is None:
        evidence_compaction = evidence.get("compaction") or {}
        settings = _compaction_settings(evidence_compaction.get("settings"))
    else:
        settings = _compaction_settings(compaction_settings)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "compaction_version": COMPACTION_VERSION,
        "compaction_settings": settings,
        "analyst_model": analyst_model,
        "evidence": evidence,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _workflow_label(workflow: Mapping[str, Any]) -> str:
    return f"{workflow.get('tool_id') or workflow.get('tool')} | {workflow.get('model')}"


def parse_analyst_json(text: str) -> dict[str, Any]:
    """Parse the analyst's JSON object, tolerating one Markdown code fence."""
    value = (text or "").strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```", value, re.DOTALL | re.IGNORECASE
    )
    if fenced:
        value = fenced.group(1)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"analyst returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise TypeError("analyst output must be one JSON object")
    return parsed


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"analyst finding field {field!r} must be non-empty text")
    return redact_secrets(value.strip())


def validate_finding(value: Mapping[str, Any], testcase: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one finding against exact testcase/workflow coverage."""
    if not isinstance(value, Mapping):
        raise TypeError("analyst finding must be an object")
    testcase_id = str(testcase.get("testcase") or "")
    if value.get("testcase") != testcase_id:
        raise ValueError(f"analyst finding testcase must be {testcase_id!r}")
    labels = {_workflow_label(workflow) for workflow in testcase.get("workflows") or []}
    winner = _required_text(value.get("winner"), "winner")
    if winner not in labels:
        raise ValueError(f"unknown winning workflow {winner!r}")
    totals = {
        _workflow_label(workflow): workflow.get("metrics", {}).get("total")
        for workflow in testcase.get("workflows") or []
        if isinstance(workflow.get("metrics", {}).get("total"), (int, float))
        and not isinstance(workflow.get("metrics", {}).get("total"), bool)
    }
    if not totals:
        raise ValueError("testcase has no numeric total-token metrics")
    minimum = min(totals.values())
    expected_winners = {label for label, total in totals.items() if total == minimum}
    if winner not in expected_winners:
        raise ValueError(
            f"winner must have the lowest total-token metric: {sorted(expected_winners)!r}"
        )
    costs = value.get("workflow_costs")
    if not isinstance(costs, list):
        raise TypeError("analyst finding workflow_costs must be a list")
    normalized_costs = []
    seen = set()
    for item in costs:
        if not isinstance(item, Mapping):
            raise TypeError("each workflow_costs item must be an object")
        workflow = _required_text(item.get("workflow"), "workflow")
        if workflow in seen:
            raise ValueError(f"duplicate workflow cost for {workflow!r}")
        seen.add(workflow)
        normalized_costs.append({
            "workflow": workflow,
            "explanation": _required_text(item.get("explanation"), "explanation"),
        })
    expected = labels - {winner}
    if seen != expected:
        raise ValueError(
            "workflow coverage mismatch: expected "
            f"{sorted(expected)!r}, got {sorted(seen)!r}"
        )
    return {
        "testcase": testcase_id,
        "winner": winner,
        "why_winner": _required_text(value.get("why_winner"), "why_winner"),
        "workflow_costs": normalized_costs,
    }


def make_cache(
    fingerprint: str,
    analyst_model: str,
    analyst_provider: str | None,
    findings: list[Mapping[str, Any]],
    transcript_paths: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "fingerprint": fingerprint,
        "analyst_model": analyst_model,
        "analyst_provider": analyst_provider,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "transcript_paths": sorted(set(transcript_paths)),
        "findings": [dict(finding) for finding in findings],
    }


def load_matching_cache(path: str, fingerprint: str, analyst_model: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as stream:
            cached = json.load(stream)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cached, dict):
        return None
    if (
        cached.get("schema_version") != SCHEMA_VERSION
        or cached.get("prompt_version") != PROMPT_VERSION
        or cached.get("fingerprint") != fingerprint
        or cached.get("analyst_model") != analyst_model
        or not isinstance(cached.get("findings"), list)
    ):
        return None
    return cached


def write_cache(path: str, cache: Mapping[str, Any]) -> None:
    """Atomically replace the cache without risking the last valid artifact."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".result-summary-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(cache, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # mkstemp defaults to 0600. Artifacts generated by the root-running
        # container must remain readable from the host checkout.
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def build_analyst_prompt(testcase: Mapping[str, Any]) -> str:
    labels = [_workflow_label(workflow) for workflow in testcase.get("workflows") or []]
    contract = {
        "testcase": testcase.get("testcase"),
        "winner": f"exactly one of: {labels}",
        "why_winner": "trace-grounded qualitative explanation",
        "workflow_costs": [
            {"workflow": "every non-winning workflow exactly once", "explanation": "where it spent more tokens and why"}
        ],
    }
    return (
        "You are analyzing a code-query benchmark. Return JSON only, with exactly the supplied schema. "
        "Choose the workflow with the lowest total-token metric as winner. Explain why using concrete "
        "recorded tool calls/results: search scope, focused reads, retries, pagination, empty/failing "
        "probes, broad graph/source payloads, or repeated refinement. Leave numeric values to the table. "
        "Do not infer correctness or completeness. workflow_costs MUST contain every non-winning label "
        "exactly once, including workflows whose transcript is missing; for those, say trace evidence is "
        "unavailable and limit the explanation to the recorded metrics.\n"
        f"OUTPUT SCHEMA:\n{json.dumps(contract, ensure_ascii=False)}\n"
        f"EVIDENCE:\n{canonical_json(testcase)}"
    )


def build_analyst_cmd(model: str, *, pi_bin: str | None = None) -> list[str]:
    return [
        pi_bin or os.environ.get("PI_BIN", "pi"),
        "--model", model,
        "--mode", "json",
        "--print",
        "--no-session",
        "--no-tools",
        "--thinking", "off",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
    ]


def parse_pi_response(stdout: str) -> tuple[str, dict[str, Any]]:
    last = None
    for line in (stdout or "").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "message_end":
            continue
        message = record.get("message") or {}
        if message.get("role") == "assistant":
            last = message
    if last is None:
        raise ValueError("pi returned no assistant message")
    if last.get("stopReason") in {"error", "aborted"}:
        raise ValueError(last.get("errorMessage") or f"pi stopped with {last.get('stopReason')}")
    content = last.get("content")
    if isinstance(content, list):
        # Reasoning models may emit separate thinking blocks even when asked
        # for JSON. Only the visible final text is part of the cache contract.
        final = "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "text"
        ).strip()
    else:
        final = text_content(content)
    if not final.strip():
        raise ValueError("pi returned an empty analyst answer")
    return final, {"model": last.get("model"), "provider": last.get("provider")}


def _transcript_paths(evidence: Mapping[str, Any]) -> list[str]:
    paths = []
    for testcase in evidence.get("testcases") or []:
        for workflow in testcase.get("workflows") or []:
            transcripts = [workflow.get("transcript") or {}]
            transcripts.extend(
                run.get("transcript") or {}
                for run in workflow.get("additional_runs") or []
            )
            paths.extend(
                transcript["path"]
                for transcript in transcripts
                if transcript.get("path")
            )
    return paths


def ensure_summary(
    evidence: Mapping[str, Any],
    cache_path: str,
    *,
    analyst_model: str | None = None,
    force: bool = False,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Load a matching cache or analyze each testcase and atomically cache it."""
    model = analyst_model or os.environ.get("PI_SUMMARY_MODEL") or os.environ.get("PI_MODEL")
    if not model and not force:
        try:
            with open(cache_path, encoding="utf-8") as stream:
                model = json.load(stream).get("analyst_model")
        except (AttributeError, OSError, json.JSONDecodeError):
            model = None
    if not model:
        raise ValueError("no analyst model: set PI_SUMMARY_MODEL or PI_MODEL")
    fingerprint = fingerprint_input(evidence, model)
    if not force:
        cached = load_matching_cache(cache_path, fingerprint, model)
        if cached is not None:
            testcase_by_id = {
                testcase.get("testcase"): testcase
                for testcase in evidence.get("testcases") or []
            }
            try:
                cached["findings"] = [
                    validate_finding(finding, testcase_by_id[finding.get("testcase")])
                    for finding in cached["findings"]
                ]
                if set(testcase_by_id) != {
                    finding["testcase"] for finding in cached["findings"]
                }:
                    raise ValueError("cached findings do not cover every testcase")
            except (KeyError, TypeError, ValueError):
                cached = None
            if cached is not None:
                return cached

    invoke = runner or subprocess.run
    timeout = int(os.environ.get("PI_SUMMARY_TIMEOUT", "600"))
    findings = []
    provider = None
    reported_model = None
    for testcase in evidence.get("testcases") or []:
        prompt = build_analyst_prompt(testcase)
        command = build_analyst_cmd(model)
        finding = None
        meta = {}
        # Up to 4 attempts: on a large testcase the analyst can return valid
        # JSON with an incorrect winner, duplicate workflow costs, or a
        # missing label; each retry appends the exact validation error so the
        # model can correct itself. Giving up early used to fail the whole
        # report on the first schema slip.
        for attempt in range(4):
            try:
                # Large normalized traces exceed Linux's per-argument limit;
                # pi's print mode accepts the prompt on standard input.
                completed = invoke(
                    command,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"analyst timed out after {timeout}s for {testcase.get('testcase')}"
                ) from exc
            if completed.returncode != 0:
                diagnostic = (completed.stderr or "").strip()[-1000:]
                raise RuntimeError(f"analyst exited {completed.returncode}: {diagnostic}")
            answer, meta = parse_pi_response(completed.stdout)
            try:
                finding = validate_finding(parse_analyst_json(answer), testcase)
                break
            except (TypeError, ValueError) as exc:
                if attempt == 3:
                    raise
                prompt += (
                    "\nYOUR PREVIOUS JSON FAILED VALIDATION:\n"
                    f"{answer}\nVALIDATION ERROR: {exc}\n"
                    "Return one corrected JSON object only. Preserve every exact "
                    "workflow label (each exactly once) and pick the winner with "
                    "the lowest total-token metric."
                )
        if finding is None:
            raise RuntimeError(f"analyst produced no finding for {testcase.get('testcase')}")
        findings.append(finding)
        provider = meta.get("provider") or provider
        reported_model = meta.get("model") or reported_model
    cache = make_cache(
        fingerprint, model, provider,
        findings, _transcript_paths(evidence),
    )
    if reported_model:
        cache["reported_model"] = reported_model
    write_cache(cache_path, cache)
    return cache

# LLM-authored Result Summary Implementation Plan

> **For AI coding agents:** implement this plan task-by-task. Use test-first development for production code. Keep changes focused and run the verification command listed in each task. Do not commit unless the user explicitly asks.

**Goal:** Generate the HTML report's Result summary with an LLM that analyzes canonical per-iteration traces, caching the validated analysis until its inputs change.

**Architecture:** Add a focused `pi_summary.py` module for evidence normalization, fingerprinting, per-testcase pi invocation, response validation, and atomic caching. Keep `pi_report.py` responsible for data loading and escaped HTML rendering, and have both direct report generation and `bench_pi.py` aggregation use the same cached-analysis path.

**Tech Stack:** Python 3 standard library, existing pi JSON-mode CLI, existing `pi_stream.py` parser, canonical `summary.json` and transcript JSON artifacts.

---

### Task 1: Define normalized evidence and fingerprinting

**Objective:** Convert report rows and transcript iterations into deterministic analyst input whose hash changes with every relevant source change.

**Files:**
- Create: `pi_summary.py`
- Read integration shapes from: `pi_report.py:108-206`
- Temporary test: `/tmp/test_pi_summary.py` (delete after verification)

**Steps:**
1. Write a temporary `unittest` that constructs two synthetic report rows and asserts the normalized evidence contains metrics, transcript provenance, calls/results, missing-trace status, and deterministic prompt grouping.
2. Add an assertion that changing a result body changes the SHA-256 fingerprint while dictionary insertion order does not.
3. Run `python3 /tmp/test_pi_summary.py`; expect failures because `pi_summary` does not exist.
4. Implement constants for schema/prompt/compaction versions, deterministic JSON serialization, trace extraction, result compaction with explicit original-length/truncated metadata, and `fingerprint_input()`.
5. Run the temporary test; expect all assertions to pass.
6. Run `python3 -m py_compile pi_summary.py`.

### Task 2: Validate analyst JSON and cache it atomically

**Objective:** Accept only complete, plain-text findings for the current testcase set and safely persist their provenance.

**Files:**
- Modify: `pi_summary.py`
- Temporary test: `/tmp/test_pi_summary.py`

**Steps:**
1. Extend the temporary test with valid output, malformed JSON, duplicate/missing testcase, unknown testcase, missing winner/explanation, and HTML-bearing text cases.
2. Assert malformed/incomplete output is rejected and valid output is normalized without allowing analyst-supplied HTML structure.
3. Add cache tests proving matching schema/model/fingerprint loads, stale cache does not, and atomic replacement leaves a valid JSON file.
4. Run the test and confirm the new assertions fail for missing functions.
5. Implement JSON-object extraction (including an optional fenced wrapper), strict findings validation, `load_matching_cache()`, and atomic `write_cache()` using a temporary file plus `os.replace()`.
6. Run the temporary test and confirm it passes.

### Task 3: Invoke pi as a tool-free report analyst

**Objective:** Generate one validated structured analysis on cache miss without exposing benchmark tools or adding analyst usage to benchmark metrics.

**Files:**
- Modify: `pi_summary.py`
- Reuse parsing from: `pi_stream.py:17-66`
- Temporary fixtures: `/tmp/fake-pi`, `/tmp/test_pi_summary.py`

**Steps:**
1. Add a fake-pi test that records argv and emits a minimal pi NDJSON session plus assistant final answer containing valid analyst JSON.
2. Assert the command uses `PI_SUMMARY_MODEL`, falls back to `PI_MODEL`, runs `--mode json`, `-p`, and `--no-session`, and provides no code-query tool allowlist.
3. Add a cache-hit assertion proving the fake pi is not called a second time; add malformed-output and nonzero-exit assertions.
4. Run the test and confirm these assertions fail before implementation.
5. Implement prompt construction, one subprocess invocation per testcase with a bounded `PI_SUMMARY_TIMEOUT`, final-answer extraction from pi NDJSON, validation, provenance capture, and `ensure_summary()` cache orchestration. Use pi's explicit `--no-tools`, `--no-extensions`, `--no-skills`, `--no-prompt-templates`, and `--no-context-files` controls.
6. Run the temporary test and confirm all assertions pass.

### Task 4: Replace rule-based HTML prose with cached LLM findings

**Objective:** Render the validated findings and remove fixed workflow sentence generation from the report path.

**Files:**
- Modify: `pi_report.py:268-385`
- Modify: `pi_report.py:547-576`
- Temporary test: `/tmp/test_pi_report_summary.py`

**Steps:**
1. Write a temporary renderer test with analyst text containing `<`, `>`, and `&`; assert one finding per testcase, escaped prose, preserved collapsible section, and no fixed `Saves context by`/`Spends context on` template.
2. Run it and confirm failure against the current renderer.
3. Remove `_trace_evidence()`, `_workflow_summary()`, and `_join_phrases()` from the summary path.
4. Change `_results_summary_html()` to accept validated findings and render winner, explanation, and per-tool cost explanations as escaped text.
5. Have `render()` build evidence, call `pi_summary.ensure_summary()`, render an explicit unavailable notice on failure, and accept `force_summary=False` plus an injectable analyst runner for probes.
6. Add `--force-summary` to the CLI and forward it to `render()`.
7. Run the temporary renderer test and `python3 -m py_compile pi_report.py pi_summary.py`.

### Task 5: Wire model selection through benchmark orchestration

**Objective:** Ensure normal aggregation has a default analyst model while direct cache-hit rendering remains credential-free.

**Files:**
- Modify: `bench_pi.py:121-130`
- Modify: `docker/run-bench.sh:15-27`
- Modify: `docker/Dockerfile:105-110`
- Modify: `docker/.env.example`
- Modify: `README.md:63-95`

**Steps:**
1. Add a temporary probe around `bench_pi` configuration showing `PI_SUMMARY_MODEL` wins and the benchmark model is used when it is absent.
2. Implement explicit analyst-model forwarding to `pi_report.render()` without changing benchmark rows or metrics.
3. Document `PI_SUMMARY_MODEL` as optional and `PI_MODEL` as fallback; ensure the Docker run path exports these variables when present.
4. Document cache location, invalidation, `--force-summary`, and unavailable behavior.
5. Run shell syntax checks: `bash -n docker/run-bench.sh`.

### Task 6: Verify cache lifecycle against fixtures

**Objective:** Prove cache miss, hit, invalidation, malformed-response rejection, and failure behavior end to end.

**Files:**
- Temporary only: `/tmp/pi-summary-fixture/`, `/tmp/fake-pi`

**Steps:**
1. Build a minimal artifact tree containing two tools, one testcase, summaries, and contrasting transcripts.
2. Render with fake pi and confirm `result-summary.json` and `report.html` are created.
3. Record fake-pi call count, render unchanged, and confirm the count does not increase.
4. Modify a transcript result, render again, and confirm the fingerprint and call count change.
5. Emit malformed analyst JSON and confirm no matching cache is written and the report contains `LLM analysis unavailable`.
6. Remove all temporary fixture/test files.

### Task 7: Regenerate and inspect the canonical report

**Objective:** Produce the requested working artifact from real benchmark data and verify it is trace-specific.

**Files:**
- Generate: `artifacts/result-summary.json`
- Regenerate: `artifacts/report.html`

**Steps:**
1. Export a usable `PI_SUMMARY_MODEL` or rely on `PI_MODEL`, with existing provider credentials.
2. Record checksums for all benchmark `summary.json` and `runs.json` files.
3. Run `python3 pi_report.py --artifacts artifacts --out artifacts/report.html --force-summary`.
4. Validate the cache schema, fingerprint, analyst provenance, and exact testcase coverage.
5. Inspect each finding against the corresponding iterations to ensure it names concrete observed searches, reads, retries, graph payloads, or failures rather than generic tool boilerplate.
6. Verify `artifacts/report.html` contains a collapsible Result summary, one finding per testcase, escaped prose, and no legacy fixed templates.
7. Recompute benchmark artifact checksums and confirm no `summary.json` or `runs.json` changed.
8. Run `git diff --check`, `python3 -m py_compile bench_pi.py pi_report.py pi_summary.py`, and `bash -n docker/run-bench.sh`.
9. Report any credential/model blocker honestly; do not fabricate an LLM result.

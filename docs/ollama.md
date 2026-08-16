# Running benchmark cells against Ollama

pi 0.84.1 has **no built-in Ollama discovery** (unlike cloud providers, where
setting an env key is enough). To use a local/remote Ollama server the provider
must be declared in `models.json`:

- native backend: `~/.pi/agent/models.json` (or `$PI_CODING_AGENT_DIR/models.json`)
- docker backend (one-shot per cell): the runner (`pi_runner.py`) generates a
  private temp `models.json` and mounts it into the container
  (`/root/.pi/agent/models.json:ro`), so the user's config is never touched.

`docker/ollama-models.sh` writes that file from env; the in-image entrypoint
(`pi-bench-run`) also calls it when `PI_PROVIDER=ollama` or `OLLAMA_BASE_URL`
is set.

## Model selection: just PI_MODEL

There is **no separate `OLLAMA_MODELS` setting**. The model id comes from the
benchmark model itself (`PI_MODEL`, or `--model`/`--model-preset`):

- OpenRouter: `PI_MODEL=openrouter/deepseek-v4-flash-0731`
- Ollama:     `PI_MODEL=ollama/qwen2.5-coder:0.5b` (the part after `ollama/`
  is what `ollama list` shows)

## Provider selection: PI_PROVIDER (when both keys exist)

If both `OPENROUTER_API_KEY` and `OLLAMA_BASE_URL` are configured, set
`PI_PROVIDER` in `docker/.env` to choose:

```bash
PI_PROVIDER=ollama      # use the ollama provider
PI_PROVIDER=openrouter  # use OpenRouter (default when unset)
```

CLI flags still win over it: `--provider ollama` / `--model-preset ollama`
override `PI_PROVIDER`.

## Quick start

```bash
# 1) pick a model already pulled locally
ollama list

# 2) declare it in docker/.env (see docker/.env.example) — no OLLAMA_MODELS
PI_PROVIDER=ollama
PI_MODEL=ollama/qwen2.5-coder:0.5b
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1

# 3) native backend (pi runs on the host; needs a models.json for ollama)
set -a; . docker/.env; set +a
python3 bench_pi.py --model-preset ollama --tools grep,rtk --runs 1

# or run the ollama-models.sh generator, then any native run works
docker/ollama-models.sh   # writes ~/.pi/agent/models.json from PI_MODEL
python3 bench_pi.py --model ollama/qwen2.5-coder:0.5b --backend native

# 4) docker backend (self-contained image; --network host reaches the host)
python3 bench_pi.py --model ollama/qwen2.5-coder:0.5b --backend docker --runs 1

# 5) in-image harness (recommended; ONLY artifacts/ is mounted)
docker run --rm -it \
  --network host \
  -e PI_PROVIDER=ollama \
  -e PI_MODEL=ollama/qwen2.5-coder:0.5b \
  -e OLLAMA_BASE_URL=http://127.0.0.1:11434/v1 \
  -v "$(pwd)/artifacts:/workspace/artifacts" \
  --entrypoint /usr/local/bin/pi-bench-run \
  agent-codebase-bench \
  --model-preset ollama --backend native --runs 1
```

## How it works

- `pi_runner._pi_args`: for `provider == "ollama"` emits
  `--provider ollama --model <id>` instead of `--model ollama/<id>`.
- `pi_runner.build_cmd` (docker backend): adds `--network host` (so the
  one-shot container sees `127.0.0.1:11434` on the host), points
  `PI_CODING_AGENT_DIR` at a private per-run dir in the container, and mounts
  the generated `models.json` there read-only. `OLLAMA_BASE_URL` is NOT passed
  as env in this path — the mount is the single source of truth (passing it
  would re-trigger the entrypoint's generator onto the RO mount and fail).
- `pi_runner._write_ollama_models_json(model_id)`: emits `{"providers":
  {"ollama": {"baseUrl":..., "api":"openai-completions", "apiKey":"ollama",
  "compat":{...}, "models":[{"id": <model_id>}]}}}` — the model id is read
  from the benchmark model, so no separate `OLLAMA_MODELS` list is needed.
- `bench_pi.py`: `PI_PROVIDER` from the environment selects the provider when
  both keys exist; `--provider` / `--model-preset` still win.
- The recorded `provider` label in usage/transcripts is `ollama` (pi's stream
  reports `"provider":"ollama"` without any mapping).

## Notes / gotchas

- **Cost is zero** for local ollama — `estimated_cost_usd` will be 0; report
  cost columns compare fairly only against other local runs (the tables render
  cost=0, which is correct).
- **Context window**: ollama models default to pi's 128K `contextWindow` in the
  generated models.json. If your model has a smaller context (e.g. 8K), edit
  `models.json` directly to add a `contextWindow` entry.
- **`compat.supportsDeveloperRole=false`** is set because Ollama's
  OpenAI-compatible endpoint does not accept the `developer` role pi uses for
  reasoning models; the system prompt is sent as `system` instead.
- Remote OpenAI-compatible endpoints (vLLM, LM Studio) can be targeted the same
  way by pointing `OLLAMA_BASE_URL` at them; set `OLLAMA_API_KEY` if they
  require one.
- The `README.md` and `docs/pi-migration.md` "Run it" sections remain valid for
  OpenRouter; use the command above when `provider == "ollama"`.
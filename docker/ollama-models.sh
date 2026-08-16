#!/usr/bin/env bash
# Generate pi's models.json for the ollama provider from environment variables.
#
# pi 0.84.1 has no built-in ollama discovery — the provider must be declared in
# ~/.pi/agent/models.json (or $PI_CODING_AGENT_DIR/models.json). This script
# writes exactly that file from env, so both the native and docker backends can
# point pi at a local/remote ollama server without hand-editing config.
#
# Env:
#   OLLAMA_BASE_URL   base URL of the ollama OpenAI-compatible endpoint
#                     (default http://127.0.0.1:11434/v1)
#   PI_MODEL          the benchmark model id to declare, e.g.
#                     "ollama/qwen2.5-coder:0.5b" (the ollama/ prefix is
#                     stripped; the id after it is what ollama serves)
#   OLLAMA_API_KEY    optional API key (ollama ignores it; kept for parity with
#                     remote OpenAI-compatible endpoints behind auth).
#
# Writes to PI_MODELS_JSON (default: $PI_CODING_AGENT_DIR/models.json, falling
# back to ~/.pi/agent/models.json), which is exactly where pi reads it.
set -euo pipefail

BASE_URL="${OLLAMA_BASE_URL:-http://127.0.0.1:11434/v1}"
API_KEY="${OLLAMA_API_KEY:-ollama}"

if [[ -n "${PI_CODING_AGENT_DIR:-}" ]]; then
    OUT="${PI_MODELS_JSON:-$PI_CODING_AGENT_DIR/models.json}"
else
    OUT="${PI_MODELS_JSON:-$HOME/.pi/agent/models.json}"
fi

mkdir -p "$(dirname "$OUT")"

# Model id comes from PI_MODEL (strip any "ollama/" prefix).
MODEL="${PI_MODEL:-}"
MODEL="${MODEL#ollama/}"

if [[ -n "$MODEL" ]]; then
    cat > "$OUT" <<EOF
{
  "providers": {
    "ollama": {
      "baseUrl": "$BASE_URL",
      "api": "openai-completions",
      "apiKey": "$API_KEY",
      "compat": {
        "supportsDeveloperRole": false,
        "supportsReasoningEffort": false
      },
      "models": [
        { "id": "$MODEL" }
      ]
    }
  }
}
EOF
else
    cat > "$OUT" <<EOF
{
  "providers": {
    "ollama": {
      "baseUrl": "$BASE_URL",
      "api": "openai-completions",
      "apiKey": "$API_KEY",
      "compat": {
        "supportsDeveloperRole": false,
        "supportsReasoningEffort": false
      }
    }
  }
}
EOF
fi

echo "[ollama-models] wrote $OUT (baseUrl=$BASE_URL model=$MODEL)"
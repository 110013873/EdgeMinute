#!/usr/bin/env bash
#
# start_edgeminute.sh — 密纪 EdgeMinute single-service launcher (systemd ExecStart)
#
# Runs BOTH the local LLM (llama.cpp / Qwen3) and the ASR web app under a single
# systemd unit (edgeminute.service). The LLM is launched in the background; the
# web app runs as this script's main (foreground) process so systemd tracks it.
# A trap tears the LLM down when this wrapper exits, and because systemd's
# default KillMode=control-group SIGTERMs the whole cgroup on `stop`/`restart`,
# the background LLM is always cleaned up with the service — no orphan process.
#
# systemd's ExecStart does NOT source the virtualenv's activate script, so the
# LD_PRELOAD fix we normally put in `activate` (working around the sklearn /
# PyTorch libgomp static-TLS clash on aarch64 — otherwise CAM++'s ClusterBackend
# fails to import) never takes effect under systemd. This wrapper re-applies it
# at launch: it locates sklearn's private libgomp copy (the filename carries a
# hash and varies per environment, so we never hardcode it) and preloads it.
#
# Paths / config come from the environment (set by install.sh / the systemd
# unit), with sensible fallbacks so the script also works when run by hand:
#   EDGEMINUTE_VENV     — virtualenv dir        (default: $HOME/funasr-env)
#   EDGEMINUTE_HOME     — project dir (app.py)  (default: this script's parent dir)
#   EDGEMINUTE_LLM      — 1 to launch the local LLM, 0/empty to skip it
#   EDGEMINUTE_LLM_BIN  — path to llama-server
#   EDGEMINUTE_LLM_MODEL— path to the .gguf model
#   EDGEMINUTE_LLM_CTX  — context length        (default: 32768)
#   EDGEMINUTE_LLM_PORT — llama-server port      (default: 8080)
#
set -euo pipefail

VENV_DIR="${EDGEMINUTE_VENV:-$HOME/funasr-env}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${EDGEMINUTE_HOME:-$(dirname "$SCRIPT_DIR")}"

PY="${VENV_DIR}/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "[edgeminute] python not found at $PY — set EDGEMINUTE_VENV to your virtualenv" >&2
  exit 1
fi

# ---- optionally launch the local LLM in the background ----
LLM_PID=""
cleanup() {
  # Tear down the LLM when the web app exits (belt-and-suspenders alongside
  # systemd's cgroup kill on stop/restart).
  if [[ -n "$LLM_PID" ]] && kill -0 "$LLM_PID" 2>/dev/null; then
    kill "$LLM_PID" 2>/dev/null || true
    wait "$LLM_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if [[ "${EDGEMINUTE_LLM:-0}" == "1" ]]; then
  LLM_BIN="${EDGEMINUTE_LLM_BIN:-}"
  LLM_MODEL="${EDGEMINUTE_LLM_MODEL:-}"
  LLM_CTX="${EDGEMINUTE_LLM_CTX:-32768}"
  LLM_PORT="${EDGEMINUTE_LLM_PORT:-8080}"
  if [[ -x "$LLM_BIN" && -f "$LLM_MODEL" ]]; then
    echo "[edgeminute] starting local LLM: $LLM_BIN (port $LLM_PORT, ctx $LLM_CTX)" >&2
    "$LLM_BIN" \
      -m "$LLM_MODEL" \
      -ngl 99 -c "$LLM_CTX" -b 4096 --ubatch-size 1024 \
      --host 0.0.0.0 --port "$LLM_PORT" &
    LLM_PID=$!
  else
    echo "[edgeminute] LLM enabled but binary/model missing — skipping LLM launch" >&2
    echo "[edgeminute]   bin=$LLM_BIN model=$LLM_MODEL" >&2
  fi
fi

# ---- libgomp preload fix (aarch64 sklearn/torch static-TLS clash) ----
SITE_PACKAGES="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
LIBGOMP_PATH="$(find "${SITE_PACKAGES}/scikit_learn.libs" -name 'libgomp*.so*' 2>/dev/null | head -n1)"
if [[ -z "$LIBGOMP_PATH" ]]; then
  LIBGOMP_PATH="$(find /usr/lib -name 'libgomp.so.1' 2>/dev/null | head -n1)"
fi
if [[ -n "$LIBGOMP_PATH" ]]; then
  export LD_PRELOAD="${LIBGOMP_PATH}${LD_PRELOAD:+:$LD_PRELOAD}"
fi

# ---- run the web app as the main (foreground) process ----
# NOTE: we deliberately do NOT `exec` here — running the app as a child lets the
# EXIT trap above reap the background LLM if the app returns on its own. systemd
# still tracks this wrapper as the unit's main PID (Type=simple), and its
# cgroup-wide SIGTERM on stop/restart tears down both processes regardless.
cd "$APP_DIR"
"$PY" "${APP_DIR}/app.py"

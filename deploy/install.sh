#!/usr/bin/env bash
#
# install.sh — 密纪 EdgeMinute one-shot installer + auto-start
#
# Installs the ASR environment and the local LLM, then registers and starts
# systemd services so EdgeMinute comes up automatically on boot.
#
# By default this installs BOTH the ASR web service AND a local LLM
# (llama.cpp + Qwen3), and starts both with auto-start on boot.
#
# Usage:
#   sudo ./deploy/install.sh                  # ASR + local LLM + web, auto-start (default)
#   sudo ./deploy/install.sh --no-llm         # ASR + web only, use an external LLM_BASE_URL
#   sudo ./deploy/install.sh --skip-setup     # skip env build, just (re)register services
#   sudo ./deploy/install.sh --no-start       # install units but don't start now
#   sudo ./deploy/install.sh --port 8899      # override web port
#
# Key options:
#   --no-llm          do NOT build/serve the local LLM (use an external LLM_BASE_URL)
#   --with-llm        force-enable the local LLM (this is the default; kept for clarity)
#   --skip-setup      skip setup_funasr.sh / LLM build (assume they already exist)
#   --no-start        enable services on boot but do not start them right now
#   --no-enable       start now but do not enable on boot
#   --venv DIR        virtualenv dir            (default: $HOME/funasr-env)
#   --user NAME       systemd service User=     (default: the invoking user)
#   --port N          web service port          (default: 8899)
#   --llm-port N      LLM server port           (default: 8080)
#   --llm-ctx N       LLM context length        (default: 32768)
#   --llm-quant TAG   LLM GGUF quantization tag (default: Q4_K_M)
#   -h | --help       show this help
#
set -euo pipefail

# ---------- resolve paths ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SYSTEMD_SRC="${SCRIPT_DIR}/systemd"
SYSTEMD_DST="/etc/systemd/system"

# ---------- defaults ----------
# The user the service runs as: prefer the human who ran sudo, not root.
DEFAULT_USER="${SUDO_USER:-$(id -un)}"
SERVICE_USER="$DEFAULT_USER"
USER_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"; USER_HOME="${USER_HOME:-$HOME}"

VENV_DIR="${VENV_DIR:-${USER_HOME}/funasr-env}"
WEB_PORT=8899
LLM_PORT=8080
LLM_CTX=32768
LLM_QUANT="Q4_K_M"
WITH_LLM=1
SKIP_SETUP=0
DO_START=1
DO_ENABLE=1
LLAMACPP_DIR="${LLAMACPP_DIR:-${USER_HOME}/llama.cpp}"
MODELS_DIR="${MODELS_DIR:-${USER_HOME}/models}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ---------- parse args ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-llm)   WITH_LLM=1; shift ;;
    --no-llm|--skip-llm) WITH_LLM=0; shift ;;
    --skip-setup) SKIP_SETUP=1; shift ;;
    --no-start)   DO_START=0; shift ;;
    --no-enable)  DO_ENABLE=0; shift ;;
    --venv)       VENV_DIR="$2"; shift 2 ;;
    --user)       SERVICE_USER="$2"; shift 2 ;;
    --port)       WEB_PORT="$2"; shift 2 ;;
    --llm-port)   LLM_PORT="$2"; shift 2 ;;
    --llm-ctx)    LLM_CTX="$2"; shift 2 ;;
    --llm-quant)  LLM_QUANT="$2"; shift 2 ;;
    --llamacpp-dir) LLAMACPP_DIR="$2"; shift 2 ;;
    --models-dir) MODELS_DIR="$2"; shift 2 ;;
    -h|--help)    grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) error "未知参数: $1"; exit 1 ;;
  esac
done

# ---------- preflight ----------
if [[ "$(id -u)" -ne 0 ]]; then
  error "注册 systemd 服务需要 root 权限，请用 sudo 运行。"
  exit 1
fi
if ! command -v systemctl &>/dev/null; then
  error "未找到 systemctl，本安装脚本依赖 systemd。"
  exit 1
fi

info "项目目录:   ${PROJECT_DIR}"
info "虚拟环境:   ${VENV_DIR}"
info "服务用户:   ${SERVICE_USER}"
info "Web 端口:   ${WEB_PORT}"
[[ "$WITH_LLM" -eq 1 ]] && info "LLM:        本地部署（llama.cpp + Qwen3，端口 ${LLM_PORT}）" || info "LLM:        跳过（--no-llm，使用外部 LLM_BASE_URL）"

# ---------- step 1: ASR environment ----------
if [[ "$SKIP_SETUP" -eq 1 ]]; then
  info "步骤 1: 已指定 --skip-setup，跳过 ASR 环境安装"
else
  info "步骤 1: 安装 ASR 环境 (setup_funasr.sh)"
  sudo -u "$SERVICE_USER" VENV_DIR="$VENV_DIR" bash "${SCRIPT_DIR}/setup_funasr.sh" --skip-verify
fi

# Install the app's own Python deps (fastapi/docxtpl/etc.) into the same venv.
if [[ -x "${VENV_DIR}/bin/pip" ]]; then
  info "安装 EdgeMinute Python 依赖 (requirements.txt)"
  sudo -u "$SERVICE_USER" "${VENV_DIR}/bin/pip" install -r "${PROJECT_DIR}/requirements.txt" --quiet || \
    warn "requirements.txt 安装出现问题，请检查上面的日志"
else
  warn "未找到 ${VENV_DIR}/bin/pip —— 跳过依赖安装（--skip-setup 且 venv 不存在？）"
fi

# ---------- step 2: local LLM (default on; skip with --no-llm) ----------
LLM_ENABLED=0
if [[ "$WITH_LLM" -eq 1 ]]; then
  if [[ "$SKIP_SETUP" -eq 1 ]]; then
    info "步骤 2: 已指定 --skip-setup，跳过 LLM 构建，仅查找已有模型并注册服务"
  else
    info "步骤 2: 构建本地 LLM (setup_llamacpp.sh)"
    # 关键：sudo -u 默认不重置 $HOME，setup_llamacpp.sh 里的 $HOME 会仍是 root 的家目录，
    # 导致模型下到 /root/models 而不是服务用户的家目录。这里用 sudo -H 重置 HOME，
    # 并显式传入 LLAMACPP_DIR，确保构建产物与下面的模型查找路径一致。
    sudo -H -u "$SERVICE_USER" \
      env HOME="$USER_HOME" LLAMACPP_DIR="$LLAMACPP_DIR" \
      bash "${SCRIPT_DIR}/setup_llamacpp.sh" --no-start --venv "$VENV_DIR" \
        --port "$LLM_PORT" --ctx "$LLM_CTX" --quant "$LLM_QUANT"
  fi

  # 校验 llama-server 二进制存在（skip-setup 时构建可能没跑过）。
  if [[ ! -x "${LLAMACPP_DIR}/build/bin/llama-server" ]]; then
    warn "未找到 ${LLAMACPP_DIR}/build/bin/llama-server，无法注册 LLM 服务。"
    warn "请先运行 setup_llamacpp.sh 完成编译，或去掉 --skip-setup。"
  else
    # setup_llamacpp.sh 把模型下到 ${HOME}/models/$(basename MS_REPO)/，按量化档位匹配 .gguf。
    MODEL_PATH="$(find "$MODELS_DIR" -iname "*${LLM_QUANT}*.gguf" 2>/dev/null | head -n1)"
    if [[ -z "$MODEL_PATH" ]]; then
      warn "在 ${MODELS_DIR} 未找到 *${LLM_QUANT}*.gguf 模型，跳过 LLM 服务注册。"
      warn "请检查 setup_llamacpp.sh 的下载日志，或用 --models-dir 指定模型目录后重跑。"
    else
      LLM_ENABLED=1
      info "LLM 模型: ${MODEL_PATH}"
    fi
  fi
fi

# ---------- step 3: render + install the (single) systemd unit ----------
info "步骤 3: 注册 systemd 服务（web + LLM 合并为一个 edgeminute 服务）"

# Web 与 LLM 由同一个 edgeminute.service 托管：start_edgeminute.sh 在后台拉起
# llama-server（若 LLM_ENABLED=1），再前台运行 web 应用作为主进程。停止/重启时
# systemd 的 cgroup 级 SIGTERM 会把两者一起收掉，无需单独维护 LLM 服务。
render_unit() {  # <template> <dest>
  local tpl="$1" dst="$2"
  sed \
    -e "s|__USER__|${SERVICE_USER}|g" \
    -e "s|__EDGEMINUTE_HOME__|${PROJECT_DIR}|g" \
    -e "s|__EDGEMINUTE_VENV__|${VENV_DIR}|g" \
    -e "s|__START_SCRIPT__|${SCRIPT_DIR}/start_edgeminute.sh|g" \
    -e "s|__ENV_FILE__|${PROJECT_DIR}/.env|g" \
    -e "s|__LLM_ENABLED__|${LLM_ENABLED}|g" \
    -e "s|__LLAMACPP_DIR__|${LLAMACPP_DIR}|g" \
    -e "s|__MODEL_PATH__|${MODEL_PATH:-}|g" \
    -e "s|__CONTEXT_SIZE__|${LLM_CTX}|g" \
    -e "s|__LLM_PORT__|${LLM_PORT}|g" \
    "$tpl" > "$dst"
}

chmod +x "${SCRIPT_DIR}/start_edgeminute.sh"

render_unit "${SYSTEMD_SRC}/edgeminute.service.tpl" "${SYSTEMD_DST}/edgeminute.service"
# Inject the chosen web port so app.py's default (8899) can be overridden via env.
if [[ "$WEB_PORT" != "8899" ]]; then
  sed -i "/^Environment=EDGEMINUTE_VENV=/a Environment=EDGEMINUTE_PORT=${WEB_PORT}" \
    "${SYSTEMD_DST}/edgeminute.service"
fi
# Point the app at the co-located LLM. Placed before EnvironmentFile=.env so a
# user's explicit LLM_BASE_URL in .env still overrides it.
if [[ "$LLM_ENABLED" -eq 1 ]]; then
  sed -i "/^Environment=EDGEMINUTE_LLM_PORT=/a Environment=LLM_BASE_URL=http://127.0.0.1:${LLM_PORT}" \
    "${SYSTEMD_DST}/edgeminute.service"
fi
info "已写入 ${SYSTEMD_DST}/edgeminute.service"

# 清理旧的分离式服务单元（从早期版本升级上来的场景）。
for old in edgeminute-web.service edgeminute-llm.service; do
  if [[ -f "${SYSTEMD_DST}/${old}" ]]; then
    systemctl disable --now "$old" >/dev/null 2>&1 || true
    rm -f "${SYSTEMD_DST}/${old}"
    info "已移除旧服务单元: ${old}"
  fi
done

SERVICES=(edgeminute.service)

systemctl daemon-reload

# ---------- step 4: enable + start ----------
for svc in "${SERVICES[@]}"; do
  if [[ "$DO_ENABLE" -eq 1 ]]; then
    systemctl enable "$svc" >/dev/null && info "已设置开机自启: ${svc}"
  fi
  if [[ "$DO_START" -eq 1 ]]; then
    systemctl restart "$svc" && info "已启动: ${svc}"
  fi
done

# ---------- summary ----------
echo
info "====== 安装完成 ======"
info "Web 界面:   http://<本机IP>:${WEB_PORT}"
[[ "$LLM_ENABLED" -eq 1 ]] && info "本地 LLM:   http://<本机IP>:${LLM_PORT}（与 web 同属 edgeminute 服务）"
info "查看日志:   journalctl -u edgeminute -f"
info "重启服务:   sudo systemctl restart edgeminute"
info "服务状态:   sudo systemctl status edgeminute"
if [[ ! -f "${PROJECT_DIR}/.env" ]]; then
  warn "未检测到 ${PROJECT_DIR}/.env —— 如需自定义配置，"
  warn "请 cp .env.example .env 后编辑，再 sudo systemctl restart edgeminute。"
fi

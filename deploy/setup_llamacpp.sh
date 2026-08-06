#!/usr/bin/env bash
#
# setup_llamacpp.sh
# 一键在 Jetson AGX Orin（JetPack 5.1.2, CUDA 11.4）上部署 llama.cpp + Qwen3-30B-A3B
# 用于：会议纪要整理 + 针对转写内容的问答
#
# 用法：
#   chmod +x setup_llamacpp.sh
#   ./setup_llamacpp.sh                  # 全流程：编译 + 下载模型 + 启动服务
#   ./setup_llamacpp.sh --skip-build      # 已经编译过，跳过编译
#   ./setup_llamacpp.sh --no-start        # 只编译+下载，不启动服务
#   ./setup_llamacpp.sh --venv DIR        # 指定带 modelscope 的 Python venv（默认 ~/funasr-env）
#
set -euo pipefail

# ---------- 配置（按需修改这几行） ----------
LLAMACPP_DIR="${LLAMACPP_DIR:-$HOME/llama.cpp}"
CUDA_ARCH="87"   # Jetson AGX Orin / Orin NX / Orin Nano 统一是 compute capability 8.7

# 下载模型用的 Python 环境：默认复用部署 FunASR 时建的 venv（里面已装好 modelscope）。
# 找不到就回退系统 python3，并在缺 modelscope 时自动 pip 安装。
VENV_DIR="${VENV_DIR:-$HOME/funasr-env}"
PIP_MIRROR="${PIP_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"

# 模型仓库 + 量化档位，默认用 Qwen3-30B-A3B 的 2507 改进版，Q4_K_M 量化
# 走魔塔社区（ModelScope）下载，比 huggingface.co 在国内快很多
MS_REPO="unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF"
QUANT="Q4_K_M"

SERVER_PORT=8080
CONTEXT_SIZE=32768   # 32K 上下文，覆盖大多数会议转写；显存/内存吃紧可以调小

SKIP_BUILD=0
NO_START=0

# ---------- 输出辅助函数 ----------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ---------- 参数解析 ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build) SKIP_BUILD=1; shift ;;
    --no-start) NO_START=1; shift ;;
    --repo) MS_REPO="$2"; shift 2 ;;
    --quant) QUANT="$2"; shift 2 ;;
    --port) SERVER_PORT="$2"; shift 2 ;;
    --ctx) CONTEXT_SIZE="$2"; shift 2 ;;
    --venv) VENV_DIR="$2"; shift 2 ;;
    --pip-mirror) PIP_MIRROR="$2"; shift 2 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^#//'
      exit 0
      ;;
    *) error "未知参数: $1"; exit 1 ;;
  esac
done

# ---------- 步骤 1：环境检查 ----------
info "步骤 1/5：检查运行环境"

# Jetson 判定不能只靠 nvidia-jetpack meta 包（很多设备不带它 → 误报）。
# 看 L4T / 设备树标识，任一命中即为 Jetson。
is_jetson() {
  [[ -f /etc/nv_tegra_release || -f /etc/nv_boot_control.conf ]] && return 0
  local f model
  for f in /proc/device-tree/model /sys/firmware/devicetree/base/model; do
    if [[ -r "$f" ]]; then
      model=$(tr -d '\0' < "$f" 2>/dev/null)
      [[ "$model" == *Jetson* || "$model" == *Tegra* ]] && return 0
    fi
  done
  return 1
}
if is_jetson; then
  # 注意：dpkg -l <未安装包> 会返回非零，pipefail 下会连累这条赋值触发 set -e，
  # 故显式 || true 兜底（很多 Jetson 并不装 nvidia-jetpack meta 包）。
  JETPACK_VERSION=$(dpkg -l nvidia-jetpack 2>/dev/null | awk '/^ii/{print $3; exit}' || true)
  [[ -n "$JETPACK_VERSION" ]] && info "检测到 Jetson 设备，JetPack: ${JETPACK_VERSION}" \
                             || info "检测到 Jetson 设备（未能读取精确 JetPack 版本）"
else
  warn "未识别为 Jetson 设备（无 L4T / 设备树标识），继续执行但请自行确认 aarch64/CUDA 环境匹配"
fi

if ! command -v nvcc &>/dev/null; then
  if [[ -x /usr/local/cuda/bin/nvcc ]]; then
    export PATH=/usr/local/cuda/bin:$PATH
    info "nvcc 不在 PATH 里，已临时加入 /usr/local/cuda/bin"
  else
    error "找不到 nvcc，CUDA 工具链可能没装好，请检查 /usr/local/cuda 是否存在"
    exit 1
  fi
fi
NVCC_VER="$(nvcc --version 2>/dev/null | tail -1 || true)"
info "CUDA 编译器: ${NVCC_VER:-未知}"

DISK_AVAIL_GB=$(df --output=avail -BG "$HOME" 2>/dev/null | tail -1 | tr -dc '0-9' || true)
if [[ -n "$DISK_AVAIL_GB" && "$DISK_AVAIL_GB" -lt 25 ]]; then
  warn "当前可用磁盘空间约 ${DISK_AVAIL_GB}GB，Qwen3-30B-A3B 的 Q4_K_M 量化文件约 18-20GB，建议预留 25GB+"
fi

# ---------- 步骤 2：安装系统依赖 ----------
info "步骤 2/5：安装系统依赖（需要 sudo 权限）"

sudo apt-get update -qq
sudo apt-get install -y \
  git cmake build-essential libcurl4-openssl-dev libssl-dev \
  > /dev/null

info "系统依赖安装完成"

# ---------- 步骤 3：编译 llama.cpp ----------
if [[ "$SKIP_BUILD" -eq 1 ]]; then
  warn "已指定 --skip-build，跳过编译，假设 ${LLAMACPP_DIR}/build 已存在"
else
  info "步骤 3/5：编译 llama.cpp（CUDA_ARCH=${CUDA_ARCH}，Jetson 上预计需要 15-30 分钟）"

  if [[ ! -d "$LLAMACPP_DIR" ]]; then
    git clone https://github.com/ggml-org/llama.cpp "$LLAMACPP_DIR"
  else
    info "目录已存在，跳过 clone（如需更新代码请自行 git pull）"
  fi

  git config --global --add safe.directory "$LLAMACPP_DIR"

  # Ubuntu 20.04（JetPack 5 系列）apt 自带的 cmake 是 3.16.3，llama.cpp 的 CUDA 后端要求 3.18+
  CMAKE_VER=$(cmake --version 2>/dev/null | head -1 | grep -oP '\d+\.\d+\.\d+' || echo "0.0.0")
  CMAKE_MAJOR_MINOR=$(echo "$CMAKE_VER" | cut -d. -f1,2)
  if awk -v v="$CMAKE_MAJOR_MINOR" 'BEGIN{exit !(v < 3.18)}'; then
    info "系统 cmake 版本 ${CMAKE_VER} 低于 3.18，安装新版 cmake"
    pip install "cmake>=3.18" -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 120 --quiet 2>/dev/null || pip3 install "cmake>=3.18" --quiet
    info "当前 cmake 版本: $(cmake --version | head -1)"
  fi

  cd "$LLAMACPP_DIR"
  cmake -B build \
    -DGGML_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}" \
    -DCMAKE_BUILD_TYPE=Release \
    -DLLAMA_BUILD_UI=OFF
  cmake --build build --config Release -j"$(nproc)"

  if [[ ! -f "${LLAMACPP_DIR}/build/bin/llama-server" ]]; then
    error "编译似乎没有成功，找不到 build/bin/llama-server"
    exit 1
  fi
  info "编译完成: ${LLAMACPP_DIR}/build/bin/llama-server"
fi

# ---------- 步骤 4：下载模型 ----------
# llama.cpp 自带的 `-hf` 下载功能依赖系统 OpenSSL 版本检测，
# JetPack 5 系列自带的 OpenSSL 1.1.1 通不过这个检测（即使装了 libssl-dev 也一样），
# 所以改用魔塔社区（ModelScope）下载到本地，再用 -m 加载本地文件，国内速度更快。
info "步骤 4/5：下载模型权重（通过魔塔社区 ModelScope，约 18-20GB，请耐心等待）"

# 选一个带 modelscope 的 Python：优先 FunASR 的 venv（部署 ASR 时已装 modelscope），
# 否则回退系统 python3。选定后若仍缺 modelscope，就地 pip 安装，避免直接报
# ModuleNotFoundError。
DL_PY=""
if [[ -x "${VENV_DIR}/bin/python" ]]; then
  DL_PY="${VENV_DIR}/bin/python"
  info "下载模型将使用 venv 解释器: ${DL_PY}"
else
  DL_PY="$(command -v python3)"
  warn "未找到 ${VENV_DIR}/bin/python（FunASR venv?），回退系统 python3: ${DL_PY}"
fi

if ! "$DL_PY" -c "import modelscope" &>/dev/null; then
  warn "所选 Python 环境缺少 modelscope，正在安装（镜像: ${PIP_MIRROR}）..."
  "$DL_PY" -m pip install modelscope -i "$PIP_MIRROR" --timeout 120 --quiet \
    || "$DL_PY" -m pip install modelscope --quiet
fi
if ! "$DL_PY" -c "import modelscope" &>/dev/null; then
  error "modelscope 安装失败，无法下载模型。"
  error "请手动在某个 Python 环境里 'pip install modelscope' 后，用 --venv 指向该环境重跑，"
  error "或自行下载 ${MS_REPO} 的 *${QUANT}*.gguf 到 ${HOME}/models/$(basename "$MS_REPO")/。"
  exit 1
fi

MODEL_DIR="${HOME}/models/$(basename "$MS_REPO")"
mkdir -p "$MODEL_DIR"

"$DL_PY" - "$MS_REPO" "$QUANT" "$MODEL_DIR" <<'PYEOF'
import sys
from modelscope import snapshot_download

repo, quant, local_dir = sys.argv[1], sys.argv[2], sys.argv[3]
snapshot_download(repo, allow_patterns=f"*{quant}*", local_dir=local_dir)
PYEOF

MODEL_PATH=$(find "$MODEL_DIR" -iname "*${QUANT}*.gguf" | head -n1)
if [[ -z "$MODEL_PATH" ]]; then
  error "下载完成但没找到匹配 *${QUANT}*.gguf 的文件，检查一下 ${MODEL_DIR} 目录里实际下载了什么"
  exit 1
fi
info "模型文件: ${MODEL_PATH}"

# ---------- 步骤 5：启动服务 ----------
cd "$LLAMACPP_DIR"
if [[ "$NO_START" -eq 1 ]]; then
  info "已指定 --no-start，跳过启动服务"
  echo
  info "====== 准备完成 ======"
  info "手动启动命令："
  info "  cd ${LLAMACPP_DIR}"
  info "  ./build/bin/llama-server -m ${MODEL_PATH} -ngl 99 -c ${CONTEXT_SIZE} --host 0.0.0.0 --port ${SERVER_PORT}"
  exit 0
fi

info "步骤 5/5：启动 llama-server（OpenAI 兼容接口，端口 ${SERVER_PORT}）"
info "启动后可以用 Ctrl+C 停止；日常使用建议配合 systemd 或 tmux/screen 常驻运行"

exec ./build/bin/llama-server \
  -m "$MODEL_PATH" \
  -ngl 99 \
  -c "${CONTEXT_SIZE}" \
  --host 0.0.0.0 \
  --port "${SERVER_PORT}"

#!/usr/bin/env bash
#
# setup_funasr.sh
# 一键在 Jetson（JetPack 5.1.2 / Python 3.8）上部署 FunASR + 说话人分离(CAM++)
#
# 适用环境：
#   - nvidia-jetpack 5.1.2-b104
#   - Python 3.8（系统自带）
#   - CUDA 11.4
#
# 用法：
#   chmod +x setup_funasr.sh
#   ./setup_funasr.sh                        # 全流程
#   ./setup_funasr.sh --skip-verify           # 不跑验证
#   ./setup_funasr.sh --wav /path/audio.wav   # 指定验证音频
#   ./setup_funasr.sh --skip-torchaudio-build # torchaudio 已装好，跳过编译
#
set -euo pipefail

# ---------- 默认参数 ----------
VENV_DIR="${VENV_DIR:-$HOME/funasr-env}"
TEST_WAV=""
SKIP_VERIFY=0
SKIP_TORCHAUDIO_BUILD=0
INSTALL_SERVER=0

TORCH_WHEEL_URL="https://developer.download.nvidia.com/compute/redist/jp/v512/pytorch/torch-2.1.0a0+41361538.nv23.06-cp38-cp38-linux_aarch64.whl"
TORCH_WHEEL_NAME="torch-2.1.0a0+41361538.nv23.06-cp38-cp38-linux_aarch64.whl"
TORCHAUDIO_BRANCH="release/2.1"
PIP_MIRROR="${PIP_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"

# funasr 版本固定写死在这里，不用命令行传参。
# 最新版在 Python 3.8 上有已知的兼容性问题（"paraformer-zh is not registered"），
# 1.2.6 是实测在这台设备上验证过可用的版本，要升级请直接改这一行。
FUNASR_VERSION="1.2.6"

# numba / llvmlite 版本同样固定写死。Jetson aarch64（AGX Orin / JetPack 5.1.2 /
# Python 3.8）上更高版本 llvmlite 的 JIT 内存链接器(LLVM RuntimeDyld)会触发
# aarch64 重定位溢出断言 `isInt<33>(Result)`，长音频转写时直接 core dump(Aborted)。
# 实测这套旧组合不触发该断言，稳定可用；正因钉死此版本，app.py 才无需再禁用 JIT。
# 要升级请改这两行，并务必在设备上实测长音频转写不崩、CAM++ 速度正常。
NUMBA_VERSION="0.56.4"
LLVMLITE_VERSION="0.39.1"

# ---------- 输出辅助函数 ----------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ---------- 参数解析 ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-verify) SKIP_VERIFY=1; shift ;;
    --wav) TEST_WAV="$2"; shift 2 ;;
    --venv) VENV_DIR="$2"; shift 2 ;;
    --with-server) INSTALL_SERVER=1; shift ;;
    --skip-torchaudio-build) SKIP_TORCHAUDIO_BUILD=1; shift ;;
    --pip-mirror) PIP_MIRROR="$2"; shift 2 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^#//'
      exit 0
      ;;
    *) error "未知参数: $1"; exit 1 ;;
  esac
done

# ---------- 步骤 1：环境检查 ----------
info "步骤 1/8：检查运行环境"

PYTHON_BIN="python3"
if ! command -v "$PYTHON_BIN" &>/dev/null; then
  error "未找到 python3"
  exit 1
fi

PYTHON_VERSION=$($PYTHON_BIN -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
info "Python 版本: ${PYTHON_VERSION}"
if [[ "$PYTHON_VERSION" != "3.8" ]]; then
  warn "检测到 Python 版本不是 3.8，本脚本下载的官方 torch wheel 是为 cp38 编译的，"
  warn "换成其他 Python 版本大概率装不上，请确认你要用的是系统自带的 python3(3.8)。"
fi

# ---------- Jetson / JetPack 检测 ----------
# 不能只靠 `dpkg -l | grep nvidia-jetpack`：该包是 JetPack 的聚合 meta 包，很多
# 通过 SDK Manager 刷机、或只装了 L4T 运行时的设备并不带它，导致误报「非 Jetson」。
# 真正可靠的是看 L4T / 设备树标识（任一命中即判定为 Jetson）。
is_jetson() {
  [[ -f /etc/nv_tegra_release ]] && return 0
  [[ -f /etc/nv_boot_control.conf ]] && return 0
  local model
  for f in /proc/device-tree/model /sys/firmware/devicetree/base/model; do
    if [[ -r "$f" ]]; then
      # 设备树 model 是 NUL 结尾字符串，tr 掉 NUL 再匹配
      model=$(tr -d '\0' < "$f" 2>/dev/null)
      [[ "$model" == *Jetson* || "$model" == *Tegra* ]] && return 0
    fi
  done
  return 1
}

# 尽力读取 JetPack 版本：优先 dpkg 的 nvidia-jetpack，其次 L4T release 文件反推。
detect_jetpack_version() {
  local v=""
  if command -v dpkg &>/dev/null; then
    v=$(dpkg -l nvidia-jetpack 2>/dev/null | awk '/^ii/{print $3; exit}' || true)
  fi
  if [[ -z "$v" && -f /etc/nv_tegra_release ]]; then
    # 例：# R35 (release), REVISION: 4.1 → L4T 35.4.1（对应 JetPack 5.1.2）
    local rel rev
    rel=$(grep -oP '(?<=# R)\d+' /etc/nv_tegra_release | head -n1)
    rev=$(grep -oP '(?<=REVISION: )[0-9.]+' /etc/nv_tegra_release | head -n1)
    [[ -n "$rel" && -n "$rev" ]] && v="L4T ${rel}.${rev}"
  fi
  echo "$v"
}

JETPACK_VERSION=""
if is_jetson; then
  JETPACK_VERSION="$(detect_jetpack_version)"
  if [[ -n "$JETPACK_VERSION" ]]; then
    info "检测到 Jetson 设备，JetPack/L4T: ${JETPACK_VERSION}"
  else
    info "检测到 Jetson 设备（未能读取精确 JetPack 版本）"
  fi
  # 只有能读到明确的 JetPack 5.1.x / L4T 35.x 才校验；读不到就不误报。
  if [[ -n "$JETPACK_VERSION" && "$JETPACK_VERSION" != *5.1.2* && "$JETPACK_VERSION" != *35.4* ]]; then
    warn "当前 JetPack/L4T (${JETPACK_VERSION}) 不是 5.1.2 / L4T 35.4，本脚本的 torch wheel"
    warn "是专门为 5.1.2 准备的。版本不一致时请到"
    warn "https://developer.download.nvidia.com/compute/redist/jp/ 找匹配的 wheel，"
    warn "替换脚本里的 TORCH_WHEEL_URL 变量。"
  fi
else
  warn "未识别为 Jetson 设备（无 /etc/nv_tegra_release、设备树 model 也不含 Jetson/Tegra）。"
  warn "若你确实在 Jetson 上运行却看到此提示，可忽略；否则本脚本的 aarch64/CUDA 假设可能不匹配。"
fi

DISK_AVAIL_GB=$(df --output=avail -BG "$HOME" | tail -1 | tr -dc '0-9')
if [[ -n "$DISK_AVAIL_GB" && "$DISK_AVAIL_GB" -lt 10 ]]; then
  warn "当前可用磁盘空间约 ${DISK_AVAIL_GB}GB，建议预留 10GB+（模型缓存 + 编译产物）"
fi

info "提醒：本脚本会从源码编译 torchaudio，在 Jetson 上通常需要 30-60 分钟，请耐心等待。"

# ---------- 步骤 2：安装系统依赖 ----------
info "步骤 2/8：安装系统依赖（需要 sudo 权限）"

sudo apt-get update -qq
sudo apt-get install -y \
  python3-venv python3-dev \
  libopenblas-dev libopenmpi-dev libomp-dev \
  build-essential cmake git wget \
  ffmpeg libavformat-dev libavcodec-dev libavutil-dev libavdevice-dev libavfilter-dev \
  > /dev/null

info "系统依赖安装完成"

# ---------- 步骤 3：创建虚拟环境 ----------
info "步骤 3/8：创建/激活虚拟环境 (${VENV_DIR})"

if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  info "已创建虚拟环境"
else
  info "虚拟环境已存在，直接复用"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip --quiet
export PIP_INDEX_URL="$PIP_MIRROR"
export PIP_TIMEOUT=120
export PIP_RETRIES=5
info "pip 已切换到镜像: ${PIP_MIRROR}（超时 ${PIP_TIMEOUT}s，重试 ${PIP_RETRIES} 次）"
pip install "numpy<2" -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 120 --quiet   # 官方 wheel 编译时基于旧版 numpy ABI，装新版 numpy 2.x 容易报错
pip install setuptools wheel -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 120 --quiet   # torchaudio 用 --no-use-pep517 编译时依赖这两个包

# ---------- 步骤 4：安装官方 PyTorch wheel ----------
info "步骤 4/8：下载并安装 JetPack 5.1.2 官方 PyTorch wheel"

if python3 -c "import torch" &>/dev/null; then
  warn "虚拟环境里已检测到 torch，跳过下载安装（如需重装请先 pip uninstall torch）"
else
  WHEEL_PATH="~/${TORCH_WHEEL_NAME}"
  if [[ ! -f "$WHEEL_PATH" ]]; then
    wget -q --show-progress "$TORCH_WHEEL_URL" -O "$WHEEL_PATH"
  fi
  pip install --no-cache "$WHEEL_PATH" -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 120
fi

CUDA_OK=$(python3 -c "import torch; print(torch.cuda.is_available())" 2>/dev/null || echo "False")
if [[ "$CUDA_OK" != "True" ]]; then
  error "torch.cuda.is_available() 返回 False，请检查 CUDA/驱动环境。"
  error "脚本会继续往下走，但后续会用 CPU 跑，速度会慢很多。"
else
  info "PyTorch 已正确识别 GPU"
fi

# ---------- 步骤 5：编译安装 torchaudio ----------
info "步骤 5/8：编译安装 torchaudio（耐心等待，这一步最慢）"

if [[ "$SKIP_TORCHAUDIO_BUILD" -eq 1 ]]; then
  warn "已指定 --skip-torchaudio-build，跳过编译，假设 torchaudio 已经装好"
elif python3 -c "import torchaudio" &>/dev/null; then
  warn "检测到 torchaudio 已安装，跳过编译"
else
  BUILD_DIR="~/torchaudio-build"
  rm -rf "$BUILD_DIR"

  # Ubuntu 20.04（JetPack 5 系列）apt 自带的 cmake 是 3.16.3，torchaudio 要求 3.18+，
  # 用 pip 装一个新版 cmake 到虚拟环境里覆盖掉系统版本，避免去折腾 apt 源。
  CMAKE_VER=$(cmake --version 2>/dev/null | head -1 | grep -oP '\d+\.\d+\.\d+' || echo "0.0.0")
  CMAKE_MAJOR_MINOR=$(echo "$CMAKE_VER" | cut -d. -f1,2)
  if awk -v v="$CMAKE_MAJOR_MINOR" 'BEGIN{exit !(v < 3.18)}'; then
    info "系统 cmake 版本 ${CMAKE_VER} 低于 3.18，安装新版 cmake 到虚拟环境"
    pip install "cmake>=3.18" -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 120 --quiet
    info "虚拟环境 cmake 版本: $(cmake --version | head -1)"
  fi

  # CMakeLists.txt 里指定用 Ninja 生成器，系统可能没装，用 pip 装最省心
  if ! command -v ninja &>/dev/null; then
    info "未检测到 ninja，安装到虚拟环境"
    pip install ninja -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 120 --quiet
  fi

  git clone --branch "$TORCHAUDIO_BRANCH" --recursive --depth=1 \
    https://github.com/pytorch/audio "$BUILD_DIR"
  cd "$BUILD_DIR"

  # 已知问题：部分 CUDA 版本编译 ctc_prefix_decoder_kernel_v2.cu 缺 float.h 头文件，提前打个补丁
  CTC_FILE="src/libtorchaudio/cuctc/src/ctc_prefix_decoder_kernel_v2.cu"
  if [[ -f "$CTC_FILE" ]] && ! grep -q '#include <float.h>' "$CTC_FILE"; then
    sed -i '1i#include <float.h>' "$CTC_FILE" || warn "自动打补丁失败，如遇编译报错请手动检查 ${CTC_FILE}"
  fi

  USE_CUDA=1 pip install -v -e . --no-use-pep517
  cd - > /dev/null
fi

if python3 -c "import torchaudio" &>/dev/null; then
  info "torchaudio 安装成功: $(python3 -c 'import torchaudio; print(torchaudio.__version__)')"
else
  error "torchaudio 导入失败，FunASR 依赖它做音频读取/重采样，请检查上面的编译日志报错"
  exit 1
fi

# ---------- 步骤 6：安装 FunASR / ModelScope ----------
info "步骤 6/8：安装 FunASR 及 ModelScope"

pip install -U modelscope --quiet

# modelscope 部分文件里用了 Python 3.9+ 风格的裸类型标注（如 list[int]），
# Python 3.8 上会报 "TypeError: 'type' object is not subscriptable"。
# 加一行 `from __future__ import annotations` 即可让类型标注延迟求值，规避这个问题。
# 注意：不能用 `import modelscope` 去定位文件路径，因为触发的正是这同一个崩溃；
# 改用 sysconfig 直接算出 site-packages 路径。
SITE_PACKAGES=$(python3 -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
MS_TORCH_UTILS="${SITE_PACKAGES}/modelscope/utils/torch_utils.py"
if [[ -f "$MS_TORCH_UTILS" ]]; then
  if ! head -1 "$MS_TORCH_UTILS" | grep -q "from __future__ import annotations"; then
    info "为 ${MS_TORCH_UTILS} 打上 Python 3.8 兼容补丁"
    sed -i '1i from __future__ import annotations' "$MS_TORCH_UTILS"
  fi
else
  warn "未找到 ${MS_TORCH_UTILS}，跳过自动补丁（modelscope 目录结构可能变了，需要手动检查）"
fi

# modelscope 的 hub 工具会 `import zoneinfo`，这是 Python 3.9+ 才有的标准库，
# 3.8 上根本不存在这个模块，装官方 backport 包再垫一层同名 shim。
if ! python3 -c "import zoneinfo" &>/dev/null; then
  info "Python 3.8 缺少 zoneinfo 标准库，安装 backports.zoneinfo 并创建兼容垫片"
  pip install backports.zoneinfo --quiet
  cat > "${SITE_PACKAGES}/zoneinfo.py" << 'EOF'
from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones
EOF
fi

info "安装指定版本 funasr==${FUNASR_VERSION}（脚本顶部固定配置，不会被自动升级覆盖）"
pip install "funasr==${FUNASR_VERSION}" --quiet

if [[ "$INSTALL_SERVER" -eq 1 ]]; then
  info "安装 funasr-server 依赖（FastAPI/uvicorn）"
  pip install fastapi uvicorn python-multipart --quiet
fi

INSTALLED_FUNASR_VERSION=$(python3 -c 'import funasr; print(funasr.__version__)' 2>/dev/null || echo '未知')
info "FunASR 版本: ${INSTALLED_FUNASR_VERSION}"
if [[ "$INSTALLED_FUNASR_VERSION" != "$FUNASR_VERSION" ]]; then
  warn "安装到的版本(${INSTALLED_FUNASR_VERSION})和脚本里固定的版本(${FUNASR_VERSION})不一致！"
  warn "常见原因：当前目录下有同名 funasr 文件夹遮挡了 site-packages，或者装了缓存/残留版本。"
  warn "可以用 python3 -c \"import funasr; print(funasr.__file__)\" 检查实际加载路径。"
fi

# 显式钉装 numba / llvmlite，覆盖 funasr 依赖解析可能拖进来的其它版本。
# 必须放在 funasr 安装之后，才能盖掉其带入的版本。详见脚本顶部版本变量处的说明。
info "钉装 numba==${NUMBA_VERSION} / llvmlite==${LLVMLITE_VERSION}（Jetson aarch64 JIT 崩溃修复）"
pip install "numba==${NUMBA_VERSION}" "llvmlite==${LLVMLITE_VERSION}" --quiet

INSTALLED_NUMBA_VERSION=$(python3 -c 'import numba; print(numba.__version__)' 2>/dev/null || echo '未知')
INSTALLED_LLVMLITE_VERSION=$(python3 -c 'import llvmlite; print(llvmlite.__version__)' 2>/dev/null || echo '未知')
info "numba 版本: ${INSTALLED_NUMBA_VERSION}，llvmlite 版本: ${INSTALLED_LLVMLITE_VERSION}"
if [[ "$INSTALLED_NUMBA_VERSION" != "$NUMBA_VERSION" || "$INSTALLED_LLVMLITE_VERSION" != "$LLVMLITE_VERSION" ]]; then
  warn "numba/llvmlite 实际版本与脚本固定版本不一致！"
  warn "期望 numba==${NUMBA_VERSION} / llvmlite==${LLVMLITE_VERSION}，"
  warn "实际 numba==${INSTALLED_NUMBA_VERSION} / llvmlite==${INSTALLED_LLVMLITE_VERSION}。"
  warn "版本不符时 Jetson aarch64 长音频转写可能 core dump，请手动重装钉死版本后再用。"
fi

# CAM++ 说话人分离依赖 sklearn 做聚类，而 sklearn 自带的私有 libgomp 副本
# （文件名带哈希后缀，如 libgomp-xxxxx.so.1.0.0）和 PyTorch 自带的 libgomp
# 在 aarch64 上同时加载会撞上静态 TLS 空间不够用的问题
# （ImportError: ... cannot allocate memory in static TLS block）。
# 实测预加载系统自带的 libgomp.so.1 不够，必须预加载 sklearn 自己这份私有副本
# （让链接器发现它已经被加载过，不再重复占用 TLS），系统路径作为找不到时的兜底。
LIBGOMP_PATH=$(find "${SITE_PACKAGES}/scikit_learn.libs" -name "libgomp*.so*" 2>/dev/null | head -n1)
if [[ -z "$LIBGOMP_PATH" ]]; then
  LIBGOMP_PATH=$(find /usr/lib/aarch64-linux-gnu -name "libgomp.so.1" 2>/dev/null | head -n1)
fi
if [[ -n "$LIBGOMP_PATH" ]]; then
  export LD_PRELOAD="$LIBGOMP_PATH"
  if ! grep -q "LD_PRELOAD=${LIBGOMP_PATH}" "${VENV_DIR}/bin/activate" 2>/dev/null; then
    info "写入 LD_PRELOAD=${LIBGOMP_PATH} 到虚拟环境的 activate 脚本，以后激活即生效"
    echo "export LD_PRELOAD=${LIBGOMP_PATH}" >> "${VENV_DIR}/bin/activate"
  fi
else
  warn "未找到系统的 libgomp.so.1，如果后面 CAM++ 说话人分离报 sklearn 相关的 libgomp 错误，需要手动排查"
fi

# ---------- 步骤 7：预下载模型 ----------
info "步骤 7/8：预下载模型权重（VAD + ASR + 标点 + 说话人分离）"

DEVICE_ARG="cuda"
if [[ "$CUDA_OK" != "True" ]]; then
  DEVICE_ARG="cpu"
fi

python3 - "$DEVICE_ARG" <<'PYEOF'
import sys
from funasr import AutoModel

device = sys.argv[1]
print(f"正在以 device={device} 预加载模型...")

AutoModel(
    model="paraformer-zh",
    vad_model="fsmn-vad",
    punc_model="ct-punc",
    spk_model="cam++",
    device=device,
)
print("模型权重预下载完成。")
PYEOF

# ---------- 步骤 8：生成并（可选）运行验证脚本 ----------
info "步骤 8/8：生成验证脚本 verify_funasr.py"

VERIFY_SCRIPT="${VENV_DIR}/verify_funasr.py"
cat > "$VERIFY_SCRIPT" <<'PYEOF'
import sys
from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess

if len(sys.argv) < 2:
    print("用法: python verify_funasr.py <音频文件路径> [cuda|cpu]")
    sys.exit(1)

wav_path = sys.argv[1]
device = sys.argv[2] if len(sys.argv) > 2 else "cuda"

model = AutoModel(
    model="paraformer-zh",
    vad_model="fsmn-vad",
    punc_model="ct-punc",
    spk_model="cam++",
    device=device,
)

result = model.generate(input=wav_path)

for seg in result[0]["sentence_info"]:
    start = seg["start"] / 1000
    end = seg["end"] / 1000
    text = rich_transcription_postprocess(seg["sentence"])
    print(f"[{start:.1f}s-{end:.1f}s] 说话人{seg['spk']}: {text}")
PYEOF

info "验证脚本已生成: ${VERIFY_SCRIPT}"

if [[ "$SKIP_VERIFY" -eq 1 ]]; then
  info "已指定 --skip-verify，跳过验证运行"
elif [[ -z "$TEST_WAV" ]]; then
  warn "未指定测试音频（--wav 参数），跳过实际验证运行"
  warn "之后可以手动执行："
  warn "  source ${VENV_DIR}/bin/activate"
  warn "  python ${VERIFY_SCRIPT} /path/to/your_meeting.wav ${DEVICE_ARG}"
elif [[ ! -f "$TEST_WAV" ]]; then
  error "指定的测试音频不存在: ${TEST_WAV}，跳过验证运行"
else
  info "使用 ${TEST_WAV} 运行验证（device=${DEVICE_ARG}）"
  python3 "$VERIFY_SCRIPT" "$TEST_WAV" "$DEVICE_ARG"
fi

# ---------- 完成汇总 ----------
echo
info "====== FunASR 部署完成 ======"
info "虚拟环境路径: ${VENV_DIR}"
info "激活方式:    source ${VENV_DIR}/bin/activate"
info "验证脚本:    python ${VERIFY_SCRIPT} <音频路径> [cuda|cpu]"
if [[ "$INSTALL_SERVER" -eq 1 ]]; then
  info "启动服务:    funasr-server --model fun-asr-nano --device ${DEVICE_ARG} --port 8899"
fi
if [[ "$CUDA_OK" != "True" ]]; then
  warn "注意：当前 GPU 未被正确识别，实际是用 CPU 跑的模型，请检查 CUDA/驱动环境后重新运行。"
fi

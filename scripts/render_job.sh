#!/usr/bin/env bash
set -euo pipefail

BLEND_PATH=""
JOB_DIR=""
MODE="TURBO"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --blend)
      BLEND_PATH="$2"
      shift 2
      ;;
    --job-dir)
      JOB_DIR="$2"
      shift 2
      ;;
    --mode)
      MODE="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$BLEND_PATH" || -z "$JOB_DIR" ]]; then
  echo "Usage: render_job.sh --blend <file.blend> --job-dir <dir> --mode <TURBO|ARTIST>" >&2
  exit 2
fi

BLENDER_BIN="${BLENDER_BIN:-blender}"
OPTIMIZE_SCRIPT="${OPTIMIZE_SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/turbo_optimize.py}"

# Prevent huge ROCm GPU core dumps (gpucore.<pid>) and host core files.
# Per ROCm docs, ulimit controls both; setting to 0 disables them.
ulimit -c 0

if [[ ! -f "$BLEND_PATH" ]]; then
  echo "Blend file not found: $BLEND_PATH" >&2
  exit 3
fi
if [[ ! -f "$OPTIMIZE_SCRIPT" ]]; then
  echo "Optimize script not found: $OPTIMIZE_SCRIPT" >&2
  exit 3
fi

mkdir -p "$JOB_DIR"
export HSA_XNACK="${HSA_XNACK:-1}"
export USE_HIPRT="${USE_HIPRT:-0}"
export RENDER_GPU_NAME="${RENDER_GPU_NAME:-Radeon}"
export RENDER_MODE="${MODE^^}"
export RENDER_OUTPUT_DIR="${RENDER_OUTPUT_DIR:-$JOB_DIR/renders}"
export RENDER_FRAME="${RENDER_FRAME:-1}"
mkdir -p "$RENDER_OUTPUT_DIR"

echo "[render_job] blender=$BLENDER_BIN mode=${RENDER_MODE} hiprt=${USE_HIPRT} gpu_name=${RENDER_GPU_NAME} frame=${RENDER_FRAME} blend=$BLEND_PATH output=$RENDER_OUTPUT_DIR"
exec "$BLENDER_BIN" -b "$BLEND_PATH" -P "$OPTIMIZE_SCRIPT" -f "$RENDER_FRAME"

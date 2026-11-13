#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -u
set -o pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd) || exit 1
source "$ROOT/app/flash-common.sh"
STAGE=""
cleanup() { [[ -z "$STAGE" ]] || rm -rf -- "$STAGE"; }
trap cleanup EXIT
trap 'printf "\n操作已中断。请重新按住 BOOT 插入后再试。\n"; exit 130' INT TERM
fail() { printf '\n失败：%s\n' "$1" >&2; exit 1; }

[[ "$(uname -s)" == Darwin ]] || fail '请在 macOS 上运行此文件。'
printf '\nDS5Dongle 简易烧录工具\n'
cat "$ROOT/firmware/version.txt" || exit 1
printf '仅用于 LCTech BL616 适配器。\n\n'
STAGE=$(mktemp -d /tmp/ds5-flash.XXXXXX) || fail '无法创建临时目录。'
if ! verify_assets "$ROOT" "$STAGE/check.log"; then
    cat "$STAGE/check.log"
    fail '文件缺失或校验失败，请重新解压完整安装包。'
fi
printf '安装包文件校验通过。\n'
TOOL_SUBDIR=$(mac_tool_subdir "$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null || true)" "$(uname -m)") || fail '不支持此 Mac 的处理器。'
TOOL_DIR="$ROOT/tools/$TOOL_SUBDIR"
[[ -x "$ROOT/app/list-ports-macos" ]] || fail '端口检测程序无法执行，请重新解压安装包。'
[[ -x "$TOOL_DIR/BLFlashCommand-macos" ]] || fail '烧录程序无法执行，请重新解压安装包。'
if ! "$TOOL_DIR/BLFlashCommand-macos" --runtime-info >"$STAGE/runtime-check.log" 2>&1; then
    cat "$STAGE/runtime-check.log"
    fail '原生烧录程序无法启动。请完整解压安装包，并按系统提示允许运行。'
fi
if [[ "$TOOL_SUBDIR" == macos-arm64 ]]; then
    printf '已选择 Apple 芯片原生版，无需 Rosetta。\n'
else
    printf '已选择 Intel Mac 原生版。\n'
fi
if [[ "${1:-}" == --check ]]; then
    "$ROOT/app/list-ports-macos" || fail '无法读取 USB 设备。'
    printf '环境检查通过；未执行烧录。\n'
    exit 0
fi

printf '\n请按住适配器 BOOT 键插入 USB，再松开 BOOT。\n'
printf '退出占用串口的程序，每次只接一个待刷适配器。\n'
while :; do
    printf '\n准备好后按回车开始，输入 q 退出：'
    read -r answer || exit 1
    [[ "$answer" != q && "$answer" != Q ]] || exit 0
    ports=$("$ROOT/app/list-ports-macos") || fail '无法读取 USB 设备。'
    PORT=""; count=0
    while IFS= read -r candidate; do
        [[ -n "$candidate" ]] || continue
        PORT="$candidate"; count=$((count + 1))
    done <<< "$ports"
    if [[ "$count" -eq 0 ]]; then
        printf '未检测到 BOOT 下载端口。请按住 BOOT 重新插入，并检查 USB 数据线。\n'
    elif [[ "$count" -gt 1 ]]; then
        printf '检测到多个下载端口，请仅保留一个待刷适配器。\n'
    else
        break
    fi
done

LOG_DIR=${DS5_FLASH_LOG_DIR:-"$ROOT/logs"}
mkdir -p "$LOG_DIR" || fail "无法创建日志目录：$LOG_DIR"
LOG="$LOG_DIR/mac-$(date '+%Y%m%d-%H%M%S')-$$.log"
cp -R "$TOOL_DIR" "$STAGE/tool" || fail '复制烧录程序失败。'
cp -R "$ROOT/tools/common/." "$STAGE/tool/" || fail '复制芯片配置失败。'
cp -R "$ROOT/firmware" "$STAGE/firmware" || fail '复制固件失败。'
EXPECTED=$(/usr/bin/shasum -a 256 "$STAGE/firmware/ds5dongle-lctech616-hs.bin")
EXPECTED=${EXPECTED%% *}
printf '\n已找到下载端口：%s\n正在烧录并校验，通常需要 10–30 秒，请勿拔线。\n日志：%s\n\n' "$PORT" "$LOG"
cd -- "$STAGE/firmware" || exit 1
"$STAGE/tool/BLFlashCommand-macos" --interface=uart --baudrate=2000000 \
    --port="$PORT" --chipname=bl616 --config=flash_prog_cfg.ini 2>&1 | tee "$LOG"
result=${PIPESTATUS[0]}
if verify_flash_log "$LOG" "$EXPECTED" "$result"; then
    printf '\n烧录成功，Boot2、分区表和固件均已校验。\n请不按 BOOT，拔插一次适配器即可使用。\n'
else
    fail "烧录或回读校验未通过。请保留日志：${LOG}；按住 BOOT 重新插入后重试。"
fi

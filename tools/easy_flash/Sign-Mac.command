#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -u
ROOT=$(cd -- "$(dirname -- "$0")" && pwd) || exit 1
PYTHON=$(/usr/bin/xcrun --find python3 2>/dev/null) || PYTHON=""
if [[ -z "$PYTHON" ]]; then
    printf '需要 Xcode 的 Python 3。请安装并首次打开 Xcode，再运行此脚本。\n' >&2
    result=1
else
    "$PYTHON" -u "$ROOT/sign_macos_release.py" "$@"
    result=$?
fi
if [[ $# -eq 0 && -t 0 ]]; then
    printf '\n按回车关闭窗口。'
    read -r answer || true
fi
exit "$result"

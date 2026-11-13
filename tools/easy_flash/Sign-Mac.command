#!/bin/bash
cd -- "$(dirname -- "$0")" || exit 1
if ! xcrun --find clang >/dev/null 2>&1; then
    printf '请先安装 Xcode Command Line Tools：xcode-select --install\n'
    exit 1
fi
/usr/bin/python3 sign_macos.py --interactive "$@"
result=$?
printf '\n按回车关闭窗口…'
read -r _
exit "$result"

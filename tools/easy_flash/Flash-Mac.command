#!/bin/bash
cd -- "$(dirname -- "$0")" || exit 1
/bin/bash app/flash-mac.sh "$@"
result=$?
if [[ -t 0 && "${1:-}" != "--check" ]]; then
    printf '\n按回车关闭窗口…'
    read -r _
fi
exit "$result"

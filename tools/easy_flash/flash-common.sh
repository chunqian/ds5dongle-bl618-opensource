#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later

mac_tool_subdir() {
    case "$1:$2" in
        1:*|*:arm64) printf '%s\n' macos-arm64 ;;
        *:x86_64) printf '%s\n' macos ;;
        *) return 1 ;;
    esac
}

verify_assets() {
    (cd -- "$1" && /usr/bin/shasum -a 256 -c asset-hashes.txt) >"$2" 2>&1
}

verify_flash_log() {
    local log="$1" expected="$2" result="$3"
    [[ "$result" -eq 0 ]] &&
        grep -Fq 'All programming completed successfully' "$log" &&
        grep -Fq 'Flash writing succeeded' "$log" &&
        [[ $(grep -c 'Verification succeeded' "$log") -ge 3 ]] &&
        grep -Fq "SHA caled by host: $expected" "$log" &&
        grep -Fq "SHA caled by dev: $expected" "$log"
}

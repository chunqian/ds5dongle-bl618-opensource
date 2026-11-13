#!/bin/bash
# Logs must be outside the signed bundle so flashing never changes its seal.
HERE=$(cd -- "$(dirname -- "$0")" && pwd) || exit 1
export DS5_FLASH_LOG_DIR="$HOME/Library/Logs/DS5Dongle-Flash"
exec /bin/bash "$HERE/payload/Flash-Mac.command" "$@"

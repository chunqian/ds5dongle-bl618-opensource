#!/usr/bin/env bash
# LCTech BL616 — 仅编译 .bin（用于 Dev Cube 线刷）
exec bash "$(dirname "$0")/build_macos.sh" "$@"

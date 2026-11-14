# DS5Dongle BL618

[中文文档 (Chinese)](README_CN.md)

A wireless DualSense controller adapter based on BL616/BL618. It bridges a DualSense or DualSense Edge gamepad over Bluetooth Classic (BR/EDR) HID to a host PC via USB HID, appearing as a standard wired DualSense (VID 054C / PID 0CE6) or DualSense Edge (PID 0DF2). Fully compatible with Steam, SDL, PS Remote Play, and more.

Ported from [awalol/DS5Dongle](https://github.com/awalol/DS5Dongle) (Raspberry Pi Pico 2W). The BT stack was migrated from BTstack to Bouffalo SDK (Zephyr-based), and the USB stack from TinyUSB to CherryUSB.

> **Unofficial project** — not affiliated with or endorsed by Sony Interactive Entertainment. "DualSense", "DualSense Edge" and "PlayStation" are trademarks of Sony Interactive Entertainment. The USB VID/PID values (054C:0CE6 / 0DF2) are emulated so the host sees a standard wired controller; use at your own risk.

## Supported Boards

| Board | Chip | USB | LED | Debug |
|-------|------|-----|-----|-------|
| **LCTech BL616** (default) | BL616 QFN32 | Type-C native | 1x blue (GPIO27) | External USB-TTL |

> **Only LCTech BL616 is supported.** macOS and Windows build scripts generate its HS firmware.

LED pins in `board_config.h` are fixed to the LCTech BL616 layout.

## Hardware Requirements

- **LCTech BL616** dev board
- **DualSense controller** (standard 0CE6) or **DualSense Edge** (0DF2, auto-detected)
- LCTech BL616: just a USB-C cable (native USB)
- For LCTech BL616 serial debug: **USB-TTL adapter** (CH340/CH341, 3.3V)

## Getting Started

### 1. Bouffalo SDK (DS5Dongle fork — required)

> The official Bouffalo SDK will **not** work with this project — it is missing the required BT HID net_buf pools and USB Audio request handling. You must use the project's SDK fork:

```bash
git clone https://github.com/sqlCRT/bouffalo_sdk.git bouffalo_sdk
```

The build script expects the SDK in `../bouffalo_sdk` (a sibling directory of this repository) and builds against the fork's `master` branch. The fork is based on upstream v2.3.28 with targeted DS5Dongle BL618 modifications; see its README for details.

### 2. Dependencies

```bash
# macOS
brew install cmake
brew install make          # GNU Make 4.0+ required by SDK

# Linux (Ubuntu)
sudo apt install cmake make
```

### 3. RISC-V Toolchain

BL616/BL618 uses the T-Head extended ISA. A standard `riscv64-elf-gcc` will not work.

```bash
# macOS ARM64 — community pre-built T-Head toolchain
curl -L https://github.com/beckmx/bouffalo_labs_mac_toolchain/releases/download/v1.0/riscv64-unknown-elf-toolchain.tar.gz \
  | tar xz -C ~/riscv-toolchain

# Linux — the SDK ships its own toolchain, or download from T-Head
```

`build_macos.sh` auto-detects the toolchain under `~/riscv-toolchain/toolchain/bin`.

### 4. Build

```bash
cd ds5dongle-bl618

# LCTech BL616 — default
bash build_macos.sh build      # incremental compile (-j auto)
bash build_macos.sh rebuild    # clean + compile
# or: bash build_lctech616.sh rebuild

# Other commands
bash build_macos.sh clean      # clean only
bash build_macos.sh strip      # remove .o/.a to shrink build dir
```

- **Supported board: LCTech BL616 only.**
- **High-Speed (480 Mbps) firmware only.** There is no USB speed build option.

Output: `firmware/lctech616/ds5dongle-lctech616-hs.bin` (~800 KB) plus boot2/partition files and a `flash_prog_cfg.ini`. Built binaries are git-ignored — attach them to a GitHub Release if you want to distribute prebuilt firmware.

#### Windows

On Windows, clone the T-Head toolchain and use the provided script:

```bat
git clone https://gitee.com/bouffalolab/toolchain_gcc_t-head_windows.git

build_windows.bat rebuild      rem LCTech BL616, High-Speed
build_windows.bat              rem incremental build
build_windows.bat flash COM5   rem flash via serial
```


The script expects the DS5Dongle BL618 SDK fork at `..\bouffalo_sdk` (step 1) and the toolchain at `%USERPROFILE%\Desktop\toolchain_gcc_t-head_windows` by default. Override with the `BL_SDK_BASE` / `TOOLCHAIN_PATH` environment variables.

### 5. Flash

#### LCTech BL616 — Dev Cube (UART/ISP mode)

1. Build the firmware first — the build script outputs `ds5dongle-lctech616-hs.bin`, `boot2_bl616_isp_release_v8.1.8.bin` and `partition_cfg_4M_nosec.toml` into `firmware/lctech616/`.
2. Hold the **BOOT** button on the LCTech BL616, then plug the board into the PC via USB-C, keeping BOOT held until the board enters UART (ISP) download mode.
3. Open [Bouffalo Lab Dev Cube](https://dev.bouffalolab.com/download), select chip **BL616**, and use the **ISP (UART)** flashing mode.
4. Select the board's COM port, then load these files:
   - Partition table: `firmware/lctech616/partition_cfg_4M_nosec.toml`
   - Boot2: `firmware/lctech616/boot2_bl616_isp_release_v8.1.8.bin`
   - Firmware: `firmware/lctech616/ds5dongle-lctech616-hs.bin`
5. Start the download.

> Use the `_nosec` partition table (`partition_cfg_4M_nosec.toml`), not `partition_cfg_4M.toml`.

Alternatively, flash via the build script:

```bash
bash build_macos.sh flash /dev/tty.usbserial-xxx
```


## Usage

1. Connect the LCTech BL616 board's Type-C port to the target host (power + USB data). For serial debug, additionally connect a USB-TTL adapter (CH340/CH341, 3.3V)
2. At startup the adapter blinks slowly and waits; press PS on a paired controller to reconnect. To pair a new controller, **double-click the adapter BOOT button** to start fast-blinking discovery, then hold **PS + Create** on the controller for 3 seconds until its light bar flashes.
3. Watch the on-board blue LED for status (see LED table below)
4. The host should see "DualSense Wireless Controller"


### LED Status Indicators

LCTech BL616 has a single blue LED (GPIO27); states are distinguished by blink cadence:

| State | Pattern (single blue LED) |
|-------|---------------------------|
| Idle / waiting to pair | Slow blink (~1Hz) |
| Scanning | Fast blink (~3Hz) |
| Connected | Solid |
| Just disconnected | Blink (~1Hz) for ~3s, then back to idle slow blink |
| Auto-off | Off after 1 min when enabled (disabled by default) |
| Event acknowledge | Single flash |
| Bonds cleared | Triple flash |

>

### BOOT Button Gestures

| Gesture | Action |
|---------|--------|
| **Single click** | Switch to the next paired controller (up to 8 remembered; no action with fewer than two, never starts pairing) |
| **Double click** | Disconnect current controller + scan for a new one (link keys preserved) |
| **Long press (3s)** | Clear all user settings and bonds; reboot on release after the triple flash |

BOOT is sampled by a dedicated task every 5ms with 15ms debounce and a 500ms release-to-next-press double-click window. Gestures are queued to the Bluetooth task for execution, so its HCI waits do not delay button sampling.

## Features

### Implemented

- BT Classic HID Host: inquiry, SDP, L2CAP, SSP auto-pairing
- Full input passthrough: sticks, buttons, triggers, gyro, accelerometer, touchpad, battery
- Full output passthrough: rumble, RGB light bar, player indicators, adaptive triggers
- Audio passthrough (bidirectional): UAC1 4ch 48kHz OUT → Opus encode → BT 0x39 dual-frame report (547B, speaker/headset); BT mic Opus → decode → UAC1 2ch 48kHz IN (microphone)
- HD haptics: USB Audio Ch2/Ch3 → 16:1 decimation → BT 0x92 haptic tag
- DualSense Edge full support: auto-detect → unlock handshake → profile prefetch → 421B descriptor, PID auto-switch (0DF2)
- Multi-controller memory: remembers up to 8 paired controllers, single click to switch
- Controller-initiated reconnect via L2CAP server registration (passive model)
- Robust reconnection: periodic scan retry (~30s), connection watchdog (3s no-input detection), Link Supervision Timeout (5s), system reset fallback for stale ACL cleanup
- Idle timeout: configurable 0–60 min auto-disconnect (default 30 min)
- Gamepad HID only: no auxiliary keyboard, mouse, volume keys, PS keyboard shortcut or synthetic F15 wake
- Configurable polling rate: 250Hz / 500Hz / real-time (~750Hz, follows the BT report rate)
- Button remap: remap any controller button to another controller button (loads existing Flash mappings)
- MuteLight: mic button LED control (toggle on/off via the controller mute button)
- USB remote wakeup signal only; request controller power-off after 3s suspend
- USB stealth mode: hide the USB device until a controller connects (configurable)
- Custom controller light-bar color (default white), plus on-board RGB status LED
- USB serial number: unique chip ID from eFuse (configurable, on by default)
- Trigger motor power reduction: configurable 0–10 levels
- Volume lock: prevent the host from changing controller speaker/headset volume
- LED auto-off: steady LEDs turn off after 1 min when enabled (disabled by default)
- On-board low-battery alerts disabled; battery data still reaches the host through HID
- LCTech BL616: native Type-C USB, single blue LED on GPIO27
- Build-time log level control (`LOG_LEVEL` 0–3)
- FreeRTOS multi-task architecture (BT / USB / Audio / Mic)

### Known Limitations

| Item | Description |
|------|-------------|
| Single active controller | One controller connected at a time; up to 8 pairings remembered (single click switches) |

### Controller Feature Compatibility

The USB side presents DualSense-compatible HID descriptors (auto-switching between DS and Edge; standard wired layout plus DAUD audio-control Feature Reports 0xF6 / 0xF9), so hosts treat the dongle like a wired controller.

| Feature | Data Path | Supported |
|---------|-----------|-----------|
| Sticks / Buttons / Triggers | HID Input passthrough | Yes |
| Gyro / Accelerometer | HID Input passthrough | Yes |
| Touchpad | HID Input passthrough | Yes |
| Battery level | HID Input passthrough | Yes (no on-board low-battery LED alert) |
| **Adaptive triggers** | HID Output SetStateData | Yes |
| **Rumble** | HID Output SetStateData | Yes |
| RGB light bar / Player LEDs | HID Output SetStateData | Yes |
| **HD haptics** | USB Audio Ch2/Ch3 → BT 0x92 | Yes |
| Controller speaker | USB Audio Ch0/Ch1 → Opus → BT 0x39 tag 0x93 | Yes |
| Controller microphone | BT Input → Opus decode → USB Audio IN | Yes |
| 3.5mm headset (output) | USB Audio → Opus → BT 0x39 tag 0x96 | Yes |
| Mic mute LED | BT Input mute button → MuteLight control | Yes |

## Configuration

Web configuration and its HID settings APIs have been removed. Firmware uses source defaults and can load existing Flash settings. Hold BOOT for 3 seconds to clear all settings. Changing defaults requires rebuilding. Highlights (defaults):

| Option | Default |
|--------|---------|
| Controller mode | Auto (DS5 / Edge / Auto) |
| Polling rate | 250Hz (250 / 500 / real-time ~750Hz) |
| Idle auto-disconnect | 30 min (0–60, 0 = off) |
| LED auto-off | On (after 1 min) |
| Custom light-bar color | White |
| USB serial number | On |
| USB stealth mode | Off |
| PS shortcut | Off |
| USB remote wakeup | Off |
| Haptics gain | 1.0 (1.0–2.0) |
| Trigger motor reduction | 0 (0–10) |
| Volume lock | Off |
| Mic / speaker passthrough | On |

## Project Structure

```
src/
├── main.c              Entry + FreeRTOS task orchestration + data bridge
├── bt_hid_host.c/h     BT Classic HID Host (Inquiry + SDP + L2CAP + SSP)
├── ds5_protocol.c/h    DualSense protocol definitions + CRC32
├── usb_gamepad.c/h     USB device (Gamepad HID + optional audio)
├── ds5_usb_audio.c/h   USB Audio Class 1 (4ch 48kHz ISO OUT + 2ch 48kHz ISO IN)
├── audio.c/h           Audio pipeline (sinc resample + Opus encode/decode + haptics + mic)
├── usb_wake.c/h        USB remote wakeup FSM
├── state_mgr.c/h       SetStateData conditional merge manager
├── config.c/h          Defaults and legacy Flash loading (bt_settings)
├── dse.c/h             DualSense Edge profile management
├── remap.c/h           Button remap
├── led_status.c/h      LED status indicator (LCTech single blue LED)
├── board_config.h      Board abstraction (LED pins/polarity, USB type, board name)
├── debug_log.h         Build-time log level macros (LOG_ERR/WRN/INF/DBG/ISR)
└── FreeRTOSConfig.h    FreeRTOS configuration
lib/
├── opus/               Opus codec (fixed-point, xiph/opus)
├── opus.cmake          Opus source file list
└── opus_config.h       Opus build configuration
firmware/               Board flash configs + local build output (binaries git-ignored)
```

## Architecture

```
┌──────────────┐          ┌──────────────┐          ┌──────────────┐
│  DualSense   │◄─ BT ──►│ BL616/BL618  │◄─ USB ──►│   Host PC    │
│  Controller  │  BR/EDR  │ LCTech/M0S/ │  HID     │  Steam/SDL   │
└──────────────┘  HID     │ LCTech616   │  Device   └──────────────┘
                          └──────────────┘
```

**Data flows:**

- **Input (Controller → Host):** BT L2CAP receives Report 0x31 → strip HID header/seq/CRC → 63-byte payload sent as USB Report 0x01
- **Output (Host → Controller):** USB EP OUT receives Report 0x02 → State Manager conditional merge → BT Report 0x31 (78B with CRC32) → L2CAP send
- **Audio OUT (Host → Controller):** USB Audio ISO OUT (4ch 48kHz) → double-buffer PCM accumulation → polyphase sinc resample 512→480 → Opus CBR encode (160kbps) → haptics decimation → 0x39 dual-frame report (547B) → L2CAP send
- **Audio IN (Controller → Host):** BT 0x31 mic Opus frame → queue → Opus decode (48kHz mono) → mono-to-stereo → ring buffer → USB Audio ISO IN (2ch 48kHz)
- **Feature (bidirectional):** GET_REPORT from BT-side cache (DSE profiles support NAK gating) | SET_REPORT adds CRC32 and forwards via L2CAP control channel

## USB Audio Control

USB audio is hidden by default. CR on macOS enables it using the DAUD HID
protocol; iOS/iPadOS leaves it hidden. Changing visibility causes USB re-enumeration.
Authorization is held in RAM and is revoked on a new host session. Hiding audio
also disables audio-driven HD haptics and microphone streaming.

Offsets below include the Report ID:

- `GET_FEATURE 0xF9`: byte 0 is `F9`; bytes 1–13 are reserved and zero.
  Bytes 14–17 contain `DAUD`, byte 18 is protocol version `1`, and bytes 19–21
  report current visibility, requested visibility, and pending transition.
- `SET_FEATURE 0xF6`: send `F6 04 44 41 55 44 01 XX`, padded to 64 bytes;
  `XX=01` enables audio and `XX=00` hides it. Check the DAUD marker before sending
  commands; Sony VID/PID alone does not identify a compatible adapter.

Legacy web settings, save, re-enumeration, version/telemetry queries and remap
editing commands are removed. Native controller HID reports remain supported.
Controller-type memory and physical remap-profile selection persist independently
through the deferred store; they never save the entire preferences structure.

## Acknowledgements

- [awalol/DS5Dongle](https://github.com/awalol/DS5Dongle) — original Pico 2W implementation, core protocol reference
- [bouffalolab/bouffalo_sdk](https://github.com/bouffalolab/bouffalo_sdk) — BL618 SDK + Zephyr BT stack
- [CherryUSB](https://github.com/cherry-embedded/CherryUSB) — USB stack
- [xiph/opus](https://github.com/xiph/opus) — Opus audio codec (fixed-point mode)
- Linux kernel `hid-playstation.c` — DualSense protocol offset reference
- BL618 porting developed with [Cursor](https://www.cursor.com/) + Claude Opus 4.6

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE) (GPL-3.0). Anyone who uses or modifies this code in a distributed product must make their source code available under the same license.

### Third-Party Notices

- Code is ported/adapted from [awalol/DS5Dongle](https://github.com/awalol/DS5Dongle), which is licensed under the MIT License (Copyright (c) 2026 awalol) — see [NOTICE](NOTICE) for the full text.
- [lib/opus](lib/opus) is the [xiph/opus](https://github.com/xiph/opus) codec, BSD-3-Clause licensed (see `lib/opus/LICENSE_PLEASE_READ.txt`).
- [CherryUSB](https://github.com/cherry-embedded/CherryUSB) and [bouffalolab/bouffalo_sdk](https://github.com/bouffalolab/bouffalo_sdk) are external build dependencies, Apache-2.0 licensed.
- The Linux kernel `hid-playstation.c` (GPL-2.0) was used as a protocol/offset reference only; no kernel code is included.

Auxiliary HID has been removed. Legacy keyboard/mouse mappings revert to their
original gamepad buttons; touchpad mouse modes are disabled. Gamepad button and
touchpad direction remapping remain available. Configuration offsets and lengths
remain compatible with existing Flash settings. Hosts that
relied on synthetic F15 wake input need wake compatibility testing.

# DS5Dongle BL618

基于 BL616/BL618 的 DualSense 无线手柄适配器。将 DualSense / DualSense Edge 手柄通过蓝牙经典（BR/EDR）HID 连接到 BL616/BL618，再通过 USB HID 透传给主机，主机端呈现为标准有线 DualSense（VID 054C / PID 0CE6）或 DualSense Edge（PID 0DF2）。Steam、SDL、PS Remote Play 等均可直接识别。

移植自 [awalol/DS5Dongle](https://github.com/awalol/DS5Dongle)（Raspberry Pi Pico 2W 版本）：BT 栈从 BTstack 迁移至 Bouffalo SDK（Zephyr 内核），USB 栈从 TinyUSB 迁移至 CherryUSB。

> **非官方项目** —— 与索尼互动娱乐（Sony Interactive Entertainment）无关，亦未获其认可。"DualSense"、"DualSense Edge" 和 "PlayStation" 是索尼互动娱乐的商标。USB VID/PID（054C:0CE6 / 0DF2）仅用于模拟，使主机将其识别为有线手柄；使用风险自负。

## 支持的开发板

| 开发板 | 芯片 | USB | LED | 调试 |
|--------|------|-----|-----|------|
| **LCTech BL616**（默认） | BL616 QFN32 | Type-C 原生 | 1x 蓝色（GPIO27） | 外接 USB-TTL |

> **仅支持 LCTech BL616。** macOS 和 Windows 构建入口均只生成该板型的 HS 固件。

LED 引脚在 `board_config.h` 中固定为 LCTech BL616 配置。

## 硬件要求

- **LCTech BL616** 开发板
- **DualSense 手柄**（标准版 0CE6）或 **DualSense Edge**（0DF2，自动识别）
- LCTech BL616：仅需 USB-C 线（原生 USB）
- LCTech BL616 串口调试：**USB-TTL 适配器**（CH340/CH341，3.3V）

## 快速开始

### 1. 安装 Bouffalo SDK（必须使用本项目分支）

> 官方 Bouffalo SDK **无法**用于本项目——缺少固件所需的 BT HID net_buf 池和 USB Audio 请求处理。必须使用本项目维护的 SDK 分支：

```bash
git clone https://github.com/sqlCRT/bouffalo_sdk.git bouffalo_sdk
```

构建脚本要求 SDK 位于 `../bouffalo_sdk`（与本仓库同级目录），直接使用该分支的 `master` 即可。该分支基于上游 v2.3.28，并针对 DS5Dongle BL618 做了针对性修改，详见其 README。

### 2. 安装依赖

```bash
# macOS
brew install cmake
brew install make          # SDK 需要 GNU Make 4.0+

# Linux (Ubuntu)
sudo apt install cmake make
```

### 3. 安装 RISC-V 工具链

BL616/BL618 使用 T-Head 扩展指令集，标准 `riscv64-elf-gcc` 无法使用。

```bash
# macOS ARM64 — 社区预编译 T-Head 工具链
curl -L https://github.com/beckmx/bouffalo_labs_mac_toolchain/releases/download/v1.0/riscv64-unknown-elf-toolchain.tar.gz \
  | tar xz -C ~/riscv-toolchain

# Linux — SDK 自带工具链，或从 T-Head 官方下载
```

`build_macos.sh` 会自动在 `~/riscv-toolchain/toolchain/bin` 下查找工具链。

### 4. 编译

```bash
cd ds5dongle-bl618

# LCTech BL616 — 默认
bash build_macos.sh build      # 增量编译（-j 自动并行）
bash build_macos.sh rebuild    # 清理后重新编译
# 或：bash build_lctech616.sh rebuild

# 其他命令
bash build_macos.sh clean      # 仅清理
bash build_macos.sh strip      # 删除 .o/.a 中间文件，缩小 build 目录
```

- **仅支持 LCTech BL616。**
- **仅构建 High-Speed（480Mbps）固件。** 不再提供 USB 速度构建选项。

产物位于 `firmware/lctech616/ds5dongle-lctech616-hs.bin`（约 800KB），以及 boot2/partition 文件和 `flash_prog_cfg.ini`。编译产物已被 git 忽略 —— 如需分发预编译固件，建议附加到 GitHub Release。

#### Windows 编译

Windows 下先克隆 T-Head 工具链，然后使用仓库自带的脚本：

```bat
git clone https://gitee.com/bouffalolab/toolchain_gcc_t-head_windows.git

build_windows.bat rebuild      rem LCTech BL616，High-Speed
build_windows.bat              rem 增量编译
build_windows.bat flash COM5   rem 串口烧录
```


脚本默认要求 DS5Dongle BL618 SDK 分支位于 `..\bouffalo_sdk`（见步骤 1），工具链位于 `%USERPROFILE%\Desktop\toolchain_gcc_t-head_windows`。可通过环境变量 `BL_SDK_BASE` / `TOOLCHAIN_PATH` 覆盖。

### 5. 烧录

已有固件可制作 **macOS 和 Windows 两个独立简易离线烧录包**：完整解压后双击对应启动器，
按住 BOOT 插入设备，再按回车即可自动查找端口、烧录和校验，无需安装 SDK 或 Python。
当前包针对 LCTech BL616 的 `3.18H-HidAudio-R5` HS 固件；Windows 包面向 x64，macOS 包自动选择 Apple 芯片 / Intel 原生程序，无需 Rosetta。
使用与打包说明见 [tools/easy_flash/README.md](tools/easy_flash/README.md)。
macOS 另提供可复制到另一台 Mac 的 Developer ID 签名、公证工具包，步骤见 [SIGNING.txt](tools/easy_flash/SIGNING.txt)。

#### LCTech BL616 — Dev Cube（UART/ISP 模式）

1. 先完成编译——构建脚本会把 `ds5dongle-lctech616-hs.bin`、`boot2_bl616_isp_release_v8.1.8.bin` 和 `partition_cfg_4M_nosec.toml` 输出到 `firmware/lctech616/`。
2. 按住 LCTech BL616 的 **BOOT** 键，然后通过 USB-C 插入电脑，保持按住直到开发板进入 UART（ISP）下载模式。
3. 打开 [Bouffalo Lab Dev Cube](https://dev.bouffalolab.com/download)，选择芯片 **BL616**，使用 **ISP（UART）烧录模式**。
4. 选择开发板对应的串口，然后加载以下文件：
   - 分区表：`firmware/lctech616/partition_cfg_4M_nosec.toml`
   - Boot2：`firmware/lctech616/boot2_bl616_isp_release_v8.1.8.bin`
   - 固件：`firmware/lctech616/ds5dongle-lctech616-hs.bin`
5. 开始烧录。

> 请使用 `_nosec` 分区表（`partition_cfg_4M_nosec.toml`），不要使用 `partition_cfg_4M.toml`。

也可以直接用构建脚本烧录：

```bash
bash build_macos.sh flash /dev/tty.usbserial-xxx
```


## 使用方法

1. 将 LCTech BL616 开发板的 Type-C 口连接到目标主机（供电 + USB 数据）；如需串口调试，另接 USB-TTL 适配器（CH340/CH341，3.3V）
2. 启动后适配器慢闪等待，已配对手柄可按 PS 键回连；配对新手柄时，先**双击适配器 BOOT** 进入扫描快闪，再同时长按手柄 **PS + Create** 3 秒，直到灯条闪烁
3. 观察板载蓝色 LED 状态（见下方 LED 状态表）
4. 主机应识别出 "DualSense Wireless Controller"


### LED 状态指示

LCTech BL616 为单颗蓝色 LED（GPIO27），不同状态以闪烁节奏区分：

| 状态 | 模式（单颗蓝灯） |
|------|------------------|
| 空闲 / 等待配对 | 慢闪（~1Hz） |
| 扫描中 | 快闪（~3Hz） |
| 已连接 | 常亮 |
| 刚断开 | 闪烁（~1Hz）约 3 秒，随后回到空闲慢闪 |
| 自动熄灭 | 启用自动熄灯后，1 分钟熄灭（默认关闭） |
| 事件确认 | 闪一次 |
| 清除配对 | 连闪三次 |

>

### BOOT 按键手势

| 手势 | 功能 |
|------|------|
| **单击** | 切换到下一个已配对手柄；仅一个时断开并等待该手柄回连，没有配对记录时不操作（不触发新配对） |
| **双击** | 断开当前手柄 + 重新扫描配对新手柄（保留 link key） |
| **长按 3 秒** | 恢复全部用户设置（含两个按键映射档案、配对记录、黑名单）；三次闪烁确认后松开，自动重启并使用实时档 |

长按操作须在适配器正常运行时进行；按住 BOOT 再插电仍是下载模式。未满 3 秒不会恢复默认。

BOOT 由独立任务每 5ms 采样，使用 15ms 消抖；两次按下之间的松开间隔在 500ms 内可识别为双击。手势通过队列交给蓝牙任务执行，不受该任务等待蓝牙命令时的轮询间隔影响。
已有保存的档位在升级时保留，首次启动或执行恢复默认后使用实时档。

## 功能特性

### 已实现

- BT Classic HID Host：Inquiry、SDP、L2CAP、SSP 自动配对
- 完整输入透传：摇杆、按键、扳机、陀螺仪、加速度计、触摸板、电量
- 完整输出透传：震动、RGB 灯条、玩家指示灯、自适应扳机
- 双向音频透传：UAC1 4ch 48kHz OUT → Opus 编码 → BT 0x39 双帧报告（547B，扬声器/耳机）；BT 麦克风 Opus → 解码 → UAC1 2ch 48kHz IN
- HD 触觉反馈：USB Audio Ch2/Ch3 → 16:1 降采样 → BT 0x92 触觉 tag
- DualSense Edge 完整支持：自动识别 → unlock 握手 → profile 预取 → 421B 描述符，PID 自动切换（0DF2）
- 多手柄记忆：最多记忆 8 个已配对手柄，单击按键快速切换
- 手柄主动回连：L2CAP 服务端注册（被动模式）
- 稳健重连：周期性扫描重试（~30s）、连接看门狗（3s 无输入检测）、链路监督超时（5s）、陈旧 ACL 清理的系统复位兜底
- 空闲超时：可配置 0–60 分钟自动断开（默认 30 分钟）
- 仅呈现手柄 HID：不提供辅助键盘、鼠标、音量键、PS 键盘快捷键或 F15 模拟唤醒
- 轮询率可配置：250Hz / 500Hz / 实时档（~750Hz，跟随 BT 报告率）
- 按键映射：可将手柄任意按键重映射为其他手柄按键（加载已有 Flash 映射）
- MuteLight：通过手柄静音键开关麦克风指示灯
- USB 远程唤醒：仅发送远程唤醒信号；挂起 3 秒后请求手柄关机
- USB 隐身模式：可配置为手柄连接前不枚举 USB 设备
- 手柄灯条自定义颜色（默认白色）+ 板载 RGB 状态灯
- USB 序列号：eFuse 芯片唯一 ID（可配置，默认开启）
- 扳机电机功率限制：可配置 0–10 档
- 音量锁定：阻止主机修改手柄扬声器/耳机音量
- LED 自动熄灭：启用后常亮 LED 1 分钟后关闭（默认关闭）
- 板载低电量告警已关闭；电量仍通过 HID 上报主机
- LCTech BL616：原生 Type-C USB，GPIO27 单蓝灯
- 构建期日志级别控制（`LOG_LEVEL` 0–3）
- FreeRTOS 多任务架构（BT / USB / Audio / Mic）

### 已知限制

| 项目 | 说明 |
|------|------|
| 单手柄在线 | 同一时刻只能连接一个手柄；最多记忆 8 个配对（单击切换） |

### 手柄功能兼容性

USB 端提供 DualSense 兼容的 HID 描述符（DS 与 Edge 自动切换；标准有线布局 + DAUD 声卡控制 Feature Report 0xF6 / 0xF9），主机将其视为有线手柄。

| 功能 | 数据路径 | 支持 |
|------|----------|------|
| 摇杆 / 按键 / 扳机 | HID Input 透传 | 是 |
| 陀螺仪 / 加速度计 | HID Input 透传 | 是 |
| 触摸板 | HID Input 透传 | 是 |
| 电池电量 | HID Input 透传 | 是（板载 LED 不再提示低电量） |
| **自适应扳机** | HID Output SetStateData | 是 |
| **震动** | HID Output SetStateData | 是 |
| RGB 灯条 / 玩家指示灯 | HID Output SetStateData | 是 |
| **HD 触觉反馈** | USB Audio Ch2/Ch3 → BT 0x92 | 是 |
| 手柄扬声器 | USB Audio Ch0/Ch1 → Opus → BT 0x39 tag 0x93 | 是 |
| 手柄麦克风 | BT Input → Opus 解码 → USB Audio IN | 是 |
| 3.5mm 耳机（输出） | USB Audio → Opus → BT 0x39 tag 0x96 | 是 |
| 麦克风静音灯 | BT Input 静音键 → MuteLight 控制 | 是 |

## 配置项

网页配置及其 HID 读写接口已移除。固件使用源码默认值，并兼容加载已有 Flash 设置；长按 BOOT 3 秒可清除全部设置。修改默认参数需要重新编译。主要配置项（默认值）：

| 配置项 | 默认值 |
|--------|--------|
| 手柄模式 | Auto（DS5 / Edge / Auto） |
| 轮询率 | 实时档（只发送新帧；可选 250Hz / 500Hz） |
| 空闲自动断开 | 30 分钟（0–60，0 = 关闭） |
| LED 自动熄灭 | 开启（1 分钟后） |
| 灯条自定义颜色 | 白色 |
| USB 序列号 | 开启 |
| USB 隐身模式 | 关闭 |
| USB 远程唤醒 | 关闭 |
| 触觉增益 | 1.0（1.0–2.0） |
| 扳机电机限制 | 0（0–10） |
| 音量锁定 | 关闭 |
| 麦克风 / 扬声器透传 | 开启 |

## 项目结构

```
src/
├── main.c              入口 + FreeRTOS 任务编排 + 数据桥接
├── bt_hid_host.c/h     BT Classic HID Host（Inquiry + SDP + L2CAP + SSP）
├── ds5_protocol.c/h    DualSense 协议定义 + CRC32
├── usb_gamepad.c/h     USB 设备（手柄 HID + 可选音频）
├── ds5_usb_audio.c/h   USB Audio Class 1（4ch 48kHz ISO OUT + 2ch 48kHz ISO IN）
├── audio.c/h           音频处理管线（sinc 重采样 + Opus 编解码 + 触觉 + 麦克风）
├── usb_wake.c/h        USB 远程唤醒 FSM
├── state_mgr.c/h       SetStateData 条件合并管理器
├── config.c/h          默认值与旧 Flash 配置加载（bt_settings）
├── dse.c/h             DualSense Edge Profile 管理
├── remap.c/h           按键映射
├── led_status.c/h      LED 状态指示（LCTech 单蓝灯）
├── board_config.h      板级抽象（LED 引脚/极性、USB 类型、板名）
├── debug_log.h         构建期日志级别宏（LOG_ERR/WRN/INF/DBG/ISR）
└── FreeRTOSConfig.h    FreeRTOS 配置
lib/
├── opus/               Opus 编解码库（定点模式，xiph/opus）
├── opus.cmake          Opus 源文件列表
└── opus_config.h       Opus 构建配置
firmware/               板级烧录配置 + 本地编译产物（二进制已 git 忽略）
```

## 架构

```
┌──────────────┐          ┌──────────────┐          ┌──────────────┐
│  DualSense   │◄─ BT ──►│ BL616/BL618  │◄─ USB ──►│   Host PC    │
│  Controller  │  BR/EDR  │ LCTech/M0S/ │  HID     │  Steam/SDL   │
└──────────────┘  HID     │ LCTech616   │  Device   └──────────────┘
                          └──────────────┘
```

**数据流：**

- **输入（手柄 → 主机）**：BT L2CAP 接收 Report 0x31 → 剥离 HID header/seq/CRC → 63 字节 payload 作为 USB Report 0x01 发送
- **输出（主机 → 手柄）**：USB EP OUT 接收 Report 0x02 → State Manager 条件合并 → BT Report 0x31（78B 含 CRC32）→ L2CAP 发送
- **音频输出（主机 → 手柄）**：USB Audio ISO OUT（4ch 48kHz）→ 双缓冲 PCM 累积 → polyphase sinc 重采样 512→480 → Opus CBR 编码（160kbps）→ 触觉降采样 → 0x39 双帧报告（547B）→ L2CAP 发送
- **音频输入（手柄 → 主机）**：BT 0x31 麦克风 Opus 帧 → 队列 → Opus 解码（48kHz 单声道）→ 单声道转立体声 → 环形缓冲 → USB Audio ISO IN（2ch 48kHz）
- **Feature（双向）**：GET_REPORT 从 BT 侧缓存返回（DSE profile 支持 NAK gating）| SET_REPORT 附加 CRC32 后经 L2CAP 控制通道转发

## USB 声卡控制

### 本地 HidAudio 固件：由 Mac 应用开启声卡

`HidAudio` 版本上电时仅呈现 HID 手柄，不呈现 USB
扬声器、耳机和麦克风。CR 的 macOS 版本在发现支持此协议的 USB 适配器后，
发送专用 HID Feature 命令开启声卡；iOS/iPadOS 版本不发送命令，因此保持纯 HID。
不再通过 USB 请求特征推测操作系统。未运行支持此协议的软件时，Mac 上也没有声卡。

切换后会有约 250ms 的 USB 断开，系统随后重新枚举手柄和音频接口。设置仅在 RAM
中保存，不写入配置 Flash；断电、物理拔插或已完成枚举后的新 USB reset 会恢复隐藏，
Mac 应用需要重新发送命令。关闭声卡时音频驱动的 HD 震动和麦克风不可用，HID 输入、
陀螺仪、扳机及 HID 普通震动通道保留。退出 CR 不主动关闭已开启的声卡。

`HidAudio-R1` 修正复位恢复：新 USB reset 撤销声卡授权时，先主动断开 USB，再在任务中
切换为纯 HID，避免主机缓存的接口编号失效。重连等待期间若再次收到 reset、拔插或相反
命令，旧开启请求会失效，最终保持隐藏；Mac 可在新一轮 HID 枚举后重新授权开启。

`HidAudio-R2` 将时间采样与 HID 请求状态读取放入同一临界区，并检查时间先后关系，
防止新命令插入时无符号时间差下溢、跳过 100ms 的 USB 控制传输完成等待。

音频接口的主机打开状态与蓝牙手柄连接状态分别维护：USB 挂起时暂停端点，恢复时按
原有接口选择重新启动；仅蓝牙断开时清空旧音频，保留 USB 接口状态。若 USB 没有重新
枚举，手柄重连后会重新发送麦克风启停指令，发送失败继续重试。USB reset、拔插和
HID 关闭声卡仍撤销接口状态。编码器及其帧缓存只由音频任务重置，过期会话的数据不再提交。

Auto 模式的手柄类型使用独立的 `detected_type` 记录。手柄类型记忆和手柄按键选择的
重映射档位由任务写入 Flash，失败后间隔一秒重试，不会保存整份用户配置。
网页参数读写、保存、重枚举、版本与遥测查询、重映射编辑接口均已删除。
仅保留 CR 使用的 DAUD 声卡控制协议；原生手柄 HID 输入、输出和 Feature 透传仍然保留。

协议（下列偏移均包含 Report ID）：

- `GET_FEATURE 0xF9`：字节 0 为 `F9`，字节 1–13 保留并置零；字节 14–17 为 ASCII `DAUD`，
  字节 18 为协议版本 `1`，字节 19 为当前声卡可见状态，20 为请求状态，21 为待切换状态。
- `SET_FEATURE 0xF6`：发送 `F6 04 44 41 55 44 01 XX` 并补零到 64 字节；
  `XX=01` 开启，`XX=00` 关闭。主机必须先检查上述专用标记，不能仅凭 Sony VID/PID
  向原装手柄或旧固件发送命令。重复发送相同状态不会触发重新枚举。

构建：`bash build_macos.sh build`。
本地验证：

```bash
clang -std=c11 -Wall -Wextra -Werror -fsanitize=address,undefined -Isrc \
  tests/test_usb_audio_control.c src/usb_audio_control.c -o /tmp/test-usb-audio-control
/tmp/test-usb-audio-control
python3 tests/test_ds5_calibration.py
python3 tests/test_usb_audio_reconnect.py
python3 tests/test_audio_lifecycle.py
python3 tests/test_config_persistence.py
```

## 致谢

- [awalol/DS5Dongle](https://github.com/awalol/DS5Dongle) —— 原始 Pico 2W 实现，核心协议参考
- [bouffalolab/bouffalo_sdk](https://github.com/bouffalolab/bouffalo_sdk) —— BL618 SDK + Zephyr BT 栈
- [CherryUSB](https://github.com/cherry-embedded/CherryUSB) —— USB 协议栈
- [xiph/opus](https://github.com/xiph/opus) —— Opus 音频编解码库（定点模式）
- Linux 内核 `hid-playstation.c` —— DualSense 协议偏移参考
- BL618 移植由 [Cursor](https://www.cursor.com/) + Claude Opus 4.6 协助开发

## 许可证

本项目采用 [GNU General Public License v3.0](LICENSE)（GPL-3.0）许可。任何在分发产品中使用或修改本代码的人，必须以相同许可证开放其源代码。

### 第三方声明

- 代码移植/改编自 [awalol/DS5Dongle](https://github.com/awalol/DS5Dongle)，其采用 MIT 许可证（Copyright (c) 2026 awalol）—— 完整文本见 [NOTICE](NOTICE)。
- [lib/opus](lib/opus) 为 [xiph/opus](https://github.com/xiph/opus) 编解码库，BSD-3-Clause 许可（见 `lib/opus/LICENSE_PLEASE_READ.txt`）。
- [CherryUSB](https://github.com/cherry-embedded/CherryUSB) 与 [bouffalolab/bouffalo_sdk](https://github.com/bouffalolab/bouffalo_sdk) 为外部构建依赖，Apache-2.0 许可。
- Linux 内核 `hid-playstation.c`（GPL-2.0）仅作为协议/偏移参考，未包含内核代码。

辅助 HID 功能已移除。旧配置中的键盘／鼠标映射恢复为对应的原手柄按键，
触摸板鼠标模式关闭；普通手柄按键重映射和触摸板方向映射仍可使用。
配置数据长度和字段偏移保持兼容，以便加载旧 Flash 设置。
唤醒不再发送 F15，依赖模拟键盘按键的主机唤醒行为需要重新实测。

# LCTech BL616 简易烧录包

为已验证的 `3.18H-HidAudio-R4` HS 固件生成两个独立离线 ZIP，运行文件按平台隔离：

| 包 | 启动入口 | 内置程序 |
| --- | --- | --- |
| `DS5Dongle-LCTech616-HS-HidAudio-R4-Flash-Windows.zip` | `Flash-Windows.bat` | Windows x64 |
| `DS5Dongle-LCTech616-HS-HidAudio-R4-Flash-Mac.zip` | `Flash-Mac.command` | macOS ARM64、Intel x86_64 |

用户完整解压对应 ZIP，按住 BOOT 插入设备，双击启动器并按回车，
即可自动选择下载端口、烧录和校验。每包附本平台使用说明、固件、校验清单和源码。
Mac 包尚未完成 Developer ID 签名和 Apple 公证。
需要在另一台 Mac 签名、公证时，可生成下述独立签名工具包。

目标是 LCTech BL616；BOOT VID/PID 只识别芯片，不区分采用相同芯片的开发板。
Windows 目标为 Windows 10/11 x64；Mac 目标为 macOS 11+，自动选择 ARM64 或 x86_64
原生程序，无需 Rosetta。即使启动工具的终端在 Rosetta 下运行，也会选择 ARM64 程序。

## 维护者生成安装包

两个包都需要 Python、Git、对应固件和固定提交的 `bouffalo_sdk` 仓库。
仅生成 Windows 包不需要 macOS 原生运行时或 Xcode；生成 Mac 包还需要 Xcode Command Line Tools
及下述 ARM64 和 Intel 运行时。用户运行 ZIP 时不需要这些开发依赖。

生成 Mac 包时，先在 Apple 芯片 Mac 上构建 ARM64 程序。发布版使用 python-build-standalone 的 macOS 11+ 运行时，避免把
本机 Homebrew 的较高系统版本依赖带入安装包。下载并校验后解压到 `build/native_python/`：

- [CPython 3.12.16 ARM64，20261110](https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.12.16%2B20261110-aarch64-apple-darwin-install_only_stripped.tar.gz)
- SHA-256：`2f11b8aa3726bdbd87dd9eb76568412faddf4f47e26913fa45d6a2e424972acd`

```sh
build/native_python/python/bin/python3.12 -m venv build/native_flasher_portable_venv
build/native_flasher_portable_venv/bin/python -m pip install -r tools/easy_flash/native-requirements.txt
build/native_flasher_portable_venv/bin/python tools/easy_flash/build_native_macos.py \
  --python-licenses build/python_notices/python/licenses
```

构建器会下载并校验固定 SHA-256 的 `bflb-flash-command 1.0.0` MIT 源码，原样打包为
独立程序；不修改上游烧录逻辑。它会检查 ARM64 架构、Rosetta 状态、Mach-O 最低系统
版本和动态库依赖，并收集第三方许可。可用 `--archive` 指定本地源码压缩包，
`--python-provenance` 指定包含 Python 下载 URL / SHA-256 的 JSON 以记录构建来源。
默认产物在 `build/native_macos/`。发布构建前还应从同一 Python 发布的
[完整归档](https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.12.16%2B20261110-aarch64-apple-darwin-pgo%2Blto-full.tar.zst)
提取 `python/licenses/` 到 `build/python_notices/`，并通过 `--python-licenses` 带入
静态链接依赖的许可。该完整归档的 SHA-256 为
`4da072af9dbbc2557d3aabf51db553a19694f6ac61b5b7169749e4acfa5a052d`。

准备 Intel 运行时（同样用于普通 Mac 包和签名包）：

```sh
# 在 x86_64 Python 环境执行；Apple 芯片维护者构建时可使用已安装的 Rosetta。
# Python 3.12.16 x86_64 / 20261110 的 install_only_stripped 归档 SHA-256：
# b78a4c479f1964ec484880bee567568266f68da102269fb93a05fe8716b45f91
/path/to/x86_64/python3.12 -m venv build/intel_build_venv
build/intel_build_venv/bin/python -m pip install -r tools/easy_flash/native-requirements.txt
build/intel_build_venv/bin/python tools/easy_flash/build_native_macos.py \
  --arch x86_64 --output build/native_macos_intel \
  --python-provenance build/intel-python-provenance.json \
  --python-licenses build/python_notices/python/licenses

```

Python 归档位于同一 [python-build-standalone 发布](https://github.com/astral-sh/python-build-standalone/releases/tag/20261110)，
文件名为 `cpython-3.12.16+20261110-x86_64-apple-darwin-install_only_stripped.tar.gz`。
`intel-python-provenance.json` 记录实际下载的 `url`、`sha256`、`tag`；许可证目录应来自对应 Python 发布。
最终使用者及签名机器无需 Rosetta：两种架构的运行时已在工具包内准备好。

生成两个包（默认 `--platform both`）：

```sh
python3 tools/easy_flash/build_package.py
```

也可分别生成：

```sh
python3 tools/easy_flash/build_package.py --platform windows
python3 tools/easy_flash/build_package.py --platform macos
```

可用 `--sdk /path/to/bouffalo_sdk`、`--firmware-dir /path/to/firmware/lctech616`、
`--native-dir /path/to/native_macos`、`--intel-native-dir /path/to/native_macos_intel` 指定已有输入，`--output` 指定输出目录。
Windows 单独打包不会读取两套 Mac 运行时，也不会执行 Xcode 或 codesign。
默认产物位于 `build/easy_flash/`，每个平台各有 ZIP、`.zip.sha256` 和解压目录。
运行目录 `app/`、`tools/` 只包含对应平台所需文件；`source/` 保留完整构建源码。

Windows 保留 SDK 原厂 1.4.2 程序；Intel Mac 与 Apple 芯片均使用开源 `bflb-flash-command 1.0.0`，
从相同源码构建 PyInstaller onedir 独立运行时。打包器要求两者的后端版本、源码 SHA-256 和 Python 版本一致。
脚本从固定 SDK 提交读取原厂程序及配置；Git 部分克隆缺少对象时，首次生成可能需要联网。既有芯片运行日志、
生成的 Flash 镜像和本地 loader 配置不会被复制。

脚本固定校验此前实机通过的 R4 固件 SHA-256，避免把普通版或错误版本误装进包。
新固件发布时须明确更新 `VERSION`、`FIRMWARE_SHA`、`NAME` 和使用说明，并重新验证；使用说明的固件版本由打包器自动填充。
源码需先提交，生成器会拒绝 `src/`、`CMakeLists.txt` 中与 HEAD 不符的改动。

解压包里的 `source/firmware-source.tar.gz` 是固件仓库源码快照。
如需从该快照继续开发，将 `source/easy_flash/` 复制到源码的 `tools/easy_flash/`，
并恢复对应 Git 提交或在自己的仓库提交源码，再生成安装包；构建依赖见根 README。

## 在另一台 Mac 上签名、公证

`build_signing_kit.py` 生成独立 ZIP，内含已准备好两套 onedir 运行时的待签名包、
App 启动器源码和 `Sign-Mac.command`。签名机器只需 Xcode Command Line Tools
及其 Python 3、带私钥的 Developer ID Application 证书和 notarytool 钥匙串凭据。
签名工具包不含任何私钥、密码或账户配置。详细步骤见 [SIGNING.txt](SIGNING.txt)。

签名工具直接使用普通 Mac 包内的两套运行时，不再替换后端或额外携带一份 Intel 程序。

```sh
python3 tools/easy_flash/build_signing_kit.py \
  --package build/easy_flash/DS5Dongle-LCTech616-HS-HidAudio-R4-Flash-Mac
```

把 `*-Mac-Signing-Kit.zip` 完整解压到另一台 Mac，双击 `Sign-Mac.command`。
工具先逐个签名内部 Mach-O 文件，更新哈希，再签名 `.app`；提交 Apple 公证后，
仅在 Accepted、stapler 和 Gatekeeper 检查均通过时生成 `*-Signed-Notarized.zip`。
`--sign-only` 产物明确标记 `Signed-Unnotarized`，不会冒充公证成功。
每次运行使用独立工作目录，保留审核日志，不覆盖输入包和历史产物。

App 会在 Terminal 中启动烧录流程，日志放在 `~/Library/Logs/DS5Dongle-Flash`，
保持 App 签名资源不变。真正签名时启用 Hardened Runtime、安全时间戳和库验证；
本机离线测试使用 ad-hoc 签名验证签名顺序、资源密封和失败处理，不代替 Developer ID / Apple 实际审核。

```sh
python3 tools/easy_flash/test_sign_macos.py \
  --package build/easy_flash/DS5Dongle-LCTech616-HS-HidAudio-R4-Flash-Mac
```

## 检查

```sh
bash -n tools/easy_flash/*.sh tools/easy_flash/Flash-Mac.command
python3 tools/easy_flash/test_package.py \
  --package build/easy_flash/DS5Dongle-LCTech616-HS-HidAudio-R4-Flash-Mac
python3 tools/easy_flash/test_package.py \
  --package build/easy_flash/DS5Dongle-LCTech616-HS-HidAudio-R4-Flash-Windows \
  --pwsh /path/to/pwsh
```

测试先检查平台隔离、完整哈希清单和固件版本。Mac 包测试不依赖 PowerShell；
Windows 包测试使用可运行的 PowerShell（开发者依赖），在 Mac 上模拟 Windows 设备清单；
实际文件复制、哈希校验、临时目录、参数传递、日志保存和成功判定均运行生产脚本。
测试使用假的串口检测结果和假的烧录进程，覆盖成功、非零退出、缺失校验结果、错误
回读哈希、损坏文件、无设备、多设备、中文/空格路径。测试不会连接真实设备。

这不能替代 Windows 自带 PowerShell 5.1、USB 驱动及官方 Windows EXE 的实机验收。
Windows 脚本仅使用 5.1 已有语法和系统命令。此次拆分包未重新实机烧录，
`package-info.json` 以 `package_hardware_tested: false` 记录。

实现要点：只识别 `349B:6160`，多个下载口时不猜选；使用 `erase=1`；文件校验失败
不调用烧录程序；只有进程成功、三段校验完成且固件主机/设备 SHA-256 一致才报成功。
原厂工具在临时目录运行，避免污染安装包。日志保存到解压目录下的 `logs/`。

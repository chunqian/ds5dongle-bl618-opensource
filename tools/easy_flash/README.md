# LCTech BL616 简易烧录包

为 `3.18H-HidAudio-R7` HS 固件分别生成 Windows 和 Mac 离线 ZIP。
Windows 包只有 `Flash-Windows.bat` 启动入口和 Windows 运行程序；Mac 包只有
`Flash-Mac.command` 启动入口，包含 Apple 芯片和 Intel 的原生运行程序。
用户完整解压，按住 BOOT 插入设备，再双击启动文件并按回车，即可自动选择下载端口、烧录和校验。
使用说明由 `USAGE.txt` 和对应平台的 `USAGE-macos.txt` / `USAGE-windows.txt` 合成。
两包固件一致：默认实时档，正常启动后长按 BOOT 3 秒清除全部设置。R7 尚未完成实机烧录验收。

目标是 LCTech BL616；BOOT VID/PID 只识别芯片，不区分采用相同芯片的开发板。
Windows 目标为 Windows 10/11 x64；Mac 目标为 macOS 11+，自动选择 ARM64 或 x86_64
原生程序，无需 Rosetta。即使启动工具的终端在 Rosetta 下运行，也会选择 ARM64 程序。

## 维护者生成安装包

在本固件 Git 仓库中使用 Apple 芯片 Mac、原生 Python、Xcode Command Line Tools，并准备同级的
`bouffalo_sdk` Git 仓库。用户运行 ZIP 时不需要这些开发依赖。
仅生成 Windows 包时无需准备 Mac 原生运行时，也不会调用 Xcode 或 codesign。

先构建 ARM64 程序。发布版使用 python-build-standalone 的 macOS 11+ 运行时，避免把
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

然后生成两个独立安装包（默认 `--platform all`）：

```sh
python3 tools/easy_flash/build_package.py

# 只生成某个平台
python3 tools/easy_flash/build_package.py --platform windows
python3 tools/easy_flash/build_package.py --platform macos
```

使用本机 Xcode 配置的签名证书构建签名包：

```sh
security find-identity -v -p codesigning
python3 tools/easy_flash/build_package.py --sign-identity '证书的 SHA-1 或完整名称'
```

`--sign-identity` 仅作用于 Mac 包，Windows 包保持原厂程序。签名覆盖 ARM64 程序及全部
Mach-O 运行库、Intel 程序、双架构端口检测程序。
签名完成后才生成文件哈希清单，避免签名导致启动校验失效。`signing-info.json`
记录各文件的签名身份、Team ID 和签名后的 SHA-256；产物名称追加 `-Signed`。
`.command` 和 `.sh` 保持启动脚本形式，未转换为 `.app`。Apple Development
是开发签名，不等同于 Developer ID 分发签名；构建器不执行苹果公证。

### 在另一台 Mac 上做 Developer ID 签名和公证

运行 `python3 tools/easy_flash/build_signing_kit.py`，生成
`build/easy_flash/DS5Dongle-R7-Mac-Signing-Kit.zip`。它只包含现成的 R7 Mac 烧录包、
`Sign-Mac.command` 和 Python 标准库签名脚本，无需在目标 Mac 安装 SDK 或重编固件。
目标 Mac 需要 Xcode 14+、Python 3.9+ 和本地 **Developer ID Application 证书及私钥**；
仅有 Managed 云托管证书不能用于此命令行流程。

完整解压后双击 `Sign-Mac.command`，选择只签名或签名并公证。
命令行支持 `--check`、`--sign-only`、`--notarize`、`--identity SHA1`、
`--profile DS5DongleNotary`、`--resume output/release-xxxx`。
详见 `SIGNING.txt`。原包保持不变；结果、提交编号和日志保存在独立输出目录。
只在 Apple 返回 Accepted、票据附加完成、Gatekeeper 检查通过后标记 `notarized`。
重新签名后同步更新运行文件哈希，固件字节保持不变。

Intel 原厂 onefile 内含不同 Team ID 的 Python 库，开启 Hardened Runtime 后需要
`disable-library-validation`；此例外只用于 Intel 烧录程序。ARM64 外部运行库逐个
统一签名，不添加这个例外。脚本会做原生架构环境检查与离线镜像校验，不写串口。
DMG 用户需先复制完整烧录目录到可写位置，再运行 `.command` 启动脚本。

产物位于 `build/easy_flash/`：

- `DS5Dongle-LCTech616-HS-HidAudio-R7-Flash-Windows.zip`
- `DS5Dongle-LCTech616-HS-HidAudio-R7-Flash-Mac.zip`，证书签名后追加 `-Signed`

每个 ZIP 均有对应的解压目录及 `.zip.sha256` 校验文件。两个包的运行目录和文件清单分别生成，
不会要求另一个平台的程序存在。维护者源码保留两个平台的脚本，便于重新打包。
Intel Mac 和 Windows 保留 SDK 原厂 1.4.2 程序；ARM64 使用上述开源 1.0.0 程序。
脚本从固定 SDK 提交读取原厂程序及配置；Git 部分克隆缺少对象时，首次生成可能需要联网。既有芯片运行日志、
生成的 Flash 镜像和本地 loader 配置不会被复制。

脚本固定校验当前 R7 固件 SHA-256，避免把普通版或错误版本误装进包。
新固件发布时须明确更新 `RELEASE`、`VERSION`、`FIRMWARE_SHA`、`NAME`、实机验证状态和使用说明，
并重新验证；签名工具包中的离线镜像 SHA-256 也需重新核对更新。
固件源码需先提交，生成器会拒绝 `src/`、`CMakeLists.txt`、`cmake/` 中与 HEAD 不符的改动。

解压包里的 `source/firmware-source.tar.gz` 是固件仓库源码快照。
如需从该快照继续开发，将 `source/easy_flash/` 复制到源码的 `tools/easy_flash/`，
并恢复对应 Git 提交或在自己的仓库提交源码，再生成安装包；构建依赖见根 README。

## 检查

```sh
bash -n tools/easy_flash/*.sh tools/easy_flash/Flash-Mac.command
python3 tools/easy_flash/test_package.py \
  --package build/easy_flash/DS5Dongle-LCTech616-HS-HidAudio-R7-Flash-Windows \
  --pwsh /path/to/pwsh
python3 tools/easy_flash/test_package.py \
  --package build/easy_flash/DS5Dongle-LCTech616-HS-HidAudio-R7-Flash-Mac-Signed
```

测试使用可运行的 PowerShell（开发者依赖），在 Mac 上模拟 Windows 设备清单；
实际文件复制、哈希校验、临时目录、参数传递、日志保存和成功判定均运行生产脚本。
测试使用假的串口检测结果和假的烧录进程，覆盖成功、非零退出、缺失校验结果、错误
回读哈希、损坏文件、无设备、多设备、中文/空格路径。测试不会连接真实设备。

这不能替代 Windows 自带 PowerShell 5.1、USB 驱动及官方 Windows EXE 的实机验收。
Windows 脚本仅使用 5.1 已有语法和系统命令；安装包的 `package-info.json` 如实记录
Windows 和 ARM64 硬件验证状态。两个 Mac 程序均可用 `--build` 检查完整固件组合，
不进行串口写入。发布验证还应对比两者生成的 `whole_flash_data.bin` 是否逐字节一致。
R6 固件曾使用同版本 ARM64 后端完成真实适配器烧录与回读校验；R7 不能继承该实机验证结论。
新工具需分别验证启动、签名和离线镜像；这些检查不能替代 Windows 实机验收。

实现要点：只识别 `349B:6160`，多个下载口时不猜选；使用 `erase=1`；文件校验失败
不调用烧录程序；只有进程成功、三段校验完成且固件主机/设备 SHA-256 一致才报成功。
原厂工具在临时目录运行，避免污染安装包。日志保存到解压目录下的 `logs/`。

# LCTech BL616 简易烧录包

为已验证的 `3.18H-HidAudio-R2` HS 固件生成一个离线 ZIP。用户完整解压后，
双击 `Flash-Mac.command` 或 `Flash-Windows.bat`，按住 BOOT 插入设备并按回车，
即可自动选择下载端口、烧录和校验。详细使用说明见 `USAGE.txt`。

目标是 LCTech BL616；BOOT VID/PID 只识别芯片，不区分采用相同芯片的开发板。
Windows 目标为 Windows 10/11 x64；Mac 目标为 macOS 11+，自动选择 ARM64 或 x86_64
原生程序，无需 Rosetta。即使启动工具的终端在 Rosetta 下运行，也会选择 ARM64 程序。

## 维护者生成安装包

在本固件 Git 仓库中使用 Apple 芯片 Mac、原生 Python、Xcode Command Line Tools，并准备同级的
`bouffalo_sdk` Git 仓库。用户运行 ZIP 时不需要这些开发依赖。

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

然后生成包含三种程序的安装包：

```sh
python3 tools/easy_flash/build_package.py
```

产物位于 `build/easy_flash/`，名称以 `Flash-NativeMac` 结尾，包括 ZIP 和解压目录。
Intel Mac 和 Windows 保留 SDK 原厂 1.4.2 程序；ARM64 使用上述开源 1.0.0 程序。
脚本从固定 SDK 提交读取原厂程序及配置；Git 部分克隆缺少对象时，首次生成可能需要联网。既有芯片运行日志、
生成的 Flash 镜像和本地 loader 配置不会被复制。

脚本固定校验此前实机通过的 R2 固件 SHA-256，避免把普通版或错误版本误装进包。
新固件发布时须明确更新 `VERSION`、`FIRMWARE_SHA`、`NAME` 和使用说明，并重新验证。
源码需先提交，生成器会拒绝 `src/`、`CMakeLists.txt` 中与 HEAD 不符的改动。

解压包里的 `source/firmware-source.tar.gz` 是固件仓库源码快照。
如需从该快照继续开发，将 `source/easy_flash/` 复制到源码的 `tools/easy_flash/`，
并恢复对应 Git 提交或在自己的仓库提交源码，再生成安装包；构建依赖见根 README。

## 检查

```sh
bash -n tools/easy_flash/*.sh tools/easy_flash/Flash-Mac.command
python3 tools/easy_flash/test_package.py \
  --package build/easy_flash/DS5Dongle-LCTech616-HS-HidAudio-R2-Flash-NativeMac \
  --pwsh /path/to/pwsh
```

测试使用可运行的 PowerShell（开发者依赖），在 Mac 上模拟 Windows 设备清单；
实际文件复制、哈希校验、临时目录、参数传递、日志保存和成功判定均运行生产脚本。
测试使用假的串口检测结果和假的烧录进程，覆盖成功、非零退出、缺失校验结果、错误
回读哈希、损坏文件、无设备、多设备、中文/空格路径。测试不会连接真实设备。

这不能替代 Windows 自带 PowerShell 5.1、USB 驱动及官方 Windows EXE 的实机验收。
Windows 脚本仅使用 5.1 已有语法和系统命令；安装包的 `package-info.json` 如实记录
Windows 和 ARM64 硬件验证状态。两个 Mac 程序均可用 `--build` 检查完整固件组合，
不进行串口写入。发布验证还应对比两者生成的 `whole_flash_data.bin` 是否逐字节一致。
ARM64 程序本机启动及生成镜像通过，不能替代真实适配器烧录验收。

本次 R2 两种 Mac 程序生成的镜像均为 832000 字节，SHA-256 为
`7ad2683db7a098ddd2f133e5d4bd61f336cdc51304045fb10f67c225fe69d268`；
已在中文/空格路径和不含开发工具的 PATH 中验证 ARM64 独立运行。

实现要点：只识别 `349B:6160`，多个下载口时不猜选；使用 `erase=1`；文件校验失败
不调用烧录程序；只有进程成功、三段校验完成且固件主机/设备 SHA-256 一致才报成功。
原厂工具在临时目录运行，避免污染安装包。日志保存到解压目录下的 `logs/`。

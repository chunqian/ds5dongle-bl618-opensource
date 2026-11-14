@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  DS5Dongle BL618 - Windows build script
rem
rem  Usage: build_windows.bat [build|rebuild|clean|flash [COMx]]
rem    build   - incremental build (High-Speed only)
rem    rebuild - clean + full build
rem    clean   - remove build directory
rem    flash   - flash via serial, e.g. build_windows.bat flash COM5
rem
rem  Environment overrides:
rem    BL_SDK_BASE     path to the DS5Dongle BL618 SDK fork
rem    TOOLCHAIN_PATH  path to the T-Head Windows toolchain
rem ============================================================

if "%BL_SDK_BASE%"=="" set "BL_SDK_BASE=%~dp0..\bouffalo_sdk"
if "%TOOLCHAIN_PATH%"=="" set "TOOLCHAIN_PATH=%USERPROFILE%\Desktop\toolchain_gcc_t-head_windows"
if defined BOARD_TYPE if /i not "%BOARD_TYPE%"=="lctech616" (
    echo [ERROR] Only LCTech BL616 ^(lctech616^) is supported.
    exit /b 1
)
if defined BOARD_M0S_DOCK (
    echo [ERROR] Remove obsolete board selectors; only LCTech BL616 is supported.
    exit /b 1
)
set "BOARD_TYPE=lctech616"
if "%NUMBER_OF_PROCESSORS%"=="" set "NUMBER_OF_PROCESSORS=8"

rem ---- sanity checks ----
if not exist "%BL_SDK_BASE%\project.build" (
    echo [ERROR] SDK not found at "%BL_SDK_BASE%"
    echo         Set BL_SDK_BASE to the DS5Dongle BL618 SDK fork ^(see README step 1^).
    exit /b 1
)
if not exist "%TOOLCHAIN_PATH%\bin\riscv64-unknown-elf-gcc.exe" (
    echo [ERROR] RISC-V toolchain not found at "%TOOLCHAIN_PATH%"
    echo         git clone https://gitee.com/bouffalolab/toolchain_gcc_t-head_windows.git
    echo         or set TOOLCHAIN_PATH to override.
    exit /b 1
)

rem ---- PATH ----
set "PATH=%TOOLCHAIN_PATH%\bin;%BL_SDK_BASE%\tools\make;%BL_SDK_BASE%\tools\cmake\bin;%BL_SDK_BASE%\tools\ninja;%PATH%"

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=build"

if /i "%ACTION%"=="clean" goto :clean
if /i "%ACTION%"=="flash" goto :flash
if /i not "%ACTION%"=="build" if /i not "%ACTION%"=="rebuild" (
    echo Usage: build_windows.bat [build^|rebuild^|clean^|flash [COMx]]
    exit /b 1
)

rem build / rebuild
if /i "%ACTION%"=="rebuild" (
    if exist build make clean 2>nul
)
call :build_one
goto :done

:build_one
    rem ---- auto clean when board/usb config changes ----
    set "BUILD_KEY=lctech616-hs"
    set "BOARD_STAMP=build\.board_type"
    if exist "%BOARD_STAMP%" (
        set "PREV_KEY="
        set /p PREV_KEY=<"%BOARD_STAMP%"
        if not "!PREV_KEY!"=="%BUILD_KEY%" (
            echo [build] Config changed ^(!PREV_KEY! -^> %BUILD_KEY%^), forcing clean...
            if exist build make clean 2>nul
        )
    )
    echo [build] Target: %BOARD_TYPE%  USB: High-Speed
    make -j%NUMBER_OF_PROCESSORS%
    if errorlevel 1 exit /b 1
    if not exist build mkdir build
    >"%BOARD_STAMP%" echo %BUILD_KEY%
    set "OUT_DIR=firmware\lctech616"
    if not exist "%OUT_DIR%" mkdir "%OUT_DIR%"
    copy /y "build\build_out\ds5dongle_bl618_bl616.bin" "%OUT_DIR%\ds5dongle-lctech616-hs.bin" >nul
    copy /y "build\build_out\boot2_bl616_isp_release_v8.1.8.bin" "%OUT_DIR%\" >nul
    copy /y "build\build_out\partition.bin" "%OUT_DIR%\" >nul
    echo [build] Output: %OUT_DIR%\ds5dongle-lctech616-hs.bin
    goto :eof

:clean
make clean
goto :done

:flash
set "COMX=%~2"
if "%COMX%"=="" set "COMX=COM5"
make flash COMX=%COMX%
goto :done

:done
echo [build] Done.
exit /b 0

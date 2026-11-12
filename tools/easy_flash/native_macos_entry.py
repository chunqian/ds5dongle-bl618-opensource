"""Native ARM64 entry point for the unmodified Bouffalo Flash Command source."""
import ctypes
import json
import platform
import sys

# A static import lets PyInstaller collect the vendor's Python code and deps.
import BLFlashCommand
import libs.bl616


def main():
    if sys.argv[1:] == ['--runtime-info']:
        translated = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(translated))
        libc = ctypes.CDLL(None)
        # ENOENT also means this process is not translated.
        libc.sysctlbyname(b'sysctl.proc_translated', ctypes.byref(translated),
                         ctypes.byref(size), None, 0)
        info = {'machine': platform.machine(), 'rosetta': bool(translated.value),
                'python': platform.python_version(), 'backend': 'bflb-flash-command 1.0.0'}
        print(json.dumps(info))
        return 0 if info['machine'] == 'arm64' and not info['rosetta'] else 1
    BLFlashCommand.MainClass().main(sys.argv[1:])
    return 0  # The launcher additionally requires all three device SHA checks.


if __name__ == '__main__':
    raise SystemExit(main())

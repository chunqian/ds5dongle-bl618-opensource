"""Exercise the real SDK LittleFS adapter with RAM flash and interrupted writes.

Run: python3 tests/test_factory_reset.py
Requires the sibling bouffalo_sdk checkout (or BL_SDK_BASE), CMake and cc.
"""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
sdk = Path(os.environ.get('BL_SDK_BASE', root.parent / 'bouffalo_sdk'))
fs = sdk / 'components/fs/littlefs'
original = fs / 'easyflash_port/lfs_easyflash.c'
before = original.read_bytes()

with tempfile.TemporaryDirectory(prefix='ds5-factory-reset-') as directory:
    tmp = Path(directory)
    patched = tmp / 'lfs_easyflash.c'
    script = tmp / 'generate.cmake'
    script.write_text(
        f'include("{root / "cmake/factory_reset_storage.cmake"}")\n'
        f'ds5_extend_reset_storage("{original}" '
        f'"{root / "src/factory_reset_storage.inc"}" "{patched}")\n')
    subprocess.run(['cmake', '-P', str(script)], check=True)
    generated = patched.read_bytes()
    mtime = patched.stat().st_mtime_ns
    subprocess.run(['cmake', '-P', str(script)], check=True)
    assert patched.read_bytes() == generated and patched.stat().st_mtime_ns == mtime
    assert original.read_bytes() == before
    (tmp / 'log.h').write_text('#define LOG_E(...) ((void)0)\n#define LOG_W(...) ((void)0)\n')
    # Exercise the actual multi-statement EF_GIANT_UNLOCK macro used in firmware.
    (tmp / 'FreeRTOS.h').write_text(
        '#include <assert.h>\n#define configUSE_RECURSIVE_MUTEXES 1\n'
        '#define portMAX_DELAY 0xffffffffu\n')
    (tmp / 'semphr.h').write_text('''
#ifndef TEST_SEMPHR_H
#define TEST_SEMPHR_H
typedef unsigned *SemaphoreHandle_t;
static unsigned mutex_depth;
static inline SemaphoreHandle_t xSemaphoreCreateRecursiveMutex(void) {
    assert(mutex_depth == 0); return &mutex_depth;
}
static inline void xSemaphoreTakeRecursive(SemaphoreHandle_t m, unsigned t) {
    (void)t; assert(m == &mutex_depth); ++*m;
}
static inline void xSemaphoreGiveRecursive(SemaphoreHandle_t m) {
    assert(m == &mutex_depth && *m); --*m;
}
#endif
''')
    command = ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
               '-Wno-sign-compare', '-Wno-deprecated-declarations',
               '-DCONFIG_FREERTOS', '-DLFS_NO_DEBUG', '-DLFS_NO_WARN', '-DLFS_NO_ERROR',
               '-fsanitize=address,undefined']
    for include in (tmp, fs / 'littlefs', fs / 'easyflash_port', fs / 'port', root / 'src'):
        command += ['-I', str(include)]
    command += [str(root / 'tests/test_factory_reset.c'), str(fs / 'littlefs/lfs.c'),
                str(fs / 'littlefs/lfs_util.c'), '-o', str(tmp / 'test')]
    subprocess.run(command, check=True)
    subprocess.run([str(tmp / 'test')], check=True)

"""Exercise the actual SDK allocator and application fatal hooks together.

The reset primitive is replaced with longjmp; no mocked xTaskCreate failure is
used as evidence that the SDK allocator returns NULL. Fixtures are temporary.
"""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SDK = Path(os.environ.get('BL_SDK_BASE', ROOT.parent / 'bouffalo_sdk'))
main = (ROOT / 'src/main.c').read_text()
audio = (ROOT / 'src/audio.c').read_text()


def extract(source, signature):
    a = source.index(signature)
    b = source.index('{', a) + 1
    depth = 1
    while depth:
        depth += (source[b] == '{') - (source[b] == '}')
        b += 1
    return source[a:b] + '\n'


source = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <setjmp.h>
typedef void *TaskHandle_t;
static jmp_buf reset_target;
static bool fail_allocation;
static unsigned scheduler_depth, resets;
static int storage;
static void GLB_SW_System_Reset(void) { ++resets; longjmp(reset_target, 1); }
void vTaskSuspendAll(void) { ++scheduler_depth; }
int xTaskResumeAll(void) { assert(scheduler_depth); --scheduler_depth; return 1; }
void *kmalloc(size_t size, unsigned flags) {
    assert(size==64 && !flags && scheduler_depth==1);
    return fail_allocation ? NULL : &storage;
}
void kfree(void *ptr) { assert(ptr==&storage && scheduler_depth==1); }
void *pvPortMalloc(size_t);
void vPortFree(void *);
'''
for sig in ('__attribute__((noreturn)) void vAssertCalled(',
            'void vApplicationMallocFailedHook(',
            'void vApplicationStackOverflowHook('):
    source += extract(main, sig)
source += r'''
int main(void) {
    assert(pvPortMalloc(64)==&storage && !scheduler_depth && !resets);
    vPortFree(&storage); assert(!scheduler_depth);
    fail_allocation=true;
    if (!setjmp(reset_target)) { pvPortMalloc(64); assert(!"failed allocation returned"); }
    assert(resets==1 && !scheduler_depth);
    if (!setjmp(reset_target)) { vAssertCalled(); }
    assert(resets==2);
    if (!setjmp(reset_target)) { vApplicationStackOverflowHook(NULL,NULL); }
    assert(resets==3);
    puts("PASS: real SDK heap_3 allocation failure reaches application reset, never returns; assert/stack overflow reset");
}
'''
# Critical application workers/recovery must not reintroduce heap allocation.
assert 'xTaskCreate(' not in main and 'xTaskCreate(' not in audio
assert 'xQueueCreate(' not in main and 'xQueueCreate(' not in audio
respawn = extract(audio, 'bool audio_check_respawn(')
assert respawn.index('vTaskSuspendAll();') < respawn.index('audio_task_handle = xTaskCreateStatic(') < respawn.index('xTaskResumeAll();')
# Check the SDK contract used to recycle another, suspended static task.
tasks = (SDK / 'components/os/freertos/tasks.c').read_text()
assert 'prvDeleteTCB( pxTCB );' in extract(tasks, 'void vTaskDelete(')
assert 'tskSTATICALLY_ALLOCATED_STACK_AND_TCB' in tasks
with tempfile.TemporaryDirectory(prefix='ds5-fatal-') as temp:
    p = Path(temp)
    (p / 'FreeRTOS.h').write_text('''#include <stddef.h>
#define configSUPPORT_DYNAMIC_ALLOCATION 1
#define configUSE_MALLOC_FAILED_HOOK 1
#define traceMALLOC(a,b) ((void)0)
#define traceFREE(a,b) ((void)0)
void vApplicationMallocFailedHook(void);
''')
    (p / 'task.h').write_text('void vTaskSuspendAll(void);\nint xTaskResumeAll(void);\n')
    (p / 'mm.h').write_text('#include <stddef.h>\nvoid *kmalloc(size_t,unsigned);\nvoid kfree(void *);\n')
    (p / 'test.c').write_text(source)
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-I'+str(p), str(p / 'test.c'),
                    str(SDK / 'components/os/freertos/portable/MemMang/heap_3.c'),
                    '-o', str(p / 'test')], check=True)
    subprocess.run([str(p / 'test')], check=True)

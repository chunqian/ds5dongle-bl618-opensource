"""Exercise production suspend/wake FSM with clock, IRQ and USB fault injection."""
from pathlib import Path
import subprocess,tempfile
ROOT=Path(__file__).resolve().parents[1]
harness=r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>
#include "config.h"
static struct config_body cfg;
struct config_body *config_get(void) { return &cfg; }
static uint64_t clock_us;
static uintptr_t masked;
static unsigned requests, forced, poweroffs;
static uint32_t session=1;
static bool poweroff_fail, resume_before_submit;
uint32_t bt_hid_host_session(void) { return session; }
static bool suspend_during_request, resume_during_request;
static int wake_result;
uint64_t bflb_mtimer_get_time_us(void) { return clock_us; }
uintptr_t bflb_irq_save(void) { uintptr_t old=masked; masked=1; return old; }
void bflb_irq_restore(uintptr_t old) { masked=old; }
void usb_wake_on_suspend(void);
void usb_wake_on_resume(void);
int usbd_send_remote_wakeup(uint8_t bus) {
    assert(!masked && bus==0); requests++;
    if(suspend_during_request) usb_wake_on_suspend();
    if(resume_during_request) usb_wake_on_resume();
    return wake_result;
}
void usbd_set_remote_wakeup(uint8_t bus) { assert(!masked && bus==0); forced++; }
int bt_hid_host_set_feature_crc_guarded(uint8_t id,const uint8_t *data,uint16_t len,uint32_t generation,bool (*allow)(uint32_t),uint32_t token) {
    assert(!masked && id==8 && len==47 && data[0]==2); if(resume_before_submit) usb_wake_on_resume();
    if(!allow(token)) return -1;
    assert(generation==session); poweroffs++; return poweroff_fail ? -1 : 0;
}
'''
harness += '#include "'+str(ROOT/'src/usb_wake.c')+'"\n'
harness+=r'''
static void setup(void) {
    clock_us=0; masked=0; poweroff_fail=resume_before_submit=false; session=1; cfg.enable_wake=1;
    requests=forced=poweroffs=0;
    suspend_during_request=resume_during_request=false;
    wake_result=0; usb_wake_init();
}
static void step(uint64_t time) { clock_us=time; usb_wake_task(); assert(!masked); }
int main(void) {
    setup(); cfg.enable_wake=0; usb_wake_on_suspend();
    step(2999999); assert(!poweroffs); step(3000000); assert(poweroffs==1);
    step(9000000); assert(poweroffs==1 && !requests);
    setup(); usb_wake_on_suspend(); step(2000000); usb_wake_on_resume();
    step(4000000); assert(!poweroffs);
    setup(); usb_wake_note_usb_reconnect(); usb_wake_on_suspend();
    assert(!usb_wake_host_suspended()); step(4000000); assert(!poweroffs);
    usb_wake_on_configured(); usb_wake_on_suspend();
    step(7000000); assert(poweroffs==1); /* mount ends grace early */
    setup(); usb_wake_note_usb_reconnect(); clock_us=5000000; usb_wake_on_suspend();
    step(8000000); assert(poweroffs==1); /* grace expires */

    setup(); usb_wake_on_suspend(); clock_us=100; usb_wake_on_bt_connect();
    assert(state==WAKE_REQUESTED && requests==1);
    usb_wake_on_resume(); step(101);
    assert(state==WAKE_DONE && !poweroffs);

    setup(); wake_result=-1; usb_wake_on_suspend(); usb_wake_on_bt_connect();
    assert(forced==1 && state==WAKE_REQUESTED);
    step(5000001); assert(state==WAKE_DONE);
    /* Changed buttons request wake even if a hub masked our suspend hook. */
    setup(); uint8_t input[10]={0}; input[7]=8; input[8]=1;
    wake_result=-1; usb_wake_on_bt_input(input,10);
    assert(requests==1 && !forced && state==WAKE_IDLE);
    input[8]=0; wake_result=0; usb_wake_on_bt_input(input,10);
    step(150000); assert(state==WAKE_DONE);
    usb_wake_on_bt_disconnect(); assert(state==WAKE_IDLE);
    setup(); usb_wake_on_suspend(); suspend_during_request=true;
    usb_wake_on_bt_connect(); assert(state==WAKE_PENDING_PRESS); /* old completion ignored */
    setup(); usb_wake_on_suspend(); resume_during_request=true;
    usb_wake_on_bt_connect(); step(150000); assert(state==WAKE_DONE);
    /* Re-suspend re-arms a new attempt; callbacks preserve ISR entry mask. */
    masked=1; usb_wake_on_suspend(); assert(masked==1 && state==WAKE_PENDING_PRESS);
    masked=0; step(160000); assert(state==WAKE_PENDING_PRESS);
    setup(); usb_wake_on_suspend(); poweroff_fail=true;
    step(3000000); assert(poweroffs==1 && poweroff_pending);
    step(3099999); assert(poweroffs==1);
    poweroff_fail=false; step(3100000); assert(poweroffs==2 && !poweroff_pending);
    setup(); usb_wake_on_suspend(); ++session;
    step(4000000); assert(!poweroffs);
    setup(); usb_wake_on_suspend(); usb_wake_on_bt_disconnect();
    step(4000000); assert(!poweroffs);
    setup(); usb_wake_on_suspend(); poweroff_fail=true; step(3000000);
    usb_wake_on_resume(); step(4000000); assert(poweroffs==1);
    setup(); usb_wake_on_suspend(); resume_before_submit=true;
    step(4000000); assert(!poweroffs && !poweroff_pending);
    puts("PASS: 3s power-off, reconnect grace, remote wake, no synthetic input, power-off retry, ISR/session races");
}
'''
with tempfile.TemporaryDirectory(prefix='ds5-wake-') as d:
    p=Path(d)
    for name,body in {
        'usbd_core.h':'#include <stdint.h>\nint usbd_send_remote_wakeup(uint8_t);\nvoid usbd_set_remote_wakeup(uint8_t);\n',
        'bflb_mtimer.h':'#include <stdint.h>\nuint64_t bflb_mtimer_get_time_us(void);\n',
        'bflb_irq.h':'#include <stdint.h>\nuintptr_t bflb_irq_save(void);\nvoid bflb_irq_restore(uintptr_t);\n',
    }.items(): (p/name).write_text(body)
    (p/'test.c').write_text(harness)
    subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-I'+str(p),'-I'+str(ROOT/'src'),str(p/'test.c'),'-o',str(p/'test')],check=True)
    subprocess.run([str(p/'test')],check=True)

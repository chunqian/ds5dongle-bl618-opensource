"""Fault-inject production Edge sequencing, deferred writes and volume snapshots."""
from pathlib import Path
import subprocess
import tempfile
ROOT = Path(__file__).resolve().parents[1]
def extract(s, signature):
    start=s.index(signature); end=s.index('{',start)+1; depth=1
    while depth:
        depth+=(s[end]=='{')-(s[end]=='}'); end+=1
    return s[start:end]
src=(ROOT/'src/usb_gamepad.c').read_text()
harness=r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "bt_hid_host.h"
#include "config.h"
#include "state_mgr.h"
#include "dse.h"
static struct config_body cfg;
struct config_body *config_get(void) { return &cfg; }
static uint64_t clock_us;
static uint32_t session=1;
static uintptr_t masked;
static unsigned calls, failures, ids[256];
static bool firmware, immediate, reset_on_send;
uint64_t bflb_mtimer_get_time_us(void) { return clock_us; }
uintptr_t bflb_irq_save(void) { uintptr_t old=masked; masked=1; return old; }
void bflb_irq_restore(uintptr_t old) { masked=old; }
uint32_t bt_hid_host_session(void) { return session; }
static enum bt_hid_host_state bt_state=BT_HID_STATE_CONNECTED;
enum bt_hid_host_state bt_hid_host_get_state(void) { return bt_state; }
bool bt_hid_host_copy_feature(uint8_t id,uint8_t *out,uint16_t cap,uint16_t *len) {
    assert(id==0x20 && cap==61); memset(out,0x5a,cap); *len=firmware?61:0; return firmware;
}
static void response(uint8_t id,unsigned len) {
    uint8_t packet[66]={0xa3,id}; dse_on_control_packet(packet,len);
}
static int submit(unsigned id,uint32_t generation) {
    assert(!masked && generation==session && calls<256); ids[calls++]=id;
    if(reset_on_send) { reset_on_send=false; ++session; dse_reset(); return -1; }
    if(failures) { --failures; return -1; }
    if(immediate && id>=0x70 && id<=0x7b) response(id,65);
    return 0;
}
int bt_hid_host_get_feature_session(uint8_t id,uint32_t generation) { return submit(id,generation); }
int bt_hid_host_set_feature_session(uint8_t id,const uint8_t *p,uint16_t len,uint32_t gen) {
    assert(id==0x65 && len==61 && p[0]==0x5a); return submit(id,gen);
}
int bt_hid_host_set_feature_crc_session(uint8_t id,const uint8_t *p,uint16_t len,uint32_t gen) {
    if(id==0x80) assert(len==59 && p[0]==0x70 && p[1]==1);
    return submit(id,gen);
}
'''
harness += '#include "'+str(ROOT/'src/dse.c')+'"\n'
harness += r'''
#define SET_REPORT_QUEUE_DEPTH 4
#define SET_REPORT_MAX_DATA 64
#define pdTRUE 1
typedef int BaseType_t;
typedef struct { uint8_t report_id,len; bool is_dse; uint32_t session,usb_session; uint8_t data[64]; } set_report_entry_t;
static int set_report_queue=1;
static uint32_t host_epoch=1;
static bool host_active=true;
uint32_t usb_gamepad_host_session(void) { return host_epoch; }
bool usb_gamepad_host_active(void) { return host_active; }
static set_report_entry_t queue[4];
static unsigned queued;
static int xQueuePeek(int q,void *p,unsigned timeout) {
    (void)q;(void)timeout; if(!queued) return 0; memcpy(p,queue,sizeof(queue[0])); return 1;
}
static int xQueueReceive(int q,void *p,unsigned timeout) {
    if(!xQueuePeek(q,p,timeout)) return 0;
    memmove(queue,queue+1,--queued*sizeof(queue[0])); return 1;
}
static int xQueueSendFromISR(int q,const void *p,void *wake) {
    (void)q;(void)wake; if(queued==4) return 0; queue[queued++]=*(const set_report_entry_t *)p; return 1;
}
static bool feature_set_rejected;
'''
harness += extract(src,'void usb_gamepad_process_feature_sets(')+'\n'
harness += 'static void forward(uint8_t report_id,uint8_t report_type,const uint8_t *payload,uint32_t payload_len) {\n'
harness += extract(src,'    if (report_type == 0x03 &&\n        (report_id == 0x60')+'\n}\n'
harness += r'''
struct usb_setup_packet { uint8_t bRequest; uint16_t wValue; };
#define HID_REQUEST_GET_REPORT 1
static int callback_result;
static bool callback_reject;
static int hid_class_handler(uint8_t bus,struct usb_setup_packet *setup,uint8_t **data,uint32_t *len) {
    (void)bus;(void)setup;(void)data;(void)len; feature_set_rejected=callback_reject; return callback_result;
}
'''
harness += extract(src,'static int gamepad_class_request(')+'\n'
harness += r'''
static void setup(void) {
    dse_reset(); clock_us=0; calls=failures=0; session=1;
    firmware=true; immediate=reset_on_send=false;
    bt_state=BT_HID_STATE_CONNECTED;
}
static void step(uint64_t t) { clock_us=t; dse_task(); assert(!masked); }
static void complete_snapshot(uint64_t t) {
    immediate=true;
    for(unsigned i=0;i<12;i++) step(t+i*80000);
    assert(dse_profiles_ready());
}
int main(void) {
    setup(); dse_on_edge_detected(); assert(calls==2 && ids[0]==0x65 && ids[1]==0x80);
    assert(!dse_profiles_ready()); step(3999999); assert(calls==2);
    for(unsigned i=0;i<12;i++) step(4000000+i*80000);
    assert(calls==14 && !dse_profiles_ready()); /* GETs are not responses. */
    for(unsigned i=0;i<12;i++) response(0x70+i,64);
    assert(!dse_profiles_ready()); /* truncated bodies never complete the snapshot */
    for(unsigned i=0;i<11;i++) response(0x70+i,65);
    step(4960000); step(5960000);
    assert(ids[calls-1]==0x7b && !dse_profiles_ready());
    response(0x7b,65); assert(dse_profiles_ready());
    /* A real full-size all-zero profile is valid; a fabricated fallback isn't. */
    setup(); firmware=false; dse_on_edge_detected(); assert(calls==1 && ids[0]==0x80);
    complete_snapshot(4000000);
    setup(); failures=1; dse_on_edge_detected(); assert(calls==1 && dse.phase==ECHO);
    step(79999); assert(calls==1); step(80000); assert(ids[1]==0x65);
    step(80000); assert(ids[2]==0x80); complete_snapshot(4080000);
    setup(); dse_on_edge_detected(); failures=1; step(4000000);
    assert(dse.next==0 && !dse_profiles_ready());
    complete_snapshot(4080000); assert(ids[2]==0x70 && ids[3]==0x70);
    /* Profile save invalidates old readiness, and failed unlock does not advance. */
    clock_us=6000000; immediate=false; dse_on_profile_write(0x60,session);
    assert(!dse_profiles_ready()); failures=1; step(6500000); assert(dse.phase==POST_UNLOCK);
    step(6580000); assert(dse.phase==POST_POLL);
    for(unsigned i=0;i<6;i++) step(7000000+i*250000);
    assert(dse.phase==PREFETCH); complete_snapshot(11500000);
    setup(); reset_on_send=true; dse_on_edge_detected();
    assert(calls==1 && dse.phase==OFF); step(5000000); assert(calls==1);
    dse_on_profile_write(0x60,session-1); assert(dse.phase==OFF);
    setup(); dse_on_edge_detected(); ++session; step(4000000);
    assert(calls==2 && dse.phase==OFF);

    /* Actual deferred queue head survives failure and stays ahead of successors. */
    setup(); clock_us=20000000; uint8_t body[59]={0};
    forward(0x60,3,body,sizeof(body)); forward(0x61,3,body,sizeof(body));
    failures=1; usb_gamepad_process_feature_sets(); assert(queued==2 && calls==1 && ids[0]==0x60);
    clock_us+=9999; usb_gamepad_process_feature_sets(); assert(calls==1);
    ++clock_us; usb_gamepad_process_feature_sets(); assert(!queued && calls==3 && ids[1]==0x60 && ids[2]==0x61);
    for(unsigned i=0;i<4;i++) forward(0x60,3,body,sizeof(body));
    assert(queued==4 && !feature_set_rejected);
    forward(0x61,3,body,sizeof(body)); assert(feature_set_rejected && queued==4);
    ++session; usb_gamepad_process_feature_sets(); assert(!queued && calls==3); /* stale writes discarded */
    forward(0x60,3,body,0); assert(feature_set_rejected && !queued);
    forward(0x60,3,body,65); assert(feature_set_rejected && !queued);
    forward(0x60,3,body,sizeof(body)); bt_state=BT_HID_STATE_DISCONNECTING;
    usb_gamepad_process_feature_sets(); assert(!queued && calls==3);
    forward(0x60,3,body,sizeof(body)); assert(feature_set_rejected && !queued);
    bt_state=BT_HID_STATE_CONNECTED; forward(0x60,3,body,sizeof(body));
    reset_on_send=true; usb_gamepad_process_feature_sets();
    clock_us+=10000; usb_gamepad_process_feature_sets(); assert(!queued && dse.phase==OFF);
    struct usb_setup_packet request={1,0x370}; uint8_t *data=body; uint32_t len=0;
    assert(gamepad_class_request(0,&request,&data,&len)==-1);
    len=64; assert(gamepad_class_request(0,&request,&data,&len)==0);
    request.bRequest=9; callback_reject=true;
    assert(gamepad_class_request(0,&request,&data,&len)==-1);
    callback_reject=false; assert(gamepad_class_request(0,&request,&data,&len)==0);
    callback_result=-2; assert(gamepad_class_request(0,&request,&data,&len)==-2);

    /* USB detach cannot truncate an accepted unlock/profile-save transaction. */
    setup(); clock_us=30000000; body[0]=0x70; body[1]=1;
    forward(0x80,3,body,sizeof(body)); forward(0x60,3,body,sizeof(body));
    failures=1; usb_gamepad_process_feature_sets(); assert(queued==2 && calls==1);
    ++host_epoch; host_active=false; clock_us+=10000;
    usb_gamepad_process_feature_sets();
    assert(!queued && calls==3 && ids[1]==0x80 && ids[2]==0x60);
    forward(0x61,3,body,sizeof(body)); assert(feature_set_rejected && !queued);
    host_active=true;

    /* Acknowledging a queued snapshot cannot clear a later volume/mute change. */
    uint8_t initial[47]={0}, out[63]; uint32_t a,b;
    state_mgr_init(initial,47); assert(!state_mgr_volume_snapshot(out,63,&a));
    state_mgr_set_volume(45,55); assert(state_mgr_volume_snapshot(out,63,&a));
    assert(out[4]==55 && out[5]==45);
    state_mgr_set_mute(true); state_mgr_vol_ack(a);
    assert(state_mgr_volume_snapshot(out,63,&b) && b!=a && (out[9]&0x60)==0x60);
    state_mgr_vol_ack(b); assert(!state_mgr_vol_dirty());
    state_mgr_set_volume(46,56); state_mgr_volume_snapshot(out,63,&a);
    state_mgr_init(initial,47); state_mgr_set_volume(cfg.speaker_volume,cfg.headset_volume); state_mgr_vol_ack(a);
    assert(state_mgr_volume_snapshot(out,63,&b) && out[4]==56 && out[5]==46);
    masked=1; state_mgr_vol_ack(b); assert(masked==1); masked=0;
    puts("PASS: Edge command failures/real responses/refetch/session reset, deferred SET retry/order/overflow/EP0 errors and versioned volume ACK");
}
'''
with tempfile.TemporaryDirectory(prefix='ds5-control-') as directory:
    p=Path(directory)
    (p/'bflb_irq.h').write_text('#include <stdint.h>\nuintptr_t bflb_irq_save(void);\nvoid bflb_irq_restore(uintptr_t);\n')
    (p/'bflb_mtimer.h').write_text('#include <stdint.h>\nuint64_t bflb_mtimer_get_time_us(void);\n')
    (p/'test.c').write_text(harness)
    subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-I'+str(p),'-I'+str(ROOT/'src'),str(p/'test.c'),str(ROOT/'src/state_mgr.c'),'-o',str(p/'test')],check=True)
    subprocess.run([str(p/'test')],check=True)

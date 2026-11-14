"""Actual host-output merge/send and USB lifetime, with transport fault injection."""
from pathlib import Path
import subprocess
import tempfile
ROOT=Path(__file__).resolve().parents[1]
main=(ROOT/'src/main.c').read_text()
usb=(ROOT/'src/usb_gamepad.c').read_text()
def extract(s,sig):
 a=s.index(sig);b=s.index('{',a)+1;depth=1
 while depth:
  depth+=(s[b]=='{')-(s[b]=='}');b+=1
 return s[a:b]+'\n'
h=r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "config.h"
#include "state_mgr.h"
#include "ds5_protocol.h"
#include "bt_hid_host.h"
#include "usb_gamepad.h"
static struct config_body cfg;
struct config_body *config_get(void) { return &cfg; }
static uintptr_t masked;
uintptr_t bflb_irq_save(void) { uintptr_t old=masked; masked=1; return old; }
void bflb_irq_restore(uintptr_t old) { masked=old; }
static uint32_t bt_epoch=1;
static enum bt_hid_host_state bt_state=BT_HID_STATE_CONNECTED;
uint32_t bt_hid_host_session(void) { return bt_epoch; }
enum bt_hid_host_state bt_hid_host_get_state(void) { return bt_state; }
static bool ds5_connected=true;
static bool driver_configured=true;
static unsigned speaker_stops, mic_stops, encoder_resets;
static void usb_audio_stop(void) { ++speaker_stops; }
static void usb_audio_mic_stop(void) { ++mic_stops; }
static void audio_reset_encoder(void) { ++encoder_resets; }
static bool usb_device_is_configured(unsigned bus) { assert(bus==0); return driver_configured; }
static int output_queue;
static unsigned wakes, calls, failures;
static uint8_t output_seq,last[78];
static void (*during_send)(void);
static bool accept_before_event;
static int xQueueOverwriteFromISR(int q,const void *data,void *wake) {
 (void)q;(void)wake;assert(masked && *(const uint8_t *)data==1);++wakes;return 1;
}
'''
h+=usb[usb.index('static volatile bool usb_configured = false;'):usb.index('/* BT presentation policy')]
h+=r'''
int bt_hid_host_send_state_session(const uint8_t *p,uint16_t len,uint32_t bt,uint32_t host) {
 assert(!masked && len==78); ++calls;
 if(during_send) { void (*fn)(void)=during_send; during_send=NULL; fn(); }
 if(!accept_before_event && (host!=host_session || bt!=bt_epoch)) return -ENOTCONN;
 if(failures) { --failures; return -EAGAIN; }
 memcpy(last,p,len); return 0;
}
'''
h+=extract(main,'static const uint8_t init_set_state[')+';\n'
for sig in ('static void build_bt_output(', 'static void on_usb_output(', 'static void process_state_output('):
 h+=extract(main,sig)
h+=r'''
static void reset_usb(void) { usb_host_session_end(); }
static void host_right(unsigned value) {
 uint8_t report[64]={2,4}; report[11]=value; on_usb_output(report,64);
}
static void newer_state(void) { host_right(7); }
static void reset_bt(void) { ++bt_epoch; state_mgr_init(init_set_state,47); state_mgr_set_mic_light(true); }
static void expect_crc(void) {
 assert(last[0]==0x31 && last[2]==DS5_BT_OUTPUT_TAG);
 uint8_t crc[4];ds5_write_le32(crc,ds5_crc32(DS5_BT_OUTPUT_CRC_SEED,last,74));
 assert(!memcmp(last+74,crc,4));
}
int main(void) {
 cfg.speaker_volume=60;cfg.headset_volume=70;cfg.led_b=64;
 state_mgr_init(init_set_state,47);
 state_mgr_apply_config(60,70,0,0);state_mgr_set_led_color(0,0,64);
 host_session_open=usb_configured=true;
 process_state_output();assert(!state_mgr_output_pending());
 /* Bursts exceed both old queue capacities; unflagged fields survive. */
 uint8_t report[64]={2,8,4};report[22]=3;report[45]=9;report[46]=8;report[47]=7;
 on_usb_output(report,64);
 for(unsigned i=0;i<40;i++) host_right(1);
 host_right(0);assert(wakes==42);
 failures=3;
 for(unsigned i=0;i<3;i++) { process_state_output();assert(state_mgr_output_pending()); }
 process_state_output();assert(!state_mgr_output_pending());
 assert(last[3+10]==0 && last[3+21]==3 && last[3+44]==9);expect_crc();
 unsigned count=calls;process_state_output();assert(calls==count);
 /* ACK of an old snapshot cannot clear a newer host or UAC/local update. */
 host_right(1);during_send=newer_state;process_state_output();
 assert(last[3+10]==1 && state_mgr_output_pending());
 state_mgr_set_volume(80,90);state_mgr_set_mic_light(true);
 process_state_output();assert(last[3+10]==7 && last[3+4]==90 && last[3+5]==80 && last[3+8]==1);
 assert(!state_mgr_output_pending());
 /* USB boundary between snapshot and submission rejects old effects. */
 host_right(5);during_send=reset_usb;process_state_output();
 assert(!host_session_open && host_session==2 && state_mgr_output_pending());
 assert(speaker_stops==1 && mic_stops==1 && encoder_resets==1);
 process_state_output();assert(!state_mgr_output_pending());
 assert(last[3+10]==0 && last[3+21]==0 && last[3+2]==0 && last[3+3]==0);
 assert(last[3+8]==0 && last[3+44]==0 && last[3+46]==64);
 assert(last[3+4]==90 && last[3+5]==80); /* configured volume retained */
 usb_host_session_end();assert(host_session==2 && !state_mgr_output_pending());
 count=wakes;host_right(9);assert(wakes==count && !state_mgr_output_pending());
 /* New host works; an already accepted transport packet cannot be recalled,
  * but its late ACK must not remove the neutral correction. */
 host_session_open=usb_configured=true;host_right(6);
 during_send=reset_usb;accept_before_event=true;process_state_output();
 assert(last[3+10]==6 && state_mgr_output_pending() && host_session==3);
 accept_before_event=false;process_state_output();assert(last[3+10]==0 && !state_mgr_output_pending());
 host_session_open=usb_configured=true;host_right(2);during_send=reset_bt;
 process_state_output();assert(state_mgr_output_pending());process_state_output();
 assert(last[3+10]==0 && last[3+8]==1 && !state_mgr_output_pending());
 /* SET_CONFIGURATION(0) has no SDK event: final epoch reads detect it. */
 host_right(4);uint32_t previous=host_session;driver_configured=false;
 assert(usb_gamepad_host_session()==previous+1 && !usb_gamepad_host_active());
 assert(state_mgr_output_pending());process_state_output();assert(last[3+10]==0);
 driver_configured=true;host_session_open=usb_configured=true;
 bt_state=BT_HID_STATE_DISCONNECTING;count=wakes;host_right(8);assert(wakes==count);
 assert(!masked);puts("PASS: burst field merge/final release, failed sends, version ACK races, USB detach neutral/rejection/idempotence, new host and BT sessions");
}
'''
# Actual event wiring: bus reset, physical/soft detach end a host epoch;
# suspend/resume deliberately keep it (they are not a different host).
for event in ('RESET','DISCONNECTED'):
 a=usb.index('    case USBD_EVENT_'+event+':');b=usb.index('        break;',a)
 assert 'usb_host_session_end();' in usb[a:b]
for event in ('SUSPEND','RESUME'):
 a=usb.index('    case USBD_EVENT_'+event+':');b=usb.index('        break;',a)
 assert 'usb_host_session_end();' not in usb[a:b]
assert 'usb_host_session_end();' in extract(usb,'void usb_soft_disconnect(')
with tempfile.TemporaryDirectory(prefix='ds5-output-lifetime-') as temp:
 p=Path(temp)
 (p/'bflb_irq.h').write_text('#include <stdint.h>\nuintptr_t bflb_irq_save(void);\nvoid bflb_irq_restore(uintptr_t);\n')
 (p/'test.c').write_text(h)
 subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-I'+str(p),'-I'+str(ROOT/'src'),str(p/'test.c'),str(ROOT/'src/state_mgr.c'),str(ROOT/'src/ds5_protocol.c'),'-o',str(p/'test')],check=True)
 subprocess.run([str(p/'test')],check=True)

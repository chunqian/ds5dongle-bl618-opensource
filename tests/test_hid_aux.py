"""Fault-inject the real auxiliary endpoint adapter and source arbiter."""
from pathlib import Path
import subprocess
import tempfile
root=Path(__file__).resolve().parents[1]
s=(root/'src/usb_gamepad.c').read_text()
def fn(sig):
    start=s.index(sig+'\n{'); end=s.index('{',start)+1; depth=1
    while depth:
        depth+=(s[end]=='{')-(s[end]=='}'); end+=1
    return s[start:end]+'\n'
h=r'''
#include <assert.h>
#include <string.h>
#include <stdio.h>
#include "usb_gamepad.h"
#include "hid_aux.h"
#define BT_HID_STATE_DISCONNECTING 9
static unsigned generation=1, state, masked, writes, fails;
static bool usb_configured=true, kbd_registered=true, kbd_ep_busy;
static uint8_t kbd_buf[9], records[128][9];
static struct hid_aux aux;
static uint32_t aux_session;
static void aux_flush(void);
static uintptr_t bflb_irq_save(void){unsigned old=masked;masked=1;return old;}
static void bflb_irq_restore(uintptr_t old){assert(masked);masked=old;}
static uint32_t bt_hid_host_session(void){return generation;}
static unsigned bt_hid_host_get_state(void){return state;}
static int usbd_ep_start_write(uint8_t bus,uint8_t ep,const uint8_t *data,uint32_t len){
    assert(masked && bus==0 && ep==USB_KBD_EP_IN && kbd_ep_busy);
    if(fails){--fails;return -2;}
    assert(writes<128);memset(records[writes],0,9);memcpy(records[writes++],data,len);return 0;
}
'''
a=s.index('static int aux_write(');b=s.index('/* ---------- HID class',a)
h+=s[a:b]+fn('static void kbd_ep_in_handler(uint8_t busid, uint8_t ep, uint32_t nbytes)')
remap=(root/'src/remap.c').read_text()
a=remap.index('void remap_kbd_tick('); b=remap.index('\n}\n',a)+3
h+=r'''
#include "remap.h"
static remap_entry_t g_profiles[REMAP_PROFILE_COUNT][REMAP_BTN_COUNT];
static uint8_t active_profile;
static bool get_src_bit(const uint8_t *p, int i){return i==0 && p[0];}
'''+remap[a:b]
h+=r'''
static void complete(void){assert(kbd_ep_busy);kbd_ep_in_handler(0,USB_KBD_EP_IN,9);}
static void drain(void){for(unsigned i=0;kbd_ep_busy && i<20;i++)complete();assert(!kbd_ep_busy);}
static bool key(unsigned n,uint8_t value){for(unsigned i=3;i<9;i++)if(records[n][i]==value)return true;return false;}
int main(void){
    uint8_t a[8]={0,0,4}, wake[8]={0,0,0x68}, up[8]={0};
    aux_reset();
    assert(usb_gamepad_send_keys(HID_AUX_REMAP,a,8,1)==0 && writes==1);
    uint8_t flight[9];memcpy(flight,kbd_buf,9);
    assert(!usb_gamepad_kbd_ready());
    assert(usb_gamepad_send_kbd_report(wake,8)!=0 && writes==1);
    assert(!memcmp(flight,kbd_buf,9)); /* busy never mutates DMA storage */
    complete();assert(writes==2 && key(1,4) && key(1,0x68));
    usb_gamepad_send_kbd_report(up,8);complete();
    assert(writes==3 && key(2,4) && !key(2,0x68)); /* source release preserves remap */
    usb_gamepad_send_keys(HID_AUX_REMAP,up,8,1);complete();
    assert(writes==4 && records[3][0]==1 && !key(3,4));drain();

    fails=2;unsigned before=writes;
    assert(usb_gamepad_send_keys(HID_AUX_REMAP,a,8,1)<0);
    assert(!kbd_ep_busy && writes==before);aux_flush();assert(writes==before);
    aux_flush();assert(writes==before+1); /* no new input needed */
    usb_gamepad_release_aux();assert(writes==before+1);complete();
    assert(writes==before+2 && !key(writes-1,4));drain();

    /* A short press/release while the endpoint is occupied survives. */
    usb_gamepad_send_keys(HID_AUX_REMAP,a,8,1);
    before=writes;
    usb_gamepad_send_mouse_session(1,0,0,0,1);
    usb_gamepad_send_mouse_session(0,0,0,0,1);
    drain();
    assert(writes==before+2 && records[before][0]==3 && records[before][1]==1);
    assert(records[before+1][0]==3 && records[before+1][1]==0);
    usb_gamepad_release_aux();drain();

    /* Three report IDs get fair turns; relative movement isn't duplicated. */
    usb_gamepad_send_keys(HID_AUX_REMAP,a,8,1);
    usb_gamepad_send_mouse_session(1,100,-50,0,1);
    usb_gamepad_send_mouse_session(1,100,-50,0,1);
    usb_gamepad_send_consumer_session(1,1);before=writes;drain();
    int x=0,y=0;bool consumer=false;
    for(unsigned i=before;i<writes;i++){
        if(records[i][0]==3){x+=(int8_t)records[i][2];y+=(int8_t)records[i][3];}
        if(records[i][0]==2)consumer=true;
    }
    assert(x==200 && y==-100 && consumer);
    usb_gamepad_release_aux();drain();
    assert(!aux.sent_buttons && !aux.sent_consumer && hid_aux_keyboard_sent(&aux));

    /* Old session cannot publish; pending keys/motion die on session rollover. */
    usb_gamepad_send_keys(HID_AUX_REMAP,a,8,1);
    usb_gamepad_send_kbd_report(wake,8);generation=2;complete();drain();
    assert(!key(writes-1,4) && !key(writes-1,0x68));before=writes;
    assert(usb_gamepad_send_keys(HID_AUX_PS,a,8,1)<0);
    assert(usb_gamepad_send_mouse_session(1,10,0,0,1)<0 && writes==before);
    usb_gamepad_send_keys(HID_AUX_PS,a,8,2);state=BT_HID_STATE_DISCONNECTING;
    complete();drain();assert(!key(writes-1,4));state=0;

    /* Suspend preserves release debt; reset cancels the old USB session. */
    usb_gamepad_send_keys(HID_AUX_PS,a,8,2);drain();
    usb_configured=false;usb_gamepad_release_aux();before=writes;aux_flush();
    assert(writes==before);usb_configured=true;aux_flush();drain();assert(!key(writes-1,4));
    usb_gamepad_send_keys(HID_AUX_REMAP,a,8,2);usb_configured=false;aux_reset();
    before=writes;usb_configured=true;aux_flush();assert(writes==before);
    assert(usb_gamepad_send_keys(HID_AUX_REMAP,a,8,2)==0);drain();
    /* Real remap producer: busy publication must not count as submitted. */
    usb_gamepad_release_aux();drain();
    g_profiles[0][0].type=REMAP_TYPE_KBD;g_profiles[0][0].value=4;
    uint8_t input[63]={1};fails=2;before=writes;
    remap_kbd_tick(input,2);assert(!aux.sent_keys[2] && writes==before);
    aux_flush();aux_flush();assert(key(writes-1,4));
    input[0]=0;remap_kbd_tick(input,2);drain();assert(!key(writes-1,4));
    assert(!masked);
    puts("PASS: shared endpoint ownership, source merging, busy/failure retries, releases, motion, fairness, session/reset isolation");
}
'''
with tempfile.TemporaryDirectory() as d:
    p=Path(d);(p/'test.c').write_text(h)
    subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-I'+str(root/'src'),str(p/'test.c'),str(root/'src/hid_aux.c'),'-o',str(p/'test')],check=True)
    subprocess.run([str(p/'test')],check=True)

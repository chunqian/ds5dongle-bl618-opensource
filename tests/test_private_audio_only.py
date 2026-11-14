"""Run actual HID handlers: reject old web commands, retain CR DAUD and native HID."""
from pathlib import Path
import subprocess,tempfile
root=Path(__file__).resolve().parents[1]
s=(root/'src/usb_gamepad.c').read_text()
def fn(sig):
 a=s.index(sig);b=s.index('{',a)+1;d=1
 while d:d+=(s[b]=='{')-(s[b]=='}');b+=1
 return s[a:b]+'\n'
h=r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "usb_audio_control.h"
#include "usb_gamepad.h"
#define LOG_INF(...) ((void)0)
#define LOG_ISR(...) ((void)0)
#define BT_HID_STATE_CONNECTED 1
#define pdTRUE 1
#define SET_REPORT_MAX_DATA 64
typedef int BaseType_t;
static bool feature_set_rejected;
static int set_report_queue=1;
static unsigned forwarded,queries,outputs;
static uint8_t feature_resp_buf[257];
static struct usb_audio_control audio_control;
static uint64_t bflb_mtimer_get_time_us(void){return 1;}
static bool dse_is_profile_report(uint8_t id){return id>=0x70 && id<=0x7b;}
static bool dse_profiles_ready(void){return true;}
static bool bt_hid_host_copy_feature(uint8_t id,uint8_t *out,uint16_t cap,uint16_t *len){
 (void)id;(void)cap;queries++;*out=0x42;*len=1;return true;
}
static void bt_hid_host_refresh_feature(uint8_t id){(void)id;queries++;}
static bool ds5_feature_fallback(uint8_t id,uint8_t *out,uint32_t *len){(void)id;(void)out;(void)len;assert(0);return false;}
bool usb_gamepad_host_active(void){return true;}
uint32_t usb_gamepad_host_session(void){return 1;}
static uint32_t bt_hid_host_session(void){return 1;}
static unsigned bt_hid_host_get_state(void){return BT_HID_STATE_CONNECTED;}
static int xQueueSendFromISR(int q,const void *p,void *wake){(void)q;(void)p;(void)wake;forwarded++;return pdTRUE;}
static void output(const uint8_t *p,uint16_t n){(void)p;(void)n;outputs++;}
static usb_gamepad_output_cb_t output_callback=output;
'''
a=s.index('typedef struct {');b=s.index('} set_report_entry_t;',a)+len('} set_report_entry_t;')
h+=s[a:b]+'\n'+fn('void usbd_hid_get_report(')+fn('void usbd_hid_set_report(')
h+=r'''
int main(void){
 uint8_t report[128]={0},*out=NULL;uint32_t len=0;
 for(unsigned id=0xf6;id<=0xff;id++){
  usbd_hid_get_report(0,0,id,3,&out,&len);
  if(id==0xf9){assert(len==22 && out[0]==0xf9 && !memcmp(out+14,"DAUD\1",5));for(unsigned i=1;i<14;i++)assert(out[i]==0);}
  else assert(!len);
  for(unsigned cmd=0;cmd<8;cmd++){
   memset(report,0,sizeof(report));report[0]=id;report[1]=cmd;
   feature_set_rejected=false;usbd_hid_set_report(0,0,id,3,report,sizeof(report));
   assert(feature_set_rejected && !audio_control.pending && !audio_control.visible);
  }
 }
 assert(!forwarded && !queries && !outputs);
 uint8_t command[64]={0xf6,4,'D','A','U','D',1,1};
 feature_set_rejected=false;usbd_hid_set_report(0,0,0xf6,3,command,sizeof(command));
 assert(!feature_set_rejected && audio_control.pending && audio_control.requested);
 usbd_hid_get_report(0,0,0xf9,3,&out,&len);assert(out[19]==0 && out[20]==1 && out[21]==1);
 /* Unprefixed payload accepted as before. */
 command[7]=0;usbd_hid_set_report(0,0,0xf6,3,command+1,63);
 assert(!feature_set_rejected && !audio_control.pending && !audio_control.requested);
 usbd_hid_get_report(0,0,5,3,&out,&len);assert(len==2 && out[0]==5 && queries==1);
 uint8_t native[64]={0x60,1};usbd_hid_set_report(0,0,0x60,3,native,64);assert(forwarded==1);
 native[0]=2;usbd_hid_set_report(0,0,2,2,native,64);assert(outputs==1);
 puts("PASS: private web reads/writes rejected without BT forwarding; CR DAUD offsets/control and native Feature/output retained");
}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d);(p/'test.c').write_text(h)
 subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-I'+str(root/'src'),str(p/'test.c'),str(root/'src/usb_audio_control.c'),'-o',str(p/'test')],check=True)
 subprocess.run([str(p/'test')],check=True)

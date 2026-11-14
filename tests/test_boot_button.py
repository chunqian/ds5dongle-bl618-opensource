"""Exercise production debounce/gestures and BT-task action dispatch without hardware."""
from pathlib import Path
import subprocess, tempfile
ROOT=Path(__file__).resolve().parents[1]
source=(ROOT/'src/main.c').read_text()
def extract(signature):
    a=source.index(signature);b=source.index('{',a)+1;depth=1
    while depth:
        depth+=(source[b]=='{')-(source[b]=='}');b+=1
    return source[a:b]
harness=r'''
#include "boot_button.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static struct boot_button button;
static enum boot_button_event events[32];
static unsigned count;
static void step(uint64_t t, bool pressed) {
    enum boot_button_event e=boot_button_update(&button,t,pressed);
    if(e) { assert(count<32);events[count++]=e;boot_button_ack(&button); }
}
static void fresh(void) { memset(&button,0,sizeof(button));count=0; }
static bool pulse(unsigned ms,unsigned a,unsigned b) { return ms>=a && ms<b; }
int main(void) {
    /* Previously merged by 100ms polling. Test every 1ms phase of the 5ms sampler. */
    for(unsigned phase=0;phase<5;phase++) {
        fresh();
        for(unsigned ms=phase;ms<1400;ms+=5)
            step((uint64_t)ms*1000,pulse(ms,90,160)||pulse(ms,190,260));
        assert(count==1 && events[0]==BOOT_BUTTON_DOUBLE);
    }
    /* Press/release bounce shorter than debounce cannot create clicks. */
    fresh();
    for(unsigned ms=0;ms<1500;ms+=5)
        step((uint64_t)ms*1000,pulse(ms,90,95)||pulse(ms,100,105)||
             pulse(ms,110,180)||pulse(ms,185,190));
    assert(count==1 && events[0]==BOOT_BUTTON_SINGLE);
    fresh();
    for(unsigned ms=0;ms<1000;ms+=5) step((uint64_t)ms*1000,pulse(ms,90,100));
    assert(!count);
    /* Wall-clock click deadline, including debounce crossing its boundary. */
    fresh();step(100000,true);step(115000,true);step(200000,false);step(215000,false);
    step(699999,false);assert(!count);step(700000,false);assert(count==1);
    fresh();step(100000,true);step(115000,true);step(200000,false);step(215000,false);
    step(699000,true);step(714000,true);step(800000,false);step(815000,false);
    step(1300000,false);assert(count==1 && events[0]==BOOT_BUTTON_DOUBLE);
    fresh();step(100000,true);step(115000,true);step(200000,false);step(215000,false);
    step(701000,true);step(716000,true);step(800000,false);step(815000,false);
    step(1300000,false);assert(count==2 && events[0]==BOOT_BUTTON_SINGLE && events[1]==BOOT_BUTTON_SINGLE);
    /* Three seconds is measured from the debounced press's initial edge. */
    fresh();step(100000,true);step(115000,true);step(3099999,true);assert(!count);
    step(3100000,true);assert(count==1 && events[0]==BOOT_BUTTON_HOLD);
    step(5000000,true);assert(count==1);
    step(5100000,false);step(5115000,false);
    assert(count==2 && events[1]==BOOT_BUTTON_RELEASE);
    step(6000000,false);assert(count==2);
    /* Release just before threshold must not reset while release debounces. */
    fresh();step(100000,true);step(115000,true);step(3099999,false);step(3114999,false);
    step(3600000,false);assert(count==1 && events[0]==BOOT_BUTTON_SINGLE);
    /* Delayed poll observes a long hold's release before the hold check. */
    fresh();step(100000,true);step(115000,true);step(3200000,false);step(3215000,false);
    step(3220000,false);assert(count==2 && events[0]==BOOT_BUTTON_HOLD && events[1]==BOOT_BUTTON_RELEASE);
    /* Full destination queue: retain HOLD, continue debounce and later emit RELEASE. */
    fresh();step(100000,true);step(115000,true);
    assert(boot_button_update(&button,3100000,true)==BOOT_BUTTON_HOLD);
    assert(boot_button_update(&button,3200000,false)==BOOT_BUTTON_HOLD);
    assert(boot_button_update(&button,3215000,false)==BOOT_BUTTON_HOLD);
    boot_button_ack(&button);step(4000000,false);
    assert(count==1 && events[0]==BOOT_BUTTON_RELEASE);
    /* A second press starts a fresh hold timer, not the previous press's timer. */
    fresh();step(100000,true);step(115000,true);step(2900000,false);step(2915000,false);
    step(3000000,true);step(3015000,true);step(5999999,true);assert(!count);
    step(6000000,true);assert(count==1 && events[0]==BOOT_BUTTON_HOLD);
    puts("PASS: fast double click across sampling phases, bounce, deadlines, hold/release and queue backpressure");
}
'''
dispatch=r'''
#include "boot_button.h"
#include <stdbool.h>
#include <stdio.h>
#include <assert.h>
#define BUTTON_QUEUE_DEPTH 16
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
#define LOG_ERR(...) ((void)0)
enum bt_hid_host_state { BT_HID_STATE_IDLE, BT_HID_STATE_CONNECTED };
enum {LED_BLINK_TRIPLE,LED_RED_BLINK,LED_GREEN_SOLID,LED_PURPLE_BLINK_SLOW,LED_PURPLE_BLINK_FAST};
static bool bt_init_failed,ds5_connected,scan_after_disconnect,request_ok;
static unsigned requests,reboots,scans,switches,disconnects,bonds=2;
static enum bt_hid_host_state state;
static enum boot_button_event queue[16];static unsigned rd,wr;
static int button_queue;
static int xQueueReceive(int q,enum boot_button_event *e,int wait){(void)q;(void)wait;if(rd==wr)return 0;*e=queue[rd++];return 1;}
static bool factory_reset_request(void){requests++;return request_ok;}
static void led_status_set(unsigned p){(void)p;}
static void vTaskDelay(unsigned ms){assert(ms==300);}
static void GLB_SW_System_Reset(void){reboots++;}
static enum bt_hid_host_state bt_hid_host_get_state(void){return state;}
static unsigned bt_hid_host_get_bonded_count(void){return bonds;}
static void bt_hid_host_switch_next(void){switches++;}
static void bt_hid_host_disconnect(void){disconnects++;}
static void bt_hid_host_scan_start(unsigned x){(void)x;scans++;}
'''+extract('static void process_button_events(void)')+r'''
static void send(enum boot_button_event e){queue[wr++]=e;process_button_events();}
int main(void){
 request_ok=false;send(BOOT_BUTTON_HOLD);send(BOOT_BUTTON_RELEASE);
 assert(requests==1 && !reboots && !scans);
 bonds=0;send(BOOT_BUTTON_SINGLE);assert(!switches && !scans && !disconnects);
 bonds=1;send(BOOT_BUTTON_SINGLE);assert(switches==1 && !scans);
 bonds=2;send(BOOT_BUTTON_SINGLE);assert(switches==2 && !scans);
 send(BOOT_BUTTON_DOUBLE);assert(scans==1 && switches==2);
 ds5_connected=true;send(BOOT_BUTTON_DOUBLE);assert(disconnects==1 && scan_after_disconnect);
 bt_init_failed=true;send(BOOT_BUTTON_SINGLE);assert(reboots==1);
 bt_init_failed=false;request_ok=true;send(BOOT_BUTTON_HOLD);assert(reboots==1);
 send(BOOT_BUTTON_RELEASE);assert(reboots==2 && requests==2);
 /* Both already queued while BT was blocked: save first, then reboot on release. */
 queue[wr++]=BOOT_BUTTON_HOLD;queue[wr++]=BOOT_BUTTON_RELEASE;process_button_events();
 assert(requests==3 && reboots==3);
 puts("PASS: BT-owned click actions, failed reset retry and queued HOLD -> RELEASE ordering");
}
'''
with tempfile.TemporaryDirectory(prefix='ds5-button-') as d:
 p=Path(d)
 for name,code,files in [('gesture',harness,[ROOT/'src/boot_button.c']),('dispatch',dispatch,[])]:
    (p/(name+'.c')).write_text(code)
    subprocess.run(['clang','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-I'+str(ROOT/'src'),str(p/(name+'.c')),*map(str,files),'-o',str(p/name)],check=True)
    subprocess.run([str(p/name)],check=True)

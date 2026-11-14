"""Fault injection for actual worker recovery, startup fallback and blacklist writes."""
from pathlib import Path
import subprocess,tempfile
ROOT=Path(__file__).resolve().parents[1]
audio=(ROOT/'src/audio.c').read_text()
bt=(ROOT/'src/bt_hid_host.c').read_text()
main=(ROOT/'src/main.c').read_text()
def extract(source,sig):
 a=source.index(sig); b=source.index('{',a)+1; d=1
 while d:
  d+=(source[b]=='{')-(source[b]=='}'); b+=1
 return source[a:b]+'\n'
workers=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
typedef void *TaskHandle_t;
typedef void *TimerHandle_t;
typedef int BaseType_t;
typedef uint32_t StackType_t;
typedef int StaticTask_t;
#define pdFAIL 0
#define pdPASS 1
#define configMAX_PRIORITIES 8
#define LOG_INF(...) ((void)0)
#define LOG_ERR(...) ((void)0)
static unsigned scheduler_suspended;
static void vTaskSuspendAll(void) { ++scheduler_suspended; }
static void xTaskResumeAll(void) { assert(scheduler_suspended); --scheduler_suspended; }
static uintptr_t masked;
static uintptr_t bflb_irq_save(void) { uintptr_t old=masked; masked=1; return old; }
static void bflb_irq_restore(uintptr_t old) { masked=old; }
static uint64_t clock_us, encode_start_us;
static uint64_t bflb_mtimer_get_time_us(void) { return clock_us; }
static bool encoding_in_progress, encoder_reinit_pending, audio_task_killed;
static bool audio_tasks_enabled, mic_task_started;
static uint32_t audio_restart_generation;
static uint64_t audio_restart_us, mic_restart_us;
static TaskHandle_t audio_task_handle;
static int decoder=1,mic_queue=1;
static unsigned suspended,deleted,resets, audio_creates,mic_creates,next_handle=1;
static unsigned audio_failures,mic_failures;
static bool die_during_create;
static void audio_task(void *p) { (void)p; }
static void audio_mic_task(void *p) { (void)p; }
static void audio_reset_encoder(void) { resets++; }
static void vTaskSuspend(TaskHandle_t h) { assert(h && !masked); suspended++; }
static void vTaskDelete(TaskHandle_t h) { assert(h && !masked); deleted++; }
static TaskHandle_t xTaskCreateStatic(void (*task)(void *),const char *name,unsigned stack,void *arg,unsigned priority,StackType_t *storage,StaticTask_t *tcb) {
 (void)name;(void)stack;(void)arg;(void)priority; assert(!masked && storage && tcb);
 if(task==audio_task) {
  assert(scheduler_suspended); audio_creates++;
  if(audio_failures) { --audio_failures; return NULL; }
  if(die_during_create) {
   die_during_create=false; ++audio_restart_generation; audio_task_killed=true;
  }
 } else { assert(task==audio_mic_task); mic_creates++; if(mic_failures) { --mic_failures; return NULL; } }
 return (void *)(uintptr_t)next_handle++;
}
'''
workers+=audio[audio.index('#define STACK_WORDS'):audio.index('static StaticQueue_t mic_queue_cb')]
workers+=audio[audio.index('#define ENCODE_TIMEOUT_US'):audio.index('static void encode_watchdog_cb')]
for sig in ['static void encode_watchdog_cb(', 'bool audio_check_respawn(', 'void audio_start_tasks(']:workers+=extract(audio,sig)
workers+=r'''
int main(void) {
 assert(!audio_check_respawn() && !audio_creates);
 audio_failures=mic_failures=1; audio_start_tasks();
 assert(audio_task_killed && !audio_task_handle && !mic_task_started);
 audio_check_respawn(); assert(audio_creates==1 && mic_creates==1);
 clock_us=1000000; assert(audio_check_respawn());
 assert(!audio_task_killed && mic_task_started && audio_task_handle);
 audio_check_respawn(); assert(audio_creates==2 && mic_creates==2);
 encoding_in_progress=true; encode_start_us=clock_us;
 encode_watchdog_cb(NULL); assert(!suspended);
 clock_us+=15000; encode_watchdog_cb(NULL);
 assert(suspended==1 && audio_task_killed && resets==1 && encoder_reinit_pending);
 audio_failures=1; assert(!audio_check_respawn());
 assert(deleted==1 && audio_task_killed && !audio_task_handle);
 audio_check_respawn(); assert(deleted==1 && audio_creates==3);
 clock_us+=1000000; assert(audio_check_respawn());
 assert(deleted==1 && !audio_task_killed && audio_task_handle && audio_creates==4);
 encoding_in_progress=true; encode_start_us=clock_us-15000; encode_watchdog_cb(NULL);
 die_during_create=true; assert(audio_check_respawn());
 assert(audio_task_killed && audio_task_handle && deleted==2);
 assert(audio_check_respawn() && !audio_task_killed && deleted==3);
 assert(!masked); puts("PASS: task allocation failure, paced retry, one-time deletion, watchdog recovery and newer failure preservation");
}
'''
blacklist=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>
typedef uint8_t u8_t;
typedef uint32_t TickType_t;
typedef struct { uint8_t val[6]; } bt_addr_t;
#define LOG_INF(...) ((void)0)
#define pdMS_TO_TICKS(x) (x)
static unsigned critical_depth;
#define taskENTER_CRITICAL() (++critical_depth)
#define taskEXIT_CRITICAL() do { assert(critical_depth); --critical_depth; } while(0)
static uint32_t tick;
static TickType_t xTaskGetTickCount(void) { return tick; }
static int bt_addr_cmp(const bt_addr_t *a,const bt_addr_t *b) { return memcmp(a,b,6); }
static void bt_addr_copy(bt_addr_t *a,const bt_addr_t *b) { *a=*b; }
static bt_addr_t stored[8];
static unsigned writes,deletes,stored_count;
static bool fail;
static void (*during_write)(void);
static int bt_settings_get_bin(const char *key,u8_t *out,size_t cap,size_t *len) {
 (void)key; assert(!critical_depth && cap>=stored_count*6); *len=stored_count*6; memcpy(out,stored,*len); return 0;
}
static int bt_settings_set_bin(const char *key,const u8_t *data,size_t len) {
 (void)key; assert(!critical_depth); writes++; if(fail) return -1;
 memcpy(stored,data,len); stored_count=len/6;
 if(during_write) { void (*fn)(void)=during_write; during_write=NULL; fn(); }
 return 0;
}
static int settings_delete(const char *key) {
 (void)key; assert(!critical_depth); deletes++; if(fail) return -1; stored_count=0; return 0;
}
'''
blacklist+=bt[bt.index('#define BLACKLIST_MAX'):bt.index('/* ---------- multi-controller')]
blacklist+=r'''
static bt_addr_t first={{1}},second={{2}};
static void change_during_save(void) { blacklist_remove(&first); blacklist_add(&second); }
int main(void) {
 blacklist_load(); assert(!blacklist_count); blacklist_persist(); assert(!writes && !deletes);
 blacklist_add(&first); fail=true; blacklist_persist(); assert(writes==1 && blacklist_dirty);
 tick=999; blacklist_persist(); assert(writes==1);
 fail=false; tick=1000; during_write=change_during_save; blacklist_persist();
 assert(writes==2 && blacklist_dirty && stored[0].val[0]==1 && blacklist_contains(&second));
 blacklist_persist(); assert(writes==3 && !blacklist_dirty && stored_count==1 && stored[0].val[0]==2);
 blacklist_persist(); assert(writes==3);
 blacklist_clear(); fail=true; blacklist_persist(); assert(deletes==1 && blacklist_dirty);
 tick=1999; blacklist_persist(); assert(deletes==1);
 fail=false; tick=2000; blacklist_persist(); assert(deletes==2 && !blacklist_dirty && !stored_count);
 blacklist_add(&first); blacklist_add(&first); assert(blacklist_count==1);
 blacklist_remove(&second); blacklist_persist(); assert(!blacklist_dirty && stored_count==1);
 assert(!critical_depth); puts("PASS: blacklist save/delete failures, retry pacing, coherent snapshot and concurrent mutation retention");
}
'''
startup=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#define LOG_INF(...) ((void)0)
#define LOG_ERR(...) ((void)0)
#define LED_RED_BLINK 1
#define LED_PURPLE_BLINK_SLOW 2
static bool bt_init_failed,bt_init_done;
static unsigned clocks,scans,led;
static int init_result,cfg;
static int bt_hid_host_init(int *p) { assert(p==&cfg); return init_result; }
static void GLB_Set_USB_CLK_From_WIFIPLL(int on) { assert(on && !bt_init_done); clocks++; }
static void led_status_set(unsigned value) { led=value; }
static void bt_hid_host_scan_start(unsigned n) { assert(!n && !bt_init_failed); scans++; }
static void start(void) {
'''
a=main.index('    int err = bt_hid_host_init(&cfg);');b=main.index('    LOG_INF("[BT_TASK] Entering main loop',a)
startup+=main[a:b]+r'''
}
int main(void) {
 init_result=-1; start(); assert(bt_init_failed && bt_init_done && clocks==1 && !scans && led==LED_RED_BLINK);
 bt_init_done=false; init_result=0; start(); assert(!bt_init_failed && bt_init_done && clocks==2 && scans==1 && led==LED_PURPLE_BLINK_SLOW);
 puts("PASS: failed BT initialization releases USB clock/wait without inquiry or partial SDK retry");
}
'''
services=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <errno.h>
#define LOG_INF(...) ((void)0)
#define BT_OP(a,b) (b)
#define BT_OGF_BASEBAND 3
static int l2cap_hid_ctrl_server,l2cap_hid_intr_server,hid_host_rec;
static unsigned calls, fail_call, scan_requests;
struct net_buf { int unused; } buffer;
static int result(void) { return ++calls==fail_call ? -EIO : 0; }
static int bt_l2cap_br_server_register(int *server) { (void)server; return result(); }
static int bt_sdp_register_service(int *rec) { (void)rec; return result(); }
static struct net_buf *bt_hci_cmd_create(unsigned op,unsigned size) {
 (void)op;(void)size; return result() ? NULL : &buffer;
}
static void net_buf_add_le16(struct net_buf *buf,unsigned value) { (void)buf;(void)value; }
static int bt_hci_cmd_send_sync(unsigned op,struct net_buf *buf,void *rsp) {
 (void)op;(void)buf;(void)rsp; return result();
}
static void request_scan_mode(bool enabled) { assert(enabled); ++scan_requests; }
static bool poll_scan_mode(void) { return false; } /* transient failure retained by scan owner */
static int initialize_services(void) { int err;
'''
init=extract(bt,'int bt_hid_host_init(')
services+=init[init.index('    err = bt_l2cap_br_server_register('):]
services+=r'''
int main(void) {
 for(fail_call=1; fail_call<=5; ++fail_call) {
  calls=scan_requests=0; assert(initialize_services()<0 && !scan_requests);
  assert(calls==fail_call);
 }
 fail_call=0; calls=scan_requests=0;
 assert(initialize_services()==0 && calls==5 && scan_requests==1);
 puts("PASS: mandatory Control/Interrupt/SDP/page-scan setup failures cannot report ready; transient scan-enable stays retryable");
}
'''

# Owner routing is part of the production call graph, not just standalone mocks.
assert 'dse_task();' not in extract(main,'static void usb_task(')
assert 'usb_gamepad_process_feature_sets();' in extract(main,'static void bt_task(')
assert 'dse_task();' in extract(main,'static void bt_task(')
assert 'handshake_finish_detection();' not in extract(bt,'static int l2cap_ctrl_recv(')
assert 'audio_check_respawn();' in extract(main,'static void usb_task(')
assert 'if (config_usb_stealth() && !bt_init_failed)' in main
with tempfile.TemporaryDirectory(prefix='ds5-recovery-') as tmp:
 p=Path(tmp)
 for name,source in [('workers',workers),('blacklist',blacklist),('startup',startup),('services',services)]:
  (p/(name+'.c')).write_text(source)
  subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined',str(p/(name+'.c')),'-o',str(p/name)],check=True)
  subprocess.run([str(p/name)],check=True)

"""Production inactivity and mic-worker tests: thresholds, reset and stale work."""
from pathlib import Path
import subprocess,tempfile
ROOT=Path(__file__).resolve().parents[1]
bt=(ROOT/'src/bt_hid_host.c').read_text()
audio=(ROOT/'src/audio.c').read_text()
main=(ROOT/'src/main.c').read_text()
def extract(source,signature):
    start=source.index(signature); end=source.index('{',start)+1; depth=1
    while depth:
        depth+=(source[end]=='{')-(source[end]=='}'); end+=1
    return source[start:end]
harness=r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <setjmp.h>
typedef uint32_t TickType_t;
#define pdMS_TO_TICKS(v) (v)
static TickType_t tick;
static uint8_t minutes;
static unsigned disconnects;
static struct { TickType_t last_activity_tick; } hid_ctx;
static uint8_t config_inactive_minutes(void) { return minutes; }
static TickType_t xTaskGetTickCount(void) { return tick; }
static void bt_hid_host_disconnect(void) { disconnects++; }
'''+extract(bt,'static void check_inactivity(')+r'''
#define MIC_OPUS_SIZE 71
#define OPUS_FRAME_SAMPLES 480
#define OPUS_RESET_STATE 1
#define portMAX_DELAY 0
#define pdTRUE 1
#define LOG_ERR(...) ((void)0)
#define LOG_INF(...) ((void)0)
struct mic_message { uint32_t session, stream, epoch; uint8_t data[MIC_OPUS_SIZE]; };
static bool mic_enabled=true, decoder_reset_pending, mic_first_frame;
static int decoder_storage, *decoder=&decoder_storage, mic_queue=1;
static unsigned queue_index, decoded_count, writes, resets;
static uint32_t session=2, stream=1, mic_generation=1;
static uint32_t usb_audio_session(void) { return stream; }
static jmp_buf finished;
static uintptr_t masked;
static uintptr_t bflb_irq_save(void) { uintptr_t old=masked; masked=1; return old; }
static void bflb_irq_restore(uintptr_t old) { masked=old; }
static uint32_t bt_hid_host_session(void) { return session; }
static void vTaskDelay(unsigned delay) { (void)delay; assert(false); }
static int xQueueReceive(int queue,void *data,unsigned delay) {
    (void)queue;(void)delay;
    if(queue_index==9) longjmp(finished,1);
    struct mic_message *frame=data;
    ++queue_index;
    frame->session=queue_index==1?1:session;
    frame->stream=queue_index==4?0:stream;
    frame->epoch=queue_index==7?0:mic_generation;
    /* Each epoch: stale queue entry, interrupted decode, fresh decode. */
    memset(frame->data,0,sizeof(frame->data)); return pdTRUE;
}
static void opus_decoder_ctl(int *codec,unsigned op) {
    assert(codec==decoder && op==OPUS_RESET_STATE); resets++;
}
static int opus_decode(int *codec,const uint8_t *data,unsigned len,int16_t *out,unsigned count,int fec) {
    (void)data;(void)fec;
    assert(codec==decoder && len==71 && count==480); decoded_count++;
    memset(out,0,480*sizeof(*out));
    if(queue_index==2) session=3;
    if(queue_index==5) stream=2;
    if(queue_index==8) mic_generation=2;
    return 480;
}
static void usb_audio_mic_write(const int16_t *data,uint32_t count) {
    (void)data; assert(masked && session==3 && decoded_count%2==0 && count==480); writes++;
}
'''+extract(audio,'void audio_mic_task(')+r'''
int main(void) {
    uint8_t input[13]={0xa1,0x31,1,127,127,127,127,0,0,0,8,0,0};
    minutes=1; tick=60000; check_inactivity(input,sizeof(input)); assert(!disconnects);
    tick++; check_inactivity(input,sizeof(input)); assert(disconnects==1);
    /* Any trigger pressure and mute/PS/touch input count; axes use 120..140. */
    for(unsigned i=3;i<=12;i++) {
        if(i==9) continue; /* sequence byte is not activity */
        uint8_t old=input[i]; input[i]=i<7?119:1;
        tick+=60001; check_inactivity(input,sizeof(input)); assert(disconnects==1);
        input[i]=old;
    }
    tick+=60001; minutes=0; check_inactivity(input,sizeof(input)); assert(disconnects==1);
    minutes=1; input[2]=2; check_inactivity(input,sizeof(input)); assert(disconnects==1);
    input[2]=1; check_inactivity(input,12); assert(disconnects==1);
    /* Fresh connection baseline, even after long idle and across tick wrap. */
    hid_ctx.last_activity_tick=UINT32_MAX-100; tick=200;
    check_inactivity(input,sizeof(input)); assert(disconnects==1);
    tick=60000; check_inactivity(input,sizeof(input)); assert(disconnects==2);
    decoder_reset_pending=true;
    if(!setjmp(finished)) audio_mic_task(NULL);
    assert(decoded_count==6 && writes==3 && resets==4);
    puts("PASS: DS5Dongle inactivity thresholds/raw flags/tick wrap, BT/USB/mic epoch stale queue and in-flight decode isolation");
}
'''
for forbidden in ('CONN_WATCHDOG_US','CONNECTING_TIMEOUT_TICKS','L2CAP_CONNECT_TIMEOUT_TICKS','HANDSHAKE_TIMEOUT_US'):
    assert forbidden not in main
assert 'SECURITY_TIMEOUT_TICKS' not in bt
assert '0x1F40' not in bt
assert 'bt_hid_host_disconnect()' not in audio
with tempfile.TemporaryDirectory(prefix='ds5-session-') as d:
    p=Path(d); (p/'test.c').write_text(harness)
    subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined',str(p/'test.c'),'-o',str(p/'test')],check=True)
    subprocess.run([str(p/'test')],check=True)

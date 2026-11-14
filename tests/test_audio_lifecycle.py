"""Production audio lifecycle with mocked USB, BT and codec boundaries (no hardware)."""
from pathlib import Path
import subprocess
import tempfile

root=Path(__file__).resolve().parents[1]
audio=(root/'src/audio.c').read_text()
usb=(root/'src/ds5_usb_audio.c').read_text()
gamepad=(root/'src/usb_gamepad.c').read_text()
def extract(source,signature):
    start=source.index(signature); end=source.index('{',start)+1; depth=1
    while depth:
        depth+=(source[end]=='{')-(source[end]=='}'); end+=1
    return source[start:end]+'\n'

harness=r'''
#include <assert.h>
#include <string.h>
#include <stdio.h>
#include "audio.h"
#include "ds5_usb_audio.h"
#include "ds5_protocol.h"
#include "config.h"
#define LOG_INF(...) ((void)0)
#define LOG_ERR(...) ((void)0)
#define OPUS_OUT_SIZE 200
#define HAPTIC_BUF_SIZE 64
#define OPUS_APPLICATION_RESTRICTED_CELT 0
#define OPUS_FRAMESIZE_10_MS 1
#define OPUS_SET_EXPERT_FRAME_DURATION(x) (x)
#define OPUS_SET_BITRATE(x) (x)
#define OPUS_SET_VBR(x) (x)
#define OPUS_SET_COMPLEXITY(x) (x)
#define OPUS_SET_FORCE_CHANNELS(x) (x)
#define OPUS_RESET_STATE 99
#define PCM_BUF_SAMPLES USB_AUDIO_ACCUM_SAMPLES
#define pdFALSE 0
typedef int BaseType_t;
static int audio_sem;
static void xSemaphoreGiveFromISR(int sem, int *woken) { (void)sem; (void)woken; }
#define portYIELD_FROM_ISR(x) ((void)(x))
#define PCM_BUF_BYTES (USB_AUDIO_ACCUM_SAMPLES*USB_AUDIO_CHANNELS*sizeof(int16_t))
#define MIC_RING_SIZE USB_AUDIO_MIC_RING_SAMPLES
#define BT_HID_STATE_CONNECTED 3
static uintptr_t masked;
static uintptr_t bflb_irq_save(void) { uintptr_t old=masked; masked=1; return old; }
static void bflb_irq_restore(uintptr_t old) { masked=old; }
static unsigned state=BT_HID_STATE_CONNECTED, session=1;
static unsigned bt_hid_host_get_state(void) { return state; }
static uint32_t bt_hid_host_session(void) { return session; }
static struct config_body cfg;
struct config_body *config_get(void) { return &cfg; }
static bool spk_active;
static void state_mgr_set_spk_active(bool on) { spk_active=on; }
static bool state_mgr_is_spk_active(void) { return spk_active; }
static unsigned reads, writes, closed, queue_resets, status_sent, audio_sent;
static bool fail_status, toggle_during_send, fail_read, fail_write;
static uint64_t clock_us;
static uint64_t bflb_mtimer_get_time_us(void) { return clock_us; }
static bool last_mic_status;
static int usbd_ep_start_read(uint8_t bus,uint8_t ep,uint8_t *data,uint32_t len) {
    (void)bus; assert(masked && ep==USB_AUDIO_EP_OUT && data && len==USB_AUDIO_OUT_MPS);
    reads++; return fail_read ? -1 : 0;
}
static int usbd_ep_start_write(uint8_t bus,uint8_t ep,const uint8_t *data,uint32_t len) {
    (void)bus; assert(masked && ep==USB_AUDIO_MIC_EP_IN && data && len==192);
    writes++; return fail_write ? -1 : 0;
}
static void usbd_ep_close(uint8_t bus,uint8_t ep) { (void)bus;(void)ep; closed++; }
static void xQueueReset(unsigned q) { assert(q); queue_resets++; }
uint32_t ds5_crc32(uint8_t seed,const uint8_t *data,uint16_t len) { (void)seed;(void)data;(void)len; return 0; }
static int bt_hid_host_send_output_session(const uint8_t *data,unsigned len,uint32_t epoch) {
    if (epoch!=session || state!=BT_HID_STATE_CONNECTED) return -1;
    if (data[0]==0x39) { assert(len==547); audio_sent++; return 0; }
    assert(data[0]==0x32 && len==142); status_sent++;
    last_mic_status=data[4]==3;
    if (toggle_during_send) { toggle_during_send=false; audio_set_mic_active(false); }
    return fail_status?-1:0;
}
static uint32_t host_epoch=1;
static uint32_t usb_gamepad_host_session(void) { return host_epoch; }
static int bt_hid_host_send_state_session(const uint8_t *data,unsigned len,uint32_t epoch,uint32_t host) {
    if (host!=host_epoch) return -1;
    return bt_hid_host_send_output_session(data,len,epoch);
}

static bool stream_active, mic_active, host_speaker_open, host_mic_open, audio_visible, audio_suspended;
static bool speaker_ep_busy, mic_ep_busy;
static uint64_t speaker_retry_us, mic_retry_us;
static uint32_t stream_generation, pcm_write_pos, mic_ring_wr, mic_ring_rd;
static uint8_t pcm_write_idx, pcm_read_idx;
static bool pcm_ready;
static int16_t pcm_buf[2][USB_AUDIO_ACCUM_SAMPLES*USB_AUDIO_CHANNELS];
static int16_t mic_ring[MIC_RING_SIZE*USB_AUDIO_MIC_CHANNELS];
static uint8_t iso_rx_buf[USB_AUDIO_OUT_MPS], iso_mic_tx_buf[USB_AUDIO_MIC_MPS];
struct usbd_interface { uint8_t intf_num; void *class_interface_handler, *class_endpoint_handler, *notify_handler; };
static struct usbd_interface audio_intf_ctrl, audio_intf_stream, audio_intf_mic;
static unsigned entity_table[2];
static void usbd_audio_init_intf(unsigned bus,struct usbd_interface *intf,unsigned version,const void *table,unsigned count) {
    (void)bus;(void)version;(void)table;(void)count;
    intf->class_interface_handler=intf->class_endpoint_handler=intf->notify_handler=(void *)1;
}
static uint8_t audio_seq, packet_counter;
static bool plug_headset, mic_enabled, mic_status_pending, decoder_reset_pending;
static unsigned mic_generation, mic_queue=1;
static bool encoder_reset_pending, encoder_full_reset_pending, encoder_reinit_pending;
static uint32_t encoder_generation;
static int encoder_storage, *encoder=&encoder_storage, encoder_force_channels;
static uint8_t opus_slots[2][OPUS_OUT_SIZE];
static int8_t haptic_slots[2][HAPTIC_BUF_SIZE];
static bool owner_context, inject_reset;
static unsigned codec_resets, codec_inits;
static void opus_encoder_ctl(int *enc,int op) {
    assert(owner_context && !masked && enc==encoder);
    if (op==OPUS_RESET_STATE) codec_resets++;
    if (inject_reset) { inject_reset=false; audio_reset_encoder(); }
}
static void opus_encoder_init(int *enc,unsigned rate,unsigned channels,unsigned app) {
    (void)app; assert(owner_context && enc==encoder && rate==48000 && channels==2); codec_inits++;
}
'''
for sig in ('uint32_t usb_audio_session(', 'static void speaker_arm(', 'static void mic_send_next(', 'void usbd_audio_open(',
            'void usbd_audio_close(', 'void usb_audio_suspend(', 'void usb_audio_resume(',
            'void usb_audio_set_visible(', 'bool usb_audio_is_active(', 'void usb_audio_stop(',
            'void usb_audio_discard_pending(', 'bool usb_audio_read(', 'bool usb_audio_mic_is_active(',
            'void usb_audio_discard_mic(', 'void usb_audio_mic_stop(', 'void usb_audio_poll(', 'static void audio_mic_ep_in_handler(', 'static void audio_ep_out_handler('):
    harness+=extract(usb,sig)
for sig in ('void audio_set_mic_active(', 'bool audio_mic_active(', 'void audio_reset_encoder(',
            'void audio_reset(', 'static void audio_owner_reset(', 'static int send_mic_status(',
            'static void audio_sync_mic(', 'static int send_audio_report('):
    harness+=extract(audio,sig)
# Exercise the production F9 flag expressions, including config and connection state.
a=gamepad.index('            uint8_t aflags = 0x80;')
b=gamepad.index('            feature_resp_buf[2] = aflags;',a)
harness+='static uint8_t status_flags(void) {\n'+gamepad[a:b]+'return aflags;\n}\n'
harness+=r'''
static void owner_reset(void) { owner_context=true; audio_owner_reset(); owner_context=false; }
int main(void) {
    usb_audio_set_visible(true);
    usbd_audio_open(0,USB_AUDIO_INTF_STREAM); usbd_audio_open(0,USB_AUDIO_INTF_MIC);
    assert(reads==1 && writes==1 && stream_active && mic_active && mic_enabled);
    assert(status_flags()==0x83); audio_sync_mic(); assert(last_mic_status && !mic_status_pending);
    owner_reset();
    unsigned before=reads;
    usb_audio_suspend(); usb_audio_suspend(); /* repeated notification is idempotent */
    assert(host_speaker_open && host_mic_open && !stream_active && !mic_active && !mic_enabled);
    assert(status_flags()==0x80);
    usb_audio_resume(); assert(reads==before+1 && writes==2 && mic_enabled && stream_active);
    usb_audio_resume(); assert(reads==before+1 && writes==2);
    audio_sync_mic(); assert(last_mic_status); owner_reset();

    /* Bluetooth-only loss preserves host interfaces; reconnection requires no SET_INTERFACE. */
    state=0; session++; pcm_ready=true; mic_ring_wr=20;
    memset(opus_slots,0x55,sizeof(opus_slots)); memset(haptic_slots,0x55,sizeof(haptic_slots));
    packet_counter=20; audio_reset();
    assert(usb_audio_is_active() && usb_audio_mic_is_active() && host_mic_open);
    assert(!mic_enabled && !pcm_ready && !mic_ring_wr && queue_resets==1);
    assert(opus_slots[0][0]==0x55 && haptic_slots[0][0]==0x55 && packet_counter==20);
    assert(status_flags()==0x80); audio_sync_mic();
    state=BT_HID_STATE_CONNECTED; session++;
    fail_status=true; audio_sync_mic(); assert(mic_enabled && mic_status_pending);
    unsigned sent=status_sent; audio_sync_mic(); assert(status_sent==sent+1 && mic_status_pending);
    fail_status=false; audio_sync_mic(); assert(last_mic_status && !mic_status_pending);
    owner_reset(); assert(!packet_counter && !opus_slots[0][0] && !haptic_slots[0][0]);

    /* Configuration disable/reenable is reflected without another host-open request. */
    cfg.disable_mic=1; cfg.disable_speaker=1; audio_sync_mic();
    assert(!mic_enabled && !last_mic_status && status_flags()==0x80);
    cfg.disable_mic=cfg.disable_speaker=0; audio_sync_mic();
    assert(mic_enabled && last_mic_status && status_flags()==0x83);
    mic_status_pending=true; toggle_during_send=true; audio_sync_mic();
    assert(mic_status_pending); audio_sync_mic(); assert(!mic_status_pending && last_mic_status);

    /* A reset request during an owner reset remains pending, never mutates codec from caller. */
    audio_reset_encoder(); inject_reset=true; unsigned resets=codec_resets;
    owner_reset(); assert(encoder_reset_pending && codec_resets==resets+1);
    owner_reset(); assert(!encoder_reset_pending && codec_resets==resets+2);
    encoder_reinit_pending=true; audio_reset_encoder(); owner_reset();
    assert(codec_inits==1 && encoder_force_channels==1);

    uint32_t stream=usb_audio_session(), generation=encoder_generation;
    assert(send_audio_report(session,stream,generation)==0 && audio_sent==1);
    audio_reset_encoder();
    assert(send_audio_report(session,stream,encoder_generation)<0); /* reset before capture */
    owner_reset(); assert(send_audio_report(session,stream,generation)<0);
    generation=encoder_generation; usb_audio_suspend(); usb_audio_resume(); owner_reset();
    assert(send_audio_report(session,stream,generation)<0); /* stream generation changed */
    assert(audio_sent==1);

    /* A closed stream stays closed across suspend; reset while suspended revokes both. */
    usbd_audio_close(0,USB_AUDIO_INTF_MIC); assert(!mic_enabled && !host_mic_open);
    before=writes; usb_audio_suspend(); usb_audio_resume(); assert(writes==before && !mic_active);
    usb_audio_suspend(); usb_audio_stop(); usb_audio_mic_stop(); usb_audio_resume();
    assert(!stream_active && !mic_active && !host_speaker_open && !host_mic_open);
    usbd_audio_open(0,USB_AUDIO_INTF_STREAM); usbd_audio_open(0,USB_AUDIO_INTF_MIC);
    assert(stream_active && mic_active); /* reset no longer leaves suspended latch */
    usb_audio_suspend(); usb_audio_set_visible(false); before=reads;
    usb_audio_resume(); usbd_audio_open(0,USB_AUDIO_INTF_STREAM);
    assert(reads==before && !host_speaker_open && !host_mic_open);
    usb_audio_set_visible(true); usb_audio_resume(); assert(reads==before);
    assert(!masked && closed==1);
    /* Initial/resumed/rearmed transfers retry without needing another SET_INTERFACE. */
    fail_read=fail_write=true;
    usbd_audio_open(0,USB_AUDIO_INTF_STREAM); usbd_audio_open(0,USB_AUDIO_INTF_MIC);
    assert(stream_active && mic_active && !speaker_ep_busy && !mic_ep_busy);
    mic_ring_wr=96; mic_ring_rd=0;
    before=reads; unsigned before_write=writes;
    usb_audio_poll(); assert(reads==before && writes==before_write);
    clock_us=10000; fail_read=fail_write=false; usb_audio_poll();
    assert(reads==before+1 && writes==before_write+1 && mic_ring_rd==48);
    usb_audio_poll(); assert(reads==before+1 && writes==before_write+1);
    fail_read=fail_write=true; masked=1;
    audio_mic_ep_in_handler(0,USB_AUDIO_MIC_EP_IN,192);
    audio_ep_out_handler(0,USB_AUDIO_EP_OUT,0); masked=0;
    assert(!speaker_ep_busy && !mic_ep_busy && mic_ring_rd==48);
    clock_us=20000; fail_read=fail_write=false; usb_audio_poll();
    assert(speaker_ep_busy && mic_ep_busy && mic_ring_rd==96);
    usb_audio_suspend(); fail_read=fail_write=true; usb_audio_resume();
    assert(host_speaker_open && host_mic_open && !speaker_ep_busy && !mic_ep_busy);
    clock_us=30000; fail_read=fail_write=false; usb_audio_poll();
    assert(speaker_ep_busy && mic_ep_busy);
    usb_audio_suspend(); usb_audio_resume();
    fail_read=true; masked=1; audio_ep_out_handler(0,USB_AUDIO_EP_OUT,0); masked=0;
    usb_audio_set_visible(false); before=reads; clock_us=40000; usb_audio_poll();
    assert(reads==before && !speaker_ep_busy && !mic_ep_busy);
    puts("PASS: retained alternate settings, suspend/resume, BT reconnect, mic-status retries, config flags, codec ownership and stale-frame rejection");
}
'''
with tempfile.TemporaryDirectory(prefix='ds5-audio-life-') as directory:
    path=Path(directory); (path/'test.c').write_text(harness)
    subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined',
                    '-I',str(root/'src'),str(path/'test.c'),'-o',str(path/'test')],check=True)
    subprocess.run([str(path/'test')],check=True)

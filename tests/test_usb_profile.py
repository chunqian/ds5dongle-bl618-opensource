"""Exercise real USB descriptors/profile application .

Run without hardware: python3 tests/test_usb_profile.py
"""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'src/usb_gamepad.c').read_text()
audio = (root / 'src/ds5_usb_audio.c').read_text()


def function(signature):
    start = source.index(signature + '\n{')
    end = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end] + '\n'


harness = r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "config.h"
#include "ds5_protocol.h"
#include "usb_gamepad.h"
#include "ds5_usb_audio.h"
#include "usb_audio_control.h"
#define taskENTER_CRITICAL() ((void)0)
#define taskEXIT_CRITICAL() ((void)0)
#define LOG_INF(...) ((void)0)
struct usbd_interface {
    const uint8_t *hid_report_descriptor;
    uint32_t hid_report_descriptor_len;
    uint8_t intf_num;
    void *class_interface_handler;
};
static struct usbd_interface hid_intf;
static struct config_body cfg;
struct config_body *config_get(void) { return &cfg; }
static bool audio_visible;
void usb_audio_set_visible(bool value) { audio_visible = value; }
static uint64_t now_us;
static uint64_t bflb_mtimer_get_time_us(void) { return now_us; }
static bool current_dse_mode, requested_dse_mode, usb_initialized, usb_profile_pending;
static uint64_t usb_profile_request_us;
static uint8_t applied_polling_mode;
static struct usb_audio_control audio_control;
'''
harness += source[source.index('#define USBD_MAX_POWER'):source.index('static bool current_dse_mode')]
harness += source[source.index('static const uint8_t hid_report_desc_ds'):source.index('/* ---- Advance-mode descriptor callbacks ---- */')]
harness += audio[audio.index('#define AUDIO_IT_SPK_ID'):audio.index('\n};')+4]
harness += 'static uint8_t other_speed_desc[USB_HID_CONFIG_SIZE];\n'
for signature in (
    'static void apply_polling_rate(uint8_t mode)',
    'static void apply_audio_profile(void)',
    'static void apply_dse_mode(bool dse)',
    'static bool usb_profile_matches_config(void)',
    'static void request_usb_profile(void)',
    'static void apply_usb_profile(void)',
    'void usb_gamepad_set_dse_mode(bool dse)',
    'static const uint8_t *desc_device_cb(uint8_t speed)',
    'static const uint8_t *desc_config_cb(uint8_t speed)',
    'static const uint8_t *desc_other_speed_cb(uint8_t speed)',
):
    harness += function(signature)
harness += r'''
static unsigned u16(const uint8_t *p) { return p[0] | (p[1]<<8); }
static void check_descriptors(bool edge, bool with_audio, bool wake, unsigned poll) {
    const uint8_t *dev=desc_device_cb(0), *desc=desc_config_cb(0);
    assert(u16(dev+10)==(edge?0x0df2:0x0ce6));
    assert(desc && u16(desc+2)==9+(with_audio?186:0)+25);
    unsigned expected=(with_audio?3:0)+1;
    assert(desc[4]==expected && !!(desc[7]&0x20)==wake);
    unsigned seen=0, hid_count=0, last_intf=255;
    for (unsigned i=9; i<u16(desc+2); i+=desc[i]) {
        assert(desc[i]>=2 && i+desc[i]<=u16(desc+2));
        if (desc[i+1]==4) {
            assert(desc[i+2]<expected);
            if (!desc[i+3]) { assert(desc[i+2]==seen); seen++; }
            last_intf=desc[i+2];
            if (!with_audio) assert(desc[i+5]==3);
        }
        if (desc[i+1]==0x21) {
            if (!hid_count++) {
                assert(last_intf==hid_intf.intf_num);
                assert(u16(desc+i+7)==hid_intf.hid_report_descriptor_len);
                assert(hid_intf.hid_report_descriptor==(edge?hid_report_desc_dse:hid_report_desc_ds));
                assert(desc[i+9+6]==poll);

            }
        }
    }
    assert(seen==expected && hid_count==1);
    assert(audio_visible==with_audio);
}
int main(void) {
    memcpy(config_desc+AUDIO_DESC_OFFSET, audio_desc, sizeof(audio_desc));
    usb_initialized=true;
    /* Real descriptor matrix: both PIDs, audio profiles, legacy auxiliary settings and polling modes. */
    for (unsigned edge=0;edge<2;edge++)
    for (unsigned aud=0;aud<2;aud++)
    for (unsigned keys=0;keys<7;keys++)
    for (unsigned mode=0;mode<3;mode++) {
        memset(&cfg,0,sizeof(cfg));
        cfg.polling_rate_mode=mode;
        cfg.enable_wake=keys==1;
        cfg.ps_shortcut_enabled=keys==2;
        cfg.tp_mode=keys==4?2:keys==5?3:0;
        requested_dse_mode=edge;
        audio_control.visible=aud;
        apply_usb_profile();
        unsigned poll=mode==0?6:mode==1?5:3;
        check_descriptors(edge,aud,keys==1,poll);
        assert(usb_profile_matches_config());
        const uint8_t *other=desc_other_speed_cb(0);
        assert(other && other[1]==7);
        for(unsigned i=9;i<u16(other+2);i+=other[i]) {
            if(other[i+1]!=5 || (other[i+3]&3)!=3) continue;
            assert(other[i+2]==USB_GAMEPAD_EP_IN);
            assert(other[i+6]==(mode==0?4:mode==1?2:1));
        }
    }
    /* A pending host setting must not mutate an attached descriptor set. */
    memset(&cfg,0,sizeof(cfg)); requested_dse_mode=false;
    audio_control.visible=false; apply_usb_profile();
    uint8_t old_device[sizeof(device_desc)], old_config[sizeof(config_desc)];
    memcpy(old_device,device_desc,sizeof(old_device));
    memcpy(old_config,config_desc,sizeof(old_config));
    cfg.enable_usb_sn=1; cfg.enable_wake=1; cfg.polling_rate_mode=2;
    now_us=123; usb_gamepad_set_dse_mode(true);
    assert(usb_profile_pending && usb_profile_request_us==123 && !usb_profile_matches_config());
    now_us=456; request_usb_profile(); assert(usb_profile_request_us==123);
    assert(!memcmp(old_device,desc_device_cb(0),sizeof(old_device)));
    assert(!memcmp(old_config,config_desc,sizeof(old_config)));
    assert(hid_intf.hid_report_descriptor==hid_report_desc_ds);
    apply_usb_profile(); assert(usb_profile_matches_config()); assert(device_desc[16]==3);
    puts("USB descriptor/profile matrix: PASS");
}
'''
assert 'GLB_SW_System_Reset' not in source
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp)
    (path / 'test.c').write_text(harness)
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                    '-I', str(root / 'src'), str(path / 'test.c'),
                    str(root / 'src/usb_audio_control.c'), '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], check=True)

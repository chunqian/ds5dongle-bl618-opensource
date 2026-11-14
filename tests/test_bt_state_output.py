"""Compare actual BL618 state functions with the DS5Dongle reference byte for byte.

DS5_REFERENCE may select the reference checkout. Tests build temporary binaries.
"""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REF = Path(os.environ.get('DS5_REFERENCE', '/Users/shenchunqian/Downloads/DS5Dongle'))
main = (ROOT / 'src/main.c').read_text()


def extract(source, signature):
    start = source.index(signature)
    end = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


production = r'''
#include <assert.h>
#include <string.h>
#include "config.h"
#include "ds5_protocol.h"
#include "usb_gamepad.h"
#include "state_mgr.h"
#include "bflb_irq.h"
#define LOG_INF(...) ((void)0)
static struct config_body cfg;
struct config_body *config_get(void) { return &cfg; }
static unsigned output_count;
static uint8_t last_output[142];
static int bt_hid_host_send_output_reliable_session(const uint8_t *data, uint16_t len, uint32_t session) {
    assert(session==7 && len==142); memcpy(last_output,data,len); ++output_count; return 0;
}
'''
production += extract(main, 'static const uint8_t init_set_state[') + ';\n'
for name in ('build_bt_output_ext', 'send_led_primer', 'on_hid_primer'):
    production += extract(main, ('static void ' if name=='build_bt_output_ext' else 'static int ')+name+'(') + '\n'
production += r'''
void initialize(struct config_body config) {
    cfg=config;
    unsigned before=output_count;
    on_hid_primer(7);
    assert(output_count==before+1);
    assert(last_output[0]==0x32 && last_output[1]==0x10);
    assert(last_output[2]==0x90 && last_output[3]==63);
    uint8_t state[47]; state_mgr_get(state,47);
    assert(!memcmp(last_output+4,state,47));
    for(unsigned i=51;i<138;i++) assert(last_output[i]==0);
    uint8_t crc[4]; ds5_write_le32(crc,ds5_crc32(DS5_BT_OUTPUT_CRC_SEED,last_output,138));
    assert(!memcmp(crc,last_output+138,4));
}
'''
# Execute the actual USB callback: congestion may coalesce wakes, never fields.
production += r'''
enum { BT_HID_STATE_CONNECTED, BT_HID_STATE_DISCONNECTING };
static int bt_state=BT_HID_STATE_CONNECTED;
static int bt_hid_host_get_state(void) { return bt_state; }
static bool ds5_connected=true;
bool usb_gamepad_host_active(void) { return true; }
static unsigned accepted;
static int output_queue;
static int xQueueOverwriteFromISR(int queue,const void *msg,void *wake) {
    (void)queue;(void)wake; assert(*(const uint8_t *)msg==1); ++accepted; return 1;
}
'''
production += extract(main, 'static void on_usb_output(')
production += r'''
void check_host_frames(void) {
    uint8_t frame[64]={DS5_USB_REPORT_ID_OUTPUT};
    frame[1]=0x0c; frame[11]=frame[22]=1; on_usb_output(frame,64);
    frame[1]=0x04; frame[11]=0; frame[22]=0;
    for(unsigned i=0;i<20;i++) on_usb_output(frame,64);
    uint8_t state[47]; state_mgr_get(state,47);
    assert(accepted==21 && state[10]==0 && state[21]==1);
    accepted=0; bt_state=BT_HID_STATE_DISCONNECTING;
    on_usb_output(frame,64); assert(!accepted);
}
'''
# Actual reference bit-field layout and functions; no duplicated model.
utils = (REF / 'src/utils.h').read_text()
start = utils.index('namespace MuteLight')
end = utils.index('\n};', utils.index('struct __attribute__((packed)) SetStateData'))+3
ref_source = (REF / 'src/state_mgr.cpp').read_text()
ref_source = ref_source[ref_source.index('static constexpr SetStateData'):]
reference = r'''
#include <cassert>
#include <cstdio>
#include <cstring>
#include <cstdint>
extern "C" {
#include "config.h"
#include "state_mgr.h"
#include "bflb_irq.h"
void initialize(struct config_body config);
void check_host_frames(void);
}
namespace reference {
static config_body cfg;
static config_body &get_config() { return cfg; }
void set_volume(uint8_t,uint8_t);
void set_gain(uint8_t);
'''+utils[start:end]+'\n'+ref_source+'\n}\n'
reference += r'''
static void compare(void) {
    uint8_t actual[47]; state_mgr_get(actual,47);
    if(memcmp(actual,&reference::state,47)) {
        for(unsigned i=0;i<47;i++) if(actual[i]!=((uint8_t*)&reference::state)[i])
            fprintf(stderr,"byte %u: actual %02x reference %02x\n",i,actual[i],((uint8_t*)&reference::state)[i]);
        assert(false);
    }
}
int main(void) {
    static_assert(sizeof(reference::SetStateData)==63);
    uint32_t random=0x12345678;
    for(unsigned locked=0;locked<2;locked++)
    for(unsigned gain=0;gain<2;gain++) {
        config_body cfg{};
        cfg.speaker_volume=88; cfg.headset_volume=77;
        cfg.speaker_gain=gain?5:0; cfg.trigger_reduce=gain?3:0;
        cfg.lock_volume=locked;
        cfg.led_b=CONFIG_DEFAULT_LED_B;
        initialize(cfg);
        reference::cfg=cfg; reference::state_init();
        reference::state.LedRed=cfg.led_r;
        reference::state.LedGreen=cfg.led_g;
        reference::state.LedBlue=cfg.led_b;
        compare();
        for(unsigned n=0;n<1000;n++) {
            uint8_t data[63];
            for(auto &v:data) { random=random*1664525+1013904223; v=random>>24; }
            data[8]&=7; data[41]%=3; data[42]&=7;
            state_mgr_update(data,47); reference::state_update(data,63);
            compare();
            assert(config_get()->headset_volume==reference::cfg.headset_volume);
            assert(config_get()->speaker_volume==reference::cfg.speaker_volume);
        }
        /* A live configuration update uses the same volume/gain/reduction
         * setters as DS5Dongle, without resetting trigger or light state. */
        state_mgr_apply_config(60,50,4,6);
        reference::set_volume(60,50); reference::set_gain(4); reference::set_trigger_reduce(6);
        compare();
        state_mgr_apply_config(40,30,0,0);
        reference::set_volume(40,30); compare();
        /* New session resets triggers instead of replaying the old game. */
        initialize(cfg); uint8_t fresh[47]; state_mgr_get(fresh,47);
        for(unsigned i=10;i<32;i++) assert(fresh[i]==0);
    }
    check_host_frames();
    puts("PASS: reference initialization/4000 state updates, 0x32 framing/CRC, session reset, host field merge and final release under burst traffic");
}
'''
# The only initial-state caller is the connection primer callback.
assert main.count('send_led_primer(session);') == 1
assert 'primer_resend_us' not in main and 'stealth_primer_countdown' not in main
with tempfile.TemporaryDirectory(prefix='ds5-state-') as directory:
    path=Path(directory)
    (path/'bflb_irq.h').write_text('#include <stdint.h>\nstatic inline uintptr_t bflb_irq_save(void) { return 0; }\nstatic inline void bflb_irq_restore(uintptr_t x) { (void)x; }\n')
    (path/'production.c').write_text(production)
    (path/'reference.cpp').write_text(reference)
    flags=['-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-I'+str(ROOT/'src'),'-I'+str(path)]
    objects=[]
    for file in (path/'production.c', ROOT/'src/state_mgr.c', ROOT/'src/ds5_protocol.c'):
        obj=path/(file.stem+'.o'); objects.append(str(obj))
        subprocess.run(['cc','-std=c11',*flags,'-c',str(file),'-o',str(obj)],check=True)
    binary=path/'test'
    subprocess.run(['c++','-std=c++20',*flags,'-Wno-missing-field-initializers',str(path/'reference.cpp'),*objects,'-o',str(binary)],check=True)
    subprocess.run([str(binary)],check=True)

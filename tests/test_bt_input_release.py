"""Test actual main.c link-loss callbacks and USB report-consumption ordering.

python3 tests/test_bt_input_release.py
No firmware hardware or SDK required. USB endpoint retry behavior is covered by
test_usb_input_fresh_frames.py; this covers publishing neutral before ACL cleanup.
"""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'src/main.c').read_text()


def function(signature):
    start = source.index(signature)
    return source[start:source.index('\n}\n', start) + 3]


harness = r'''
#include "bt_hid_host.h"
#include "ds5_protocol.h"
#include "usb_gamepad.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#define LOG_INF(...) ((void)0)
typedef unsigned TickType_t;
typedef int QueueHandle_t;
typedef int BaseType_t;
#define pdTRUE 1
static unsigned critical_depth;
#define taskENTER_CRITICAL() (++critical_depth)
#define taskEXIT_CRITICAL() do { assert(critical_depth); --critical_depth; } while (0)
'''
start = source.index('static QueueHandle_t input_queue;')
harness += source[start:source.index('/* Apply dongle config', start)]
harness += r'''
static bool queued, disconnect_during_copy, switching, suspended;
static bool game_was_active_at_disconnect;
static uint8_t queue_data[78], host_report[63];
static unsigned publications, scans, remap_resets;
static uint64_t now=2000000;
enum { LED_RED_BLINK, LED_PURPLE_BLINK_SLOW, LED_PURPLE_BLINK_FAST, LED_GREEN_SOLID };
struct config_body { uint8_t dse_detected; };
static struct config_body config;
static struct config_body *config_get(void) { return &config; }
static uint8_t config_controller_mode(void) { return 0; }
static uint8_t config_dse_detected(void) { return 0; }
static bool config_usb_stealth(void) { return false; }
bool bt_hid_host_is_dse(void) { return false; }
bool bt_hid_host_is_switching(void) { return switching; }
int bt_hid_host_scan_start(uint8_t d) { (void)d; scans++; return 0; }
static bool usb_wake_host_suspended(void) { return suspended; }
static uint64_t bflb_mtimer_get_time_us(void) { return now; }
static TickType_t xTaskGetTickCount(void) { return now/1000; }
static void led_status_set(int s) { (void)s; }
static void usb_wake_on_bt_disconnect(void) {}
static void usb_wake_on_bt_connect(void) {}
static void dse_reset(void) {}
static void audio_reset(void) {}
static void remap_on_disconnect(void) { remap_resets++; }
static void send_led_primer(void) {}
static bool usb_audio_is_active(void) { return false; }
static void state_mgr_set_spk_active(bool b) { (void)b; }
void usb_soft_connect(void) {}
static void audio_mic_feed(const uint8_t *b, uint16_t n) { (void)b; (void)n; }
static void xQueueReset(QueueHandle_t q) { (void)q; queued=false; }
static void xQueueOverwrite(QueueHandle_t q, const uint8_t *b) {
    (void)q; assert(critical_depth); memcpy(queue_data,b,78); queued=true;
}
static BaseType_t xQueueReceive(QueueHandle_t q, uint8_t *b, TickType_t wait) {
    (void)q; (void)wait;
    if (!queued) return 0;
    memcpy(b,queue_data,78); queued=false; return pdTRUE;
}
int usb_gamepad_send_raw_input(const uint8_t *b) {
    publications++; memcpy(host_report,b,63); return 0;
}
static void on_hid_state(enum bt_hid_host_state);
static void *copy_with_disconnect(void *dst, const void *src, size_t n) {
    if (n==78 && disconnect_during_copy) {
        disconnect_during_copy=false;
        on_hid_state(BT_HID_STATE_DISCONNECTING);
    }
    return memcpy(dst,src,n);
}
#define memcpy copy_with_disconnect
'''
for signature in ('static void on_hid_input(', 'static bool process_input_release(',
                  'static void on_hid_state('):
    harness += function(signature)
harness += '\n#undef memcpy\n'
start = source.index('        BaseType_t input_ready = xQueueReceive(')
end = source.index('            last_input_us =', start)
harness += 'static bool receive_input(uint8_t *raw_report) {\nTickType_t wait=10;\n'
harness += source[start:end] + 'return true;\n}\nreturn false;\n}\n'
# The USB task also flushes any loss that arrives during a yielding input action.
assert 'process_input_release();\n        if (!ds5_connected)' in source
harness += r'''
static void setup(void) {
    ds5_connected=false; ever_connected=false; input_release_pending=false;
    queued=disconnect_during_copy=switching=suspended=scan_after_disconnect=false;
    critical_depth=publications=scans=remap_resets=0;
    memset(host_report,0x55,sizeof(host_report));
    on_hid_state(BT_HID_STATE_CONNECTED);
    assert(ds5_connected);
}
static void expect_neutral(void) {
    for (unsigned i=0;i<63;i++)
        assert(host_report[i]==(i<4 ? 0x7f : i==7 ? 8 : 0));
}
int main(void) {
    uint8_t report[78]={0x31,0}, readback[78];
    memset(report+2,0xff,63); /* Held directions, buttons, sticks and triggers. */

    /* A queued pressed report must be discarded even when dequeued before the
     * release task runs. SDK resources can remain alive for the full timeout. */
    setup(); on_hid_input(report,sizeof(report)); assert(queued);
    on_hid_state(BT_HID_STATE_DISCONNECTING);
    assert(!ds5_connected && input_release_pending && !publications);
    assert(!receive_input(readback)); expect_neutral();
    assert(publications==1 && !queued && remap_resets==1);
    now+=12000000; assert(!process_input_release());
    on_hid_state(BT_HID_STATE_IDLE); assert(!process_input_release() && publications==1);

    /* No queued data / no further BT input is required to emit neutral. */
    setup(); on_hid_state(BT_HID_STATE_DISCONNECTING);
    assert(!receive_input(readback)); expect_neutral();
    on_hid_input(report,sizeof(report)); assert(!queued);

    /* A watchdog may interrupt the RX callback halfway through its copy. */
    setup(); disconnect_during_copy=true; on_hid_input(report,sizeof(report));
    assert(!queued && input_release_pending && !ds5_connected);
    assert(!receive_input(readback)); expect_neutral();

    /* Reconnect before the USB task wakes must not erase a pending release. */
    setup(); on_hid_state(BT_HID_STATE_DISCONNECTING);
    on_hid_state(BT_HID_STATE_IDLE); on_hid_state(BT_HID_STATE_CONNECTED);
    assert(ds5_connected && input_release_pending);
    on_hid_input(report,sizeof(report)); assert(!receive_input(readback)); expect_neutral();
    on_hid_input(report,sizeof(report)); assert(receive_input(readback));
    assert(!memcmp(readback,report,sizeof(report)));

    /* If an input action yields and link loss arrives mid-report, finishing
     * that report is followed by neutral in the same USB iteration. */
    setup(); on_hid_input(report,sizeof(report)); assert(receive_input(readback));
    on_hid_state(BT_HID_STATE_DISCONNECTING);
    usb_gamepad_send_raw_input(readback+2); assert(process_input_release());
    expect_neutral(); assert(publications==2 && !queued);

    /* User-requested inquiry waits for actual SDK cleanup, not logical loss. */
    setup(); scan_after_disconnect=true;
    on_hid_state(BT_HID_STATE_DISCONNECTING);
    assert(scan_after_disconnect && !scans);
    on_hid_state(BT_HID_STATE_IDLE); assert(!scan_after_disconnect && scans==1);
    assert(!critical_depth);
    puts("PASS: immediate logical loss, neutral without ACL cleanup, stale reports, mid-copy loss, quick reconnect and deferred scan");
    return 0;
}
'''
with tempfile.TemporaryDirectory(prefix='ds5-input-release-') as directory:
    tmp = Path(directory)
    (tmp / 'test.c').write_text(harness)
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-function', '-Wno-unused-parameter', '-Wno-unused-variable',
                    '-fsanitize=address,undefined', '-I', str(root / 'src'),
                    str(tmp / 'test.c'), '-o', str(tmp / 'test')], check=True)
    subprocess.run([str(tmp / 'test')], check=True)

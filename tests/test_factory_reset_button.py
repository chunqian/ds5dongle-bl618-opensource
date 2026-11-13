"""Exercise the production BOOT gesture FSM and competing USB reset branch.

Run: python3 tests/test_factory_reset_button.py
"""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
main = (root / 'src/main.c').read_text()
usb = (root / 'src/usb_gamepad.c').read_text()
start = main.index('    enum { BTN_IDLE,')
state = main[start:main.index('    uint64_t btn_last_poll_us', start)]
start = main.index('            switch (btn_fsm)')
end = main.index('\n        }\n\n        /* Wait for button release;', start)
fsm = main[start:end]
start = usb.index('    if (usb_reset_pending')
reset = usb[start:usb.index('    if (usb_config_save_pending)', start)]

harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include "led_status.h"
#include "factory_reset.h"
#define BOOT_HOLD_TICKS 30
#define BOOT_CLICK_WINDOW 5
#define LOG_DBG(...) ((void)0)
#define LOG_INF(...) ((void)0)
#define LOG_ERR(...) ((void)0)
#define pdMS_TO_TICKS(v) (v)
static bool pending, ds5_connected, conn_led_off;
static uint64_t conn_led_start_us;
static enum factory_reset_request_result request_result = FACTORY_RESET_REQUEST_ARMED;
static bool usb_reset_pending, arm_during_delay;
static unsigned requests, resets, switches, scans, disconnects, saves;
static unsigned idle_ticks, scan_fail_count, scan_retry_ticks, bonded_count = 2;
static enum led_pattern led;
enum factory_reset_request_result factory_reset_request(void) {
    ++requests;
    pending = request_result != FACTORY_RESET_REQUEST_CANCELLED;
    return request_result;
}
bool factory_reset_is_pending(void) { return pending; }
void led_status_set(enum led_pattern p) { led = p; }
static int bt_hid_host_get_bonded_count(void) { return bonded_count; }
static int bt_hid_host_switch_next(void) { ++switches; return 1; }
static void bt_hid_host_request_pairing(void) { ++scans; }
static uint64_t bflb_mtimer_get_time_us(void) { return 1234567; }
static void GLB_SW_System_Reset(void) { ++resets; }
static void config_save(void) { ++saves; }
static void vTaskDelay(unsigned ms) {
    assert(ms == 100 || ms == 300);
    if (arm_during_delay) { pending = true; arm_during_delay = false; }
}
'''
harness += state + '\nstatic void poll(bool pressed) {\n' + fsm + '\n}\n'
harness += '\nstatic void usb_reset(void) {\n' + reset + '\n}\n'
harness += r'''
static void setup(void) {
    pending = ds5_connected = usb_reset_pending = arm_during_delay = false;
    conn_led_off = true;
    conn_led_start_us = 0;
    bonded_count = 2;
    request_result = FACTORY_RESET_REQUEST_ARMED;
    requests = resets = switches = scans = disconnects = saves = 0;
    btn_fsm = BTN_IDLE;
    btn_press_samples = btn_wait_samples = btn_click_count = 0;
}
static void ticks(bool pressed, unsigned n) { while (n--) poll(pressed); }
int main(void) {
    setup(); ticks(true, 29); assert(!requests && !resets);
    poll(true); assert(requests == 1 && pending && led == LED_BLINK_TRIPLE && !resets);
    ticks(true, 60); assert(requests == 1 && !resets); /* Holding never reboots into BOOT ROM. */
    poll(false); assert(resets == 1 && !switches && !scans && !disconnects);

    setup(); ticks(true, 29); ticks(false, 6); /* Releasing early remains a click. */
    assert(!requests && !resets && switches == 1);
    setup(); poll(true); poll(false); poll(true); ticks(false, 6);
    assert(!requests && !resets && !switches && scans == 1);
    setup(); ds5_connected = true;
    poll(true); poll(false); poll(true); ticks(false, 6);
    assert(scans == 1 && !disconnects && !requests);
    for (unsigned bonds = 0; bonds <= 1; ++bonds) {
        for (unsigned connected = 0; connected <= 1; ++connected) {
            setup(); bonded_count = bonds; ds5_connected = connected;
            poll(true); ticks(false, 6);
            assert(scans == 1 && !switches && !disconnects);
            assert(!conn_led_off && conn_led_start_us == 1234567);
            assert(led == LED_PURPLE_BLINK_FAST);
        }
    }

    setup(); request_result = FACTORY_RESET_REQUEST_CANCELLED;
    ticks(true, 30); ticks(true, 60); poll(false);
    assert(requests == 1 && !pending && !resets);
    request_result = FACTORY_RESET_REQUEST_ARMED; ticks(true, 30); poll(false);
    assert(requests == 2 && resets == 1);

    setup(); request_result = FACTORY_RESET_REQUEST_UNCERTAIN; ticks(true, 30);
    assert(pending && led == LED_BLINK_TRIPLE && !resets);
    ticks(true, 60); assert(requests == 1 && !resets);
    usb_reset_pending = true; usb_reset(); assert(!resets && !saves);
    poll(false); assert(resets == 1 && !switches && !scans);

    setup(); usb_reset_pending = true; usb_reset(); assert(resets == 1 && saves == 1);
    setup(); usb_reset_pending = pending = true; usb_reset();
    assert(!resets && !saves && usb_reset_pending);
    setup(); usb_reset_pending = arm_during_delay = true; usb_reset();
    assert(!resets && saves == 1 && pending);
    puts("PASS: BOOT threshold, release-only reboot, single/double click, cancelled retry, uncertain-commit reboot, competing USB reset");
}
'''
with tempfile.TemporaryDirectory(prefix='ds5-reset-button-') as directory:
    tmp = Path(directory)
    (tmp / 'test.c').write_text(harness)
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-variable', '-fsanitize=address,undefined',
                    '-I', str(root / 'src'), str(tmp / 'test.c'),
                    '-o', str(tmp / 'test')], check=True)
    subprocess.run([str(tmp / 'test')], check=True)

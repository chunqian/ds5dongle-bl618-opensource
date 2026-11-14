"""Exercise BT-driven USB presentation using the production state callback."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def extract(source, signature):
    start = source.index(signature)
    end = source.index("{", start) + 1
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define LOG_INF(...) ((void)0)
#define DS5_USB_INPUT_PAYLOAD_LEN 63
#define DS5_DPAD_NONE 8
enum { LED_RED_BLINK, LED_PURPLE_BLINK_SLOW, LED_PURPLE_BLINK_FAST, LED_GREEN_SOLID };
static int input_queue, output_queue;
static void xQueueReset(int queue) { (void)queue; }
static uint64_t handshake_start_us, connecting_start_tick;
static uint64_t conn_led_start_us;
static unsigned usb_fwd_count;
static uint8_t cached_battery_level, cached_battery_state;
static bool ds5_connected, battery_low, first_input_logged;
static bool bt_disconnect_pending_usb, conn_led_off, scan_after_disconnect;
static bool wake_enabled, suspended, visible;
static unsigned visibility_calls, wake_connects, wake_disconnects, neutral_reports;
static struct { uint8_t dse_detected; } config;
static uint64_t bflb_mtimer_get_time_us(void) { return 10000000; }
static unsigned xTaskGetTickCount(void) { return 100; }
static void usb_gamepad_send_raw_input(const uint8_t *data) {
    assert(data[0]==0x7f && data[3]==0x7f && data[7]==DS5_DPAD_NONE);
    ++neutral_reports;
}
static void usb_wake_on_bt_disconnect(void) { ++wake_disconnects; }
static void usb_wake_on_bt_connect(void) { ++wake_connects; }
static bool config_wake_enabled(void) { return wake_enabled; }
static bool usb_wake_host_suspended(void) { return suspended; }
static bool usb_gamepad_set_bt_visible(bool requested) {
    bool changed=visible!=requested; visible=requested; ++visibility_calls; return changed;
}
static void dse_reset(void) {}
static void audio_reset(void) {}
static void remap_on_disconnect(void) {}
static void led_status_set(unsigned value) { (void)value; }
static void bt_hid_host_scan_start(unsigned value) { (void)value; }
static void usb_gamepad_set_dse_mode(bool value) { (void)value; }
static void usb_gamepad_request_config_save(void) {}
static bool bt_hid_host_is_dse(void) { return true; }
static uint8_t config_controller_mode(void) { return 2; }
static uint8_t config_dse_detected(void) { return config.dse_detected; }
#define config_get() (&config)
'''
harness += extract((ROOT / "src/bt_hid_host.h").read_text(), "enum bt_hid_host_state {") + ";\n"
harness += extract((ROOT / "src/main.c").read_text(), "static void on_hid_state(")
harness += r'''
static void setup(void) {
    ds5_connected=bt_disconnect_pending_usb=suspended=wake_enabled=false;
    visible=true;
    visibility_calls=wake_connects=wake_disconnects=neutral_reports=0;
}
int main(void) {
    setup();
    on_hid_state(BT_HID_STATE_SCANNING);
    on_hid_state(BT_HID_STATE_IDLE);
    on_hid_state(BT_HID_STATE_CONNECTING);
    on_hid_state(BT_HID_STATE_IDLE);
    assert(visible && !visibility_calls); /* No ACL: keep configuration access. */

    for (unsigned connected=0; connected<2; ++connected)
    for (unsigned wake=0; wake<2; ++wake)
    for (unsigned asleep=0; asleep<2; ++asleep) {
        setup(); ds5_connected=connected; wake_enabled=wake; suspended=asleep;
        on_hid_state(BT_HID_STATE_DISCONNECTING);
        on_hid_state(BT_HID_STATE_IDLE);
        assert(visible==(wake || asleep));
        assert(visibility_calls==(!wake && !asleep));
        assert(!bt_disconnect_pending_usb && !ds5_connected);
        assert(neutral_reports==connected && wake_disconnects==1);
        unsigned calls=visibility_calls;
        on_hid_state(BT_HID_STATE_IDLE);
        assert(visibility_calls==calls); /* Consumed teardown is not replayed. */
    }

    for (unsigned hidden=0; hidden<2; ++hidden)
    for (unsigned asleep=0; asleep<2; ++asleep) {
        setup(); visible=!hidden; suspended=asleep;
        on_hid_state(BT_HID_STATE_CONNECTED);
        assert(visible==(!hidden || !asleep));
        assert(visibility_calls==!asleep);
        assert(ds5_connected && wake_connects==1);
    }
    puts("PASS: immediate USB hide after ACL teardown, wake/suspend exceptions, reconnect, startup errors");
}
'''
with tempfile.TemporaryDirectory(prefix="ds5-usb-bt-") as directory:
    path = Path(directory)
    (path / "test.c").write_text(harness)
    subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(path / "test.c"),
                    "-o", str(path / "test")], check=True)
    subprocess.run([str(path / "test")], check=True)

"""Fault-inject the production USB reset handler and deferred reconnect path.

Run: python3 tests/test_usb_audio_reconnect.py [firmware_source_directory]
Hardware/RTOS calls are doubles; state transitions and task code are production.
"""
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
source = (root / "src/usb_gamepad.c").read_text()
start = source.index("void usb_gamepad_process_deferred(void)")
end = source.index("    if (usb_reset_pending)", start)
task = source[start:end] + "}\n"


def event_body(name):
    start = source.index("    case " + name + ":")
    end = source.index("        break;", start)
    return source[source.index("\n", start) + 1:end]


harness = r"""
#include "usb_audio_control.h"
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define taskENTER_CRITICAL() ((void)0)
#define taskEXIT_CRITICAL() ((void)0)
#define pdMS_TO_TICKS(x) (x)
#define LOG_INF(...) ((void)0)
static struct usb_audio_control audio_control;
static bool usb_configured, ep_in_busy, kbd_ep_busy, pending_active;
static bool first_usb_send_logged, detached;
static unsigned profile, profile_changes, connects, inject;
static uint64_t now_us;
static uint64_t bflb_mtimer_get_time_us(void) { return now_us; }
static void usb_soft_disconnect(void) { detached = true; }
static void usb_soft_connect(void) { assert(detached); detached = false; connects++; }
static void usb_audio_stop(void) {}
static void usb_audio_mic_stop(void) {}
static void audio_set_mic_active(bool enabled) { (void)enabled; }
static void audio_reset_encoder(void) {}
static void apply_audio_profile(void) {
    unsigned next = audio_control.visible ? 3 : 0;
    if (next != profile) {
        assert(detached && "descriptor layout must only change while detached");
        profile = next;
        profile_changes++;
    }
}
"""
harness += "static void bus_reset(void) {\n" + event_body("USBD_EVENT_RESET") + "}\n"
harness += "static void physical_disconnect(void) {\n" + event_body("USBD_EVENT_DISCONNECTED") + "}\n"
harness += r"""
static void vTaskDelay(unsigned ms) {
    assert(detached && ms == 250);
    now_us += ms * 1000;
    unsigned event = inject;
    inject = 0;
    if (event == 1) bus_reset();
    if (event == 2) physical_disconnect();
}
""" + task + r"""
static void setup(void) {
    memset(&audio_control, 0, sizeof(audio_control));
    usb_configured = true;
    ep_in_busy = kbd_ep_busy = pending_active = first_usb_send_logged = false;
    detached = false;
    profile = profile_changes = connects = inject = 0;
    now_us = 0;
}
static void request(bool enabled) {
    uint8_t cmd[] = {4,'D','A','U','D',1,enabled};
    assert(usb_audio_control_command(&audio_control, cmd, sizeof(cmd), now_us));
}
static void configured(void) {
    usb_audio_control_configured(&audio_control);
    usb_configured = true;
}
static void enable(void) {
    request(true);
    now_us += 100001;
    usb_gamepad_process_deferred();
    assert(profile == 3 && audio_control.visible && !detached);
    bus_reset(); /* The reset caused by our own reconnect retains the grant. */
    assert(profile == 3 && audio_control.visible && !detached);
    configured();
}
int main(void) {
    /* No command: no reconnect and no audio, even if time advances. */
    setup(); now_us = 10000000;
    usb_gamepad_process_deferred();
    assert(!audio_control.visible && !connects);

    setup(); enable();
    unsigned before = profile_changes;
    bus_reset(); /* Same-host bus reset; no OS device-match callback assumed. */
    assert(profile == 3 && profile_changes == before);
    assert(audio_control.visible && detached);
    /* Must recover even if the host never sends SET_CONFIGURATION again. */
    now_us += 100001;
    usb_gamepad_process_deferred();
    assert(profile == 0 && !audio_control.visible && !detached);
    assert(connects == 2);
    bus_reset(); configured();
    enable(); /* CR can authorize the newly discovered HID-only device. */
    assert(connects == 3);
    request(true); now_us += 100001;
    usb_gamepad_process_deferred();
    assert(connects == 3); /* duplicate command cannot create a reconnect loop */
    request(false); now_us += 100001;
    usb_gamepad_process_deferred();
    assert(profile == 0 && !audio_control.visible && connects == 4);

    for (unsigned event = 1; event <= 2; event++) {
        setup(); request(true); now_us += 100001;
        inject = event; /* invalidate the request during the detached delay */
        usb_gamepad_process_deferred();
        assert(!audio_control.visible && !audio_control.requested);
        assert(!audio_control.grant_until_us && profile == 0 && !detached);
        bus_reset(); configured(); enable();
    }
    puts("PASS: actual reset/task paths, reset/unplug during wait, stable layout, recovery, no reconnect loop");
}
"""
with tempfile.TemporaryDirectory(prefix="ds5-usb-reconnect-") as directory:
    path = Path(directory)
    (path / "test.c").write_text(harness)
    subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-I", str(root / "src"),
                    str(path / "test.c"), str(root / "src/usb_audio_control.c"),
                    "-o", str(path / "test")], check=True)
    subprocess.run([str(path / "test")], check=True)

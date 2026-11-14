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
end = source.index("    deferred_store_process();", start)
task = source[start:end] + "}\n"


def event_body(name):
    start = source.index("    case " + name + ":")
    end = source.index("        break;", start)
    return source[source.index("\n", start) + 1:end]


def extract(signature):
    start = source.index(signature)
    end = source.index("{", start) + 1
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


harness = r"""
#include "usb_audio_control.h"
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define taskENTER_CRITICAL() enter_critical()
#define taskEXIT_CRITICAL() exit_critical()
#define pdMS_TO_TICKS(x) (x)
#define LOG_INF(...) ((void)0)
static struct usb_audio_control audio_control;
static bool usb_configured, ep_in_busy, pending_valid, pending_active;
static bool first_usb_send_logged, usb_bt_hidden, usb_reenumerating;
static bool usb_profile_pending, host_asleep, profile_restored;
static uint64_t usb_profile_request_us;
static bool usb_profile_matches_config(void) { return !usb_profile_pending || profile_restored; }
static bool usb_wake_host_suspended(void) { return host_asleep; }
static void request_usb_profile(void) { usb_profile_pending=true; }
static uint32_t usb_regs[0x118/4];
#define USB_BASE ((uintptr_t)usb_regs)
#define detached (usb_regs[0x114/4] & 1u)
static unsigned suspends;
static void note_suspend(void) { ++suspends; }
static void (*hook_suspend)(void) = note_suspend;
static unsigned profile, profile_changes, connects, inject;
static unsigned critical_depth, unprotected_time_samples;
static bool inject_command_on_enter;
static uint64_t now_us;
static void enter_critical(void) {
    /* An IRQ may complete immediately before task interrupts are masked. */
    if (!critical_depth && inject_command_on_enter) {
        inject_command_on_enter = false;
        now_us++;
        const uint8_t cmd[] = {4,'D','A','U','D',1,1};
        assert(usb_audio_control_command(&audio_control, cmd, sizeof(cmd), now_us));
    }
    critical_depth++;
}
static void exit_critical(void) { assert(critical_depth); critical_depth--; }
static uint64_t bflb_mtimer_get_time_us(void) {
    if (!critical_depth) unprotected_time_samples++;
    return now_us;
}

static void usb_host_session_end(void) {} /* Exercised with real state in test_output_lifecycle.py. */
static void usb_wake_note_usb_reconnect(void) {}
static void usb_audio_stop(void) {}
static void usb_audio_suspend(void) {}
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
harness += "static void apply_usb_profile(void) { apply_audio_profile(); }\n"
harness += extract("void usb_soft_disconnect(") + "\n"
harness += extract("void usb_soft_connect(").replace("void usb_soft_connect", "static void actual_usb_soft_connect") + "\n"
harness += "static void usb_soft_connect(void) { bool was_detached=detached; actual_usb_soft_connect(); if(was_detached && !detached) ++connects; }\n"
harness += extract("bool usb_gamepad_set_bt_visible(") + "\n"
harness += "static void suspend(void) {\n" + event_body("USBD_EVENT_SUSPEND") + "}\n"
harness += "static void bus_reset(void) {\n" + event_body("USBD_EVENT_RESET") + "}\n"
harness += "static void physical_disconnect(void) {\n" + event_body("USBD_EVENT_DISCONNECTED") + "}\n"
harness += r"""
static void vTaskDelay(unsigned ms) {
    assert(!critical_depth);
    assert(detached && ms == 250);
    now_us += ms * 1000;
    unsigned event = inject;
    inject = 0;
    if (event == 1) bus_reset();
    if (event == 2) physical_disconnect();
    if (event == 3) { usb_gamepad_set_bt_visible(false); suspend(); }
    if (event == 4) {
        usb_gamepad_set_bt_visible(false); usb_gamepad_set_bt_visible(true);
        assert(detached); /* BT may not shorten the audio descriptor detach. */
    }
}
""" + task + r"""
static void setup(void) {
    memset(&audio_control, 0, sizeof(audio_control));
    usb_configured = true;
    ep_in_busy = pending_valid = pending_active = first_usb_send_logged = false;
    memset(usb_regs,0,sizeof(usb_regs));
    usb_bt_hidden=usb_reenumerating=false; suspends=0;
    usb_profile_pending=host_asleep=profile_restored=false; usb_profile_request_us=0;
    profile = profile_changes = connects = inject = 0;
    critical_depth = unprotected_time_samples = 0;
    inject_command_on_enter = false;
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
    pending_valid = pending_active = true; /* Cached input from the old profile. */
    request(true);
    now_us += 100001;
    usb_gamepad_process_deferred();
    assert(profile == 3 && audio_control.visible && !detached);
    assert(!pending_valid && !pending_active);
    bus_reset(); /* The reset caused by our own reconnect retains the grant. */
    assert(profile == 3 && audio_control.visible && !detached);
    configured();
}
int main(void) {
    /* Cancelled profile changes must release a BT reattach held at the gate. */
    setup(); usb_gamepad_set_bt_visible(false); usb_profile_pending=true;
    usb_gamepad_set_bt_visible(true); assert(detached);
    profile_restored=true; usb_gamepad_process_deferred();
    assert(!detached && !usb_profile_pending && connects==1);

    /* No command: no reconnect and no audio, even if time advances. */
    setup(); now_us = 10000000;
    usb_gamepad_process_deferred();
    assert(!audio_control.visible && !connects);

    /* Fresh SET_REPORT between the old clock-sample position and IRQ masking
     * must still receive the complete 100ms grace, including its last us. */
    setup(); now_us = 1000000; inject_command_on_enter = true;
    usb_gamepad_process_deferred();
    assert(!connects && !audio_control.visible && audio_control.pending);
    assert(!unprotected_time_samples);
    now_us += 99999;
    usb_gamepad_process_deferred();
    assert(!connects && !audio_control.visible);
    now_us++;
    usb_gamepad_process_deferred();
    assert(connects == 1 && audio_control.visible && !critical_depth);

    setup(); enable();
    unsigned before = profile_changes;
    bus_reset(); /* Same-host bus reset; no OS device-match callback assumed. */
    assert(profile == 3 && profile_changes == before);
    assert(audio_control.visible && detached);
    suspend(); assert(!suspends);
    usb_gamepad_set_bt_visible(true);
    assert(detached); /* BT cannot bypass descriptor revocation after reset. */
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
    setup(); request(true); now_us+=100001; inject=3;
    usb_gamepad_process_deferred();
    assert(detached && usb_bt_hidden && !connects && !suspends);
    now_us+=10000000; usb_gamepad_process_deferred();
    assert(detached && !connects); /* Grant expiry also respects BT visibility. */
    assert(usb_gamepad_set_bt_visible(true));
    assert(!detached && connects==1);
    assert(!usb_gamepad_set_bt_visible(true) && connects==1);
    suspend(); assert(suspends==1); /* Genuine host suspend is still delivered. */

    setup(); request(true); now_us+=100001; inject=4;
    usb_gamepad_process_deferred();
    assert(!detached && connects==1 && audio_control.visible);
    setup(); usb_profile_pending=true; host_asleep=true; now_us=100001;
    usb_gamepad_process_deferred(); assert(!detached && usb_profile_pending);
    host_asleep=false; usb_gamepad_process_deferred(); assert(!usb_profile_pending);
    /* Expiration wins over a coincident profile reconnect. */
    setup(); request(true); now_us=100001; usb_gamepad_process_deferred();
    usb_profile_pending=true; now_us=audio_control.grant_until_us;
    usb_gamepad_process_deferred(); assert(!audio_control.visible);
    puts("PASS: BT visibility, suspend filtering, descriptor detach interval, atomic time/request snapshot, full command grace, reset/unplug recovery, no reconnect loop");
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

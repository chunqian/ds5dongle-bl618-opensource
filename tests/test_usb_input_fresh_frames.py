"""Run the production input sender with endpoint/IRQ fault injection.

python3 tests/test_usb_input_fresh_frames.py [path/to/usb_gamepad.c]
No firmware hardware or SDK is needed; this compiles the actual C functions.
"""
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
source = (Path(sys.argv[1]) if len(sys.argv) > 1 else root / 'src/usb_gamepad.c').read_text()


def function(signature):
    start = source.index(signature)
    return source[start:source.index('\n}\n', start) + 3]


def event(name):
    start = source.index('    case ' + name + ':')
    return source[source.index('\n', start) + 1:source.index('        break;', start)]


harness = r'''
#include "usb_audio_control.h"
#include "config.h"
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define ATTR_TCM_SECTION
#define DS5_USB_INPUT_PAYLOAD_LEN 63
#define DS5_USB_REPORT_ID_INPUT 1
#define USB_GAMEPAD_EP_IN 0x84
#define LOG_INF(...) ((void)0)
static uint8_t usb_in_buf[64];
static volatile uint8_t pending_payload[63];
static volatile bool pending_valid, pending_active, usb_configured, ep_in_busy;
static struct config_body test_config;
struct config_body *config_get(void) { return &test_config; }
static bool first_usb_send_logged, kbd_ep_busy;
static struct usb_audio_control audio_control;
static void (*hook_configured)(void), (*hook_resume)(void), (*hook_suspend)(void);
static uintptr_t masked;
static bool completion_waiting, inject_during_copy;
static unsigned queued, attempts, failures;
static uint8_t accepted[16][64];
static void hid_ep_in_handler(uint8_t, uint8_t, uint32_t);
static void complete(void) {
    assert(ep_in_busy);
    uintptr_t old = masked;
    masked = 1; /* ISR entry keeps the task from running. */
    hid_ep_in_handler(0, USB_GAMEPAD_EP_IN, 64);
    assert(masked == 1); /* Inner helpers must preserve the entry mask. */
    masked = old;
}
static uintptr_t bflb_irq_save(void) { uintptr_t old = masked; masked = 1; return old; }
static void bflb_irq_restore(uintptr_t old) {
    masked = old;
    if (!masked && completion_waiting) {
        completion_waiting = false;
        complete();
    }
}
static void *copy_with_interrupt(void *dst, const void *src, size_t n) {
    if (dst == (const void *)pending_payload && inject_during_copy) {
        inject_during_copy = false;
        memcpy(dst, src, n / 2);
        /* A real completion arriving halfway through publication must wait. */
        if (masked) completion_waiting = true; else complete();
        memcpy((uint8_t *)dst + n / 2, (const uint8_t *)src + n / 2, n - n / 2);
    } else memcpy(dst, src, n);
    return dst;
}
static int usbd_ep_start_write(uint8_t bus, uint8_t ep, const uint8_t *data, uint32_t size) {
    assert(bus == 0 && ep == USB_GAMEPAD_EP_IN && size == 64 && data[0] == 1);
    attempts++;
    if (failures) { failures--; return -2; }
    assert(queued < 16);
    memcpy(accepted[queued++], data, 64);
    return 0;
}
static uint64_t bflb_mtimer_get_time_us(void) { return 1000000; }
static void usb_soft_disconnect(void) {}
static void apply_audio_profile(void) {}
static void usb_audio_stop(void) {}
static void usb_audio_mic_stop(void) {}
static void audio_set_mic_active(bool enabled) { (void)enabled; }
static void audio_reset_encoder(void) {}
#define memcpy copy_with_interrupt
'''
for signature in ('static void try_send_pending(void)',
                  'static void hid_ep_in_handler(uint8_t',
                  'int usb_gamepad_send_raw_input(const uint8_t'):
    harness += function(signature)
for name in ('CONFIGURED', 'RESET', 'SUSPEND', 'RESUME', 'DISCONNECTED'):
    harness += '\nstatic void event_' + name + '(void) {\n' + event('USBD_EVENT_' + name) + '}\n'
harness += r'''
#undef memcpy
static void setup(void) {
    memset(&audio_control, 0, sizeof(audio_control));
    memset((void *)pending_payload, 0, sizeof(pending_payload));
    memset(usb_in_buf, 0, sizeof(usb_in_buf));
    pending_valid = pending_active = ep_in_busy = first_usb_send_logged = kbd_ep_busy = false;
    memset(&test_config, 0, sizeof(test_config));
    test_config.polling_rate_mode = 2;
    usb_configured = true;
    masked = 0;
    completion_waiting = inject_during_copy = false;
    queued = attempts = failures = 0;
}
static void expect_frame(unsigned index, const uint8_t *payload) {
    assert(index < queued && accepted[index][0] == 1);
    assert(!memcmp(accepted[index] + 1, payload, 63));
}
int main(void) {
    /* Keep the IRQ doubles referenced when regression-testing older source. */
    (void)bflb_irq_save; (void)bflb_irq_restore;
    uint8_t a[63], b[63], c[63], neutral[63] = {0};
    for (unsigned i = 0; i < 63; i++) { a[i] = i; b[i] = 255 - i; c[i] = i ^ 0x55; }
    memset(neutral, 127, 4); neutral[7] = 8;

    /* Completing a frame without new BT input must not start another transfer. */
    setup(); usb_gamepad_send_raw_input(a);
    assert(queued == 1 && !pending_active && ep_in_busy);
    complete(); try_send_pending();
    assert(queued == 1 && !ep_in_busy && !pending_active && !masked);

    /* A newly received report is fresh even when its payload is unchanged. */
    usb_gamepad_send_raw_input(a); complete();
    assert(queued == 2 && !ep_in_busy); expect_frame(0, a); expect_frame(1, a);

    /* Keep the DMA source intact while busy; send only the latest waiting frame. */
    setup(); usb_gamepad_send_raw_input(a);
    usb_gamepad_send_raw_input(b); usb_gamepad_send_raw_input(c);
    assert(queued == 1 && !memcmp(usb_in_buf + 1, a, 63));
    complete(); assert(queued == 2); expect_frame(1, c);
    complete(); assert(queued == 2 && !ep_in_busy);

    /* Simulate an IN completion precisely halfway through copying a new frame. */
    setup(); usb_gamepad_send_raw_input(a); inject_during_copy = true;
    usb_gamepad_send_raw_input(b);
    assert(queued == 2 && !completion_waiting && !masked);
    expect_frame(1, b); complete(); assert(queued == 2 && !ep_in_busy);

    /* Failed submissions retain the last report for a task retry. */
    setup(); failures = 1; usb_gamepad_send_raw_input(a);
    assert(!queued && pending_active && !ep_in_busy && !masked);
    try_send_pending(); assert(queued == 1 && attempts == 2); expect_frame(0, a);
    complete(); assert(queued == 1 && !ep_in_busy);

    /* A newer frame supersedes a failed/stale one before retry. */
    setup(); failures = 1; usb_gamepad_send_raw_input(a);
    usb_gamepad_send_raw_input(b); expect_frame(0, b); complete();
    assert(queued == 1);

    /* One neutral release on controller loss must still reach the host. */
    setup(); usb_gamepad_send_raw_input(a); usb_gamepad_send_raw_input(neutral);
    complete(); expect_frame(1, neutral); complete(); assert(queued == 2);

    /* Enumeration/resume flush one waiting frame, then become idle again. */
    setup(); usb_configured = false; usb_gamepad_send_raw_input(a);
    assert(!queued && pending_active); event_CONFIGURED(); expect_frame(0, a);
    complete(); event_SUSPEND(); usb_gamepad_send_raw_input(b);
    assert(queued == 1 && pending_active); event_RESUME(); expect_frame(1, b);
    complete(); assert(queued == 2 && !ep_in_busy);

    /* Bus reset and physical removal must not replay a previous session's frame. */
    setup(); usb_gamepad_send_raw_input(a); usb_gamepad_send_raw_input(b);
    event_RESET(); event_CONFIGURED(); assert(queued == 1 && !ep_in_busy);
    usb_gamepad_send_raw_input(c); expect_frame(1, c);
    usb_gamepad_send_raw_input(b); event_DISCONNECTED(); event_CONFIGURED();
    assert(queued == 2 && !ep_in_busy && !pending_active && !pending_valid && !masked);

    for (unsigned mode = 0; mode < 2; mode++) {
        /* Periodic modes never transmit an uninitialized cache, but keep
         * replaying the latest valid frame once one has been received. */
        setup(); test_config.polling_rate_mode = mode;
        try_send_pending(); assert(!attempts);
        usb_gamepad_send_raw_input(a);
        assert(queued == 1 && !pending_active);
        complete(); assert(queued == 2 && ep_in_busy);
        complete();
        assert(queued == 3 && ep_in_busy && !pending_active);
        expect_frame(1, a); expect_frame(2, a);

        /* Coalescing and IRQ-safe publication apply to periodic modes too. */
        usb_gamepad_send_raw_input(b); inject_during_copy = true;
        usb_gamepad_send_raw_input(c);
        assert(queued == 4 && !pending_active && !masked);
        expect_frame(3, c); complete(); expect_frame(4, c);

        /* Switching to real-time must stop replays after the in-flight
         * transfer, without inventing a new frame from the valid cache. */
        test_config.polling_rate_mode = 2; complete(); try_send_pending();
        assert(queued == 5 && !ep_in_busy && !pending_active);
        /* Switching back must resume with the cached frame even if BT is idle. */
        test_config.polling_rate_mode = mode; try_send_pending();
        assert(queued == 6 && ep_in_busy); expect_frame(5, c);

        /* A real unsent update must survive a mode change and go out once. */
        usb_gamepad_send_raw_input(b); test_config.polling_rate_mode = 2;
        complete(); assert(queued == 7); expect_frame(6, b);
        complete(); assert(queued == 7 && !ep_in_busy);

        /* A failed periodic replay can retry without new BT input. */
        setup(); test_config.polling_rate_mode = mode;
        usb_gamepad_send_raw_input(a); failures = 1; complete();
        assert(queued == 1 && !ep_in_busy && pending_valid && !pending_active);
        try_send_pending(); assert(queued == 2); expect_frame(1, a);
        failures = 1; complete();
        test_config.polling_rate_mode = 2; try_send_pending();
        assert(queued == 2 && !ep_in_busy); /* failed replay is not fresh */

        /* A failed first/new frame remains pending across mode changes. */
        setup(); test_config.polling_rate_mode = mode; failures = 1;
        usb_gamepad_send_raw_input(a); test_config.polling_rate_mode = 2;
        assert(pending_active && !ep_in_busy);
        try_send_pending(); expect_frame(0, a); complete();
        assert(queued == 1 && !ep_in_busy);

        /* Reset/removal must invalidate the periodic cache, not only dirty. */
        setup(); test_config.polling_rate_mode = mode;
        usb_gamepad_send_raw_input(a); event_RESET(); event_CONFIGURED();
        assert(queued == 1 && !pending_valid && !ep_in_busy);
        usb_gamepad_send_raw_input(b); event_DISCONNECTED(); event_CONFIGURED();
        assert(queued == 2 && !pending_valid && !ep_in_busy);
    }
    puts("PASS: real-time fresh-only, periodic replay, mode switches, unchanged fresh report, latest-wins, IRQ-interleaved copy, retry, neutral release, reset/resume/reconnect");
}
'''
with tempfile.TemporaryDirectory(prefix='ds5-fresh-input-') as directory:
    path = Path(directory)
    (path / 'test.c').write_text(harness)
    subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-I', str(root / 'src'),
                    str(root / 'src/usb_audio_control.c'), str(path / 'test.c'),
                    '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], check=True)

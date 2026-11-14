#include "usb_wake.h"
#include "usb_gamepad.h"
#include "bt_hid_host.h"
#include "config.h"
#include "usbd_core.h"
#include "bflb_mtimer.h"
#include "bflb_irq.h"
#include <string.h>
#include "debug_log.h"

#define WAKE_KEYCODE_F15 0x68
#define WAKE_SETTLE_US 150000ULL
#define WAKE_KEY_HOLD_US 80000ULL
#define WAKE_KEY_UP_SETTLE_US 200000ULL
#define WAKE_REQUEST_TIMEOUT_US 5000000ULL
#define WAKE_KEY_ATTEMPTS 2
#define WAKE_POWEROFF_DEBOUNCE_US 3000000ULL
#define WAKE_RECONNECT_GRACE_US 5000000ULL

typedef enum {
    WAKE_IDLE, WAKE_PENDING_PRESS, WAKE_REQUESTED,
    WAKE_KEY_DOWN, WAKE_KEY_UP_SENT, WAKE_DONE,
} wake_state_t;

/* USB callbacks run in interrupt context. Snapshot/update together, preserving
 * the caller's interrupt mask; never hold it across USB or Bluetooth calls. */
static bool host_suspended, host_resumed, poweroff_pending, key_down_on_host;
static wake_state_t state;
static uint64_t state_entered_us, suspend_at_us, reconnect_until_us;
static uint32_t wake_generation;
static uint8_t key_attempts, prev_b7 = 8, prev_b8, prev_b9;

static uint64_t now_us(void) { return bflb_mtimer_get_time_us(); }
static void enter_state(wake_state_t next)
{
    state = next;
    state_entered_us = now_us();
}

static void request_host_wake(void)
{
    if (!config_wake_enabled()) return;
    uintptr_t irq = bflb_irq_save();
    uint32_t generation = wake_generation;
    bflb_irq_restore(irq);
    int result = usbd_send_remote_wakeup(0);
    if (result && usb_wake_host_suspended()) {
        usbd_set_remote_wakeup(0);
        result = 0;
    }
    irq = bflb_irq_save();
    if (!result && generation == wake_generation)
        enter_state(WAKE_REQUESTED);
    bflb_irq_restore(irq);
}

void usb_wake_init(void)
{
    uintptr_t irq = bflb_irq_save();
    state = WAKE_IDLE;
    host_suspended = host_resumed = poweroff_pending = key_down_on_host = false;
    suspend_at_us = reconnect_until_us = state_entered_us = 0;
    key_attempts = prev_b8 = prev_b9 = 0;
    prev_b7 = 8;
    ++wake_generation;
    bflb_irq_restore(irq);
}

void usb_wake_note_usb_reconnect(void)
{
    uintptr_t irq = bflb_irq_save();
    reconnect_until_us = now_us() + WAKE_RECONNECT_GRACE_US;
    poweroff_pending = false;
    bflb_irq_restore(irq);
}

void usb_wake_on_suspend(void)
{
    uintptr_t irq = bflb_irq_save();
    uint64_t now = now_us();
    if (now < reconnect_until_us) {
        bflb_irq_restore(irq);
        return;
    }
    ++wake_generation;
    host_suspended = poweroff_pending = true;
    host_resumed = false;
    suspend_at_us = now;
    key_attempts = prev_b8 = prev_b9 = 0;
    prev_b7 = 8;
    enter_state(config_wake_enabled() ? WAKE_PENDING_PRESS : WAKE_IDLE);
    bflb_irq_restore(irq);
}

void usb_wake_on_resume(void)
{
    uintptr_t irq = bflb_irq_save();
    host_suspended = poweroff_pending = false;
    host_resumed = true;
    bflb_irq_restore(irq);
}

void usb_wake_on_configured(void)
{
    usb_wake_on_resume();
    uintptr_t irq = bflb_irq_save();
    reconnect_until_us = 0;
    bflb_irq_restore(irq);
}

void usb_wake_on_bt_connect(void)
{
    uintptr_t irq = bflb_irq_save();
    bool wake = host_suspended &&
        (state == WAKE_IDLE || state == WAKE_DONE || state == WAKE_PENDING_PRESS);
    bflb_irq_restore(irq);
    if (wake) request_host_wake();
}

void usb_wake_on_bt_disconnect(void)
{
    uintptr_t irq = bflb_irq_save();
    ++wake_generation;
    enter_state(WAKE_IDLE);
    prev_b7 = 8; prev_b8 = prev_b9 = key_attempts = 0;
    /* If a keydown already reached the host, the task still owes a release. */
    bflb_irq_restore(irq);
}

bool usb_wake_host_suspended(void)
{
    uintptr_t irq = bflb_irq_save();
    bool suspended = host_suspended;
    bflb_irq_restore(irq);
    return suspended;
}

void usb_wake_on_bt_input(const uint8_t *payload, uint16_t len)
{
    if (!config_wake_enabled() || len < 10) return;
    uintptr_t irq = bflb_irq_save();
    bool changed = payload[7] != prev_b7 || payload[8] != prev_b8 || payload[9] != prev_b9;
    bool armable = state == WAKE_IDLE || state == WAKE_DONE || state == WAKE_PENDING_PRESS;
    prev_b7 = payload[7]; prev_b8 = payload[8]; prev_b9 = payload[9];
    bflb_irq_restore(irq);
    /* Like DS5Dongle, let the USB stack check suspend even if a hub hid the
     * suspend notification. A rejected request does not send an F15. */
    if (changed && armable) request_host_wake();
}

void usb_wake_task(void)
{
    uintptr_t irq = bflb_irq_save();
    uint64_t now = now_us();
    bool power_off = poweroff_pending && host_suspended &&
                     now - suspend_at_us >= WAKE_POWEROFF_DEBOUNCE_US;
    if (power_off) poweroff_pending = false;
    wake_state_t current = state;
    uint64_t entered = state_entered_us;
    uint32_t generation = wake_generation;
    bool resumed = host_resumed || !host_suspended;
    bool release = key_down_on_host;
    uint8_t attempts = key_attempts;
    bflb_irq_restore(irq);

    if (power_off) {
        uint8_t payload[47] = {2};
        bt_hid_host_set_feature_crc(0x08, payload, sizeof(payload));
        /* Keep page scan available: a PS press can reconnect and wake USB. */
    }
    if (!config_wake_enabled() || current == WAKE_IDLE || current == WAKE_DONE) {
        if (release && usb_gamepad_kbd_ready()) {
            uint8_t up[8] = {0};
            if (usb_gamepad_send_kbd_report(up, sizeof(up)) == 0) {
                irq = bflb_irq_save(); key_down_on_host = false; bflb_irq_restore(irq);
            }
        }
        return;
    }
    bool down = false, send = false;
    wake_state_t next = current;
    if (current == WAKE_REQUESTED) {
        if (resumed && now - entered >= WAKE_SETTLE_US) {
            down = send = true; next = WAKE_KEY_DOWN;
        } else if (!resumed && now - entered > WAKE_REQUEST_TIMEOUT_US) {
            next = WAKE_DONE;
        }
    } else if (current == WAKE_KEY_DOWN && now - entered >= WAKE_KEY_HOLD_US) {
        send = true; next = WAKE_KEY_UP_SENT;
    } else if (current == WAKE_KEY_UP_SENT && now - entered >= WAKE_KEY_UP_SETTLE_US) {
        if (attempts < WAKE_KEY_ATTEMPTS) {
            down = send = true; next = WAKE_KEY_DOWN;
        } else next = WAKE_DONE;
    }
    if (send) {
        if (!usb_gamepad_kbd_ready()) return;
        uint8_t report[8] = {0};
        if (down) report[2] = WAKE_KEYCODE_F15;
        if (usb_gamepad_send_kbd_report(report, sizeof(report)) != 0) return;
    }
    irq = bflb_irq_save();
    if (send) key_down_on_host = down;
    if (generation == wake_generation && state == current && next != current) {
        if (send && down) ++key_attempts;
        enter_state(next);
    }
    bflb_irq_restore(irq);
}

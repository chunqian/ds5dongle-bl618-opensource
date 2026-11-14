#include "usb_wake.h"
#include "bt_hid_host.h"
#include "config.h"
#include "usbd_core.h"
#include "bflb_mtimer.h"
#include "bflb_irq.h"
#include <string.h>
#include "debug_log.h"

#define WAKE_REQUEST_TIMEOUT_US 5000000ULL
#define WAKE_POWEROFF_DEBOUNCE_US 3000000ULL
#define WAKE_RECONNECT_GRACE_US 5000000ULL

typedef enum {
    WAKE_IDLE, WAKE_PENDING_PRESS, WAKE_REQUESTED,
    WAKE_DONE,
} wake_state_t;

/* USB callbacks run in interrupt context. Snapshot/update together, preserving
 * the caller's interrupt mask; never hold it across USB or Bluetooth calls. */
static bool host_suspended, host_resumed, poweroff_pending;
static wake_state_t state;
static uint64_t state_entered_us, suspend_at_us, reconnect_until_us;
static uint32_t wake_generation, poweroff_session;
static uint64_t poweroff_retry_us;
static uint8_t prev_b7 = 8, prev_b8, prev_b9;

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
    host_suspended = host_resumed = poweroff_pending = false;
    suspend_at_us = reconnect_until_us = state_entered_us = 0;
    prev_b8 = prev_b9 = 0;
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
    poweroff_session = bt_hid_host_session();
    poweroff_retry_us = now;
    prev_b8 = prev_b9 = 0;
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
    poweroff_pending = false;
    enter_state(WAKE_IDLE);
    prev_b7 = 8; prev_b8 = prev_b9 = 0;
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
     * suspend notification. No keyboard reports are generated. */
    if (changed && armable) request_host_wake();
}

static bool poweroff_allowed(uint32_t generation)
{
    uintptr_t irq = bflb_irq_save();
    bool allowed = poweroff_pending && host_suspended &&
                   generation == wake_generation && poweroff_session == bt_hid_host_session();
    bflb_irq_restore(irq);
    return allowed;
}

void usb_wake_task(void)
{
    uintptr_t irq = bflb_irq_save();
    uint64_t now = now_us();
    bool power_off = poweroff_pending && host_suspended &&
                     now - suspend_at_us >= WAKE_POWEROFF_DEBOUNCE_US &&
                     now >= poweroff_retry_us;
    uint32_t session = poweroff_session;
    wake_state_t current = state;
    uint64_t entered = state_entered_us;
    uint32_t generation = wake_generation;
    bool resumed = host_resumed || !host_suspended;
    bflb_irq_restore(irq);

    if (power_off) {
        uint8_t payload[47] = {2};
        irq = bflb_irq_save();
        bool valid = poweroff_pending && host_suspended &&
                     generation == wake_generation && session == bt_hid_host_session();
        bflb_irq_restore(irq);
        if (valid) {
            int result = bt_hid_host_set_feature_crc_guarded(0x08, payload, sizeof(payload),
                                                         session, poweroff_allowed, generation);
            irq = bflb_irq_save();
            if (generation == wake_generation && session == poweroff_session) {
                if (result == 0) poweroff_pending = false;
                else poweroff_retry_us = now_us() + 100000;
            }
            bflb_irq_restore(irq);
        }
        /* Keep page scan available: a PS press can reconnect and wake USB. */
    }
    /* Remote wake only: no synthetic keyboard press after resume. */
    irq = bflb_irq_save();
    if (generation == wake_generation && state == current && current == WAKE_REQUESTED &&
        (!config_wake_enabled() || resumed || now - entered > WAKE_REQUEST_TIMEOUT_US))
        enter_state(WAKE_DONE);
    bflb_irq_restore(irq);
}

#include "dse.h"
#include "bt_hid_host.h"
#include "bflb_mtimer.h"
#include "bflb_irq.h"
#include "debug_log.h"
#include <string.h>

/* Only accepted submissions advance a command. Readiness additionally needs
 * every actual profile response; a GET submission is not a response. */
enum phase { OFF, ECHO, UNLOCK, WAIT_UNLOCK, POST_UNLOCK, POST_POLL, PREFETCH };
static struct {
    enum phase phase;
    uint32_t epoch, session;
    uint64_t due, written;
    uint16_t requested, received;
    uint8_t next, polls;
    bool ready, busy;
} dse = { .ready = true };

bool dse_is_profile_report(uint8_t id) { return id >= 0x70 && id <= 0x7b; }
bool dse_profiles_ready(void)
{
    uintptr_t irq = bflb_irq_save();
    bool ready = dse.ready;
    bflb_irq_restore(irq);
    return ready;
}

void dse_reset(void)
{
    uintptr_t irq = bflb_irq_save();
    uint32_t epoch = dse.epoch + 1;
    memset(&dse, 0, sizeof(dse));
    dse.epoch = epoch;
    dse.ready = true;
    bflb_irq_restore(irq);
}

void dse_on_edge_detected(void)
{
    dse_reset();
    uintptr_t irq = bflb_irq_save();
    dse.session = bt_hid_host_session();
    dse.phase = ECHO;
    dse.ready = false;
    bflb_irq_restore(irq);
    /* Preserve the reference's immediate 0x65 -> 0x80 order on success. */
    dse_task();
    dse_task();
}

void dse_on_control_packet(const uint8_t *pkt, uint16_t size)
{
    if (size == 1 && (pkt[0] & 0xf0) == 0) {
        LOG_INF("[DSE] HID HANDSHAKE: 0x%02X\n", pkt[0]);
        return; /* A handshake has no report ID: never assign it to a GET. */
    }
    if (size != 65 || pkt[0] != 0xa3 || !dse_is_profile_report(pkt[1])) return;
    uintptr_t irq = bflb_irq_save();
    uint16_t bit = 1u << (pkt[1] - 0x70);
    if (dse.phase == PREFETCH && dse.session == bt_hid_host_session() &&
        (dse.requested & bit)) {
        dse.received |= bit;
        if (dse.received == 0xfff) {
            dse.ready = true;
            dse.phase = OFF;
        }
    }
    bflb_irq_restore(irq);
}

void dse_on_profile_write(uint8_t id, uint32_t session)
{
    if (id < 0x60 || id > 0x62) return;
    uintptr_t irq = bflb_irq_save();
    if (session == bt_hid_host_session()) {
        ++dse.epoch;
        dse.session = session;
        dse.written = bflb_mtimer_get_time_us();
        dse.due = dse.written + 500000;
        dse.phase = POST_UNLOCK;
        dse.ready = dse.busy = false;
        dse.requested = dse.received = 0;
        dse.next = dse.polls = 0;
    }
    bflb_irq_restore(irq);
}

void dse_task(void)
{
    uint64_t now = bflb_mtimer_get_time_us();
    uintptr_t irq = bflb_irq_save();
    enum phase phase = dse.phase;
    if (phase == OFF || dse.busy || now < dse.due) {
        bflb_irq_restore(irq);
        return;
    }
    if (dse.session != bt_hid_host_session()) {
        dse_reset();
        bflb_irq_restore(irq);
        return;
    }
    if (phase == WAIT_UNLOCK) dse.phase = phase = PREFETCH;
    uint8_t index = dse.next;
    if (phase == PREFETCH) {
        while (index < 12 && (dse.received & (1u << index))) ++index;
        if (index == 12) {
            dse.next = 0;
            dse.due = now + 1000000;
            bflb_irq_restore(irq);
            return;
        }
        /* Mark before submission: an immediate RX callback is legitimate. */
        dse.requested |= 1u << index;
    }
    uint32_t epoch = dse.epoch, session = dse.session;
    dse.busy = true;
    bflb_irq_restore(irq);

    int result = 0;
    if (phase == ECHO) {
        uint8_t fw[61]; uint16_t len;
        if (bt_hid_host_copy_feature(0x20, fw, sizeof(fw), &len) && len >= 61)
            result = bt_hid_host_set_feature_session(0x65, fw, 61, session);
        /* Missing 0x20 skips 0x65, exactly as DS5Dongle does. */
    } else if (phase == UNLOCK || phase == POST_UNLOCK) {
        uint8_t unlock[59] = {0x70, 1};
        result = bt_hid_host_set_feature_crc_session(0x80, unlock, sizeof(unlock), session);
    } else {
        result = bt_hid_host_get_feature_session(phase == POST_POLL ? 0x81 : 0x70 + index, session);
    }

    now = bflb_mtimer_get_time_us();
    irq = bflb_irq_save();
    if (epoch == dse.epoch && session == dse.session) {
        dse.busy = false;
        if (dse.phase != phase) { /* Last synchronous response completed the snapshot. */
            bflb_irq_restore(irq);
            return;
        }
        dse.due = now + 80000;
        if (result == 0) {
            if (phase == ECHO) { dse.phase = UNLOCK; dse.due = now; }
            else if (phase == UNLOCK) { dse.phase = WAIT_UNLOCK; dse.due = now + 4000000; }
            else if (phase == POST_UNLOCK) { dse.phase = POST_POLL; dse.due = dse.written + 1000000; }
            else if (phase == POST_POLL) {
                if (++dse.polls == 6) { dse.phase = PREFETCH; dse.due = dse.written + 5500000; }
                else dse.due = now + 250000;
            } else dse.next = index + 1;
        }
    }
    bflb_irq_restore(irq);
}

#include "bt_hid_host.h"
#include "ds5_protocol.h"
#include "dse.h"
#include "config.h"
#include "bflb_irq.h"

#include <bluetooth/bluetooth.h>
#include <bluetooth/conn.h>
#include <bluetooth/l2cap.h>
#include <bluetooth/sdp.h>
#include <bluetooth/hci_host.h>
#include "conn_internal.h"
#include "keys.h"
#include "settings.h"
#include <string.h>
#include "debug_log.h"
#include <errno.h>
#include "bflb_efuse.h"
#include "hci_core.h"
#include "FreeRTOS.h"
#include "task.h"
#include "semphr.h"
#include "bl616_glb.h"


#define BT_INQUIRY_LEN         30 /* HCI units of 1.28 s, as in DS5Dongle. */
#define BT_MAX_DISCOVERED      8
#define L2CAP_BR_MTU           672

/* ---------- feature report cache ---------- */

#define FEATURE_CACHE_SLOTS  24
#define FEATURE_DATA_MAX     256

static struct {
    uint8_t  report_id;
    uint8_t  data[FEATURE_DATA_MAX];
    uint16_t len;
} feature_cache[FEATURE_CACHE_SLOTS];

static void feature_cache_clear(void)
{
    uintptr_t irq = bflb_irq_save();
    memset(feature_cache, 0, sizeof(feature_cache));
    bflb_irq_restore(irq);
}

static void feature_cache_store(uint8_t report_id,
                                const uint8_t *data, uint16_t len)
{
    if (report_id >= 0x70 && report_id <= 0x7B && len != 63)
        return;
    if (len > FEATURE_DATA_MAX)
        len = FEATURE_DATA_MAX;

    uintptr_t irq = bflb_irq_save();
    int slot = -1;
    for (int i = 0; i < FEATURE_CACHE_SLOTS; i++) {
        if (feature_cache[i].report_id == report_id && feature_cache[i].len > 0) {
            slot = i;
            break;
        }
    }
    if (slot < 0) {
        for (int i = 0; i < FEATURE_CACHE_SLOTS; i++) {
            if (feature_cache[i].len == 0) {
                slot = i;
                break;
            }
        }
    }
    if (slot < 0)
        slot = 0;

    feature_cache[slot].report_id = report_id;
    memcpy(feature_cache[slot].data, data, len);
    feature_cache[slot].len = len;
    bflb_irq_restore(irq);
}

/* ---------- persistent blacklist ----------
 *
 * When the user clears bonds (BOOT long-press), all bonded MACs are added
 * to a persistent blacklist stored in flash. This prevents the controller
 * from silently re-pairing via PS-only button press (controller-initiated
 * reconnect). The user must put the controller into PS+Share pairing mode
 * (dongle-initiated inquiry) to re-pair, which removes the MAC from the
 * blacklist on successful L2CAP HID connection.
 * Mirrors DS5Dongle's bt_cleared_addrs / BLCK TLV mechanism. */

#define BLACKLIST_MAX     8
#define NV_BLACKLIST_KEY  "DS5_BLK"

static bt_addr_t blacklist_addrs[BLACKLIST_MAX];
static int       blacklist_count;
static volatile bool blacklist_dirty;
static uint32_t blacklist_version;
static TickType_t blacklist_retry_tick;
static bool blacklist_retry_wait;

static void blacklist_load(void)
{
    size_t real_len = 0;
    int ret = bt_settings_get_bin(NV_BLACKLIST_KEY,
                                  (u8_t *)blacklist_addrs,
                                  sizeof(blacklist_addrs), &real_len);
    if (ret == 0 && real_len > 0 &&
        (real_len % sizeof(bt_addr_t)) == 0) {
        blacklist_count = (int)(real_len / sizeof(bt_addr_t));
        if (blacklist_count > BLACKLIST_MAX)
            blacklist_count = BLACKLIST_MAX;
        LOG_INF("[BT] Blacklist: loaded %d entries\n", blacklist_count);
    } else {
        blacklist_count = 0;
    }
}

static void blacklist_persist(void)
{
    bt_addr_t snapshot[BLACKLIST_MAX];
    taskENTER_CRITICAL();
    TickType_t now = xTaskGetTickCount();
    if (!blacklist_dirty || (blacklist_retry_wait &&
        (TickType_t)(now - blacklist_retry_tick) < pdMS_TO_TICKS(1000))) {
        taskEXIT_CRITICAL();
        return;
    }
    int count = blacklist_count;
    uint32_t version = blacklist_version;
    memcpy(snapshot, blacklist_addrs, count * sizeof(bt_addr_t));
    taskEXIT_CRITICAL();
    int result = count ? bt_settings_set_bin(NV_BLACKLIST_KEY,
                       (const u8_t *)snapshot, count * sizeof(bt_addr_t)) :
                       settings_delete(NV_BLACKLIST_KEY);
    taskENTER_CRITICAL();
    if (!result && version == blacklist_version) blacklist_dirty = false;
    blacklist_retry_wait = result != 0;
    if (result) blacklist_retry_tick = now;
    taskEXIT_CRITICAL();
}

static void blacklist_clear(void)
{
    taskENTER_CRITICAL();
    blacklist_count = 0;
    blacklist_dirty = true;
    ++blacklist_version;
    taskEXIT_CRITICAL();
}

static bool blacklist_contains(const bt_addr_t *addr)
{
    taskENTER_CRITICAL();
    bool found = false;
    for (int i = 0; i < blacklist_count; i++)
        if (bt_addr_cmp(addr, &blacklist_addrs[i]) == 0) { found = true; break; }
    taskEXIT_CRITICAL();
    return found;
}

static void __attribute__((unused)) blacklist_add(const bt_addr_t *addr)
{
    taskENTER_CRITICAL();
    if (blacklist_count < BLACKLIST_MAX && !blacklist_contains(addr)) {
        bt_addr_copy(&blacklist_addrs[blacklist_count++], addr);
        blacklist_dirty = true;
        ++blacklist_version;
    }
    taskEXIT_CRITICAL();
}

static void blacklist_remove(const bt_addr_t *addr)
{
    taskENTER_CRITICAL();
    for (int i = 0; i < blacklist_count; i++) {
        if (bt_addr_cmp(addr, &blacklist_addrs[i]) == 0) {
            for (int j = i; j < blacklist_count - 1; j++)
                bt_addr_copy(&blacklist_addrs[j], &blacklist_addrs[j + 1]);
            blacklist_count--;
            blacklist_dirty = true;
            ++blacklist_version;
            break;
        }
    }
    taskEXIT_CRITICAL();
}

/* ---------- multi-controller memory ---------- */

#define MAX_BONDED_CONTROLLERS 8
#define NV_ACTIVE_CTRL_KEY     "active_ctrl" /* Legacy one-byte index. */
#define NV_ACTIVE_ADDR_KEY     "active_addr"

static bt_addr_t bonded_list[MAX_BONDED_CONTROLLERS];
static uint8_t   bonded_count = 0;
static uint8_t   active_idx   = 0;
static bt_addr_t active_addr;
static volatile bool active_addr_dirty;
static volatile bool bonded_refresh_pending;
static bt_addr_t pairing_addr;
static volatile bool pairing_addr_pending;

static int bonded_list_find_addr(const bt_addr_t *addr)
{
    for (int i = 0; i < bonded_count; i++) {
        if (bt_addr_cmp(&bonded_list[i], addr) == 0)
            return i;
    }
    return -1;
}

static void bonded_list_collect_cb(const struct bt_br_bond_info *info,
                                   void *user_data)
{
    (void)user_data;
    if (bonded_count < MAX_BONDED_CONTROLLERS &&
        bonded_list_find_addr(info->addr) < 0) {
        bt_addr_copy(&bonded_list[bonded_count], info->addr);
        bonded_count++;
    }
}

/* Caller holds the critical section. Persist identity, never the SDK slot. */
static void bonded_list_select_index(uint8_t index)
{
    bt_addr_t selected = {{0}};
    active_idx = bonded_count ? index : 0;
    if (bonded_count)
        bt_addr_copy(&selected, &bonded_list[active_idx]);
    if (bt_addr_cmp(&active_addr, &selected) != 0) {
        bt_addr_copy(&active_addr, &selected);
        active_addr_dirty = true;
    }
}

static void bonded_list_refresh(void)
{
    /* The SDK modifies its BR key table in RX context. Take a bounded,
     * in-memory snapshot without letting RX change slots during enumeration. */
    taskENTER_CRITICAL();
    bonded_count = 0;
    memset(bonded_list, 0, sizeof(bonded_list));
    bt_br_foreach_bond(bonded_list_collect_cb, NULL);
    int idx = -1;
    if (pairing_addr_pending) {
        idx = bonded_list_find_addr(&pairing_addr);
        if (idx >= 0)
            pairing_addr_pending = false;
    }
    if (idx < 0)
        idx = bonded_list_find_addr(&active_addr);
    bonded_list_select_index(idx >= 0 ? (uint8_t)idx : 0);
    bonded_refresh_pending = false;
    taskEXIT_CRITICAL();
}

static bool bonded_list_copy_active(bt_addr_t *addr)
{
    taskENTER_CRITICAL();
    bool valid = bonded_count > 0;
    bt_addr_copy(addr, &active_addr);
    taskEXIT_CRITICAL();
    return valid;
}

static void bonded_list_persist(void)
{
    taskENTER_CRITICAL();
    if (!active_addr_dirty) {
        taskEXIT_CRITICAL();
        return;
    }
    bt_addr_t selected = active_addr;
    active_addr_dirty = false;
    taskEXIT_CRITICAL();

    /* Flash writes stay outside the critical section and RX callbacks.
     * An all-zero address records an empty list, preventing legacy resurrection. */
    int err = bt_settings_set_bin(NV_ACTIVE_ADDR_KEY, selected.val, sizeof(selected.val));
    if (err) {
        active_addr_dirty = true;
        LOG_WRN("[BT] Failed to save active controller address (%d)\n", err);
    } else {
        settings_delete(NV_ACTIVE_CTRL_KEY);
    }
}

static void bonded_list_load(void)
{
    bt_addr_t stored = {{0}};
    size_t real_len = 0;
    int ret = bt_settings_get_bin(NV_ACTIVE_ADDR_KEY, stored.val,
                                  sizeof(stored.val), &real_len);
    bool have_address = ret == 0 && real_len == sizeof(stored.val);
    pairing_addr_pending = false;
    active_addr = have_address ? stored : (bt_addr_t){{0}};
    active_addr_dirty = false;
    bonded_list_refresh();

    if (!have_address) {
        uint8_t stored_idx = 0;
        real_len = 0;
        ret = bt_settings_get_bin(NV_ACTIVE_CTRL_KEY, &stored_idx,
                                  sizeof(stored_idx), &real_len);
        taskENTER_CRITICAL();
        if (ret == 0 && real_len == 1 && stored_idx < bonded_count)
            bonded_list_select_index(stored_idx);
        active_addr_dirty = true; /* One-time migration, including an empty list. */
        taskEXIT_CRITICAL();
    }
    bonded_list_persist();
    LOG_INF("[BT] Bonded list: %d controllers, active_idx=%d\n",
            bonded_count, active_idx);
}

/* ---------- internal state ---------- */

static struct {
    volatile enum bt_hid_host_state state;
    struct bt_conn *conn;
    volatile bool acl_disconnected;
    TickType_t last_activity_tick;
    bt_addr_t target_addr;
    bool has_target;
    bool selection_only;
    bt_addr_t selection_addr;

    struct bt_l2cap_br_chan ctrl_chan;
    struct bt_l2cap_br_chan intr_chan;
    volatile bool ctrl_connected;
    volatile bool intr_connected;
    volatile bool ctrl_accepting;
    volatile bool intr_accepting;
    bool hid_service_found;

    volatile bool outgoing;
    volatile bool is_dse;
    volatile bool check_dse;

    volatile bool connect_pending;
    volatile bool discovery_active;
    volatile TickType_t connect_pending_tick;
    volatile uint8_t connect_retry_count;
    bt_addr_t     pending_addr;

    volatile bool sdp_pending;

    /* Security establishment deferred to task context */
    volatile bool security_pending;
    volatile bool security_done;
    volatile TickType_t acl_connect_tick;
    volatile TickType_t security_done_tick;

    struct {
        TickType_t last_attempt, probe_tick;
        bool attempted;
        volatile bool primer_done, detection_ready;
        volatile uint8_t sent;
        uint8_t attempts[4], probe_attempts;
    } handshake;

    bt_hid_input_cb_t  input_cb;
    bt_hid_state_cb_t  state_cb;
    bt_hid_primer_cb_t primer_cb;
    uint16_t filter_vid;
    uint16_t filter_pid;
} hid_ctx;

/* L2CAP requires headroom for ACL + L2CAP headers (typically ~8 bytes).
 * BT_L2CAP_BUF_SIZE adds this headroom to the payload MTU. */
#ifndef BT_L2CAP_BUF_SIZE
#define BT_L2CAP_BUF_SIZE(mtu) ((mtu) + 8)
#endif

struct net_buf_pool hid_tx_pool;
struct net_buf_pool hid_rx_pool;

static bool first_intr_logged = false;
static bool first_output_logged = false;

/* ---------- Application-level TX FIFO (CAN_SEND_NOW equivalent) ----------
 *
 * DS5Dongle uses BTstack's L2CAP_EVENT_CAN_SEND_NOW for flow control:
 *   bt_write() → enqueue to send_fifo (10-deep)
 *   CAN_SEND_NOW fires → dequeue one → l2cap_send → if more, request again
 *
 * Zephyr equivalent:
 *   bt_hid_host_send_output() → enqueue to app_tx_fifo (10-deep)
 *   on_hid_output_sent() fires when HCI ACKs the packet → drain next one
 *
 * This ensures only ONE queued output packet is in-flight at any time,
 * preventing net_buf pool exhaustion and HCI credit starvation.
 */
#define APP_TX_DEPTH    10
#define APP_TX_DATA_SZ  512  /* DS5Dongle send_element size, includes HID header */
#define APP_TX_SLOTS (APP_TX_DEPTH + 1) /* ring sentinel; ten usable queued slots */

static uint8_t  app_tx_buf[APP_TX_SLOTS][APP_TX_DATA_SZ];
static uint16_t app_tx_len[APP_TX_SLOTS];
static bool app_tx_reliable[APP_TX_SLOTS];
/* One failed critical submission remains ahead of the FIFO. Ordinary streaming
 * reports keep the reference project's drop-on-error policy. */
static uint8_t app_tx_retry_data[APP_TX_DATA_SZ];
static uint16_t app_tx_retry_len;
static bool app_tx_retry_pending;
static volatile uint8_t  app_tx_head = 0;   /* next slot to consume */
static volatile uint8_t  app_tx_tail = 0;   /* next slot to fill */
static volatile bool     app_tx_busy = false; /* true: one packet is in HCI */
static uint8_t feature_refresh_pending[32]; /* coalesced GETs, indexed by report ID */
static void feature_refresh_poll(void);
static volatile uint32_t app_tx_generation = 1; /* invalidate callbacks across FIFO resets */

/* bt_conn_tx_cb_t: SDK declaration from conn_internal.h */
static void hid_tx_drain_one(void);  /* forward declaration */

static void on_hid_output_sent(struct bt_conn *conn, void *user_data)
{
    /* HCI controller acknowledged the previous packet — equivalent to
     * BTstack's L2CAP_EVENT_CAN_SEND_NOW: mark idle then drain next. */
    taskENTER_CRITICAL();
    bool current = conn == hid_ctx.conn && app_tx_busy &&
                   (uint32_t)(uintptr_t)user_data == app_tx_generation;
    if (current)
        app_tx_busy = false;
    taskEXIT_CRITICAL();
    if (current)
        hid_tx_drain_one();
}

/* Consume buf on every path. The SDK BR wrapper discards the return value
 * from bt_l2cap_send_cb(), which already consumes buf on both success and
 * failure. Call that layer directly so failures cannot leave busy latched. */
static int hid_send_buf(struct bt_l2cap_br_chan *chan, struct net_buf *buf,
                       bt_conn_tx_cb_t cb, uint32_t generation)
{
    struct bt_conn *conn = NULL;
    uint16_t cid = 0, mtu = 0;
    taskENTER_CRITICAL();
    bool connected = (chan == &hid_ctx.intr_chan && hid_ctx.intr_connected) ||
                     (chan == &hid_ctx.ctrl_chan && hid_ctx.ctrl_connected);
    if (generation == app_tx_generation && (!cb || app_tx_busy) &&
        connected && hid_ctx.state != BT_HID_STATE_DISCONNECTING &&
        hid_ctx.conn && chan->chan.conn == hid_ctx.conn) {
        conn = bt_conn_ref(hid_ctx.conn);
        cid = chan->tx.cid;
        mtu = chan->tx.mtu;
    }
    taskEXIT_CRITICAL();

    if (!conn) {
        net_buf_unref(buf);
        return -ENOTCONN;
    }
    if (buf->len > mtu) {
        net_buf_unref(buf);
        bt_conn_unref(conn);
        return -EMSGSIZE;
    }

    extern int bt_l2cap_send_cb(struct bt_conn *conn, uint16_t cid,
                              struct net_buf *buf, bt_conn_tx_cb_t cb,
                              void *user_data);
    int err = bt_l2cap_send_cb(conn, cid, buf, cb,
                             (void *)(uintptr_t)generation);
    bt_conn_unref(conn);
    return err;
}

static int hid_tx_submit(struct net_buf *buf, uint32_t generation)
{
    return hid_send_buf(&hid_ctx.intr_chan, buf, on_hid_output_sent, generation);
}

static void hid_tx_drain_one(void)
{
    /* DS5Dongle also advances after a failed send. No completion callback is
     * scheduled on failure, so drain queued successors here, with a bound. */
    for (unsigned attempt = 0; attempt < APP_TX_DEPTH; ++attempt) {
        uint8_t data[APP_TX_DATA_SZ];
        uint16_t len;
        bool reliable;
        uint32_t generation;
        taskENTER_CRITICAL();
        if (app_tx_busy || (!app_tx_retry_pending && app_tx_head == app_tx_tail) ||
            !hid_ctx.intr_connected || !hid_ctx.conn ||
            hid_ctx.state == BT_HID_STATE_DISCONNECTING) {
            taskEXIT_CRITICAL();
            return;
        }
        app_tx_busy = true;
        generation = app_tx_generation;
        if (app_tx_retry_pending) {
            len = app_tx_retry_len;
            memcpy(data, app_tx_retry_data, len);
            reliable = true;
            app_tx_retry_pending = false;
        } else {
            uint8_t idx = app_tx_head;
            len = app_tx_len[idx];
            reliable = app_tx_reliable[idx];
            memcpy(data, app_tx_buf[idx], len);
            app_tx_head = (idx + 1) % APP_TX_SLOTS;
        }
        taskEXIT_CRITICAL();

        int err = -ENOBUFS;
        struct net_buf *buf = net_buf_alloc(&hid_tx_pool, K_NO_WAIT);
        if (buf) {
            net_buf_reserve(buf, BT_L2CAP_CHAN_SEND_RESERVE);
            net_buf_add_mem(buf, data, len);
            err = hid_tx_submit(buf, generation);
        }
        if (err >= 0)
            return; /* Only successful submissions have a completion callback. */

        LOG_ERR("[HID] drain: send err=%d\n", err);
        taskENTER_CRITICAL();
        bool current = generation == app_tx_generation;
        if (current) {
            app_tx_busy = false;
            if (reliable && err != -EMSGSIZE && err != -EINVAL &&
                hid_ctx.state != BT_HID_STATE_DISCONNECTING) {
                memcpy(app_tx_retry_data, data, len);
                app_tx_retry_len = len;
                app_tx_retry_pending = true;
            }
        }
        taskEXIT_CRITICAL();
        if (!current || reliable)
            return; /* bounded: retry on the next task poll, before successors */
    }
}

static void app_tx_fifo_reset(void)
{
    taskENTER_CRITICAL();
    if (++app_tx_generation == 0)
        ++app_tx_generation;
    app_tx_head = 0;
    app_tx_tail = 0;
    app_tx_busy = false;
    app_tx_retry_pending = false;
    memset(feature_refresh_pending, 0, sizeof(feature_refresh_pending));
    taskEXIT_CRITICAL();
}

void bt_hid_host_poll_tx(void)
{
    /* A producer may refill the FIFO during a bounded failure drain. No
     * completion is pending then; the BT task supplies the next opportunity. */
    hid_tx_drain_one();
    feature_refresh_poll();
}

static struct bt_br_discovery_result discovery_results[BT_MAX_DISCOVERED];

/* ---------- helpers ---------- */

static const bt_addr_t *conn_get_br_addr(const struct bt_conn *conn)
{
    struct bt_conn_info info;
    if (bt_conn_get_info(conn, &info) == 0)
        return info.br.dst;
    return NULL;
}

static const char *state_name(enum bt_hid_host_state s) {
    switch (s) {
    case BT_HID_STATE_IDLE:          return "IDLE";
    case BT_HID_STATE_SCANNING:      return "SCANNING";
    case BT_HID_STATE_CONNECTING:    return "CONNECTING";
    case BT_HID_STATE_SDP_QUERY:     return "SDP_QUERY";
    case BT_HID_STATE_L2CAP_CONTROL: return "L2CAP_CTRL";
    case BT_HID_STATE_L2CAP_INTERRUPT: return "L2CAP_INTR";
    case BT_HID_STATE_HANDSHAKE:     return "HANDSHAKE";
    case BT_HID_STATE_CONNECTED:     return "CONNECTED";
    case BT_HID_STATE_DISCONNECTING: return "DISCONNECTING";
    default:                         return "?";
    }
}

static void set_state(enum bt_hid_host_state new_state)
{
    LOG_INF("[BT-FSM] %s -> %s\n", state_name(hid_ctx.state), state_name(new_state));
    hid_ctx.state = new_state;

    if (new_state == BT_HID_STATE_CONNECTED && hid_ctx.conn) {
        const bt_addr_t *remote = conn_get_br_addr(hid_ctx.conn);
        if (remote) {
            bt_addr_copy(&hid_ctx.target_addr, remote);
            hid_ctx.has_target = true;
            char a[BT_ADDR_STR_LEN];
            bt_addr_to_str(remote, a, sizeof(a));
            LOG_INF("[BT] Connected to bonded device: %s\n", a);
        }
    }

    if (new_state == BT_HID_STATE_CONNECTED)
        hid_ctx.selection_only = false;
    if (hid_ctx.state_cb)
        hid_ctx.state_cb(new_state);
}

/* ---------- L2CAP callbacks ---------- */

static struct net_buf *l2cap_alloc_buf(struct bt_l2cap_chan *chan)
{
    return net_buf_alloc(&hid_rx_pool, K_NO_WAIT);
}

static const uint8_t handshake_ids[] = {
    DS5_FEATURE_REPORT_PAIRING, DS5_FEATURE_REPORT_FIRMWARE,
    0x22, DS5_FEATURE_REPORT_CALIBRATION
};
#define HANDSHAKE_RETRY_TICKS pdMS_TO_TICKS(100)
#define HANDSHAKE_PROBE_TICKS pdMS_TO_TICKS(1000)

static void handshake_begin(void)
{
    memset(&hid_ctx.handshake, 0, sizeof(hid_ctx.handshake));
    hid_ctx.check_dse = false; /* Enable immediately before GET 0x70, as DS5Dongle does. */
    set_state(BT_HID_STATE_HANDSHAKE);
}

static void handshake_maybe_begin(void)
{
    /* Incoming channels and the SDK security callback may arrive in either
     * order. Initialize once, only after encryption and BOTH HID channels. */
    if (hid_ctx.conn && hid_ctx.security_done &&
        hid_ctx.ctrl_connected && hid_ctx.intr_connected &&
        hid_ctx.state != BT_HID_STATE_DISCONNECTING &&
        hid_ctx.state != BT_HID_STATE_HANDSHAKE &&
        hid_ctx.state != BT_HID_STATE_CONNECTED)
        handshake_begin();
}

/* RX may identify the controller before GET 0x70 returns in the BT task.
 * Finish the initial GET batch and state/0x32 first, as in DS5Dongle's
 * channel-open handler, before unlocking Edge or exposing CONNECTED. */
static void handshake_finish_detection(void)
{
    taskENTER_CRITICAL();
    if (hid_ctx.state != BT_HID_STATE_HANDSHAKE ||
        !hid_ctx.handshake.primer_done || !hid_ctx.handshake.detection_ready ||
        !hid_ctx.conn || !hid_ctx.ctrl_connected || !hid_ctx.intr_connected) {
        taskEXIT_CRITICAL();
        return;
    }
    hid_ctx.handshake.detection_ready = false;
    uint32_t generation = app_tx_generation;
    bool is_dse = hid_ctx.is_dse;
    taskEXIT_CRITICAL();

    if (is_dse)
        dse_on_edge_detected();
    if (generation == app_tx_generation &&
        hid_ctx.state == BT_HID_STATE_HANDSHAKE &&
        hid_ctx.conn && hid_ctx.ctrl_connected && hid_ctx.intr_connected)
        set_state(BT_HID_STATE_CONNECTED);
}

/* Run in the BT task. Like DS5Dongle, issue 0x09/0x20/0x22/0x05/0x70 in order
 * without waiting for earlier replies, then initialize state and send 0x32.
 * Probe retries remain separate because HID HANDSHAKE errors carry no ID. */
static void handshake_send_pending(void)
{
    if (hid_ctx.state != BT_HID_STATE_HANDSHAKE || !hid_ctx.conn ||
        !hid_ctx.ctrl_connected || !hid_ctx.intr_connected)
        return;
    TickType_t now = xTaskGetTickCount();
    if (hid_ctx.handshake.attempted &&
        (TickType_t)(now - hid_ctx.handshake.last_attempt) < HANDSHAKE_RETRY_TICKS)
        return;
    hid_ctx.handshake.attempted = true;
    hid_ctx.handshake.last_attempt = now;
    uint32_t generation = app_tx_generation;

    for (unsigned i = 0; i < sizeof(handshake_ids); ++i) {
        uint8_t bit = 1u << i;
        if ((hid_ctx.handshake.sent & bit) || hid_ctx.handshake.attempts[i] >= 3)
            continue;
        ++hid_ctx.handshake.attempts[i];
        /* Set before submission: a response can arrive during the SDK call. */
        hid_ctx.handshake.sent |= bit;
        int err = bt_hid_host_get_feature(handshake_ids[i]);
        if (generation != app_tx_generation || hid_ctx.state != BT_HID_STATE_HANDSHAKE)
            return;
        if (err) {
            hid_ctx.handshake.sent &= ~bit;
            LOG_WRN("[HID] Init GET_REPORT(0x%02x) failed (%d), attempt %u\n",
                    handshake_ids[i], err, hid_ctx.handshake.attempts[i]);
        }
    }
    /* First probe still follows the GET batch immediately. Primer retries
     * must remain possible after a reply or after all probe attempts. */
    if (!hid_ctx.handshake.detection_ready && hid_ctx.handshake.probe_attempts < 6 &&
        (!hid_ctx.handshake.probe_attempts ||
         (TickType_t)(now - hid_ctx.handshake.probe_tick) >= HANDSHAKE_PROBE_TICKS)) {
        hid_ctx.handshake.probe_tick = now;
        ++hid_ctx.handshake.probe_attempts;
        hid_ctx.check_dse = true;
        int err = bt_hid_host_get_feature(0x70);
        if (generation != app_tx_generation || hid_ctx.state != BT_HID_STATE_HANDSHAKE)
            return;
        if (err) LOG_WRN("[HID] Edge probe send failed (%d)\n", err);
    }

    if (!hid_ctx.handshake.primer_done) {
        if (hid_ctx.primer_cb && hid_ctx.primer_cb(generation) != 0)
            return;
        taskENTER_CRITICAL();
        if (generation != app_tx_generation || hid_ctx.state != BT_HID_STATE_HANDSHAKE) {
            taskEXIT_CRITICAL();
            return;
        }
        hid_ctx.handshake.primer_done = true;
        taskEXIT_CRITICAL();
        handshake_finish_detection();
    }
}

static int l2cap_ctrl_recv(struct bt_l2cap_chan *chan, struct net_buf *buf)
{
    if (!hid_ctx.conn || chan->conn != hid_ctx.conn ||
        !hid_ctx.ctrl_connected || hid_ctx.state == BT_HID_STATE_DISCONNECTING)
        return 0;
    uint8_t *data = buf->data;
    uint16_t len  = buf->len;

    if (len < 1)
        return 0;

    uint8_t header = data[0];
    uint8_t trans  = (header >> 4) & 0x0F;
    uint8_t param  = header & 0x0F;

    switch (trans) {
    case HID_TRANS_HANDSHAKE:
        LOG_INF("[HID] Handshake: 0x%02x\n", param);
        if (hid_ctx.state == BT_HID_STATE_HANDSHAKE && hid_ctx.check_dse &&
            param == HID_HANDSHAKE_INVALID_ID) {
            /* Match DS5Dongle: one INVALID_ID while probing means DS5. */
            hid_ctx.check_dse = false;
            hid_ctx.is_dse = false;
            LOG_INF("[HID] DualSense (non-Edge) confirmed\n");
            hid_ctx.handshake.detection_ready = true;
        }
        break;

    case HID_TRANS_DATA:
        if (param == HID_REPORT_FEATURE) {
            if (len > 2) {
                uint8_t rid = data[1];
                LOG_INF("[HID] Feature report received, id=0x%02x len=%d\n",
                       rid, len - 2);

                feature_cache_store(rid, data + 2, len - 2);
                if (rid == 0x70 && hid_ctx.state == BT_HID_STATE_HANDSHAKE &&
                    hid_ctx.check_dse) {
                    hid_ctx.check_dse = false;
                    hid_ctx.is_dse    = true;
                    LOG_INF("[HID] DualSense Edge detected\n");
                    hid_ctx.handshake.detection_ready = true;
                }
            } else {
                LOG_WRN("[HID] Feature DATA too short (len=%d), skipping\n",
                       len);
            }
        }
        break;

    default:
        LOG_DBG("[HID] Control: trans=0x%x param=0x%x len=%d\n",
               trans, param, len);
        break;
    }

    dse_on_control_packet(data, len);

    return 0;
}

/* Raw, pre-remap input and thresholds match DS5Dongle's inactivity check. */
static void check_inactivity(const uint8_t *data, uint16_t len)
{
    uint8_t minutes = config_inactive_minutes();
    if (len < 13 || !(data[2] & 1) || !minutes)
        return;
    TickType_t now = xTaskGetTickCount();
    bool active = data[3] < 120 || data[3] > 140 ||
                  data[4] < 120 || data[4] > 140 ||
                  data[5] < 120 || data[5] > 140 ||
                  data[6] < 120 || data[6] > 140 ||
                  data[7] || data[8] || data[10] != 0x08 || data[11] || data[12];
    if (active) {
        hid_ctx.last_activity_tick = now;
    } else if ((TickType_t)(now - hid_ctx.last_activity_tick) >
               pdMS_TO_TICKS((uint32_t)minutes * 60000u)) {
        hid_ctx.last_activity_tick = now;
        bt_hid_host_disconnect();
    }
}

static int l2cap_intr_recv(struct bt_l2cap_chan *chan, struct net_buf *buf)
{
    if (!hid_ctx.conn || chan->conn != hid_ctx.conn ||
        hid_ctx.state == BT_HID_STATE_DISCONNECTING)
        return 0;
    uint8_t *data = buf->data;
    uint16_t len  = buf->len;

    if (len < 2)
        return 0;

    uint8_t header = data[0];
    uint8_t trans  = (header >> 4) & 0x0F;
    uint8_t param  = header & 0x0F;

    if (trans == HID_TRANS_DATA && param == HID_REPORT_INPUT) {
        if (!first_intr_logged) {
            first_intr_logged = true;
            LOG_INF("[HID] First input report from controller: len=%d rid=0x%02x\n",
                   len - 1, data[1]);
        }
        if (hid_ctx.input_cb) {
            hid_ctx.input_cb(data + 1, len - 1);
        }
        check_inactivity(data, len);
    } else {
        LOG_WRN("[HID] intr_recv: unexpected trans=0x%x param=0x%x len=%d\n",
               trans, param, len);
    }

    return 0;
}

static void l2cap_ctrl_connected(struct bt_l2cap_chan *chan)
{
    LOG_DBG("[HID-CB] l2cap_ctrl_connected ENTER chan=%p state=%d\n",
           chan, hid_ctx.state);
    if (!hid_ctx.conn || chan->conn != hid_ctx.conn ||
        hid_ctx.state == BT_HID_STATE_DISCONNECTING || hid_ctx.ctrl_connected)
        return;
    hid_ctx.ctrl_accepting = false;
    hid_ctx.ctrl_connected = true;

    /* Only create outgoing interrupt channel if WE initiated the connection
     * (via inquiry → connect). For controller-initiated reconnects, the
     * controller will open the interrupt channel via the L2CAP server. */
    if (hid_ctx.outgoing && !hid_ctx.intr_connected && !hid_ctx.intr_accepting &&
        !hid_ctx.intr_chan.chan.conn) {
        hid_ctx.intr_accepting = true;
        set_state(BT_HID_STATE_L2CAP_INTERRUPT);

        int err = bt_l2cap_chan_connect(hid_ctx.conn,
                                        &hid_ctx.intr_chan.chan,
                                        HID_PSM_INTERRUPT);
        if (err) {
            hid_ctx.intr_accepting = false;
            LOG_ERR("[HID] Failed to connect interrupt channel: %d\n", err);
            bt_hid_host_disconnect();
        }
    }
    handshake_maybe_begin();
}

static void l2cap_intr_connected(struct bt_l2cap_chan *chan)
{
    LOG_DBG("[HID-CB] l2cap_intr_connected ENTER chan=%p state=%d\n",
           chan, hid_ctx.state);
    if (!hid_ctx.conn || chan->conn != hid_ctx.conn ||
        hid_ctx.state == BT_HID_STATE_DISCONNECTING || hid_ctx.intr_connected)
        return;
    hid_ctx.intr_accepting = false;
    hid_ctx.intr_connected = true;
    hid_ctx.last_activity_tick = xTaskGetTickCount();

    /* Successful L2CAP HID open removes this MAC from the blacklist,
     * treating it as an explicit user re-pair (PS+Share mode). */
    if (hid_ctx.conn) {
        const bt_addr_t *remote = conn_get_br_addr(hid_ctx.conn);
        if (remote && blacklist_contains(remote)) {
            blacklist_remove(remote);
            blacklist_dirty = true;
        }
    }

    /* Stop accepting further connections while connected (matches
     * DS5Dongle's gap_connectable_control(false) on HID open). */
    bt_br_set_connectable(false);
    bt_br_set_discoverable(false);

    /* The BT task sends the initial GET batch followed by the 0x32 primer. */
    handshake_maybe_begin();

}

static void l2cap_ctrl_disconnected(struct bt_l2cap_chan *chan)
{
    LOG_INF("[HID] Control channel disconnected (conn=%p)\n", hid_ctx.conn);
    if (!hid_ctx.conn || chan->conn != hid_ctx.conn)
        return;
    hid_ctx.ctrl_accepting = false;
    hid_ctx.ctrl_connected = false;
    if (!hid_ctx.intr_connected && !hid_ctx.acl_disconnected)
        bt_hid_host_disconnect();
}

static void l2cap_intr_disconnected(struct bt_l2cap_chan *chan)
{
    LOG_INF("[HID] Interrupt channel disconnected\n");
    if (!hid_ctx.conn || chan->conn != hid_ctx.conn)
        return;
    hid_ctx.intr_accepting = false;
    hid_ctx.intr_connected = false;
    if (!hid_ctx.ctrl_connected && !hid_ctx.acl_disconnected)
        bt_hid_host_disconnect();
}

static struct bt_l2cap_chan_ops ctrl_ops = {
    .alloc_buf    = l2cap_alloc_buf,
    .recv         = l2cap_ctrl_recv,
    .connected    = l2cap_ctrl_connected,
    .disconnected = l2cap_ctrl_disconnected,
};

static struct bt_l2cap_chan_ops intr_ops = {
    .alloc_buf    = l2cap_alloc_buf,
    .recv         = l2cap_intr_recv,
    .connected    = l2cap_intr_connected,
    .disconnected = l2cap_intr_disconnected,
};

/* ---------- SDP callback ---------- */

static uint8_t sdp_discover_cb(struct bt_conn *conn,
                                struct bt_sdp_client_result *result)
{
    if (conn != hid_ctx.conn)
        return BT_SDP_DISCOVER_UUID_STOP;

    if (!result) {
        LOG_INF("[SDP] Discovery complete, HID found=%d\n",
               hid_ctx.hid_service_found);

        if (!hid_ctx.hid_service_found) {
            LOG_INF("[SDP] No HID service, disconnecting\n");
            bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
            return BT_SDP_DISCOVER_UUID_STOP;
        }

        set_state(BT_HID_STATE_L2CAP_CONTROL);

        memset(&hid_ctx.ctrl_chan, 0, sizeof(hid_ctx.ctrl_chan));
        memset(&hid_ctx.intr_chan, 0, sizeof(hid_ctx.intr_chan));

        hid_ctx.ctrl_chan.chan.ops = &ctrl_ops;
        hid_ctx.ctrl_chan.rx.mtu  = L2CAP_BR_MTU;
        hid_ctx.intr_chan.chan.ops = &intr_ops;
        hid_ctx.intr_chan.rx.mtu  = L2CAP_BR_MTU;

        int err = bt_l2cap_chan_connect(conn, &hid_ctx.ctrl_chan.chan,
                                           HID_PSM_CONTROL);
        if (err) {
            LOG_ERR("[SDP] Failed to connect control channel: %d\n", err);
            bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
        }
        return BT_SDP_DISCOVER_UUID_STOP;
    }

    LOG_INF("[SDP] Found HID service record\n");
    hid_ctx.hid_service_found = true;
    return BT_SDP_DISCOVER_UUID_CONTINUE;
}

/* ---------- SSP Authentication ---------- */

static void auth_passkey_confirm(struct bt_conn *conn, unsigned int passkey)
{
    if (conn != hid_ctx.conn || hid_ctx.state == BT_HID_STATE_DISCONNECTING)
        return;
    LOG_INF("[BT] SSP confirm passkey: %06u (auto-accepting)\n", passkey);
    bt_conn_auth_passkey_confirm(conn);
}

static void auth_pincode_entry(struct bt_conn *conn, bool secure)
{
    (void)secure;
    if (conn != hid_ctx.conn || hid_ctx.state == BT_HID_STATE_DISCONNECTING)
        return;
    /* Legacy pairing compatibility, matching DS5Dongle's PIN response. */
    int err = bt_conn_auth_pincode_entry(conn, "0000");
    if (err) {
        LOG_WRN("[BT] PIN reply failed (%d)\n", err);
        bt_hid_host_disconnect();
    }
}

static void auth_passkey_display(struct bt_conn *conn, unsigned int passkey)
{
    LOG_INF("[BT] SSP display passkey: %06u\n", passkey);
}

static void auth_cancel(struct bt_conn *conn)
{
    LOG_INF("[BT] SSP pairing cancelled\n");
}

static void auth_pairing_complete(struct bt_conn *conn, bool bonded)
{
    LOG_INF("[BT] Pairing complete, bonded=%d\n", bonded);

    if (conn == hid_ctx.conn && bonded &&
        hid_ctx.state != BT_HID_STATE_DISCONNECTING) {
        /* Pairing Complete may precede Link Key Notify. Remember the peer,
         * but only select it once the SDK actually has its key. */
        const bt_addr_t *addr = conn_get_br_addr(conn);
        if (addr) {
            taskENTER_CRITICAL();
            bt_addr_copy(&pairing_addr, addr);
            pairing_addr_pending = true;
            bonded_refresh_pending = true;
            taskEXIT_CRITICAL();
        }
    }
}

static void clear_rejected_key(struct bt_conn *conn, enum bt_security_err err)
{
    if (conn != hid_ctx.conn ||
        (err != BT_SECURITY_ERR_AUTH_FAIL &&
         err != BT_SECURITY_ERR_PIN_OR_KEY_MISSING))
        return;
    const bt_addr_t *remote = conn_get_br_addr(conn);
    if (remote) {
        extern void bt_keys_link_key_clear_addr(const bt_addr_t *addr);
        bt_addr_t rejected = *remote;
        bt_keys_link_key_clear_addr(&rejected);
        taskENTER_CRITICAL();
        if (pairing_addr_pending && bt_addr_cmp(&pairing_addr, &rejected) == 0)
            pairing_addr_pending = false;
        taskEXIT_CRITICAL();
        bonded_list_refresh();
        hid_ctx.has_target = bonded_list_copy_active(&hid_ctx.target_addr);
        LOG_WRN("[BT] Explicit authentication failure: cleared rejected key\n");
    }
}

static void auth_pairing_failed(struct bt_conn *conn,
                                 enum bt_security_err reason)
{
    LOG_ERR("[BT] Pairing failed, reason=%d\n", reason);
    if (conn == hid_ctx.conn) {
        clear_rejected_key(conn, reason);
        bt_hid_host_disconnect();
    }
}

static struct bt_conn_auth_cb auth_callbacks = {
    .pincode_entry     = auth_pincode_entry,
    .passkey_display   = auth_passkey_display,
    .passkey_confirm   = auth_passkey_confirm,
    .cancel            = auth_cancel,
    .pairing_complete  = auth_pairing_complete,
    .pairing_failed    = auth_pairing_failed,
};

/* ---------- SDP ---------- */

static void start_sdp_discovery(struct bt_conn *conn)
{
    static struct bt_sdp_discover_params params;

    static struct bt_uuid_16 hid_uuid = BT_UUID_INIT_16(0x1124);

    params.uuid  = &hid_uuid.uuid;
    params.func  = sdp_discover_cb;
    params.pool  = &hid_tx_pool;

    set_state(BT_HID_STATE_SDP_QUERY);

    int err = bt_sdp_discover(conn, &params);
    if (err) {
        LOG_ERR("[SDP] Failed to start discovery: %d\n", err);
        bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
    }
}

/* ---------- BR/EDR connection callbacks ---------- */

static void security_changed(struct bt_conn *conn, bt_security_t level,
                              enum bt_security_err err)
{
    LOG_DBG("[BT-CB] security_changed ENTER conn=%p level=%d err=%d state=%d\n",
           conn, level, err, hid_ctx.state);

    if (conn != hid_ctx.conn || hid_ctx.state == BT_HID_STATE_DISCONNECTING) {
        LOG_DBG("[BT-CB] security_changed: not our conn, ignoring\n");
        return;
    }

    if (err) {
        LOG_ERR("[BT] Security elevation failed: level=%d err=%d\n", level, err);
        clear_rejected_key(conn, err);
        bt_hid_host_disconnect();
        return;
    }

    if (level < BT_SECURITY_L2)
        return;

    LOG_INF("[BT] Security level raised to %d (outgoing=%d state=%s)\n",
           level, hid_ctx.outgoing, state_name(hid_ctx.state));

    hid_ctx.security_done = true;
    hid_ctx.security_done_tick = xTaskGetTickCount();

    if (level >= BT_SECURITY_L2 && hid_ctx.outgoing
        && (hid_ctx.state == BT_HID_STATE_CONNECTING
            || hid_ctx.state == BT_HID_STATE_SDP_QUERY)) {
        /* Encryption complete for outgoing connection (new pairing).
         * Defer L2CAP channel creation to task context, matching
         * DS5Dongle's ENCRYPTION_CHANGE + new_pair path. */
        LOG_DBG("[BT] security_changed: outgoing — deferring L2CAP to task\n");
        hid_ctx.sdp_pending = true;
    } else if (level >= BT_SECURITY_L2 && !hid_ctx.outgoing) {
        /* Incoming reconnection: encryption OK.
         * Controller will create L2CAP channels via our servers. */
        LOG_INF("[BT] security OK (incoming), waiting for controller L2CAP\n");
    }
    handshake_maybe_begin();
    LOG_DBG("[BT-CB] security_changed EXIT\n");
}

/* Explicit controller switching reserves admission until that peer completes
 * initialization, or the user requests a fresh scan/clears pairing. */
static bool selection_accepts(const bt_addr_t *remote)
{
    taskENTER_CRITICAL();
    bool allowed = !hid_ctx.selection_only ||
        (remote && bt_addr_cmp(remote, &hid_ctx.selection_addr) == 0);
    taskEXIT_CRITICAL();
    return allowed;
}

static void conn_connected(struct bt_conn *conn, uint8_t err)
{
    LOG_DBG("[BT-CB] conn_connected ENTER conn=%p err=%d state=%d\n",
           conn, err, hid_ctx.state);

    if (err) {
        LOG_ERR("[BT] Connection failed: 0x%02x\n", err);
        bonded_refresh_pending = true;
        if (hid_ctx.conn != NULL && conn != hid_ctx.conn)
            return;
        hid_ctx.conn = NULL;
        hid_ctx.outgoing = false;
        bt_br_set_connectable(true);
        bt_br_set_discoverable(true);
        LOG_INF("[BT] Re-enabled page scan after failed connection\n");
        set_state(BT_HID_STATE_IDLE);
        return;
    }

    char addr_str[BT_ADDR_STR_LEN];
    const bt_addr_t *remote = conn_get_br_addr(conn);
    if (remote)
        bt_addr_to_str(remote, addr_str, sizeof(addr_str));
    else
        snprintf(addr_str, sizeof(addr_str), "??:??:??:??:??:??");
    LOG_INF("[BT] Connected to %s\n", addr_str);

    if (!selection_accepts(remote)) {
        bt_conn_disconnect(conn, BT_HCI_ERR_CONN_LIMIT_EXCEEDED);
        return;
    }

    if (hid_ctx.conn != NULL && conn != hid_ctx.conn) {
        LOG_WRN("[BT] Already connected, rejecting second ACL from %s\n",
               addr_str);
        bt_conn_disconnect(conn, BT_HCI_ERR_CONN_LIMIT_EXCEEDED);
        return;
    }

    /* The BT task stops any active inquiry. Do not wait for Inquiry Cancel
     * from the RX callback that also delivers its completion. */

    if (!hid_ctx.outgoing && remote && blacklist_contains(remote)) {
        LOG_WRN("[BT] Rejecting blacklisted device %s "
               "(re-pair via PS+Share to unblock)\n", addr_str);
        bt_conn_disconnect(conn, BT_HCI_ERR_AUTH_FAIL);
        return;
    }

    if (!hid_ctx.conn) {
        LOG_DBG("[BT] conn_connected: taking ref on conn=%p\n", conn);
        hid_ctx.conn = bt_conn_ref(conn);
    }

    hid_ctx.connect_pending = false;
    hid_ctx.connect_pending_tick = 0;
    hid_ctx.connect_retry_count = 0;

    set_state(BT_HID_STATE_CONNECTING);

    /* Log internal security state for diagnostics */
    LOG_DBG("[BT] conn handle=0x%04x sec_level=%d required_sec=%d encrypt=%d\n",
           conn->handle, conn->sec_level, conn->required_sec_level,
           conn->encrypt);

    /* Start authentication from the BT task, not an RX callback. The SDK
     * initializes new connections and owns required_sec_level while pairing. */
    hid_ctx.security_pending = true;
    hid_ctx.security_done = false;
    hid_ctx.acl_connect_tick = xTaskGetTickCount();

    LOG_DBG("[BT] %s connection — security deferred to task context\n",
           hid_ctx.outgoing ? "Outgoing" : "Incoming");
    LOG_DBG("[BT-CB] conn_connected EXIT\n");
}

static volatile int8_t cached_rssi = 1; /* 1 = unknown (positive → web shows "Unknown") */
static volatile bool switch_pending = false;

static bool clear_bonds_pending;
static void clear_bond_storage(void);

static void conn_disconnected(struct bt_conn *conn, uint8_t reason)
{
    LOG_INF("[BT] ACL disconnected: conn=%p owner=%p reason=0x%02x\n",
            conn, hid_ctx.conn, reason);
    bonded_refresh_pending = true;
    if (conn != hid_ctx.conn)
        return;

    /* HCI Disconnection Complete can precede the SDK's deferred L2CAP
     * teardown. Keep our reference and channel storage until both finish. */
    hid_ctx.acl_disconnected = true;
    set_state(BT_HID_STATE_DISCONNECTING);
}

static bool hid_channel_released(const struct bt_l2cap_br_chan *channel)
{
    /* The SDK clears chan.conn BEFORE destroy()/RTX cleanup. Waiting only
     * for NULL would allow this higher-priority task to reuse storage early. */
    if (channel->chan.conn || channel->chan.state != BT_L2CAP_DISCONNECTED ||
        channel->chan.psm)
        return false;
#if defined(BFLB_BREDR_PATCH_CLEAR_L2CAP_BR_STALE_CID)
    if (channel->rx.cid || channel->tx.cid)
        return false;
#endif
#if defined(BFLB_BLE_PATCH_FREE_ALLOCATED_BUFFER_IN_OS)
    if (channel->chan.rtx_work.timer.timer.hdl)
        return false;
#endif
    return true;
}

bool bt_hid_host_poll_disconnect(void)
{
    if (!hid_ctx.acl_disconnected || !hid_ctx.conn ||
        !hid_channel_released(&hid_ctx.ctrl_chan) ||
        !hid_channel_released(&hid_ctx.intr_chan))
        return false;

    struct bt_conn *conn = hid_ctx.conn;
    bonded_list_refresh();
    taskENTER_CRITICAL();
    pairing_addr_pending = false;
    hid_ctx.ctrl_connected = false;
    hid_ctx.intr_connected = false;
    hid_ctx.ctrl_accepting = false;
    hid_ctx.intr_accepting = false;
    hid_ctx.outgoing = false;
    hid_ctx.is_dse = false;
    hid_ctx.check_dse = false;
    memset(&hid_ctx.handshake, 0, sizeof(hid_ctx.handshake));
    hid_ctx.security_pending = false;
    hid_ctx.security_done = false;
    hid_ctx.sdp_pending = false;
    hid_ctx.connect_pending = false;
    hid_ctx.connect_pending_tick = 0;
    hid_ctx.connect_retry_count = 0;
    first_intr_logged = false;
    first_output_logged = false;
    app_tx_fifo_reset();
    taskEXIT_CRITICAL();

    feature_cache_clear();
    dse_reset();
    cached_rssi = 1;
    if (clear_bonds_pending)
        clear_bond_storage();

    hid_ctx.conn = NULL;
    hid_ctx.acl_disconnected = false;
    bt_conn_unref(conn);
    /* IDLE notifies the application to reset audio/input and send neutral
     * state. Preserve switch_pending until that notification is complete. */
    set_state(BT_HID_STATE_IDLE);
    switch_pending = false;
    bt_br_set_connectable(true);
    bt_br_set_discoverable(true);
    LOG_INF("[BT] Disconnect cleanup complete; waiting for controller\n");
    return true;
}

static struct bt_conn_cb conn_callbacks = {
    .connected        = conn_connected,
    .disconnected     = conn_disconnected,
    .security_changed = security_changed,
};

/* ---------- Inquiry / Discovery ---------- */

static bool discovery_is_peripheral(const struct bt_br_discovery_result *result)
{
    uint32_t cod = result->cod[0] | ((uint32_t)result->cod[1] << 8) |
                   ((uint32_t)result->cod[2] << 16);
    return (cod & 0x000F00) == 0x000500;
}

/* Task context only. A successful SDK stop includes the controller's
 * Inquiry Cancel completion and detaches the result callback. */
static bool discovery_stop(void)
{
    if (!hid_ctx.discovery_active)
        return true;
    int err = bt_br_discovery_stop();
    if (err && err != -EALREADY) {
        LOG_WRN("[BT] Inquiry stop failed (%d); not paging\n", err);
        return false;
    }
    hid_ctx.discovery_active = false;
    return true;
}

static void discovery_complete(struct bt_br_discovery_result *results,
                               size_t count)
{
    LOG_INF("[BT] Inquiry complete, found %zu device(s)\n", count);

    hid_ctx.discovery_active = false;
    if (hid_ctx.conn || hid_ctx.state != BT_HID_STATE_SCANNING) {
        LOG_INF("[BT] Inquiry ended after connection state advanced, skipping\n");
        return;
    }

    for (size_t i = 0; i < count; i++) {
        char addr_str[BT_ADDR_STR_LEN];
        bt_addr_to_str(&results[i].addr, addr_str, sizeof(addr_str));

        LOG_DBG("[BT] Device: %s COD=0x%02x%02x%02x\n", addr_str,
                results[i].cod[2], results[i].cod[1], results[i].cod[0]);

        if (discovery_is_peripheral(&results[i])) {
            LOG_INF("[BT] Peripheral found, deferring connect to task context\n");
            bt_addr_copy(&hid_ctx.pending_addr, &results[i].addr);
            hid_ctx.connect_pending = true;
            hid_ctx.connect_pending_tick = 0;
            hid_ctx.connect_retry_count = 0;
            /* Set CONNECTING instead of IDLE to prevent the auto-reconnect
             * logic from starting a new scan before poll_connect runs. */
            set_state(BT_HID_STATE_CONNECTING);
            return;
        }
    }

    LOG_INF("[BT] No peripheral found; waiting for incoming connection\n");
    set_state(BT_HID_STATE_IDLE);
}

/* ---------- L2CAP server (for controller-initiated reconnection) ---------- */

static int l2cap_server_reject_blacklisted(struct bt_conn *conn)
{
    if (hid_ctx.state == BT_HID_STATE_DISCONNECTING)
        return -EACCES;
    const bt_addr_t *remote = conn_get_br_addr(conn);
    if (remote && blacklist_contains(remote)) {
        char a[BT_ADDR_STR_LEN];
        bt_addr_to_str(remote, a, sizeof(a));
        LOG_WRN("[HID] Rejecting L2CAP from blacklisted %s\n", a);
        return -EACCES;
    }
    return 0;
}

static void l2cap_server_ensure_conn(struct bt_conn *conn)
{
    if (!hid_ctx.conn) {
        hid_ctx.conn = bt_conn_ref(conn);
        set_state(BT_HID_STATE_CONNECTING);
    }
}

static int l2cap_ctrl_server_accept(struct bt_conn *conn,
                                     struct bt_l2cap_chan **chan)
{
    char addr_str[BT_ADDR_STR_LEN] = "??";
    const bt_addr_t *remote = conn_get_br_addr(conn);
    if (remote)
        bt_addr_to_str(remote, addr_str, sizeof(addr_str));

    LOG_DBG("[HID] Incoming L2CAP control (PSM 0x%04x) from %s "
           "state=%s conn=%p our_conn=%p\n",
           HID_PSM_CONTROL, addr_str, state_name(hid_ctx.state),
           conn, hid_ctx.conn);

    if (!selection_accepts(remote)) return -EACCES;
    int err = l2cap_server_reject_blacklisted(conn);
    if (err) return err;

    /* Reject L2CAP from a different ACL than the primary connection —
     * prevents a racing "rejected" second controller from stealing channels */
    if (hid_ctx.conn && conn != hid_ctx.conn) {
        LOG_WRN("[HID] Control: rejecting L2CAP from non-primary conn %p "
               "(primary=%p)\n", conn, hid_ctx.conn);
        return -EACCES;
    }

    if (hid_ctx.ctrl_connected || hid_ctx.ctrl_accepting || hid_ctx.ctrl_chan.chan.conn) {
        LOG_WRN("[HID] Control channel already connected/accepting\n");
        return -ENOMEM;
    }
    hid_ctx.ctrl_accepting = true;

    memset(&hid_ctx.ctrl_chan, 0, sizeof(hid_ctx.ctrl_chan));
    hid_ctx.ctrl_chan.chan.ops = &ctrl_ops;
    hid_ctx.ctrl_chan.rx.mtu  = L2CAP_BR_MTU;
    *chan = &hid_ctx.ctrl_chan.chan;

    l2cap_server_ensure_conn(conn);
    LOG_INF("[HID] Control server: accepted, chan=%p\n", *chan);
    return 0;
}

static int l2cap_intr_server_accept(struct bt_conn *conn,
                                     struct bt_l2cap_chan **chan)
{
    char addr_str[BT_ADDR_STR_LEN] = "??";
    const bt_addr_t *remote = conn_get_br_addr(conn);
    if (remote)
        bt_addr_to_str(remote, addr_str, sizeof(addr_str));

    LOG_DBG("[HID] Incoming L2CAP interrupt (PSM 0x%04x) from %s "
           "state=%s conn=%p our_conn=%p\n",
           HID_PSM_INTERRUPT, addr_str, state_name(hid_ctx.state),
           conn, hid_ctx.conn);

    if (!selection_accepts(remote)) return -EACCES;
    int err = l2cap_server_reject_blacklisted(conn);
    if (err) return err;

    /* Reject L2CAP from a different ACL than the primary connection */
    if (hid_ctx.conn && conn != hid_ctx.conn) {
        LOG_WRN("[HID] Interrupt: rejecting L2CAP from non-primary conn %p "
               "(primary=%p)\n", conn, hid_ctx.conn);
        return -EACCES;
    }

    if (hid_ctx.intr_connected || hid_ctx.intr_accepting || hid_ctx.intr_chan.chan.conn) {
        LOG_WRN("[HID] Interrupt channel already connected/accepting\n");
        return -ENOMEM;
    }
    hid_ctx.intr_accepting = true;

    memset(&hid_ctx.intr_chan, 0, sizeof(hid_ctx.intr_chan));
    hid_ctx.intr_chan.chan.ops = &intr_ops;
    hid_ctx.intr_chan.rx.mtu  = L2CAP_BR_MTU;
    *chan = &hid_ctx.intr_chan.chan;

    l2cap_server_ensure_conn(conn);
    LOG_INF("[HID] Interrupt server: accepted, chan=%p\n", *chan);
    return 0;
}

static struct bt_l2cap_server l2cap_hid_ctrl_server = {
    .psm    = HID_PSM_CONTROL,
    .sec_level = BT_SECURITY_L2,
    .accept = l2cap_ctrl_server_accept,
};

static struct bt_l2cap_server l2cap_hid_intr_server = {
    .psm    = HID_PSM_INTERRUPT,
    .sec_level = BT_SECURITY_L2,
    .accept = l2cap_intr_server_accept,
};

/* ---------- SDP Service Record for HID Host ---------- */

static struct bt_sdp_attribute hid_host_attrs[] = {
    BT_SDP_NEW_SERVICE,
    /* Service Class ID List: HID (0x1124) */
    {
        BT_SDP_ATTR_SVCLASS_ID_LIST,
        { BT_SDP_TYPE_SIZE_VAR(BT_SDP_SEQ8, 3),
          BT_SDP_DATA_ELEM_LIST(
              { BT_SDP_TYPE_SIZE(BT_SDP_UUID16), BT_SDP_ARRAY_16(0x1124) },
          ),
        }
    },
    /* Protocol Descriptor List: L2CAP(PSM=0x0011) + HIDP */
    {
        BT_SDP_ATTR_PROTO_DESC_LIST,
        { BT_SDP_TYPE_SIZE_VAR(BT_SDP_SEQ8, 13),
          BT_SDP_DATA_ELEM_LIST(
              { BT_SDP_TYPE_SIZE_VAR(BT_SDP_SEQ8, 6),
                BT_SDP_DATA_ELEM_LIST(
                    { BT_SDP_TYPE_SIZE(BT_SDP_UUID16), BT_SDP_ARRAY_16(0x0100) },
                    { BT_SDP_TYPE_SIZE(BT_SDP_UINT16), BT_SDP_ARRAY_16(HID_PSM_CONTROL) },
                ),
              },
              { BT_SDP_TYPE_SIZE_VAR(BT_SDP_SEQ8, 3),
                BT_SDP_DATA_ELEM_LIST(
                    { BT_SDP_TYPE_SIZE(BT_SDP_UUID16), BT_SDP_ARRAY_16(0x0011) },
                ),
              },
          ),
        }
    },
    /* Additional Protocol Descriptor List: L2CAP(PSM=0x0013) + HIDP */
    {
        BT_SDP_ATTR_ADD_PROTO_DESC_LIST,
        { BT_SDP_TYPE_SIZE_VAR(BT_SDP_SEQ8, 15),
          BT_SDP_DATA_ELEM_LIST(
              { BT_SDP_TYPE_SIZE_VAR(BT_SDP_SEQ8, 13),
                BT_SDP_DATA_ELEM_LIST(
                    { BT_SDP_TYPE_SIZE_VAR(BT_SDP_SEQ8, 6),
                      BT_SDP_DATA_ELEM_LIST(
                          { BT_SDP_TYPE_SIZE(BT_SDP_UUID16), BT_SDP_ARRAY_16(0x0100) },
                          { BT_SDP_TYPE_SIZE(BT_SDP_UINT16), BT_SDP_ARRAY_16(HID_PSM_INTERRUPT) },
                      ),
                    },
                    { BT_SDP_TYPE_SIZE_VAR(BT_SDP_SEQ8, 3),
                      BT_SDP_DATA_ELEM_LIST(
                          { BT_SDP_TYPE_SIZE(BT_SDP_UUID16), BT_SDP_ARRAY_16(0x0011) },
                      ),
                    },
                ),
              },
          ),
        }
    },
    /* Profile Descriptor List: HID v1.11 */
    {
        BT_SDP_ATTR_PROFILE_DESC_LIST,
        { BT_SDP_TYPE_SIZE_VAR(BT_SDP_SEQ8, 8),
          BT_SDP_DATA_ELEM_LIST(
              { BT_SDP_TYPE_SIZE_VAR(BT_SDP_SEQ8, 6),
                BT_SDP_DATA_ELEM_LIST(
                    { BT_SDP_TYPE_SIZE(BT_SDP_UUID16), BT_SDP_ARRAY_16(0x1124) },
                    { BT_SDP_TYPE_SIZE(BT_SDP_UINT16), BT_SDP_ARRAY_16(0x0111) },
                ),
              },
          ),
        }
    },
    BT_SDP_SERVICE_NAME("DS5Dongle HID"),
};

static struct bt_sdp_record hid_host_rec = {
    .attrs = hid_host_attrs,
    .attr_count = ARRAY_SIZE(hid_host_attrs),
};

/* ---------- bt_enable ready synchronization ---------- */

static volatile bool bt_ready_flag;
static SemaphoreHandle_t bt_ready_sem;
static StaticSemaphore_t bt_ready_sem_buf;

static void bt_ready_cb(int err)
{
    LOG_INF("[BT] bt_ready_cb: err=%d (stack init complete)\n", err);
    bt_ready_flag = (err == 0);
    xSemaphoreGive(bt_ready_sem);
}

/* ---------- public API ---------- */

int bt_hid_host_init(const struct bt_hid_host_config *config)
{
    memset(&hid_ctx, 0, sizeof(hid_ctx));
    feature_cache_clear();
    hid_ctx.input_cb   = config->input_cb;
    hid_ctx.state_cb   = config->state_cb;
    hid_ctx.primer_cb  = config->primer_cb;
    hid_ctx.filter_vid = config->target_vid;
    hid_ctx.filter_pid = config->target_pid;
    hid_ctx.state      = BT_HID_STATE_IDLE;

    net_buf_init(&hid_tx_pool, 20, BT_L2CAP_BUF_SIZE(L2CAP_BR_MTU), NULL);
    net_buf_init(&hid_rx_pool, 4, BT_L2CAP_BUF_SIZE(L2CAP_BR_MTU), NULL);

    bt_conn_cb_register(&conn_callbacks);
    bt_conn_auth_cb_register(&auth_callbacks);

    bt_ready_sem = xSemaphoreCreateBinaryStatic(&bt_ready_sem_buf);

    int err = bt_enable(bt_ready_cb);
    if (err) {
        LOG_ERR("[BT] bt_enable failed: %d\n", err);
        return err;
    }

    LOG_INF("[BT] bt_enable returned, waiting for stack init...\n");
    if (xSemaphoreTake(bt_ready_sem, pdMS_TO_TICKS(10000)) != pdTRUE) {
        LOG_ERR("[BT] ERROR: stack init timeout!\n");
        return -1;
    }
    if (!bt_ready_flag) {
        LOG_ERR("[BT] ERROR: stack init failed\n");
        return -1;
    }
    LOG_INF("[BT] Bluetooth fully initialized\n");

    /* Generate BD_ADDR from chip eFuse ID (boards often ship without
     * a pre-programmed BT MAC, resulting in 00:00:00:00:00:00).
     * Must be called AFTER bt_enable (HCI transport must be up). */
    {
        bt_addr_le_t cur;
        bt_get_local_public_address(&cur);
        bool all_zero = true;
        for (int i = 0; i < 6; i++)
            if (cur.a.val[i]) { all_zero = false; break; }

        if (all_zero) {
            uint8_t chipid[8];
            bflb_efuse_get_chipid(chipid);
            bt_addr_t bd;
            bd.val[0] = chipid[0];
            bd.val[1] = chipid[1];
            bd.val[2] = chipid[2];
            bd.val[3] = chipid[3];
            bd.val[4] = chipid[4];
            bd.val[5] = chipid[5] | 0xC0;
            int r1 = bt_set_bd_addr(&bd);
            int r2 = bt_set_local_public_address(bd.val);
            LOG_INF("[BT] Set BD_ADDR from chipid: %02X:%02X:%02X:%02X:%02X:%02X "
                   "(hci=%d host=%d)\n",
                   bd.val[5], bd.val[4], bd.val[3],
                   bd.val[2], bd.val[1], bd.val[0], r1, r2);
        }

        bt_get_local_public_address(&cur);
        LOG_INF("[BT] BD_ADDR: %02X:%02X:%02X:%02X:%02X:%02X\n",
               cur.a.val[5], cur.a.val[4], cur.a.val[3],
               cur.a.val[2], cur.a.val[1], cur.a.val[0]);
    }

    blacklist_load();
    bonded_list_load();

    /* Register L2CAP servers so the controller can initiate reconnection
     * (matching DS5Dongle's l2cap_register_service for both PSMs). */
    err = bt_l2cap_br_server_register(&l2cap_hid_ctrl_server);
    if (err)
        LOG_ERR("[BT] Warning: register control server failed: %d\n", err);

    err = bt_l2cap_br_server_register(&l2cap_hid_intr_server);
    if (err)
        LOG_ERR("[BT] Warning: register interrupt server failed: %d\n", err);

    err = bt_sdp_register_service(&hid_host_rec);
    if (err)
        LOG_ERR("[BT] Warning: register SDP HID record failed: %d\n", err);
    else
        LOG_INF("[BT] SDP HID service record registered\n");

    /* Match Pico's aggressive page scan: interval=window=0x0012 (11.25ms)
     * = 100% duty cycle. Ensures near-instant response to controller
     * reconnection attempts, critical for low-battery reconnect success.
     * HCI Write_Page_Scan_Activity: OGF 0x03, OCF 0x001C. */
    {
        struct net_buf *ps_buf = bt_hci_cmd_create(
            BT_OP(BT_OGF_BASEBAND, 0x001c), 4);
        if (ps_buf) {
            net_buf_add_le16(ps_buf, 0x0012);  /* interval: 11.25ms */
            net_buf_add_le16(ps_buf, 0x0012);  /* window:   11.25ms */
            int ps_err = bt_hci_cmd_send_sync(
                BT_OP(BT_OGF_BASEBAND, 0x001c), ps_buf, NULL);
            LOG_INF("[BT] Write Page Scan Activity: interval=0x0012 "
                   "window=0x0012 err=%d\n", ps_err);
        }
    }

    /* Make device discoverable so the controller can find us if needed */
    err = bt_br_set_connectable(true);
    LOG_INF("[BT] set_connectable: %d\n", err);

    err = bt_br_set_discoverable(true);
    LOG_INF("[BT] set_discoverable: %d\n", err);

    /* Diagnostic: check if BR/EDR write_scan_enable succeeded.
     * If both return 0, the controller accepted BR/EDR commands,
     * confirming that BR/EDR HCI path is functional. */
    LOG_INF("[BT] BR/EDR init complete — phone should see \"%s\"\n",
           CONFIG_BT_DEVICE_NAME);

    return 0;
}

int bt_hid_host_scan_start(uint8_t inquiry_length)
{
    if (hid_ctx.state != BT_HID_STATE_IDLE || hid_ctx.conn || hid_ctx.discovery_active)
        return -1;

    /* Don't scan if an outgoing connect retry is pending —
     * HCI can't do inquiry and create_connection simultaneously */
    if (hid_ctx.connect_pending) {
        LOG_INF("[BT] scan_start suppressed: outgoing connect pending\n");
        return -2;
    }

    /* An explicit inquiry searches for a new controller. The remembered
     * peer is for passive reconnect/selection, not an inquiry address filter.
     * Keep the persisted active address and its link key unchanged. */
    hid_ctx.has_target = false;
    hid_ctx.selection_only = false;
    hid_ctx.connect_pending_tick = 0;
    hid_ctx.connect_retry_count = 0;
    hid_ctx.discovery_active = true;
    set_state(BT_HID_STATE_SCANNING);

    memset(discovery_results, 0, sizeof(discovery_results));

    struct bt_br_discovery_param param = {
        .length     = inquiry_length ? inquiry_length : BT_INQUIRY_LEN,
        .limited    = false,
    };

    int err = bt_br_discovery_start(&param, discovery_results,
                                     BT_MAX_DISCOVERED,
                                     discovery_complete);
    if (err) {
        hid_ctx.discovery_active = false;
        LOG_ERR("[BT] Discovery start failed: %d\n", err);
        if (!hid_ctx.conn && hid_ctx.state == BT_HID_STATE_SCANNING)
            set_state(BT_HID_STATE_IDLE);
        return err;
    }

    LOG_INF("[BT] Scanning for devices...\n");
    return 0;
}

void bt_hid_host_scan_stop(void)
{
    if (discovery_stop() && hid_ctx.state == BT_HID_STATE_SCANNING)
        set_state(BT_HID_STATE_IDLE);
}

bool bt_hid_host_poll_scan_early(void)
{
    if (!hid_ctx.discovery_active)
        return false;

    if (hid_ctx.conn || hid_ctx.state != BT_HID_STATE_SCANNING) {
        discovery_stop(); /* Incoming reconnect wins over the inquiry. */
        return false;
    }

    for (int i = 0; i < BT_MAX_DISCOVERED; i++) {
        static const bt_addr_t zero = {{0}};
        if (bt_addr_cmp(&discovery_results[i].addr, &zero) == 0)
            break;

        if (discovery_is_peripheral(&discovery_results[i])) {
            /* Snapshot before the blocking SDK call: RX may finish inquiry
             * or accept an incoming ACL while we await command completion. */
            bt_addr_t candidate = discovery_results[i].addr;
            if (!discovery_stop())
                return false;
            if (hid_ctx.conn || hid_ctx.state != BT_HID_STATE_SCANNING)
                return false;
            LOG_INF("[BT] Inquiry stopped; connecting to peripheral\n");
            bt_addr_copy(&hid_ctx.pending_addr, &candidate);
            hid_ctx.connect_pending = true;
            hid_ctx.connect_pending_tick = 0;
            hid_ctx.connect_retry_count = 0;
            set_state(BT_HID_STATE_CONNECTING);
            return true;
        }
    }
    return false;
}

bool bt_hid_host_poll_connect(void)
{
    if (!hid_ctx.connect_pending)
        return false;

    /* If controller already reconnected (incoming), cancel outgoing */
    if (hid_ctx.conn != NULL) {
        LOG_INF("[BT] poll_connect: cancelled (controller already connected, "
               "state=%d)\n", hid_ctx.state);
        hid_ctx.connect_pending = false;
        hid_ctx.connect_pending_tick = 0;
        hid_ctx.connect_retry_count = 0;
        return false;
    }

    /* Pace retries of an explicit inquiry result, never an incoming reconnect. */
    if (hid_ctx.connect_pending_tick != 0) {
        TickType_t elapsed = xTaskGetTickCount() - hid_ctx.connect_pending_tick;
        if (elapsed < pdMS_TO_TICKS(500))
            return false;
    }

    /* Cancel if another connect path is active (not from our deferred flow).
     * States IDLE, SCANNING, and CONNECTING are valid — early termination and
     * discovery_complete set CONNECTING before deferring to poll_connect. */
    if (hid_ctx.state != BT_HID_STATE_IDLE &&
        hid_ctx.state != BT_HID_STATE_SCANNING &&
        hid_ctx.state != BT_HID_STATE_CONNECTING) {
        LOG_INF("[BT] poll_connect: cancelled (state=%d, not idle)\n",
               hid_ctx.state);
        hid_ctx.connect_pending = false;
        hid_ctx.connect_pending_tick = 0;
        hid_ctx.connect_retry_count = 0;
        return false;
    }

    /* If scanning is in progress, cancel it first (HCI can't do both) */
    if (hid_ctx.discovery_active) {
        if (!discovery_stop())
            return false;
        /* Let the next poll recheck state/ownership after the blocking stop. */
        return false;
    }

    LOG_INF("[BT] Discovery: attempting outgoing connect (state=%d conn=%p)\n",
           hid_ctx.state, hid_ctx.conn);
    int rc = bt_hid_host_connect(hid_ctx.pending_addr.val);
    if (rc != 0) {
        hid_ctx.connect_retry_count++;
        if (hid_ctx.connect_retry_count >= 3) {
            LOG_WRN("[BT] Discovery: gave up after %d attempts\n",
                    hid_ctx.connect_retry_count);
            hid_ctx.connect_retry_count = 0;
            hid_ctx.connect_pending = false;
            hid_ctx.connect_pending_tick = 0;
        } else {
            LOG_WRN("[BT] Discovery: attempt %d failed (%d), retry in 500ms\n",
                   hid_ctx.connect_retry_count, rc);
            hid_ctx.connect_pending_tick = xTaskGetTickCount();
        }
    } else {
        hid_ctx.connect_pending = false;
        hid_ctx.connect_pending_tick = 0;
        hid_ctx.connect_retry_count = 0;
    }
    return true;
}

bool bt_hid_host_poll_security(void)
{
    if (!hid_ctx.security_pending || !hid_ctx.conn ||
        hid_ctx.state == BT_HID_STATE_DISCONNECTING)
        return false;

    hid_ctx.security_pending = false;

    struct bt_conn *conn = hid_ctx.conn;

    LOG_DBG("[BT] poll_security: conn=%p handle=0x%04x state=%s "
           "sec_level=%d required_sec=%d encrypt=%d outgoing=%d\n",
           conn, conn->handle, state_name(hid_ctx.state),
           conn->sec_level, conn->required_sec_level, conn->encrypt,
           hid_ctx.outgoing);

    int err = bt_conn_set_security(conn, BT_SECURITY_L2);
    LOG_DBG("[BT] poll_security: bt_conn_set_security(L2) = %d "
           "(now sec=%d req=%d)\n",
           err, conn->sec_level, conn->required_sec_level);

    if (err == -EBUSY) {
        LOG_DBG("[BT] poll_security: pairing already in progress, waiting\n");
    } else if (err && err != -EALREADY) {
        LOG_DBG("[BT] poll_security: set_security failed (%d), "
               "trying HCI Auth_Requested directly\n", err);
        struct net_buf *buf = bt_hci_cmd_create(
            BT_HCI_OP_AUTH_REQUESTED, 2);
        if (buf) {
            net_buf_add_le16(buf, conn->handle);
            int hci_err = bt_hci_cmd_send_sync(
                BT_HCI_OP_AUTH_REQUESTED, buf, NULL);
            LOG_DBG("[BT] poll_security: HCI Auth_Requested = %d\n", hci_err);
        }
    }

    return true;
}

bool bt_hid_host_poll_sdp(void)
{
    if (!hid_ctx.sdp_pending || !hid_ctx.conn)
        return false;

    hid_ctx.sdp_pending = false;

    if (!hid_ctx.outgoing || !hid_ctx.security_done ||
        hid_ctx.ctrl_connected || hid_ctx.ctrl_accepting ||
        hid_ctx.ctrl_chan.chan.conn ||
        hid_ctx.state != BT_HID_STATE_CONNECTING) {
        LOG_DBG("[BT] poll_sdp: state already advanced (%s), skipping\n",
               state_name(hid_ctx.state));
        return false;
    }

    /* Only discovery-initiated connections create HID channels locally.
     * Paired incoming reconnects use the two servers, like DS5Dongle. */
    LOG_DBG("[BT] poll_sdp: creating outgoing L2CAP channels\n");

    set_state(BT_HID_STATE_L2CAP_CONTROL);

    memset(&hid_ctx.ctrl_chan, 0, sizeof(hid_ctx.ctrl_chan));
    hid_ctx.ctrl_chan.chan.ops = &ctrl_ops;
    hid_ctx.ctrl_chan.rx.mtu  = L2CAP_BR_MTU;
    /* A peer may already have opened Interrupt while encryption completed.
     * Never overwrite channel storage still owned by the SDK. */
    if (!hid_ctx.intr_connected && !hid_ctx.intr_accepting &&
        !hid_ctx.intr_chan.chan.conn) {
        memset(&hid_ctx.intr_chan, 0, sizeof(hid_ctx.intr_chan));
        hid_ctx.intr_chan.chan.ops = &intr_ops;
        hid_ctx.intr_chan.rx.mtu = L2CAP_BR_MTU;
    }

    hid_ctx.ctrl_accepting = true;
    int err = bt_l2cap_chan_connect(hid_ctx.conn,
                                    &hid_ctx.ctrl_chan.chan,
                                    HID_PSM_CONTROL);
    if (err) {
        hid_ctx.ctrl_accepting = false;
        LOG_DBG("[BT] poll_sdp: L2CAP control connect failed: %d\n", err);
        bt_hid_host_disconnect();
    }
    return true;
}

/* Recover a missed SDK notification only from actual encryption state.
 * DS5Dongle leaves authentication/connection deadlines to the BT stack. */
bool bt_hid_host_poll_security_ready(void)
{
    if (hid_ctx.security_done || !hid_ctx.conn ||
        hid_ctx.state != BT_HID_STATE_CONNECTING)
        return false;
    struct bt_conn *conn = hid_ctx.conn;
    if (!conn->encrypt || conn->sec_level < BT_SECURITY_L2)
        return false;
    hid_ctx.security_done = true;
    hid_ctx.security_done_tick = xTaskGetTickCount();
    if (hid_ctx.outgoing)
        hid_ctx.sdp_pending = true;
    handshake_maybe_begin();
    return true;
}

int bt_hid_host_connect(const uint8_t *addr)
{
    bt_addr_t bt_addr;
    memcpy(bt_addr.val, addr, 6);

    LOG_DBG("[BT] connect ENTER addr=%02X:%02X:%02X:%02X:%02X:%02X state=%d conn=%p\n",
           addr[5], addr[4], addr[3], addr[2], addr[1], addr[0],
           hid_ctx.state, hid_ctx.conn);

    if (hid_ctx.discovery_active)
        return -EBUSY; /* Poll path must confirm Inquiry Cancel first. */

    if (hid_ctx.conn) {
        LOG_DBG("[BT] connect: already have conn=%p, skip create\n", hid_ctx.conn);
        return -EBUSY; /* Do not turn an incoming reconnect into an outgoing one. */
    }

    hid_ctx.outgoing = true;
    set_state(BT_HID_STATE_CONNECTING);
    LOG_DBG("[BT] connect [3] state=CONNECTING, outgoing=true\n");

    /* The SDK answers Link Key Request from its actual key store. Do not
     * delete a valid key based on a potentially older application snapshot. */

    LOG_DBG("[BT] connect [4] bt_conn_create_br...\n");
    /* Disable page scan before paging — BL616 HCI controller can't do both */
    bt_br_set_connectable(false);
    bt_br_set_discoverable(false);
    struct bt_conn *conn = bt_conn_create_br(&bt_addr,
                                              BT_BR_CONN_PARAM_DEFAULT);
    LOG_DBG("[BT] connect [5] returned %p\n", conn);
    if (!conn) {
        LOG_WRN("[BT] bt_conn_create_br failed — re-enabling page scan\n");
        hid_ctx.outgoing = false;
        bt_br_set_connectable(true);
        bt_br_set_discoverable(true);
        set_state(BT_HID_STATE_IDLE);
        return -1;
    }

    /* Bouffalo SDK's bt_conn_create_br does NOT return a caller
     * reference (unlike standard Zephyr). SDK examples never call
     * bt_conn_unref after bt_conn_create_br. */
    LOG_DBG("[BT] connect EXIT OK\n");
    return 0;
}

void bt_hid_host_disconnect(void)
{
    taskENTER_CRITICAL();
    if (!hid_ctx.conn || hid_ctx.state == BT_HID_STATE_DISCONNECTING) {
        taskEXIT_CRITICAL();
        return;
    }
    struct bt_conn *conn = bt_conn_ref(hid_ctx.conn);
    enum bt_hid_host_state previous = hid_ctx.state;
    hid_ctx.state = BT_HID_STATE_DISCONNECTING;
    taskEXIT_CRITICAL();

    /* Request only. Cleanup and releasing our owner reference require the
     * actual disconnect event, exactly as DS5Dongle's bt_disconnect(). */
    int err = bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
    if (err && err != -ENOTCONN) {
        LOG_WRN("[BT] Disconnect request failed (%d), retaining connection\n", err);
        taskENTER_CRITICAL();
        if (hid_ctx.conn == conn && !hid_ctx.acl_disconnected &&
            hid_ctx.state == BT_HID_STATE_DISCONNECTING)
            hid_ctx.state = previous; /* Allow a later request to retry. */
        taskEXIT_CRITICAL();
    }
    bt_conn_unref(conn);
}

int bt_hid_host_read_rssi(int8_t *rssi)
{
    if (!hid_ctx.conn || hid_ctx.state != BT_HID_STATE_CONNECTED)
        return -1;
    int8_t val = 0;
    int ret = bt_le_read_rssi(hid_ctx.conn->handle, &val);
    if (ret == 0) {
        cached_rssi = val;
        if (rssi) *rssi = val;
    } else {
        LOG_WRN("[BT] Read RSSI failed: %d\n", ret);
    }
    return ret;
}

int8_t bt_hid_host_get_cached_rssi(void)
{
    return cached_rssi;
}

static int send_output_session(const uint8_t *data, uint16_t len, uint32_t generation, bool reliable)
{
    if (generation != app_tx_generation)
        return -ENOTCONN;
    if (!hid_ctx.intr_connected || !hid_ctx.conn ||
        hid_ctx.state == BT_HID_STATE_DISCONNECTING)
        return -ENOTCONN;

    if (!data || !len)
        return -EINVAL;
    if (len > L2CAP_BR_MTU - 1 || (reliable && len >= APP_TX_DATA_SZ))
        return -EMSGSIZE;

    /* Both 0x31 state and 0x32 initialization use the same ordered FIFO,
     * as DS5Dongle bt_write(). Retain BL618's larger 0x39 audio direct path. */
    if (len > APP_TX_DATA_SZ - 1) {
        /* Direct path: allocate and send immediately */
        struct net_buf *buf = net_buf_alloc(&hid_tx_pool, K_NO_WAIT);
        if (!buf) {
            LOG_WRN("[HID] send_output direct: pool empty (len=%d)\n", (int)len);
            return -ENOBUFS;
        }
        net_buf_reserve(buf, BT_L2CAP_CHAN_SEND_RESERVE);
        net_buf_add_u8(buf, HID_HEADER(HID_TRANS_DATA, HID_REPORT_OUTPUT));
        net_buf_add_mem(buf, data, len);
        int err = hid_send_buf(&hid_ctx.intr_chan, buf, NULL, generation);
        if (err < 0) {
            LOG_ERR("[HID] send_output direct: l2cap err=%d\n", err);
            return err;
        }
        return 0;
    }

    /* FIFO path for state/initialization reports. */
    taskENTER_CRITICAL();
    if (generation != app_tx_generation || !hid_ctx.conn ||
        !hid_ctx.intr_connected || hid_ctx.state == BT_HID_STATE_DISCONNECTING) {
        taskEXIT_CRITICAL();
        return -ENOTCONN;
    }
    if (reliable && len + 1 > hid_ctx.intr_chan.tx.mtu) {
        taskEXIT_CRITICAL();
        return -EMSGSIZE;
    }
    uint8_t next_tail = (app_tx_tail + 1) % APP_TX_SLOTS;

    /* DS5Dongle rejects the new packet when full, preserving queued order. */
    if (next_tail == app_tx_head) {
        taskEXIT_CRITICAL();
        return -ENOBUFS;
    }

    uint8_t slot = app_tx_tail;
    app_tx_buf[slot][0] = HID_HEADER(HID_TRANS_DATA, HID_REPORT_OUTPUT);
    memcpy(app_tx_buf[slot] + 1, data, len);
    app_tx_len[slot] = len + 1;
    app_tx_reliable[slot] = reliable;
    app_tx_tail = next_tail;
    taskEXIT_CRITICAL();

    if (!first_output_logged) {
        first_output_logged = true;
        LOG_INF("[HID] First output report enqueued, len=%d\n", (int)len);
    }

    /* Trigger drain if channel is idle (CAN_SEND_NOW equivalent) */
    hid_tx_drain_one();

    return 0;
}

/* Success transfers retry ownership to the queue, not a controller ACK. */
int bt_hid_host_send_output_reliable_session(const uint8_t *data, uint16_t len, uint32_t session)
{
    return send_output_session(data, len, session, true);
}
int bt_hid_host_send_output_session(const uint8_t *data, uint16_t len, uint32_t session)
{
    return send_output_session(data, len, session, false);
}

uint32_t bt_hid_host_session(void)
{
    return app_tx_generation;
}

int bt_hid_host_send_output(const uint8_t *data, uint16_t len)
{
    return bt_hid_host_send_output_session(data, len, app_tx_generation);
}

int bt_hid_host_set_feature_session(uint8_t report_id, const uint8_t *data,
                                     uint16_t len, uint32_t generation)
{
    if (!hid_ctx.ctrl_connected || !hid_ctx.conn)
        return -1;

    struct net_buf *buf = net_buf_alloc(&hid_tx_pool, K_NO_WAIT);
    if (!buf)
        return -1;

    net_buf_reserve(buf, BT_L2CAP_CHAN_SEND_RESERVE);
    net_buf_add_u8(buf, HID_HEADER(HID_TRANS_SET_REPORT, HID_REPORT_FEATURE));
    net_buf_add_u8(buf, report_id);
    if (data && len > 0)
        net_buf_add_mem(buf, data, len);

    int err = hid_send_buf(&hid_ctx.ctrl_chan, buf, NULL, generation);
    if (err < 0) {
        return err;
    }

    return 0;
}

int bt_hid_host_set_feature(uint8_t report_id, const uint8_t *data, uint16_t len)
{
    return bt_hid_host_set_feature_session(report_id, data, len, app_tx_generation);
}

int bt_hid_host_get_feature_session(uint8_t report_id, uint32_t generation)
{
    if (!hid_ctx.ctrl_connected || !hid_ctx.conn)
        return -1;

    struct net_buf *buf = net_buf_alloc(&hid_tx_pool, K_NO_WAIT);
    if (!buf)
        return -1;

    net_buf_reserve(buf, BT_L2CAP_CHAN_SEND_RESERVE);
    net_buf_add_u8(buf, HID_HEADER(HID_TRANS_GET_REPORT, HID_REPORT_FEATURE));
    net_buf_add_u8(buf, report_id);

    int err = hid_send_buf(&hid_ctx.ctrl_chan, buf, NULL, generation);
    if (err < 0) {
        return err;
    }

    LOG_DBG("[HID] GET_REPORT(Feature, 0x%02x) sent\n", report_id);
    return 0;
}

int bt_hid_host_get_feature(uint8_t report_id)
{
    return bt_hid_host_get_feature_session(report_id, app_tx_generation);
}

void bt_hid_host_refresh_feature(uint8_t report_id)
{
    taskENTER_CRITICAL();
    /* Do not mix host GETs into the initial type-detection batch. A host
     * read during handshake can retry after CONNECTED. */
    if (hid_ctx.state == BT_HID_STATE_CONNECTED && hid_ctx.ctrl_connected)
        feature_refresh_pending[report_id >> 3] |= 1u << (report_id & 7);
    taskEXIT_CRITICAL();
}

static void feature_refresh_poll(void)
{
    int report_id = -1;
    taskENTER_CRITICAL();
    uint32_t generation = app_tx_generation;
    if (hid_ctx.state == BT_HID_STATE_CONNECTED && hid_ctx.ctrl_connected) {
        for (unsigned i = 0; i < 256; ++i) {
            if (feature_refresh_pending[i >> 3] & (1u << (i & 7))) {
                feature_refresh_pending[i >> 3] &= ~(1u << (i & 7));
                report_id = (int)i;
                break;
            }
        }
    }
    taskEXIT_CRITICAL();
    if (report_id < 0)
        return;
    /* A failed submission has no response: keep it for a later task poll.
     * Requests for other IDs, or a repeated ISR request, remain independent. */
    if (bt_hid_host_get_feature_session((uint8_t)report_id, generation)) {
        taskENTER_CRITICAL();
        if (generation == app_tx_generation && hid_ctx.state == BT_HID_STATE_CONNECTED)
            feature_refresh_pending[report_id >> 3] |= 1u << (report_id & 7);
        taskEXIT_CRITICAL();
    }
}

enum bt_hid_host_state bt_hid_host_get_state(void)
{
    return hid_ctx.state;
}

bool bt_hid_host_get_connected_addr(uint8_t *addr)
{
    if (!hid_ctx.conn)
        return false;

    const bt_addr_t *remote = conn_get_br_addr(hid_ctx.conn);
    if (!remote)
        return false;

    memcpy(addr, remote->val, 6);
    return true;
}

/* ---------- bonding persistence ---------- */

int bt_hid_host_try_reconnect(void)
{
    if (hid_ctx.state != BT_HID_STATE_IDLE)
        return -1;

    bonded_list_refresh();
    bt_addr_t selected;
    if (!bonded_list_copy_active(&selected)) {
        hid_ctx.has_target = false;
        LOG_INF("[BT] No bonded BR/EDR device found\n");
        return -1;
    }

    const bt_addr_t *addr = &selected;
    char addr_str[BT_ADDR_STR_LEN];
    bt_addr_to_str(addr, addr_str, sizeof(addr_str));

    bt_addr_copy(&hid_ctx.target_addr, addr);
    hid_ctx.has_target = true;

    bt_br_set_connectable(true);
    bt_br_set_discoverable(true);

    /* DS5Dongle approach: don't actively page the controller.
     * Stay connectable and let the controller reconnect to us.
     * The controller will create ACL + L2CAP channels. */
    LOG_INF("[BT] Bonded device [%d/%d] %s — waiting for controller to reconnect "
           "(page scan enabled)\n", active_idx, bonded_count, addr_str);
    return 0;
}

int bt_hid_host_switch_next(void)
{
    bonded_list_refresh();
    taskENTER_CRITICAL();
    if (bonded_count <= 1) {
        taskEXIT_CRITICAL();
        LOG_INF("[BT] switch_next: only %d bonded, nothing to switch\n",
               bonded_count);
        return -1;
    }

    pairing_addr_pending = false; /* Explicit user selection supersedes pairing. */
    bonded_list_select_index((active_idx + 1) % bonded_count);
    bt_addr_t selected = active_addr;
    hid_ctx.selection_addr = selected;
    hid_ctx.selection_only = true;
    taskEXIT_CRITICAL();
    bonded_list_persist();

    const bt_addr_t *addr = &selected;
    bt_addr_copy(&hid_ctx.target_addr, addr);
    hid_ctx.has_target = true;

    char addr_str[BT_ADDR_STR_LEN];
    bt_addr_to_str(addr, addr_str, sizeof(addr_str));

    /* Inquiry Cancel can yield to an incoming ACL. Recheck ownership AFTER it. */
    discovery_stop();
    if (hid_ctx.conn) {
        switch_pending = true;

        LOG_INF("[BT] Switching: disconnect current → wait ACL teardown → "
               "reconnect [%d/%d] %s\n", active_idx, bonded_count, addr_str);
        bt_hid_host_disconnect();
        /* The disconnect completion path will handle full cleanup and
         * enable connectable only after old ACL is fully torn down. */
    } else {
        hid_ctx.ctrl_accepting = false;
        hid_ctx.intr_accepting = false;
        hid_ctx.outgoing = false;
        hid_ctx.is_dse = false;
        hid_ctx.check_dse = false;
        hid_ctx.security_pending = false;
        hid_ctx.security_done = false;
        hid_ctx.sdp_pending = false;
        hid_ctx.connect_pending = false;
        hid_ctx.connect_pending_tick = 0;
        hid_ctx.connect_retry_count = 0;
        memset(&hid_ctx.handshake, 0, sizeof(hid_ctx.handshake));

        app_tx_fifo_reset();
        feature_cache_clear();
        dse_reset();
        cached_rssi = 1;

        bt_br_set_connectable(true);
        bt_br_set_discoverable(true);

        LOG_INF("[BT] Switched to controller [%d/%d] %s — waiting for reconnect\n",
               active_idx, bonded_count, addr_str);
        set_state(BT_HID_STATE_IDLE);
    }

    return (int)active_idx;
}

bool bt_hid_host_is_switching(void)
{
    return switch_pending;
}

uint8_t bt_hid_host_get_bonded_count(void)
{
    bonded_list_refresh();
    return bonded_count;
}

uint8_t bt_hid_host_get_active_idx(void)
{
    return active_idx;
}

static void clear_bond_storage(void)
{
    bt_keys_link_key_clear_addr(NULL);
    blacklist_clear();
    blacklist_persist();
    pairing_addr_pending = false;
    bonded_list_refresh();
    active_addr_dirty = true;
    bonded_list_persist();
    hid_ctx.has_target = false;
    hid_ctx.selection_only = false;
    memset(&hid_ctx.target_addr, 0, sizeof(hid_ctx.target_addr));
    clear_bonds_pending = false;
    LOG_INF("[BT] All link keys, blacklist and active selection cleared\n");
}

void bt_hid_host_clear_bonds(void)
{
    discovery_stop(); /* If it fails, IDLE prevents discovery from paging a peer. */
    hid_ctx.connect_pending = false;
    hid_ctx.connect_pending_tick = 0;
    hid_ctx.connect_retry_count = 0;
    clear_bonds_pending = true;
    if (hid_ctx.conn) {
        /* The SDK can still write a link key while the ACL is alive. */
        bt_hid_host_disconnect();
        return;
    }
    clear_bond_storage();
    set_state(BT_HID_STATE_IDLE);
    bt_br_set_connectable(true);
    bt_br_set_discoverable(true);
}

bool bt_hid_host_copy_feature(uint8_t report_id, uint8_t *data,
                               uint16_t capacity, uint16_t *len)
{
    uintptr_t irq = bflb_irq_save();
    *len = 0;
    for (int i = 0; i < FEATURE_CACHE_SLOTS; i++) {
        if (feature_cache[i].report_id == report_id && feature_cache[i].len > 0) {
            uint16_t n = feature_cache[i].len;
            if (n > capacity) n = capacity;
            memcpy(data, feature_cache[i].data, n);
            *len = n;
            bflb_irq_restore(irq);
            return true;
        }
    }
    bflb_irq_restore(irq);
    return false;
}

bool bt_hid_host_is_dse(void)
{
    return hid_ctx.is_dse;
}

int bt_hid_host_set_feature_crc_guarded(uint8_t report_id, const uint8_t *data,
        uint16_t len, uint32_t generation, bool (*allow)(uint32_t), uint32_t token)
{
    if (generation != app_tx_generation)
        return -ENOTCONN;
    if (!hid_ctx.ctrl_connected || !hid_ctx.conn) {
        LOG_WRN("[HID] SET_FEATURE_CRC(0x%02x): not connected (ctrl=%d conn=%p)\n",
               report_id, hid_ctx.ctrl_connected, hid_ctx.conn);
        return -1;
    }

    struct net_buf *buf = net_buf_alloc(&hid_tx_pool, K_NO_WAIT);
    if (!buf) {
        LOG_ERR("[HID] SET_FEATURE_CRC(0x%02x): net_buf alloc failed\n", report_id);
        return -1;
    }

    net_buf_reserve(buf, BT_L2CAP_CHAN_SEND_RESERVE);
    net_buf_add_u8(buf, HID_HEADER(HID_TRANS_SET_REPORT, HID_REPORT_FEATURE));
    net_buf_add_u8(buf, report_id);
    if (data && len > 0)
        net_buf_add_mem(buf, data, len);

    /* Overwrite last 4 bytes of data with CRC (matching DS5Dongle's
     * fill_feature_report_checksum). The HID descriptor's Report Count
     * includes 4 bytes of CRC space that the USB host sends as zeros. */
    if (len >= 4) {
        uint8_t *crc_region = buf->data + 1;
        uint16_t crc_data_len = 1 + (len - 4);
        uint32_t crc = ds5_crc32(DS5_BT_FEATURE_CRC_SEED,
                                  crc_region, crc_data_len);
        ds5_write_le32(crc_region + crc_data_len, crc);
    }

    /* Recheck revocable work AFTER allocation/formatting. No IRQ lock is held
     * across SDK calls. Once transport accepts a command it cannot be recalled. */
    if (allow && !allow(token)) {
        net_buf_unref(buf);
        return -ECANCELED;
    }

    int err = hid_send_buf(&hid_ctx.ctrl_chan, buf, NULL, generation);
    if (err < 0) {
        LOG_ERR("[HID] SET_FEATURE_CRC(0x%02x): l2cap send failed err=%d\n",
               report_id, err);
        return err;
    }

    LOG_INF("[HID] SET_REPORT(Feature, 0x%02x) sent with CRC, len=%d\n",
           report_id, (int)(len + 2));
    return 0;
}

int bt_hid_host_set_feature_crc_session(uint8_t report_id, const uint8_t *data,
                                        uint16_t len, uint32_t generation)
{
    return bt_hid_host_set_feature_crc_guarded(report_id, data, len, generation, NULL, 0);
}

int bt_hid_host_set_feature_crc(uint8_t report_id, const uint8_t *data, uint16_t len)
{
    return bt_hid_host_set_feature_crc_session(report_id, data, len, app_tx_generation);
}

void bt_hid_host_persist_if_dirty(void)
{
    if (bonded_refresh_pending || pairing_addr_pending)
        bonded_list_refresh();
    bonded_list_persist();
    blacklist_persist();
}

void bt_hid_host_handshake_tick(void)
{
    handshake_send_pending();
    handshake_finish_detection(); /* RX only records detection; SDK work stays in BT task. */
}

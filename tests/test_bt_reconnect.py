"""Exercise passive reconnect and discovery paths from the production handlers."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "src/bt_hid_host.c").read_text()
MAIN = (ROOT / "src/main.c").read_text()
HEADER = (ROOT / "src/bt_hid_host.h").read_text()


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
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t TickType_t;
typedef uint8_t bt_security_t;
typedef struct { uint8_t val[6]; } bt_addr_t;
struct bt_br_discovery_result { bt_addr_t addr; uint8_t cod[3]; };
struct bt_br_discovery_param { uint8_t length; bool limited; };
#define BT_MAX_DISCOVERED 8
static struct bt_br_discovery_result discovery_results[BT_MAX_DISCOVERED];
struct bt_conn { unsigned refs, handle; bt_addr_t addr; } current, other;
struct net_buf { int unused; };
#define BT_L2CAP_DISCONNECTED 0
#define BFLB_BREDR_PATCH_CLEAR_L2CAP_BR_STALE_CID
#define BFLB_BLE_PATCH_FREE_ALLOCATED_BUFFER_IN_OS
struct bt_l2cap_chan {
    struct bt_conn *conn; void *ops; unsigned state, psm;
    struct { struct { struct { void *hdl; } timer; } timer; } rtx_work;
};
struct bt_l2cap_br_chan { struct bt_l2cap_chan chan; struct { unsigned mtu, cid; } rx; struct { unsigned cid; } tx; };
struct bt_l2cap_server { unsigned psm, sec_level; int (*accept)(struct bt_conn *, struct bt_l2cap_chan **); };
enum bt_security_err { BT_SECURITY_ERR_SUCCESS };
typedef void (*bt_hid_input_cb_t)(void);
typedef void (*bt_hid_state_cb_t)(void);
typedef void (*bt_hid_primer_cb_t)(void);
#define LOG_INF(...) ((void)0)
#define LOG_WRN(...) ((void)0)
#define LOG_ERR(...) ((void)0)
#define LOG_DBG(...) ((void)0)
#define BT_SECURITY_L2 2
#define BT_HCI_ERR_REMOTE_USER_TERM_CONN 0x13
#define BT_ADDR_STR_LEN 18
#define HID_PSM_CONTROL 0x11
#define HID_PSM_INTERRUPT 0x13
#define L2CAP_BR_MTU 672
#define BT_BR_CONN_PARAM_DEFAULT NULL
#define pdMS_TO_TICKS(v) (v)
static unsigned critical_depth;
#define taskENTER_CRITICAL() (++critical_depth)
#define taskEXIT_CRITICAL() do { assert(critical_depth); --critical_depth; } while (0)
'''
harness += extract(HEADER, "enum bt_hid_host_state {") + ";\n"
harness += next(line for line in SOURCE.splitlines() if line.startswith("#define BT_INQUIRY_LEN")) + "\n"
start = SOURCE.index("static struct {\n    volatile enum bt_hid_host_state state;")
end = SOURCE.index("} hid_ctx;", start) + len("} hid_ctx;")
harness += SOURCE[start:end]
harness += r'''
static TickType_t tick;
static int ctrl_ops, intr_ops;
static bool first_intr_logged, first_output_logged, switch_pending;
static bool bonded_refresh_pending, pairing_addr_pending;
static unsigned pages, channel_count, channels[8], disconnects, scan_calls, delays;
static unsigned fifo_resets, cache_resets, dse_resets;
static int disconnect_error;
static bool clear_bonds_pending;
static void clear_bond_storage(void) { clear_bonds_pending=false; }
static bool connectable, discoverable;
static int reconnect_result;
static bool fail_create;
static int stop_error, scan_error, channel_error;
static bool incoming_during_stop, complete_during_stop, incoming_during_start;
static unsigned stops, handshakes;
static bool blacklist_dirty;
static void conn_connected(struct bt_conn *conn, uint8_t err);
static void discovery_complete(struct bt_br_discovery_result *results, size_t count);
static void handshake_begin(void) { ++handshakes; hid_ctx.state=BT_HID_STATE_HANDSHAKE; }
static bool blacklist_contains(const bt_addr_t *addr) { return false; }
static void blacklist_remove(const bt_addr_t *addr) {}
#define BT_OP(ogf, ocf) (ocf)
#define BT_OGF_BASEBAND 3
#define BT_HCI_ERR_CONN_LIMIT_EXCEEDED 9
#define BT_HCI_ERR_AUTH_FAIL 5
#define BT_HCI_OP_AUTH_REQUESTED 0x0411
static int security_error, security_calls, auth_error;
static bool auth_buffer_available;
static int bt_conn_set_security(struct bt_conn *conn, int level) {
    assert(conn==hid_ctx.conn && level==BT_SECURITY_L2); ++security_calls; return security_error;
}
static struct net_buf *bt_hci_cmd_create(unsigned op, unsigned size) { static struct net_buf buf; return auth_buffer_available ? &buf : NULL; }
static void net_buf_add_le16(struct net_buf *buf, unsigned value) {}
static int bt_hci_cmd_send_sync(unsigned op, struct net_buf *buf, void *rsp) { return auth_error; }
static TickType_t xTaskGetTickCount(void) { return tick; }
static void set_state(enum bt_hid_host_state state) { hid_ctx.state=state; }
static struct bt_conn *bt_conn_ref(struct bt_conn *conn) { ++conn->refs; return conn; }
static void bt_conn_unref(struct bt_conn *conn) { assert(conn->refs>1); --conn->refs; }
static const bt_addr_t *conn_get_br_addr(const struct bt_conn *conn) { return &conn->addr; }
static void bt_addr_to_str(const bt_addr_t *addr, char *out, unsigned len) { out[0]=0; }
static int bt_addr_cmp(const bt_addr_t *a, const bt_addr_t *b) { return memcmp(a,b,sizeof(*a)); }
static void bt_addr_copy(bt_addr_t *a, const bt_addr_t *b) { *a=*b; }
static int bt_br_discovery_start(const struct bt_br_discovery_param *param,
    struct bt_br_discovery_result *results, size_t count,
    void (*complete)(struct bt_br_discovery_result *, size_t)) {
    assert(param->length==30 && !param->limited); /* 38.4 s, DS5Dongle's HCI length. */
    assert(results==discovery_results && count==BT_MAX_DISCOVERED && complete);
    ++scan_calls;
    if (incoming_during_start) { incoming_during_start=false; conn_connected(&current,0); }
    return scan_error;
}
static int bt_br_discovery_stop(void) {
    ++stops;
    if (incoming_during_stop) { incoming_during_stop=false; conn_connected(&current,0); }
    if (complete_during_stop) { complete_during_stop=false; discovery_complete(discovery_results,1); }
    return stop_error;
}
static int scan_mode_error, scan_mode_calls;
static void (*during_scan)(void);
static int bt_br_set_connectable(bool enabled) {
    ++scan_mode_calls;
    if(during_scan) { void (*fn)(void)=during_scan; during_scan=NULL; fn(); }
    if(scan_mode_error) return scan_mode_error;
    if(connectable==enabled) return -EALREADY;
    connectable=enabled; return 0;
}
static int bt_br_set_discoverable(bool enabled) {
    if(scan_mode_error) return scan_mode_error;
    if(discoverable==enabled) return -EALREADY;
    discoverable=enabled; return 0;
}
static struct bt_conn *bt_conn_create_br(const bt_addr_t *addr, void *params) { ++pages; return fail_create ? NULL : &current; }
static int bt_conn_disconnect(struct bt_conn *conn, uint8_t reason) { ++disconnects; return disconnect_error; }
static int bt_l2cap_chan_connect(struct bt_conn *conn, struct bt_l2cap_chan *chan, unsigned psm) {
    assert(conn==hid_ctx.conn && channel_count<8); channels[channel_count++]=psm; return channel_error;
}
static void clear_rejected_key(struct bt_conn *conn, enum bt_security_err err) {}
static void bonded_list_refresh(void) {}
static void app_tx_fifo_reset(void) { ++fifo_resets; }
static void feature_cache_clear(void) { ++cache_resets; }
static void dse_reset(void) { ++dse_resets; }
static int l2cap_server_reject_blacklisted(struct bt_conn *conn) { return 0; }
static int bt_hid_host_try_reconnect(void) { return reconnect_result; }
static void vTaskDelay(TickType_t delay) { ++delays; tick+=delay; }
void bt_hid_host_disconnect(void);
'''
harness += SOURCE[SOURCE.index('static struct {\n    bool desired, dirty, attempted;'):SOURCE.index('/* ---------- feature report cache')]
for signature in (
    "static bool selection_accepts(",
    "static void handshake_maybe_begin(",
    "static bool discovery_is_peripheral(",
    "static bool discovery_stop(",
    "static void discovery_complete(",
    "int bt_hid_host_scan_start(",
    "bool bt_hid_host_poll_scan_early(",
    "static void security_changed(",
    "static void conn_connected(",
    "static void l2cap_server_ensure_conn(",
    "static int l2cap_ctrl_server_accept(",
    "static int l2cap_intr_server_accept(",
    "static void l2cap_ctrl_connected(",
    "static void l2cap_intr_connected(",
    "bool bt_hid_host_poll_security(",
    "bool bt_hid_host_poll_sdp(",
    "int bt_hid_host_connect(",
    "bool bt_hid_host_poll_connect(",
    "void bt_hid_host_disconnect(",
    "void bt_hid_host_poll_control(",
    "static void conn_disconnected(",
    "static bool hid_channel_released(",
    "bool bt_hid_host_poll_disconnect(",
    "static void l2cap_ctrl_disconnected(",
    "static void l2cap_intr_disconnected(",
):
    harness += "\n" + extract(SOURCE, signature) + "\n"
harness += SOURCE[SOURCE.index("static struct bt_l2cap_server l2cap_hid_ctrl_server ="):
                  SOURCE.index("/* ---------- SDP Service Record")]
start = MAIN.index('    LOG_INF("[BT_TASK] Startup inquiry')
end = MAIN.index('    LOG_INF("[BT_TASK] Entering main loop', start)
harness += "\nstatic void startup(void) { int err=0;\n" + MAIN[start:end] + "\n}\n"
# The main loop must not invoke a hidden reverse-connect/periodic-inquiry path.
assert "bt_hid_host_poll_incoming_l2cap_fallback" not in MAIN
assert "bt_hid_host_force_disconnect" not in MAIN
assert "bt_hid_host_drop_stale_conn" not in MAIN
assert "bt_hid_host_poll_disconnect();" in MAIN
idle = MAIN[MAIN.index("if (bt_hid_host_get_state() == BT_HID_STATE_HANDSHAKE)"):]
idle = idle[:idle.index("static bool should_use_dse")]
assert "bt_hid_host_scan_start" not in idle
harness += r'''
static void setup(void) {
    memset(&hid_ctx,0,sizeof(hid_ctx));
    memset(&scan_mode,0,sizeof(scan_mode)); scan_mode_error=scan_mode_calls=0; during_scan=NULL;
    current.refs=2; other.refs=1; hid_ctx.conn=&current;
    security_error=security_calls=auth_error=0; auth_buffer_available=false;
    hid_ctx.state=BT_HID_STATE_CONNECTING;
    pages=channel_count=disconnects=scan_calls=delays=0;
    fifo_resets=cache_resets=dse_resets=0; disconnect_error=0;
    connectable=discoverable=false; switch_pending=false; tick=100;
    fail_create=false;
    stop_error=scan_error=channel_error=0; stops=handshakes=0;
    incoming_during_stop=complete_during_stop=incoming_during_start=false;
}
static void discover_new_peer(bool early) {
    /* Preserve A as the remembered peer; B is a newly discovered gamepad. */
    hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE;
    hid_ctx.has_target=true; hid_ctx.target_addr.val[0]=1;
    assert(bt_hid_host_scan_start(0)==0);
    assert(!hid_ctx.has_target && hid_ctx.target_addr.val[0]==1);
    discovery_results[0].addr.val[0]=2;
    discovery_results[0].cod[0]=0x08; discovery_results[0].cod[1]=0x25;
    if (early) assert(bt_hid_host_poll_scan_early());
    else discovery_complete(discovery_results,1);
    assert(hid_ctx.connect_pending && hid_ctx.pending_addr.val[0]==2);
    assert(!hid_ctx.connect_retry_count && !hid_ctx.connect_pending_tick);
}
static void reverse_scan_request(void) { request_scan_mode(false); }
int main(void) {
    assert(l2cap_hid_ctrl_server.sec_level==BT_SECURITY_L2 &&
           l2cap_hid_intr_server.sec_level==BT_SECURITY_L2);
    /* One startup inquiry, with or without a remembered peer; no blocking wait. */
    for (unsigned saved=0; saved<2; ++saved) {
        setup(); hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE;
        hid_ctx.has_target=saved; startup();
        assert(scan_calls==1 && !delays && !pages && hid_ctx.discovery_active);
        discovery_complete(discovery_results,0);
        assert(hid_ctx.state==BT_HID_STATE_IDLE && !hid_ctx.discovery_active);
        assert(!bt_hid_host_poll_scan_early() && !bt_hid_host_poll_connect());
        assert(scan_calls==1); /* No repeated inquiry after an empty result. */
    }

    /* Peripheral without the extra Gamepad bit is now accepted by both paths. */
    for (unsigned early=0; early<2; ++early) {
        setup(); hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE;
        assert(bt_hid_host_scan_start(0)==0);
        discovery_results[0].addr.val[0]=2; discovery_results[0].cod[1]=0x05;
        if (early) assert(bt_hid_host_poll_scan_early());
        else discovery_complete(discovery_results,1);
        assert(hid_ctx.connect_pending && hid_ctx.pending_addr.val[0]==2);
    }
    setup(); hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE;
    assert(bt_hid_host_scan_start(0)==0);
    discovery_results[0].addr.val[0]=2; discovery_results[0].cod[1]=0x04;
    assert(!bt_hid_host_poll_scan_early() && !hid_ctx.connect_pending);
    discovery_complete(discovery_results,1);
    assert(!hid_ctx.connect_pending && hid_ctx.state==BT_HID_STATE_IDLE);

    /* A failed Inquiry Cancel must never start paging. */
    setup(); hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE;
    assert(bt_hid_host_scan_start(0)==0);
    discovery_results[0].addr.val[0]=2; discovery_results[0].cod[1]=0x05;
    stop_error=-EIO;
    assert(!bt_hid_host_poll_scan_early() && hid_ctx.discovery_active);
    assert(!bt_hid_host_poll_connect() && !pages);
    uint8_t fresh_addr[6]={2};
    assert(bt_hid_host_connect(fresh_addr)==-EBUSY && !pages);
    stop_error=0; assert(bt_hid_host_poll_scan_early());
    assert(!hid_ctx.discovery_active && bt_hid_host_poll_connect() && pages==1);

    /* Incoming ACL during the blocking cancel wins; do not relabel it outgoing. */
    setup(); hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE;
    assert(bt_hid_host_scan_start(0)==0);
    discovery_results[0].addr.val[0]=2; discovery_results[0].cod[1]=0x05;
    incoming_during_stop=true;
    assert(!bt_hid_host_poll_scan_early() && hid_ctx.conn==&current);
    assert(!hid_ctx.outgoing && !hid_ctx.connect_pending && hid_ctx.security_pending);
    assert(!bt_hid_host_poll_connect() && !pages);

    /* Natural inquiry completion can win the same cancel race. */
    setup(); hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE;
    assert(bt_hid_host_scan_start(0)==0);
    discovery_results[0].addr.val[0]=2; discovery_results[0].cod[1]=0x05;
    complete_during_stop=true; stop_error=-EALREADY;
    assert(!bt_hid_host_poll_scan_early() && hid_ctx.connect_pending);
    assert(bt_hid_host_poll_connect() && pages==1);

    /* Startup can also race incoming ACL; stop the inquiry from the task. */
    setup(); hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE;
    incoming_during_start=true; startup();
    assert(hid_ctx.conn==&current && hid_ctx.discovery_active && !stops);
    assert(!bt_hid_host_poll_scan_early() && stops==1 && !hid_ctx.discovery_active);
    assert(!pages && hid_ctx.security_pending);
    discovery_complete(discovery_results,0);
    assert(hid_ctx.state==BT_HID_STATE_CONNECTING); /* Late callback cannot set IDLE. */

    /* Every ordering of security, Control and Interrupt becomes ready once. */
    const unsigned order[6][3]={{0,1,2},{0,2,1},{1,0,2},{1,2,0},{2,0,1},{2,1,0}};
    for (unsigned row=0; row<6; ++row) {
        setup();
        hid_ctx.ctrl_chan.chan.conn=hid_ctx.intr_chan.chan.conn=&current;
        for (unsigned col=0; col<3; ++col) {
            switch (order[row][col]) {
            case 0: security_changed(&current,BT_SECURITY_L2,0); break;
            case 1: l2cap_ctrl_connected(&hid_ctx.ctrl_chan.chan); break;
            case 2: l2cap_intr_connected(&hid_ctx.intr_chan.chan); break;
            }
            assert(handshakes==(col==2) && !channel_count);
        }
        security_changed(&current,BT_SECURITY_L2,0);
        l2cap_ctrl_connected(&hid_ctx.ctrl_chan.chan);
        l2cap_intr_connected(&hid_ctx.intr_chan.chan);
        assert(handshakes==1 && hid_ctx.state==BT_HID_STATE_HANDSHAKE);
    }

    /* Local HID create failure follows the normal disconnect event path. */
    setup(); hid_ctx.outgoing=true; channel_error=-ENOBUFS;
    security_changed(&current,BT_SECURITY_L2,0);
    assert(bt_hid_host_poll_sdp() && channel_count==1);
    bt_hid_host_poll_control(); assert(disconnects==1);
    assert(hid_ctx.state==BT_HID_STATE_DISCONNECTING && !hid_ctx.ctrl_accepting);
    assert(hid_ctx.conn==&current && !handshakes && !fifo_resets);
    setup(); hid_ctx.outgoing=true; hid_ctx.security_done=true; channel_error=-ENOBUFS;
    hid_ctx.ctrl_chan.chan.conn=&current;
    l2cap_ctrl_connected(&hid_ctx.ctrl_chan.chan);
    bt_hid_host_poll_control();
    assert(disconnects==1 && !hid_ctx.intr_accepting && !handshakes);

    /* Preserve a peer-opened Interrupt when the outgoing Control is deferred. */
    setup(); hid_ctx.outgoing=true;
    hid_ctx.intr_connected=true; hid_ctx.intr_chan.chan.conn=&current;
    hid_ctx.intr_chan.tx.cid=0x42;
    security_changed(&current,BT_SECURITY_L2,0);
    assert(bt_hid_host_poll_sdp() && channel_count==1);
    assert(hid_ctx.intr_chan.chan.conn==&current && hid_ctx.intr_chan.tx.cid==0x42);
    hid_ctx.ctrl_chan.chan.conn=&current;
    l2cap_ctrl_connected(&hid_ctx.ctrl_chan.chan);
    assert(handshakes==1 && channel_count==1);

    setup(); hid_ctx.outgoing=true; hid_ctx.security_done=true;
    hid_ctx.intr_chan.chan.conn=&current; /* SDK still owns an unready channel. */
    hid_ctx.ctrl_chan.chan.conn=&current;
    l2cap_ctrl_connected(&hid_ctx.ctrl_chan.chan);
    assert(!channel_count && !handshakes);

    setup(); hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE; scan_error=-EIO;
    startup(); assert(!hid_ctx.discovery_active && hid_ctx.state==BT_HID_STATE_IDLE);
    assert(!bt_hid_host_poll_connect() && !pages);

    /* Both inquiry paths must allow B after A was saved, without clearing A. */
    setup(); discover_new_peer(true);
    assert(bt_hid_host_poll_connect() && pages==1);
    setup(); discover_new_peer(false);
    assert(bt_hid_host_poll_connect() && pages==1);

    /* Refused scans do not alter the active connection/pending attempt. */
    setup(); hid_ctx.has_target=true; hid_ctx.connect_retry_count=1;
    assert(bt_hid_host_scan_start(0)<0 && hid_ctx.has_target && !scan_calls);
    hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE; hid_ctx.connect_pending=true;
    assert(bt_hid_host_scan_start(0)<0 && hid_ctx.has_target);
    assert(hid_ctx.connect_retry_count==1);

    /* Failed submission followed by success cannot shorten the NEXT budget. */
    setup(); discover_new_peer(true); fail_create=true;
    assert(bt_hid_host_poll_connect() && pages==1 && hid_ctx.connect_pending);
    tick+=499; assert(!bt_hid_host_poll_connect() && pages==1);
    tick++; fail_create=false;
    assert(bt_hid_host_poll_connect() && pages==2 && !hid_ctx.connect_pending);
    assert(!hid_ctx.connect_retry_count && !hid_ctx.connect_pending_tick);
    discover_new_peer(false); fail_create=true;
    unsigned before=pages;
    for (unsigned n=1; n<=3; ++n) {
        assert(bt_hid_host_poll_connect() && pages==before+n);
        assert(hid_ctx.connect_pending==(n<3));
        tick+=500;
    }
    assert(!hid_ctx.connect_retry_count && !hid_ctx.connect_pending_tick);

    /* Incoming ACL or another advanced state cancels the whole retry session. */
    setup(); discover_new_peer(true); fail_create=true;
    assert(bt_hid_host_poll_connect()); hid_ctx.conn=&current;
    assert(!bt_hid_host_poll_connect() && !hid_ctx.connect_pending);
    assert(!hid_ctx.connect_retry_count && !hid_ctx.connect_pending_tick);
    discover_new_peer(false);
    assert(bt_hid_host_poll_connect()); tick+=500;
    hid_ctx.state=BT_HID_STATE_HANDSHAKE;
    assert(!bt_hid_host_poll_connect() && !hid_ctx.connect_pending);
    assert(!hid_ctx.connect_retry_count && !hid_ctx.connect_pending_tick);

    /* Incoming reconnect: authenticate, accept Control, then Interrupt.
     * No locally created HID channel, including a stale SDP work item. */
    setup(); security_changed(&current,BT_SECURITY_L2,0);
    assert(hid_ctx.security_done && !hid_ctx.outgoing && !hid_ctx.sdp_pending);
    hid_ctx.sdp_pending=true;
    assert(!bt_hid_host_poll_sdp() && !channel_count && !hid_ctx.sdp_pending);
    struct bt_l2cap_chan *chan=NULL;
    assert(l2cap_ctrl_server_accept(&current,&chan)==0);
    assert(chan==&hid_ctx.ctrl_chan.chan);
    chan->conn=&current; l2cap_ctrl_connected(chan);
    assert(hid_ctx.ctrl_connected && !channel_count && !hid_ctx.outgoing);
    tick+=4000; assert(!bt_hid_host_poll_sdp() && !bt_hid_host_poll_connect());
    bt_hid_host_poll_control();
    assert(!pages && !channel_count && !disconnects);
    assert(l2cap_intr_server_accept(&current,&chan)==0);
    assert(chan==&hid_ctx.intr_chan.chan && hid_ctx.intr_accepting);
    assert(l2cap_intr_server_accept(&other,&chan)==-EACCES);

    /* A discovery race must not relabel an incoming ACL as outgoing. */
    uint8_t addr[6]={1,2,3,4,5,6};
    assert(bt_hid_host_connect(addr)==-EBUSY && !hid_ctx.outgoing);
    hid_ctx.connect_pending=true;
    assert(!bt_hid_host_poll_connect() && !hid_ctx.connect_pending && !pages);
    hid_ctx.state=BT_HID_STATE_IDLE; hid_ctx.connect_pending=true;
    assert(!bt_hid_host_poll_connect() && !pages && !hid_ctx.outgoing);

    /* Requesting disconnect never releases the ACL or clears channel storage.
     * HCI-first ordering must wait for the SDK to detach BOTH channels. */
    setup(); hid_ctx.ctrl_connected=hid_ctx.intr_connected=true;
    hid_ctx.ctrl_chan.chan.conn=hid_ctx.intr_chan.chan.conn=&current;
    bt_hid_host_disconnect(); bt_hid_host_disconnect();
    bt_hid_host_poll_control();
    assert(disconnects==1 && current.refs==2 && hid_ctx.conn==&current);
    bt_hid_host_poll_control();
    assert(!fifo_resets && !cache_resets && !dse_resets && !connectable);
    assert(!bt_hid_host_poll_disconnect());
    conn_disconnected(&current,0x13);
    assert(!bt_hid_host_poll_disconnect() && hid_ctx.conn==&current);
    l2cap_ctrl_disconnected(&hid_ctx.ctrl_chan.chan);
    hid_ctx.ctrl_chan.chan.conn=NULL; /* SDK does this after the callback. */
    assert(!bt_hid_host_poll_disconnect());
    l2cap_intr_disconnected(&hid_ctx.intr_chan.chan);
    assert(!bt_hid_host_poll_disconnect());
    hid_ctx.intr_chan.chan.conn=NULL;
    hid_ctx.intr_chan.chan.state=1;
    assert(!bt_hid_host_poll_disconnect());
    hid_ctx.intr_chan.chan.state=BT_L2CAP_DISCONNECTED;
    hid_ctx.intr_chan.chan.psm=HID_PSM_INTERRUPT;
    assert(!bt_hid_host_poll_disconnect());
    hid_ctx.intr_chan.chan.psm=0;
    hid_ctx.intr_chan.tx.cid=0x41;
    assert(!bt_hid_host_poll_disconnect());
    hid_ctx.intr_chan.tx.cid=0;
    hid_ctx.intr_chan.chan.rtx_work.timer.timer.hdl=&current;
    assert(!bt_hid_host_poll_disconnect());
    hid_ctx.intr_chan.chan.rtx_work.timer.timer.hdl=NULL;
    assert(bt_hid_host_poll_disconnect());
    assert(!hid_ctx.conn && hid_ctx.state==BT_HID_STATE_IDLE && current.refs==1);
    bt_hid_host_poll_control();
    assert(fifo_resets==1 && cache_resets==1 && dse_resets==1 && disconnects==1);
    bt_hid_host_poll_control();
    assert(connectable && discoverable && !hid_ctx.connect_pending);
    conn_disconnected(&current,0x13); assert(!bt_hid_host_poll_disconnect());
    assert(current.refs==1 && fifo_resets==1 && !bt_hid_host_poll_connect() && !pages);

    /* L2CAP-first: request ACL disconnect only after both channels close. */
    setup(); hid_ctx.ctrl_connected=hid_ctx.intr_connected=true;
    hid_ctx.ctrl_chan.chan.conn=hid_ctx.intr_chan.chan.conn=&current;
    l2cap_intr_disconnected(&hid_ctx.intr_chan.chan); hid_ctx.intr_chan.chan.conn=NULL;
    bt_hid_host_poll_control();
    assert(!disconnects && !bt_hid_host_poll_disconnect());
    l2cap_ctrl_disconnected(&hid_ctx.ctrl_chan.chan); hid_ctx.ctrl_chan.chan.conn=NULL;
    bt_hid_host_poll_control();
    assert(disconnects==1 && hid_ctx.conn==&current && !fifo_resets);
    assert(!bt_hid_host_poll_disconnect());
    conn_disconnected(&current,0x08); assert(bt_hid_host_poll_disconnect());
    bt_hid_host_poll_control();
    assert(fifo_resets==1 && connectable && discoverable && current.refs==1);

    /* A late event from a rejected/older ACL cannot tear down the owner. */
    setup(); conn_disconnected(&other,0x13);
    struct bt_l2cap_chan old_channel={.conn=&other};
    l2cap_ctrl_disconnected(&old_channel); l2cap_intr_disconnected(&old_channel);
    assert(hid_ctx.conn==&current && !hid_ctx.acl_disconnected && !fifo_resets);
    bt_hid_host_poll_control();
    assert(!bt_hid_host_poll_disconnect() && !disconnects && current.refs==2);

    /* Failed HCI submission keeps ownership and permits a later retry. */
    setup(); disconnect_error=-ENOBUFS; bt_hid_host_disconnect();
    assert(!disconnects); bt_hid_host_poll_control(); assert(disconnects==1);
    assert(hid_ctx.conn==&current && current.refs==2 && !fifo_resets);
    assert(hid_ctx.state==BT_HID_STATE_DISCONNECTING && hid_ctx.disconnect_requested);
    disconnect_error=0; tick+=100; bt_hid_host_disconnect();
    bt_hid_host_poll_control();
    assert(disconnects==2 && hid_ctx.state==BT_HID_STATE_DISCONNECTING);
    conn_disconnected(&current,0x13); assert(bt_hid_host_poll_disconnect());

    /* Explicit discovery still pages and opens Control -> Interrupt after auth. */
    setup(); hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE;
    assert(bt_hid_host_connect(addr)==0 && pages==1 && hid_ctx.outgoing);
    hid_ctx.conn=&current;
    hid_ctx.sdp_pending=true;
    assert(!bt_hid_host_poll_sdp() && !channel_count); /* Not encrypted yet. */
    security_changed(&current,BT_SECURITY_L2,0);
    assert(bt_hid_host_poll_sdp() && channel_count==1 && channels[0]==HID_PSM_CONTROL);
    assert(hid_ctx.ctrl_accepting);
    assert(l2cap_ctrl_server_accept(&current,&chan)==-ENOMEM);
    hid_ctx.ctrl_chan.chan.conn=&current;
    l2cap_ctrl_connected(&hid_ctx.ctrl_chan.chan);
    assert(channel_count==2 && channels[1]==HID_PSM_INTERRUPT);
    l2cap_ctrl_connected(&hid_ctx.ctrl_chan.chan);
    assert(channel_count==2); /* Duplicate callback cannot create another Interrupt. */
    hid_ctx.intr_chan.chan.conn=&current;
    l2cap_intr_connected(&hid_ctx.intr_chan.chan);
    assert(handshakes==1);
    assert(!critical_depth);
    /* A selected controller reserves both ACL and L2CAP admission. */
    setup(); hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE;
    current.addr.val[0]=1; other.addr.val[0]=2;
    hid_ctx.selection_only=true; hid_ctx.selection_addr=other.addr;
    conn_connected(&current,0);
    bt_hid_host_poll_control();
    assert(!hid_ctx.conn && disconnects==1 && hid_ctx.selection_only);
    struct bt_l2cap_chan *selected_channel=NULL;
    assert(l2cap_ctrl_server_accept(&current,&selected_channel)==-EACCES);
    assert(l2cap_intr_server_accept(&current,&selected_channel)==-EACCES);
    assert(!hid_ctx.ctrl_accepting && !hid_ctx.intr_accepting);
    conn_connected(&other,0); assert(hid_ctx.conn==&other && hid_ctx.selection_only);
    setup(); hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE;
    hid_ctx.selection_only=true; hid_ctx.selection_addr=other.addr;
    assert(bt_hid_host_scan_start(0)==0 && !hid_ctx.selection_only);
    conn_connected(&current,0); assert(hid_ctx.conn==&current);
    /* Failed page-scan restore is retried without active inquiry/paging. */
    setup(); scan_mode_error=-ENOBUFS;
    conn_disconnected(&current,0x13); assert(bt_hid_host_poll_disconnect());
    bt_hid_host_poll_control(); assert(!connectable && !discoverable && scan_mode.dirty);
    unsigned attempts=scan_mode_calls;
    bt_hid_host_poll_control(); assert(scan_mode_calls==attempts);
    scan_mode_error=0; tick+=100; bt_hid_host_poll_control();
    assert(connectable && discoverable && !scan_mode.dirty && !pages && !scan_calls);
    /* Paging must wait for both scan bits to be disabled successfully. */
    setup(); hid_ctx.conn=NULL; scan_mode_error=-ENOBUFS;
    assert(bt_hid_host_connect(addr)==-EAGAIN && !pages);
    scan_mode_error=0; tick+=100;
    assert(bt_hid_host_connect(addr)==0 && pages==1);
    /* A callback that changes desired mode during HCI cannot be lost. */
    setup(); request_scan_mode(true); during_scan=reverse_scan_request;
    assert(!poll_scan_mode() && scan_mode.dirty && !scan_mode.desired);
    assert(poll_scan_mode() && !connectable && !discoverable);
    /* Security submission failures are paced; accepted operations aren't replayed. */
    setup(); hid_ctx.security_pending=true; security_error=-ENOMEM;
    assert(bt_hid_host_poll_security() && security_calls==1 && hid_ctx.security_pending);
    assert(!bt_hid_host_poll_security() && security_calls==1);
    tick+=100; auth_buffer_available=true; auth_error=-ENOBUFS;
    assert(bt_hid_host_poll_security() && hid_ctx.security_pending);
    tick+=100; auth_error=0;
    assert(bt_hid_host_poll_security() && !hid_ctx.security_pending);
    assert(!bt_hid_host_poll_security() && current.refs==2);
    setup(); hid_ctx.security_pending=true; security_error=-EBUSY;
    assert(bt_hid_host_poll_security() && !hid_ctx.security_pending);
    setup(); hid_ctx.security_pending=true; security_error=-ENOMEM;
    for(unsigned i=0;i<5;i++) { assert(bt_hid_host_poll_security()); tick+=100; }
    assert(bt_hid_host_poll_security() && hid_ctx.disconnect_requested && !disconnects);
    bt_hid_host_poll_control(); assert(disconnects==1 && current.refs==2);
    puts("PASS: startup inquiry, Peripheral filtering, cancel barriers/races, isolated retries, ordered encrypted HID, passive reconnect and teardown");
}
'''
with tempfile.TemporaryDirectory(prefix="ds5-reconnect-") as directory:
    work = Path(directory)
    (work / "test.c").write_text(harness)
    subprocess.run(["cc", "-std=c11", "-fsanitize=address,undefined",
                    str(work / "test.c"), "-o", str(work / "test")], check=True)
    subprocess.run([str(work / "test")], check=True)

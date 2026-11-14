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
#define BT_INQUIRY_LEN 5
static struct bt_br_discovery_result discovery_results[BT_MAX_DISCOVERED];
struct bt_conn { unsigned refs; bt_addr_t addr; } current, other;
#define BT_L2CAP_DISCONNECTED 0
#define BFLB_BREDR_PATCH_CLEAR_L2CAP_BR_STALE_CID
#define BFLB_BLE_PATCH_FREE_ALLOCATED_BUFFER_IN_OS
struct bt_l2cap_chan {
    struct bt_conn *conn; void *ops; unsigned state, psm;
    struct { struct { struct { void *hdl; } timer; } timer; } rtx_work;
};
struct bt_l2cap_br_chan { struct bt_l2cap_chan chan; struct { unsigned mtu, cid; } rx; struct { unsigned cid; } tx; };
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
start = SOURCE.index("static struct {\n    volatile enum bt_hid_host_state state;")
end = SOURCE.index("} hid_ctx;", start) + len("} hid_ctx;")
harness += SOURCE[start:end]
harness += r'''
static TickType_t tick;
static int ctrl_ops, intr_ops, cached_rssi;
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
    assert(param->length==BT_INQUIRY_LEN && !param->limited);
    assert(results==discovery_results && count==BT_MAX_DISCOVERED && complete);
    ++scan_calls; return 0;
}
static int bt_br_discovery_stop(void) { return 0; }
static void bt_br_set_connectable(bool enabled) { connectable=enabled; }
static void bt_br_set_discoverable(bool enabled) { discoverable=enabled; }
static struct bt_conn *bt_conn_create_br(const bt_addr_t *addr, void *params) { ++pages; return fail_create ? NULL : &current; }
static int bt_conn_disconnect(struct bt_conn *conn, uint8_t reason) { ++disconnects; return disconnect_error; }
static int bt_l2cap_chan_connect(struct bt_conn *conn, struct bt_l2cap_chan *chan, unsigned psm) {
    assert(conn==hid_ctx.conn && channel_count<8); channels[channel_count++]=psm; return 0;
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
for signature in (
    "static void discovery_complete(",
    "int bt_hid_host_scan_start(",
    "bool bt_hid_host_poll_scan_early(",
    "static void security_changed(",
    "static void l2cap_server_ensure_conn(",
    "static int l2cap_ctrl_server_accept(",
    "static int l2cap_intr_server_accept(",
    "static void l2cap_ctrl_connected(",
    "bool bt_hid_host_poll_sdp(",
    "int bt_hid_host_connect(",
    "bool bt_hid_host_poll_connect(",
    "void bt_hid_host_disconnect(",
    "static void conn_disconnected(",
    "static bool hid_channel_released(",
    "bool bt_hid_host_poll_disconnect(",
    "static void l2cap_ctrl_disconnected(",
    "static void l2cap_intr_disconnected(",
):
    harness += "\n" + extract(SOURCE, signature) + "\n"
start = MAIN.index('    LOG_INF("[BT_TASK] try_reconnect...')
end = MAIN.index('    LOG_INF("[BT_TASK] Entering main loop', start)
harness += "\nstatic void startup(void) {\n" + MAIN[start:end] + "\n}\n"
# The main loop must not invoke a hidden reverse-connect/periodic-inquiry path.
assert "bt_hid_host_poll_incoming_l2cap_fallback" not in MAIN
assert "bt_hid_host_force_disconnect" not in MAIN
assert "bt_hid_host_drop_stale_conn" not in MAIN
assert "bt_hid_host_poll_disconnect();" in MAIN
idle = MAIN[MAIN.index("if (bt_hid_host_get_state() == BT_HID_STATE_HANDSHAKE)"):]
idle = idle[:idle.index("static bool has_user_activity")]
assert "bt_hid_host_scan_start" not in idle
harness += r'''
static void setup(void) {
    memset(&hid_ctx,0,sizeof(hid_ctx));
    current.refs=2; other.refs=1; hid_ctx.conn=&current;
    hid_ctx.state=BT_HID_STATE_CONNECTING;
    pages=channel_count=disconnects=scan_calls=delays=0;
    fifo_resets=cache_resets=dse_resets=0; disconnect_error=0;
    connectable=discoverable=false; switch_pending=false; tick=100;
    fail_create=false;
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
int main(void) {
    /* A saved peer must not block the task that performs auth/initialization. */
    setup(); reconnect_result=0; startup();
    assert(!delays && !scan_calls && !pages);
    reconnect_result=-1; hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE;
    startup(); assert(scan_calls==1 && !delays);

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
    assert(disconnects==1 && current.refs==2 && hid_ctx.conn==&current);
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
    assert(fifo_resets==1 && cache_resets==1 && dse_resets==1 && disconnects==1);
    assert(connectable && discoverable && !hid_ctx.connect_pending);
    conn_disconnected(&current,0x13); assert(!bt_hid_host_poll_disconnect());
    assert(current.refs==1 && fifo_resets==1 && !bt_hid_host_poll_connect() && !pages);

    /* L2CAP-first: request ACL disconnect only after both channels close. */
    setup(); hid_ctx.ctrl_connected=hid_ctx.intr_connected=true;
    hid_ctx.ctrl_chan.chan.conn=hid_ctx.intr_chan.chan.conn=&current;
    l2cap_intr_disconnected(&hid_ctx.intr_chan.chan); hid_ctx.intr_chan.chan.conn=NULL;
    assert(!disconnects && !bt_hid_host_poll_disconnect());
    l2cap_ctrl_disconnected(&hid_ctx.ctrl_chan.chan); hid_ctx.ctrl_chan.chan.conn=NULL;
    assert(disconnects==1 && hid_ctx.conn==&current && !fifo_resets);
    assert(!bt_hid_host_poll_disconnect());
    conn_disconnected(&current,0x08); assert(bt_hid_host_poll_disconnect());
    assert(fifo_resets==1 && connectable && discoverable && current.refs==1);

    /* A late event from a rejected/older ACL cannot tear down the owner. */
    setup(); conn_disconnected(&other,0x13);
    struct bt_l2cap_chan old_channel={.conn=&other};
    l2cap_ctrl_disconnected(&old_channel); l2cap_intr_disconnected(&old_channel);
    assert(hid_ctx.conn==&current && !hid_ctx.acl_disconnected && !fifo_resets);
    assert(!bt_hid_host_poll_disconnect() && !disconnects && current.refs==2);

    /* Failed HCI submission keeps ownership and permits a later retry. */
    setup(); disconnect_error=-ENOBUFS; bt_hid_host_disconnect();
    assert(hid_ctx.conn==&current && current.refs==2 && !fifo_resets);
    assert(hid_ctx.state==BT_HID_STATE_CONNECTING);
    disconnect_error=0; bt_hid_host_disconnect();
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
    hid_ctx.ctrl_chan.chan.conn=&current;
    l2cap_ctrl_connected(&hid_ctx.ctrl_chan.chan);
    assert(channel_count==2 && channels[1]==HID_PSM_INTERRUPT);
    assert(!critical_depth);
    puts("PASS: new-peer inquiry, isolated retry budgets, passive reconnect, discovery races, event-ordered teardown and outgoing pairing");
}
'''
with tempfile.TemporaryDirectory(prefix="ds5-reconnect-") as directory:
    work = Path(directory)
    (work / "test.c").write_text(harness)
    subprocess.run(["cc", "-std=c11", "-fsanitize=address,undefined",
                    str(work / "test.c"), "-o", str(work / "test")], check=True)
    subprocess.run([str(work / "test")], check=True)

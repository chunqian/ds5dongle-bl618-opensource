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
struct bt_conn { unsigned refs; bt_addr_t addr; } current, other;
struct bt_l2cap_chan { struct bt_conn *conn; void *ops; };
struct bt_l2cap_br_chan { struct bt_l2cap_chan chan; struct { unsigned mtu; } rx; };
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
static bool connectable, discoverable;
static int reconnect_result;
static TickType_t xTaskGetTickCount(void) { return tick; }
static void set_state(enum bt_hid_host_state state) { hid_ctx.state=state; }
static struct bt_conn *bt_conn_ref(struct bt_conn *conn) { ++conn->refs; return conn; }
static void bt_conn_unref(struct bt_conn *conn) { assert(conn->refs>1); --conn->refs; }
static const bt_addr_t *conn_get_br_addr(const struct bt_conn *conn) { return &conn->addr; }
static void bt_addr_to_str(const bt_addr_t *addr, char *out, unsigned len) { out[0]=0; }
static int bt_br_discovery_stop(void) { return 0; }
static void bt_br_set_connectable(bool enabled) { connectable=enabled; }
static void bt_br_set_discoverable(bool enabled) { discoverable=enabled; }
static struct bt_conn *bt_conn_create_br(const bt_addr_t *addr, void *params) { ++pages; return &current; }
static int bt_conn_disconnect(struct bt_conn *conn, uint8_t reason) { ++disconnects; return 0; }
static int bt_l2cap_chan_connect(struct bt_conn *conn, struct bt_l2cap_chan *chan, unsigned psm) {
    assert(conn==hid_ctx.conn && channel_count<8); channels[channel_count++]=psm; return 0;
}
static void clear_rejected_key(struct bt_conn *conn, enum bt_security_err err) {}
static void bonded_list_refresh(void) {}
static void app_tx_fifo_reset(void) {}
static void feature_cache_clear(void) {}
static void dse_reset(void) {}
static int l2cap_server_reject_blacklisted(struct bt_conn *conn) { return 0; }
static int bt_hid_host_try_reconnect(void) { return reconnect_result; }
static int bt_hid_host_scan_start(uint8_t seconds) { ++scan_calls; return 0; }
static void vTaskDelay(TickType_t delay) { ++delays; tick+=delay; }
void bt_hid_host_disconnect(void);
'''
for signature in (
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
    "void bt_hid_host_force_disconnect(",
):
    harness += "\n" + extract(SOURCE, signature) + "\n"
start = MAIN.index('    LOG_INF("[BT_TASK] try_reconnect...')
end = MAIN.index('    LOG_INF("[BT_TASK] Entering main loop', start)
harness += "\nstatic void startup(void) {\n" + MAIN[start:end] + "\n}\n"
# The main loop must not invoke a hidden reverse-connect/periodic-inquiry path.
assert "bt_hid_host_poll_incoming_l2cap_fallback" not in MAIN
idle = MAIN[MAIN.index("if (bt_hid_host_get_state() == BT_HID_STATE_IDLE &&"):]
idle = idle[:idle.index("static bool has_user_activity")]
assert "bt_hid_host_scan_start" not in idle
harness += r'''
static void setup(void) {
    memset(&hid_ctx,0,sizeof(hid_ctx));
    current.refs=2; other.refs=1; hid_ctx.conn=&current;
    hid_ctx.state=BT_HID_STATE_CONNECTING;
    pages=channel_count=disconnects=scan_calls=delays=0;
    connectable=discoverable=false; switch_pending=false; tick=100;
}
int main(void) {
    /* A saved peer must not block the task that performs auth/initialization. */
    setup(); reconnect_result=0; startup();
    assert(!delays && !scan_calls && !pages);
    reconnect_result=-1; startup(); assert(scan_calls==1 && !delays);

    /* Incoming reconnect: authenticate, accept Control, then Interrupt.
     * No locally created HID channel, including a stale SDP work item. */
    setup(); security_changed(&current,BT_SECURITY_L2,0);
    assert(hid_ctx.security_done && !hid_ctx.outgoing && !hid_ctx.sdp_pending);
    hid_ctx.sdp_pending=true;
    assert(!bt_hid_host_poll_sdp() && !channel_count && !hid_ctx.sdp_pending);
    struct bt_l2cap_chan *chan=NULL;
    assert(l2cap_ctrl_server_accept(&current,&chan)==0);
    assert(chan==&hid_ctx.ctrl_chan.chan);
    l2cap_ctrl_connected(chan);
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

    /* Normal and forced teardown return to passive page scan. */
    setup(); conn_disconnected(&current,0x13);
    assert(!hid_ctx.conn && hid_ctx.state==BT_HID_STATE_IDLE && current.refs==1);
    assert(connectable && discoverable && !hid_ctx.connect_pending);
    tick+=5000; assert(!bt_hid_host_poll_connect() && !pages);
    setup(); bt_hid_host_force_disconnect();
    assert(connectable && discoverable && !hid_ctx.conn && !hid_ctx.connect_pending);
    conn_disconnected(&current,0x13); /* Late event must not schedule paging. */
    assert(!bt_hid_host_poll_connect() && !pages);

    /* Explicit discovery still pages and opens Control -> Interrupt after auth. */
    setup(); hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE;
    assert(bt_hid_host_connect(addr)==0 && pages==1 && hid_ctx.outgoing);
    hid_ctx.conn=&current;
    hid_ctx.sdp_pending=true;
    assert(!bt_hid_host_poll_sdp() && !channel_count); /* Not encrypted yet. */
    security_changed(&current,BT_SECURITY_L2,0);
    assert(bt_hid_host_poll_sdp() && channel_count==1 && channels[0]==HID_PSM_CONTROL);
    l2cap_ctrl_connected(&hid_ctx.ctrl_chan.chan);
    assert(channel_count==2 && channels[1]==HID_PSM_INTERRUPT);
    assert(!critical_depth);
    puts("PASS: nonblocking startup, passive incoming HID, discovery races, passive teardown and outgoing pairing");
}
'''
with tempfile.TemporaryDirectory(prefix="ds5-reconnect-") as directory:
    work = Path(directory)
    (work / "test.c").write_text(harness)
    subprocess.run(["cc", "-std=c11", "-fsanitize=address,undefined",
                    str(work / "test.c"), "-o", str(work / "test")], check=True)
    subprocess.run([str(work / "test")], check=True)

"""Exercise production Bluetooth teardown with reordered SDK callbacks.

Run: python3 tests/test_bt_reconnect.py
Compiles the actual firmware functions against a small host-side SDK double.
No adapter is needed or accessed. SDK references model ACL, deferred channel
cleanup, and in-flight sends separately; releasing a live/NULL ref asserts.
"""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'src/bt_hid_host.c').read_text()


def function(signature):
    start = source.index(signature)
    return source[start:source.index('\n}\n', start) + 3]


harness = r'''
#include "bt_hid_host.h"
#include "ds5_protocol.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#define LOG_INF(...) ((void)0)
#define LOG_DBG(...) ((void)0)
#define LOG_WRN(...) ((void)0)
#define LOG_ERR(...) ((void)0)
#define pdMS_TO_TICKS(x) (x)
#define portTICK_PERIOD_MS 1
#define BT_SECURITY_L2 2
#define BT_ADDR_STR_LEN 18
#define L2CAP_BR_MTU 672
#define BT_HCI_ERR_REMOTE_USER_TERM_CONN 0x13
#define BT_HCI_ERR_CONN_LIMIT_EXCEEDED 0x09
#define BT_HCI_ERR_AUTH_FAIL 0x05
typedef uint32_t TickType_t;
typedef struct { uint8_t val[6]; } bt_addr_t;
typedef int bt_security_t;
enum bt_security_err { BT_SECURITY_ERR_SUCCESS, BT_SECURITY_ERR_AUTH_FAIL,
    BT_SECURITY_ERR_PIN_OR_KEY_MISSING, BT_SECURITY_ERR_UNSPECIFIED };
enum { BT_CONN_DISCONNECTED, BT_CONN_CONNECT, BT_CONN_CONNECTED, BT_CONN_DISCONNECT };
struct bt_conn { int ref, state, handle, sec_level, required_sec_level, encrypt;
    bt_addr_t addr; };
struct bt_l2cap_chan_ops { int unused; };
struct bt_l2cap_chan { struct bt_conn *conn; struct bt_l2cap_chan_ops *ops; };
struct bt_l2cap_br_chan { struct bt_l2cap_chan chan;
    struct { uint16_t cid, mtu; } rx, tx; };
struct net_buf { uint16_t len; unsigned frees; uint8_t data[700]; };
static struct net_buf allocated;
static int hid_tx_pool;
#define K_NO_WAIT 0
#define BT_L2CAP_CHAN_SEND_RESERVE 8
#define APP_TX_DEPTH 10
#define APP_TX_DATA_SZ 80
static uint8_t app_tx_head, app_tx_tail, app_tx_buf[10][80];
static uint16_t app_tx_len[10];
static struct net_buf *net_buf_alloc(int *pool, int wait) {
    (void)pool; (void)wait; memset(&allocated,0,sizeof(allocated)); return &allocated;
}
static void net_buf_reserve(struct net_buf *b, size_t n) { (void)b; (void)n; }
static void net_buf_add_u8(struct net_buf *b, uint8_t v) { b->data[b->len++]=v; }
static void net_buf_add_mem(struct net_buf *b, const void *d, size_t n) {
    assert(b->len+n<=sizeof(b->data)); memcpy(b->data+b->len,d,n); b->len+=n;
}
typedef void (*bt_conn_tx_cb_t)(struct bt_conn *, void *);
static struct bt_l2cap_chan_ops ctrl_ops, intr_ops;
static TickType_t tick;
static unsigned critical_depth;
#define taskENTER_CRITICAL() (++critical_depth)
#define taskEXIT_CRITICAL() do { assert(critical_depth); --critical_depth; } while (0)
static TickType_t xTaskGetTickCount(void) { return tick; }
static int atomic_get(const int *v) { return *v; }
static struct bt_conn *bt_conn_ref(struct bt_conn *c) {
    assert(c && c->ref > 0); ++c->ref; return c;
}
static void bt_conn_unref(struct bt_conn *c) { assert(c && c->ref > 0); --c->ref; }
static void net_buf_unref(struct net_buf *b) { assert(b->frees++ == 0); }
static void bt_addr_copy(bt_addr_t *dst, const bt_addr_t *src) { *dst = *src; }
static int bt_addr_cmp(const bt_addr_t *a, const bt_addr_t *b) { return memcmp(a, b, sizeof(*a)); }
static void bt_addr_to_str(const bt_addr_t *a, char *s, size_t n) { (void)a; snprintf(s,n,"controller"); }
static const bt_addr_t *conn_get_br_addr(const struct bt_conn *c) { return &c->addr; }
static bool blacklist_contains(const bt_addr_t *a) { (void)a; return false; }
static const char *state_name(enum bt_hid_host_state s) { (void)s; return "state"; }
static bool first_intr_logged, first_output_logged, switch_pending;
static int8_t cached_rssi;
static uint8_t bonded_count=1, active_idx;
static bt_addr_t bonded_list[8];
static struct bt_conn current, other;
static unsigned disconnects, rejected, scans_enabled, resets, deleted_keys, attempts, drains;
static int connect_result, disconnect_result;
static bool callback_during_disconnect, finish_sdk_in_send;
static bool callback_during_create;
static bool incoming_during_open;
static bool app_tx_busy;
static bt_addr_t deleted_addr;
static void conn_disconnected(struct bt_conn *, uint8_t);
static void conn_connected(struct bt_conn *, uint8_t);
static int l2cap_ctrl_server_accept(struct bt_conn *, struct bt_l2cap_chan **);
static int l2cap_intr_server_accept(struct bt_conn *, struct bt_l2cap_chan **);
static void hid_tx_drain_one(void) { ++drains; }
'''
start = source.index('static struct {', source.index('/* ---------- internal state'))
harness += source[start:source.index('} hid_ctx;', start) + len('} hid_ctx;')]
harness += r'''
static void set_state(enum bt_hid_host_state s) { hid_ctx.state=s; }
static int bt_br_set_connectable(bool b) { if (b) { assert(!hid_ctx.teardown_pending); ++scans_enabled; } return 0; }
static int bt_br_set_discoverable(bool b) { (void)b; return 0; }
static int bt_br_discovery_stop(void) { return 0; }
static void app_tx_fifo_reset(void) { app_tx_busy=false; }
static void feature_cache_clear(void) {}
static void dse_reset(void) {}
static void GLB_SW_System_Reset(void) { ++resets; }
void bt_keys_link_key_clear_addr(const bt_addr_t *a) { ++deleted_keys; deleted_addr=*a; }
static int bt_conn_disconnect(struct bt_conn *c, uint8_t reason) {
    assert(c && c->ref > 0 && !critical_depth);
    if (c != &current) { ++rejected; return 0; }
    ++disconnects;
    if (disconnect_result) return disconnect_result;
    c->state=BT_CONN_DISCONNECT;
    if (callback_during_disconnect) {
        c->state=BT_CONN_DISCONNECTED;
        conn_disconnected(c, reason);
        assert(hid_ctx.conn==c && c->ref>=2);
    }
    return 0;
}
int bt_hid_host_connect(const uint8_t *addr) {
    (void)addr; ++attempts;
    if (!connect_result) hid_ctx.state=BT_HID_STATE_CONNECTING;
    return connect_result;
}
#define BT_BR_CONN_PARAM_DEFAULT NULL
static struct bt_conn *bt_conn_create_br(const bt_addr_t *addr, const void *param) {
    (void)param; current.addr=*addr; current.state=BT_CONN_CONNECT;
    if (callback_during_create) { current.state=BT_CONN_CONNECTED; conn_connected(&current,0); }
    return &current; /* SDK's two references, without a caller reference. */
}
static int bt_l2cap_chan_connect(struct bt_conn *c, struct bt_l2cap_chan *ch, uint16_t psm) {
    assert(!ch->conn);
    if (incoming_during_open) {
        struct bt_l2cap_chan *accepted=NULL;
        int r=psm==HID_PSM_CONTROL ? l2cap_ctrl_server_accept(c,&accepted) : l2cap_intr_server_accept(c,&accepted);
        assert(r==-ENOMEM && !accepted);
    }
    ch->conn=c; return 0;
}
/* In the real SDK this happens after its ACL callback, on the work queue. */
static void sdk_finish(void) {
    current.state=BT_CONN_DISCONNECTED;
    hid_ctx.ctrl_chan.chan.conn=NULL;
    hid_ctx.intr_chan.chan.conn=NULL;
    bt_conn_unref(&current); /* SDK state/work ref */
}
int bt_l2cap_send_cb(struct bt_conn *c, uint16_t cid, struct net_buf *b,
                     bt_conn_tx_cb_t cb, void *data) {
    (void)cb; (void)data;
    assert(c==&current && c->ref>=3 && cid==0x41 && !critical_depth);
    if (finish_sdk_in_send) {
        conn_disconnected(c, 0x08);
        sdk_finish();
        bt_hid_host_poll_disconnect();
        assert(hid_ctx.conn==c && c->ref==2); /* owner + sender */
        net_buf_unref(b); /* SDK also consumes the buffer on failure */
        return -ENOTCONN;
    }
    net_buf_unref(b);
    return 0;
}
'''
for define in ('DISCONNECT_TIMEOUT_TICKS', 'DISCONNECT_RETRY_TICKS',
               'SECURITY_TIMEOUT_TICKS', 'INCOMING_L2CAP_FALLBACK_MS'):
    harness += next(line for line in source.splitlines() if line.startswith('#define '+define+' ')) + '\n'
for signature in (
    'static bool channel_is_current(', 'static struct bt_conn *active_conn_ref(',
    'static int send_hid_buf(', 'static void request_disconnect(',
    'static void record_security_failure(', 'static void conn_disconnected(',
    'static void l2cap_ctrl_disconnected(', 'static void l2cap_intr_disconnected(',
    'static void on_hid_output_sent(', 'static void security_changed(',
    'static void auth_pairing_failed(', 'static void conn_connected(',
    'static int l2cap_server_reject_blacklisted(', 'static void l2cap_server_ensure_conn(',
    'static int l2cap_ctrl_server_accept(', 'static int l2cap_intr_server_accept(',
    'void bt_hid_host_disconnect(', 'void bt_hid_host_force_disconnect(',
    'void bt_hid_host_drop_stale_conn(', 'bool bt_hid_host_has_pending_conn(',
    'bool bt_hid_host_poll_disconnect(', 'bool bt_hid_host_poll_connect(',
    'bool bt_hid_host_poll_security_watchdog(', 'bool bt_hid_host_poll_sdp(',
    'bool bt_hid_host_poll_incoming_l2cap_fallback(',
    'int bt_hid_host_get_feature(', 'int bt_hid_host_set_feature(',
    'int bt_hid_host_set_feature_crc(', 'int bt_hid_host_send_output(',
):
    harness += function(signature)
harness += function('int bt_hid_host_connect(').replace(
    'int bt_hid_host_connect(', 'static int production_connect(', 1)
harness += r'''
static void setup(void) {
    memset(&hid_ctx,0,sizeof(hid_ctx));
    memset(&current,0,sizeof(current)); memset(&other,0,sizeof(other));
    /* One app owner and one SDK state/work reference. */
    current.ref=other.ref=2; current.state=other.state=BT_CONN_CONNECTED;
    current.addr.val[0]=1; other.addr.val[0]=2;
    hid_ctx.conn=&current; hid_ctx.state=BT_HID_STATE_CONNECTED;
    hid_ctx.ctrl_connected=hid_ctx.intr_connected=true;
    hid_ctx.ctrl_chan.chan.conn=hid_ctx.intr_chan.chan.conn=&current;
    hid_ctx.intr_chan.tx.cid=0x41; hid_ctx.intr_chan.tx.mtu=672;
    hid_ctx.ctrl_chan.tx.cid=0x41; hid_ctx.ctrl_chan.tx.mtu=672;
    bonded_count=1; active_idx=0; bonded_list[0]=current.addr;
    tick=1000; critical_depth=0; switch_pending=false;
    disconnects=rejected=scans_enabled=resets=deleted_keys=attempts=drains=0;
    callback_during_disconnect=finish_sdk_in_send=false;
    callback_during_create=false;
    incoming_during_open=false;
    connect_result=disconnect_result=0;
}
int main(void) {
    /* Disconnect callback runs inside the blocking HCI request. It must not
     * unref/null the owner's pointer or let a new session reuse its channels. */
    setup(); callback_during_disconnect=true;
    bt_hid_host_force_disconnect();
    assert(!disconnects && current.ref==2 && hid_ctx.teardown_pending);
    assert(bt_hid_host_poll_disconnect());
    assert(disconnects==1 && hid_ctx.conn==&current && !scans_enabled);
    struct bt_l2cap_chan *accepted=NULL;
    assert(l2cap_ctrl_server_accept(&other,&accepted)==-EBUSY);
    assert(l2cap_intr_server_accept(&current,&accepted)==-EBUSY);
    conn_connected(&other,0); assert(rejected==1 && hid_ctx.conn==&current);
    conn_connected(&current,0); assert(hid_ctx.teardown_pending);
    bt_hid_host_poll_disconnect(); assert(current.ref==2);
    sdk_finish(); bt_hid_host_poll_disconnect();
    assert(!hid_ctx.conn && !current.ref && scans_enabled==1 && !resets);
    assert(hid_ctx.state==BT_HID_STATE_IDLE && !hid_ctx.teardown_pending);
    assert(!bt_hid_host_poll_disconnect());

    /* Channel-null alone is not proof that SDK destroy/timer cleanup ended. */
    setup(); conn_disconnected(&current,0x08); current.state=BT_CONN_DISCONNECTED;
    hid_ctx.ctrl_chan.chan.conn=hid_ctx.intr_chan.chan.conn=NULL;
    bt_hid_host_poll_disconnect(); assert(hid_ctx.conn==&current && current.ref==2);
    bt_conn_unref(&current); bt_hid_host_poll_disconnect(); assert(!hid_ctx.conn);

    /* An early callback, a late callback, and a lost callback all converge on
     * actual SDK completion. Retry starts once, after the normal 500 ms gap. */
    for (unsigned order=0;order<3;order++) {
        setup(); hid_ctx.fallback_retry=true;
        if (order==0) conn_disconnected(&current,0x16);
        bt_hid_host_force_disconnect();
        if (order==1) conn_disconnected(&current,0x16);
        sdk_finish(); bt_hid_host_poll_disconnect();
        assert(hid_ctx.connect_pending && !hid_ctx.conn && !current.ref);
        TickType_t scheduled=hid_ctx.connect_pending_tick;
        conn_disconnected(&other,0x16); /* Unrelated event cannot reset delay. */
        assert(hid_ctx.connect_pending_tick==scheduled);
        assert(!bt_hid_host_poll_connect()); tick+=499;
        assert(!bt_hid_host_poll_connect()); tick++;
        assert(bt_hid_host_poll_connect() && attempts==1 && !hid_ctx.connect_pending);
        assert(!bt_hid_host_poll_connect());
    }

    /* Old callbacks cannot change the current session, its TX busy bit, or
     * request a disconnect of the current controller. */
    setup(); struct bt_l2cap_chan stale={.conn=&other}; app_tx_busy=true;
    l2cap_ctrl_disconnected(&stale); l2cap_intr_disconnected(&stale);
    on_hid_output_sent(&other,NULL); conn_disconnected(&other,0x08);
    assert(hid_ctx.ctrl_connected && hid_ctx.intr_connected && app_tx_busy);
    assert(!hid_ctx.teardown_pending && !disconnects && !drains);
    l2cap_intr_disconnected(&hid_ctx.intr_chan.chan);
    assert(hid_ctx.teardown_pending); /* Loss of either required channel recovers. */

    /* Repeated USB watchdog requests cannot postpone recovery forever.
     * Wraparound must not break the deadline either. */
    for (unsigned wrap=0;wrap<2;wrap++) {
        setup(); if (wrap) tick=UINT32_MAX-5000;
        bt_hid_host_force_disconnect(); TickType_t begun=tick;
        for (unsigned i=0;i<12;i++) {
            bt_hid_host_poll_disconnect(); assert(!resets);
            tick+=1000; bt_hid_host_force_disconnect();
            assert(hid_ctx.teardown_started==begun);
        }
        bt_hid_host_poll_disconnect(); assert(resets==1 && hid_ctx.conn==&current);
        assert(!scans_enabled); /* Do not reuse live channel memory on timeout. */
    }

    /* A failed HCI submission is retried, bounded by the same deadline. */
    setup(); disconnect_result=-ENOBUFS; bt_hid_host_disconnect();
    bt_hid_host_poll_disconnect(); assert(disconnects==1);
    tick+=999; bt_hid_host_poll_disconnect(); assert(disconnects==1);
    disconnect_result=0; tick++; bt_hid_host_poll_disconnect(); assert(disconnects==2);

    /* An authentication timeout preserves the bond; only explicit key errors
     * for our own connection permit deletion, once, in task context. */
    setup(); hid_ctx.state=BT_HID_STATE_CONNECTING;
    current.sec_level=1; current.required_sec_level=2;
    hid_ctx.acl_connect_tick=tick; tick+=8000;
    assert(bt_hid_host_poll_security_watchdog() && !deleted_keys);
    bt_hid_host_poll_disconnect(); assert(!deleted_keys);
    setup(); record_security_failure(&other,BT_SECURITY_ERR_AUTH_FAIL);
    assert(!hid_ctx.clear_key_pending);
    auth_pairing_failed(&current,BT_SECURITY_ERR_UNSPECIFIED);
    bt_hid_host_poll_disconnect(); assert(!deleted_keys);
    setup(); auth_pairing_failed(&current,BT_SECURITY_ERR_PIN_OR_KEY_MISSING);
    assert(!deleted_keys); bt_hid_host_poll_disconnect();
    assert(deleted_keys==1 && deleted_addr.val[0]==1);
    bt_hid_host_poll_disconnect(); assert(deleted_keys==1);

    /* Encryption already completed despite a lost security callback. */
    setup(); hid_ctx.state=BT_HID_STATE_CONNECTING; hid_ctx.acl_connect_tick=tick;
    current.sec_level=2; current.encrypt=1; tick+=8000;
    assert(bt_hid_host_poll_security_watchdog());
    assert(hid_ctx.security_done && hid_ctx.security_done_tick==tick && !deleted_keys);

    /* Connected and failed outgoing callbacks retain the owner until SDK
     * cleanup; a new incoming connection is allowed after that boundary. */
    setup(); conn_connected(&current,0);
    assert(hid_ctx.security_pending && !hid_ctx.teardown_pending && current.ref==2);
    security_changed(&current,2,BT_SECURITY_ERR_SUCCESS);
    assert(hid_ctx.security_done);
    conn_connected(&current,0x04); assert(hid_ctx.teardown_pending && current.ref==2);
    sdk_finish(); bt_hid_host_poll_disconnect(); conn_connected(&other,0);
    assert(hid_ctx.conn==&other && other.ref==3 && hid_ctx.security_pending);

    /* A controller opening L2CAP before the deferred outgoing poll must not
     * have its live channel object zeroed or attached a second time. */
    setup(); hid_ctx.state=BT_HID_STATE_CONNECTING; hid_ctx.sdp_pending=true;
    hid_ctx.ctrl_connected=hid_ctx.ctrl_accepting=false;
    assert(l2cap_ctrl_server_accept(&current,&accepted)==-ENOMEM);
    hid_ctx.intr_connected=hid_ctx.intr_accepting=false;
    assert(l2cap_intr_server_accept(&current,&accepted)==-ENOMEM);
    assert(!bt_hid_host_poll_sdp());
    assert(hid_ctx.ctrl_chan.chan.conn==&current && hid_ctx.intr_chan.chan.conn==&current);

    /* Reserve channel storage before SDK channel creation can yield to an
     * incoming L2CAP accept, including the incoming-reconnect fallback. */
    setup(); hid_ctx.state=BT_HID_STATE_CONNECTING; hid_ctx.sdp_pending=true;
    hid_ctx.ctrl_chan.chan.conn=hid_ctx.intr_chan.chan.conn=NULL;
    hid_ctx.ctrl_connected=hid_ctx.intr_connected=false; incoming_during_open=true;
    assert(bt_hid_host_poll_sdp() && hid_ctx.ctrl_chan.chan.conn==&current);
    setup(); hid_ctx.state=BT_HID_STATE_CONNECTING; hid_ctx.security_done=true;
    hid_ctx.security_done_tick=tick; tick+=3000;
    assert(!bt_hid_host_poll_incoming_l2cap_fallback()); /* attached interrupt is kept */
    hid_ctx.intr_connected=false; hid_ctx.intr_chan.chan.conn=NULL; incoming_during_open=true;
    assert(bt_hid_host_poll_incoming_l2cap_fallback());
    assert(hid_ctx.intr_chan.chan.conn==&current && hid_ctx.intr_accepting);

    /* Retain ACL across an in-flight send even if SDK cleanup completes then.
     * A failed send consumes its buffer exactly once, without stale chan->conn. */
    setup(); struct net_buf buf={.len=78};
    assert(send_hid_buf(&hid_ctx.intr_chan,&buf,NULL)==0 && buf.frees==1 && current.ref==2);
    buf=(struct net_buf){.len=78}; finish_sdk_in_send=true;
    assert(send_hid_buf(&hid_ctx.intr_chan,&buf,NULL)==-ENOTCONN);
    assert(buf.frees==1 && current.ref==1 && hid_ctx.conn==&current);
    bt_hid_host_poll_disconnect(); assert(!current.ref && !hid_ctx.conn);
    buf=(struct net_buf){.len=78};
    assert(send_hid_buf(&hid_ctx.intr_chan,&buf,NULL)==-ENOTCONN && buf.frees==1);
    setup(); buf=(struct net_buf){.len=700};
    assert(send_hid_buf(&hid_ctx.intr_chan,&buf,NULL)==-EMSGSIZE && buf.frees==1 && current.ref==2);

    /* Every public sender observes the same buffer-ownership contract, even
     * when teardown occurs inside the SDK call (CRC feature included). */
    uint8_t payload[547]={0x39};
    for (unsigned sender=0; sender<4; sender++) {
        setup(); finish_sdk_in_send=true; int result;
        switch(sender) {
        case 0: result=bt_hid_host_get_feature(0x05); break;
        case 1: result=bt_hid_host_set_feature(0x08,payload,47); break;
        case 2: result=bt_hid_host_set_feature_crc(0x08,payload,47); break;
        default: result=bt_hid_host_send_output(payload,sizeof(payload)); break;
        }
        assert(result==-ENOTCONN && allocated.frees==1 && current.ref==1);
        bt_hid_host_poll_disconnect(); assert(!hid_ctx.conn && !current.ref);
    }
    setup(); assert(!bt_hid_host_send_output(payload,sizeof(payload)));
    assert(allocated.len==548 && allocated.data[0]==0xa2 && !memcmp(allocated.data+1,payload,547));
    assert(!bt_hid_host_get_feature(0x05));
    assert(allocated.len==2 && allocated.data[0]==0x43 && allocated.data[1]==0x05);

    /* Paging owns a reference too, so an 8-second connect timeout can cancel
     * an ACL even before its connected callback. A callback arriving inside
     * create must not cause the owner reference to be acquired twice. */
    for (unsigned early=0; early<2; early++) {
        setup(); hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE;
        callback_during_create=early;
        assert(!production_connect(current.addr.val));
        assert(hid_ctx.conn==&current && current.ref==3);
        bt_hid_host_disconnect(); bt_hid_host_poll_disconnect();
        assert(disconnects==1 && current.ref==3);
        current.state=BT_CONN_DISCONNECTED; conn_disconnected(&current,0x16);
        bt_conn_unref(&current); /* SDK outgoing sticky reference */
        sdk_finish(); bt_hid_host_poll_disconnect();
        assert(!hid_ctx.conn && !current.ref);
    }

    /* References acquired by other task operations also delay reuse. */
    setup(); struct bt_conn *held=active_conn_ref(); assert(current.ref==3);
    bt_hid_host_force_disconnect(); assert(!active_conn_ref()); sdk_finish();
    bt_hid_host_poll_disconnect(); assert(hid_ctx.conn==held && held->ref==2);
    bt_conn_unref(held); bt_hid_host_poll_disconnect(); assert(!hid_ctx.conn);

    /* Retry budget is per attempt sequence, not accumulated across sessions. */
    setup(); hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE; hid_ctx.connect_pending=true;
    connect_result=-ENOMEM; assert(bt_hid_host_poll_connect() && hid_ctx.connect_retries==1);
    tick+=500; connect_result=0; assert(bt_hid_host_poll_connect() && !hid_ctx.connect_retries);
    hid_ctx.state=BT_HID_STATE_IDLE; hid_ctx.connect_pending=true;
    connect_result=-ENOMEM; assert(bt_hid_host_poll_connect() && hid_ctx.connect_retries==1);
    assert(!critical_depth);
    puts("PASS: Bluetooth callback ordering, ownership, bounded recovery, bonds, channels, TX and retries");
}
'''
with tempfile.TemporaryDirectory(prefix='ds5-bt-reconnect-') as tmp:
    tmp = Path(tmp)
    (tmp / 'test.c').write_text(harness)
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-function', '-Wno-unused-parameter', '-Wno-unused-variable',
                    '-fsanitize=address,undefined', '-I', str(root / 'src'),
                    str(tmp / 'test.c'), str(root / 'src/ds5_protocol.c'),
                    '-o', str(tmp / 'test')], check=True)
    subprocess.run([str(tmp / 'test')], check=True)

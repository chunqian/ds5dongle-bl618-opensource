"""Exercise production HID FIFO code with consuming SDK failures and callbacks."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / "src/bt_hid_host.c").read_text()


def function(name):
    start = source.index("int " + name + "(")
    end = source.index("{", start) + 1
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


fifo = source[source.index("#define APP_TX_DEPTH"):
              source.index("static struct bt_br_discovery_result discovery_results")]
harness = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
struct bt_conn { int refs; } connection, other;
struct bt_l2cap_chan { struct bt_conn *conn; };
struct bt_l2cap_br_chan {
    struct bt_l2cap_chan chan;
    struct { uint16_t cid, mtu; } tx;
};
struct net_buf { unsigned len, frees; uint8_t data[700]; } buffers[128];
typedef void (*bt_conn_tx_cb_t)(struct bt_conn *, void *);
static struct {
    struct bt_conn *conn;
    bool intr_connected, ctrl_connected;
    struct bt_l2cap_br_chan intr_chan, ctrl_chan;
} hid_ctx;
static unsigned critical_depth, allocated, allocation_failures;
static unsigned sends, result_count, result_index, completion_count, completion_index;
static bool reset_during_send, refill_on_error;
static int results[32];
static unsigned sent_ids[128];
static struct { struct bt_conn *conn; bt_conn_tx_cb_t cb; void *arg; } completions[128];
static int hid_tx_pool;
static bool first_output_logged;
#define taskENTER_CRITICAL() (++critical_depth)
#define taskEXIT_CRITICAL() do { assert(critical_depth); --critical_depth; } while (0)
#define LOG_ERR(...) ((void)0)
#define LOG_WRN(...) ((void)0)
#define LOG_INF(...) ((void)0)
#define LOG_DBG(...) ((void)0)
#define K_NO_WAIT 0
#define BT_L2CAP_CHAN_SEND_RESERVE 8
#define DS5_BT_OUTPUT_REPORT_SIZE 78
#define DS5_BT_OUTPUT_REPORT_ID 0x31
#define HID_TRANS_DATA 0xA
#define HID_REPORT_OUTPUT 2
#define HID_REPORT_FEATURE 3
#define HID_TRANS_GET_REPORT 4
#define HID_TRANS_SET_REPORT 5
#define DS5_BT_FEATURE_CRC_SEED 0
#define HID_HEADER(a,b) (((a)<<4)|(b))
static struct bt_conn *bt_conn_ref(struct bt_conn *c) {
    assert(c && c->refs>0); ++c->refs; return c;
}
static void bt_conn_unref(struct bt_conn *c) { assert(c->refs>0); --c->refs; }
static struct net_buf *net_buf_alloc(void *pool, int wait) {
    assert(!critical_depth);
    if (allocation_failures) { --allocation_failures; return NULL; }
    assert(allocated<128); return &buffers[allocated++];
}
static void net_buf_unref(struct net_buf *b) {
    assert(b->frees++==0); b->len=0; /* SDK owns the buffer after submission. */
}
static void net_buf_reserve(struct net_buf *b, unsigned n) { (void)b; (void)n; }
static void net_buf_add_mem(struct net_buf *b, const void *p, unsigned n) {
    assert(!b->frees && b->len+n<=sizeof(b->data));
    memcpy(b->data+b->len,p,n); b->len+=n;
}
static void net_buf_add_u8(struct net_buf *b, uint8_t n) { net_buf_add_mem(b,&n,1); }
static uint32_t ds5_crc32(uint32_t seed, const uint8_t *data, uint16_t len) {
    assert(len>0); return 0x11223344;
}
static void ds5_write_le32(uint8_t *out, uint32_t value) {
    for(unsigned i=0;i<4;i++) out[i]=(value>>(i*8))&0xff;
}
'''
harness += fifo + "\n" + function("bt_hid_host_send_output")
harness += "\n" + "\n".join(function(name) for name in (
    "bt_hid_host_get_feature", "bt_hid_host_set_feature", "bt_hid_host_set_feature_crc"))
harness += r'''
static void enqueue(unsigned id) {
    uint8_t data[78]={0x31}; data[1]=id;
    assert(bt_hid_host_send_output(data,sizeof(data))==0);
}
int bt_l2cap_send_cb(struct bt_conn *conn, uint16_t cid,
                    struct net_buf *buf, bt_conn_tx_cb_t cb, void *arg) {
    assert(!critical_depth && conn==&connection && conn->refs==2);
    assert(cid==0x41 || cid==0x40);
    assert(!buf->frees);
    sent_ids[sends++]=buf->data[2];
    int result=result_index<result_count?results[result_index++]:0;
    net_buf_unref(buf); /* No callback is scheduled on failure. */
    if (result<0 && refill_on_error)
        enqueue(42);
    if (reset_during_send) {
        reset_during_send=false;
        app_tx_fifo_reset();
        /* Model a new session acquiring the same storage before return. */
        assert(result<0);
    }
    if (!result && cb) {
        completions[completion_count].conn=conn;
        completions[completion_count].cb=cb;
        completions[completion_count++].arg=arg;
    }
    return result;
}
static void complete(void) {
    assert(completion_index<completion_count);
    unsigned i=completion_index++;
    completions[i].cb(completions[i].conn,completions[i].arg);
}
static void setup(void) {
    for(unsigned i=0;i<allocated;i++) assert(buffers[i].frees==1);
    assert(!critical_depth);
    app_tx_fifo_reset();
    memset(buffers,0,sizeof(buffers));
    allocated=allocation_failures=sends=result_count=result_index=0;
    completion_count=completion_index=0;
    reset_during_send=refill_on_error=false;
    connection.refs=other.refs=1;
    hid_ctx.conn=&connection; hid_ctx.intr_connected=true;
    hid_ctx.intr_chan.chan.conn=&connection;
    hid_ctx.intr_chan.tx.cid=0x41; hid_ctx.intr_chan.tx.mtu=672;
    hid_ctx.ctrl_connected=true; hid_ctx.ctrl_chan.chan.conn=&connection;
    hid_ctx.ctrl_chan.tx.cid=0x40; hid_ctx.ctrl_chan.tx.mtu=672;
}
static struct net_buf *payload(void) {
    struct net_buf *b=net_buf_alloc(&hid_tx_pool,0);
    uint8_t data[3]={0xA2,0x31,7}; net_buf_add_mem(b,data,3); return b;
}
int main(void) {
    /* Preserve exact SDK errors, release buffers once, and balance refs. */
    int errors[]={-ENOTCONN,-ENOBUFS};
    /* GET/SET/CRC/direct audio share the consuming adapter and return errors. */
    for(unsigned i=0;i<2;i++) {
        uint8_t feature[8]={0}; uint8_t output[142]={0x32};
        setup(); results[0]=errors[i]; result_count=1;
        assert(bt_hid_host_get_feature(0x20)==errors[i]);
        assert(buffers[0].data[0]==0x43 && buffers[0].data[1]==0x20);
        setup(); results[0]=errors[i]; result_count=1;
        assert(bt_hid_host_set_feature(0x65,feature,sizeof(feature))==errors[i]);
        setup(); results[0]=errors[i]; result_count=1;
        assert(bt_hid_host_set_feature_crc(0x80,feature,sizeof(feature))==errors[i]);
        assert(buffers[0].data[6]==0x44 && buffers[0].data[9]==0x11);
        setup(); results[0]=errors[i]; result_count=1;
        assert(bt_hid_host_send_output(output,sizeof(output))==errors[i]);
        assert(!completion_count && connection.refs==1);
    }
    setup(); assert(bt_hid_host_get_feature(0x05)==0 && !completion_count);
    setup(); hid_ctx.ctrl_chan.tx.mtu=1;
    assert(bt_hid_host_get_feature(0x05)==-EMSGSIZE && !sends);
    for(unsigned i=0;i<2;i++) {
        setup(); results[0]=errors[i]; result_count=1; app_tx_busy=true;
        assert(hid_tx_submit(payload(),app_tx_generation)==errors[i]);
        assert(sends==1 && completion_count==0 && connection.refs==1);
    }
    setup(); app_tx_busy=true; hid_ctx.intr_chan.tx.mtu=2;
    assert(hid_tx_submit(payload(),app_tx_generation)==-EMSGSIZE);
    assert(!sends && connection.refs==1);
    setup(); app_tx_busy=true; hid_ctx.intr_chan.chan.conn=&other;
    assert(hid_tx_submit(payload(),app_tx_generation)==-ENOTCONN);
    assert(!sends && connection.refs==1 && other.refs==1);

    /* A successful submission stays busy until its callback. An SDK error
     * for its queued successor must not strand the next queued report. */
    setup(); enqueue(1); enqueue(2); enqueue(3);
    assert(sends==1 && app_tx_busy);
    results[0]=-ENOBUFS; results[1]=0; result_count=2;
    complete();
    assert(sends==3 && sent_ids[1]==2 && sent_ids[2]==3 && app_tx_busy);
    complete(); assert(!app_tx_busy && app_tx_head==app_tx_tail);

    setup(); results[0]=-ENOTCONN; result_count=1;
    enqueue(1); assert(!app_tx_busy && !completion_count);
    enqueue(2); assert(app_tx_busy && sends==2); complete();
    assert(!app_tx_busy);

    /* Allocation failure also has no completion callback. */
    setup(); enqueue(1); enqueue(2); enqueue(3); allocation_failures=1;
    complete(); assert(sends==2 && sent_ids[1]==3 && app_tx_busy);
    complete(); assert(!app_tx_busy);

    setup(); enqueue(1);
    for(unsigned i=2;i<10;i++) enqueue(i);
    for(unsigned i=0;i<8;i++) results[i]=-ENOBUFS;
    result_count=8; complete();
    assert(sends==9 && !app_tx_busy && app_tx_head==app_tx_tail);

    /* A producer can refill the queue while all SDK sends fail. Bound the
     * callback's work, then recover on the next BT-task poll without new input. */
    setup(); enqueue(1); enqueue(2);
    refill_on_error=true;
    for(unsigned i=0;i<32;i++) results[i]=-ENOBUFS;
    result_count=32; complete();
    assert(sends==1+APP_TX_DEPTH && !app_tx_busy && app_tx_head!=app_tx_tail);
    refill_on_error=false; result_count=result_index=0;
    bt_hid_host_poll_tx(); assert(app_tx_busy);
    complete(); assert(!app_tx_busy && app_tx_head==app_tx_tail);

    /* Old callbacks must not clear busy/drain a newly reset session, even
     * if the SDK reuses the same connection pointer. */
    setup(); enqueue(1); app_tx_fifo_reset(); enqueue(2); enqueue(3);
    complete(); assert(app_tx_busy && sends==2);
    complete(); assert(app_tx_busy && sends==3);
    complete(); assert(!app_tx_busy);
    setup(); enqueue(1);
    on_hid_output_sent(&other,completions[0].arg);
    assert(app_tx_busy); complete(); assert(!app_tx_busy);

    setup(); enqueue(1); enqueue(2);
    reset_during_send=true; results[0]=-ENOTCONN; result_count=1;
    complete(); assert(!app_tx_busy && app_tx_head==app_tx_tail);
    enqueue(3); complete(); assert(!app_tx_busy);
    setup();
    puts("PASS: SDK error propagation, single buffer release, FIFO recovery, bounded failures, stale callbacks and refs");
}
'''
with tempfile.TemporaryDirectory(prefix="ds5-bt-tx-test-") as temporary:
    c = Path(temporary) / "test.c"
    binary = Path(temporary) / "test"
    c.write_text(harness)
    subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-unused-parameter", "-fsanitize=address,undefined",
                    str(c), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)

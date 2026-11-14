"""Run production initialization/auth handlers, including the patched SDK HCI handler.

BL_SDK_BASE may select the SDK; all compiled fixtures and SDK copies are temporary.
"""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SDK = Path(os.environ.get("BL_SDK_BASE", ROOT.parent / "bouffalo_sdk"))
SOURCE = (ROOT / "src/bt_hid_host.c").read_text()


def extract(source, signature):
    start = source.index(signature)
    end = source.index("{", start) + 1
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


handshake_fields = SOURCE[SOURCE.index("    struct {\n        TickType_t last_attempt"):
                          SOURCE.index("    } handshake;") + len("    } handshake;")]
constants = SOURCE[SOURCE.index("static const uint8_t handshake_ids[]"):
                   SOURCE.index("static void handshake_begin")]
support = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t TickType_t;
typedef uint8_t bt_security_t, u8_t;
typedef uint16_t u16_t;
typedef struct { uint8_t val[6]; } bt_addr_t;
enum bt_security_err {
    BT_SECURITY_ERR_SUCCESS, BT_SECURITY_ERR_AUTH_FAIL,
    BT_SECURITY_ERR_PIN_OR_KEY_MISSING, BT_SECURITY_ERR_OOB_NOT_AVAILABLE,
    BT_SECURITY_ERR_AUTH_REQUIREMENT, BT_SECURITY_ERR_PAIR_NOT_SUPPORTED,
    BT_SECURITY_ERR_PAIR_NOT_ALLOWED, BT_SECURITY_ERR_INVALID_PARAM,
    BT_SECURITY_ERR_UNSPECIFIED
};
enum bt_hid_host_state {
    BT_HID_STATE_IDLE, BT_HID_STATE_CONNECTING, BT_HID_STATE_SDP_QUERY,
    BT_HID_STATE_HANDSHAKE, BT_HID_STATE_CONNECTED, BT_HID_STATE_DISCONNECTING
};
struct bt_conn { unsigned refs, state, handle, sec_level, required_sec_level; bool encrypt; bt_addr_t addr; } current, other;
struct bt_l2cap_chan { struct bt_conn *conn; } control;
struct net_buf { uint8_t *data; uint16_t len; };
#define BT_CONN_CONNECTED 1
#define BT_SECURITY_L1 1
#define BT_SECURITY_L2 2
#define BT_HCI_ERR_REMOTE_USER_TERM_CONN 0x13
#define DS5_FEATURE_REPORT_PAIRING 0x09
#define DS5_FEATURE_REPORT_FIRMWARE 0x20
#define DS5_FEATURE_REPORT_CALIBRATION 0x05
#define HID_TRANS_HANDSHAKE 0
#define HID_TRANS_DATA 0xA
#define HID_REPORT_FEATURE 3
#define HID_HANDSHAKE_INVALID_ID 2
#define pdMS_TO_TICKS(v) (v)
#define portTICK_PERIOD_MS 1
#define LOG_INF(...) ((void)0)
#define LOG_WRN(...) ((void)0)
#define LOG_ERR(...) ((void)0)
#define LOG_DBG(...) ((void)0)
#define BT_DBG(...) ((void)0)
#define BT_ERR(...) ((void)0)
#define sys_le16_to_cpu(v) (v)
static unsigned critical_depth;
#define taskENTER_CRITICAL() (++critical_depth)
#define taskEXIT_CRITICAL() do { assert(critical_depth); --critical_depth; } while (0)
static struct {
    struct bt_conn *conn;
    bt_addr_t target_addr;
    bool has_target, acl_disconnected, disconnect_requested, disconnect_attempted;
    TickType_t disconnect_retry_tick;
    enum bt_hid_host_state state;
    bool ctrl_connected, intr_connected, check_dse, is_dse;
    bool security_done, outgoing, sdp_pending;
    TickType_t acl_connect_tick, security_done_tick;
    int (*primer_cb)(uint32_t);
'''
support += handshake_fields + "\n} hid_ctx;\n"
support += r'''
static TickType_t tick;
static uint32_t app_tx_generation=1;
static unsigned sent[256], sent_count, fail_count[256], cached[256];
static unsigned events[256], event_count, primers, primer_failures;
static unsigned deleted_keys, disconnected, edge_unlocks, notifications, encryptions;
static bool rx_context;
static bool immediate_response, optional_response, reset_on_send, disconnect_on_unlock;
static bool reject_initialization, reject_probe;
static bool reply_during_primer, disconnect_during_primer;
static uint8_t reset_report;
static void event(unsigned value) { assert(event_count<256); events[event_count++]=value; }
static TickType_t xTaskGetTickCount(void) { return tick; }
static void set_state(enum bt_hid_host_state s) {
    hid_ctx.state=s;
    if (s==BT_HID_STATE_CONNECTED) {
        assert(primers==1 && hid_ctx.handshake.primer_done && !critical_depth);
        event(0x100);
    }
}
static void feature_cache_store(uint8_t id, const uint8_t *data, uint16_t len) { ++cached[id]; }
static void dse_on_edge_detected(void) {
    assert(!rx_context && primers==1 && hid_ctx.handshake.primer_done && !critical_depth);
    event(0x65); event(0x80);
    ++edge_unlocks;
    if (disconnect_on_unlock) {
        ++app_tx_generation; hid_ctx.state=BT_HID_STATE_IDLE;
        hid_ctx.conn=NULL; hid_ctx.ctrl_connected=hid_ctx.intr_connected=false;
    }
}
static void dse_on_control_packet(const uint8_t *p, uint16_t len) { (void)p; (void)len; }
static const bt_addr_t *conn_get_br_addr(struct bt_conn *c) { return &c->addr; }
/* Key-list synchronization is exercised by test_bt_pairing_address.py. */
static bool pairing_addr_pending;
static bt_addr_t pairing_addr;
static int bt_addr_cmp(const bt_addr_t *a, const bt_addr_t *b) { return memcmp(a,b,sizeof(*a)); }
static void bonded_list_refresh(void) {}
static bool bonded_list_copy_active(bt_addr_t *addr) { memset(addr,0,sizeof(*addr)); return false; }
void bt_keys_link_key_clear_addr(const bt_addr_t *a) { assert(!bt_addr_cmp(a,&current.addr)); ++deleted_keys; }
static struct bt_conn *bt_conn_ref(struct bt_conn *c) { ++c->refs; return c; }
static void bt_conn_unref(struct bt_conn *c);
static int bt_conn_disconnect(struct bt_conn *c, uint8_t reason) { assert(c==&current); ++disconnected; return 0; }
void bt_hid_host_disconnect(void);
static bool poll_scan_mode(void) { return true; }
static int l2cap_ctrl_recv(struct bt_l2cap_chan *, struct net_buf *);
static void handshake_begin(void);
static void reply(uint8_t id) {
    uint8_t data[]={0xA3,id,0x55}; struct net_buf b={data,sizeof(data)};
    rx_context=true; l2cap_ctrl_recv(&control,&b); rx_context=false;
}
static void reject(void) {
    uint8_t data[]={2}; struct net_buf b={data,sizeof(data)};
    rx_context=true; l2cap_ctrl_recv(&control,&b); rx_context=false;
}
static int primer(uint32_t session) {
    assert(session==app_tx_generation);
    assert(!critical_depth && hid_ctx.state==BT_HID_STATE_HANDSHAKE);
    assert(sent_count>=5 && sent[sent_count-1]==0x70);
    if(primer_failures) { --primer_failures; return -1; }
    ++primers; event(0x32);
    if (reply_during_primer) {
        reply(0x70);
        assert(!edge_unlocks && hid_ctx.state==BT_HID_STATE_HANDSHAKE);
    }
    if (disconnect_during_primer) {
        ++app_tx_generation; hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE;
        memset(&hid_ctx.handshake,0,sizeof(hid_ctx.handshake));
    }
    return 0;
}
int bt_hid_host_get_feature(uint8_t id) {
    assert(!critical_depth); event(id);
    assert(sent_count<256); sent[sent_count++]=id;
    if (reset_on_send && id==reset_report) {
        reset_on_send=false; ++app_tx_generation; handshake_begin(); return -1;
    }
    if (fail_count[id]) { --fail_count[id]; return -1; }
    if ((id!=0x70 && reject_initialization) || (id==0x70 && reject_probe)) {
        reject(); return 0;
    }
    if (immediate_response || (optional_response && id!=0x70)) reply(id);
    return 0;
}
'''
support += constants
for signature in (
    "static void handshake_begin(", "static void handshake_maybe_begin(",
    "static void handshake_finish_detection(",
    "static void handshake_send_pending(",
    "static int l2cap_ctrl_recv(", "void bt_hid_host_handshake_tick(",
    "void bt_hid_host_disconnect(", "void bt_hid_host_poll_control(", "static void clear_rejected_key(",
    "static void auth_pairing_failed(", "static void security_changed(",
):
    support += "\n" + extract(SOURCE, signature) + "\n"
support += extract(SOURCE, "bool bt_hid_host_poll_security_ready(")

support += r'''
struct bt_hci_evt_auth_complete { uint8_t status; uint16_t handle; };
#define BT_HCI_ERR_SUCCESS 0
#define BT_HCI_ERR_AUTH_FAIL 5
#define BT_HCI_ERR_PIN_OR_KEY_MISSING 6
#define BT_HCI_ERR_PAIRING_NOT_SUPPORTED 0x29
#define BT_HCI_ERR_PAIRING_NOT_ALLOWED 0x18
#define BT_HCI_ERR_INVALID_PARAM 0x12
static struct bt_conn *bt_conn_lookup_handle(uint16_t h) {
    struct bt_conn *c=h==1?&current:h==2?&other:NULL;
    if (c) ++c->refs; return c;
}
static void bt_conn_unref(struct bt_conn *c) { assert(c->refs>1); --c->refs; }
static void reset_pairing(struct bt_conn *c) { c->required_sec_level=c->sec_level; }
static void bt_l2cap_encrypt_change(struct bt_conn *c, uint8_t status) { (void)c; (void)status; }
static void link_encr(uint16_t h) { assert(h==1); ++encryptions; }
static void bt_conn_security_changed(struct bt_conn *c, enum bt_security_err err) {
    ++notifications; security_changed(c,c->sec_level,err);
}
static void setup(void) {
    memset(&hid_ctx,0,sizeof(hid_ctx)); memset(sent,0,sizeof(sent));
    memset(fail_count,0,sizeof(fail_count)); memset(cached,0,sizeof(cached));
    memset(&current,0,sizeof(current)); memset(&other,0,sizeof(other));
    current.refs=other.refs=1; current.state=other.state=BT_CONN_CONNECTED;
    current.sec_level=other.sec_level=BT_SECURITY_L1;
    hid_ctx.conn=&current; control.conn=&current;
    hid_ctx.ctrl_connected=hid_ctx.intr_connected=true;
    hid_ctx.primer_cb=primer;
    event_count=primers=primer_failures=0; assert(!critical_depth);
    tick=0; sent_count=deleted_keys=disconnected=edge_unlocks=notifications=encryptions=0;
    immediate_response=optional_response=reset_on_send=disconnect_on_unlock=false; ++app_tx_generation;
    reject_initialization=reject_probe=false;
    reply_during_primer=disconnect_during_primer=false; reset_report=9;
    handshake_begin();
}
'''

tests = r'''
static void auth_event(uint8_t status, uint16_t handle) {
    struct bt_hci_evt_auth_complete event={status,handle};
    struct net_buf b={(uint8_t *)&event,sizeof(event)}; auth_complete(&b);
    bt_hid_host_poll_control();
}
int main(void) {
    setup(); bt_hid_host_handshake_tick();
    /* All five requests are submitted in the first pass, before any reply. */
    assert(tick==0 && sent_count==5 && sent[0]==9 && sent[1]==0x20 &&
           sent[2]==0x22 && sent[3]==5 && sent[4]==0x70);
    const unsigned initial[]={9,0x20,0x22,5,0x70,0x32};
    assert(event_count==6 && !memcmp(events,initial,sizeof(initial)) && primers==1);
    /* Missing 0x09 does not suppress the other GETs or the Edge probe.
     * Repeated ticks do not resend successful requests before probe retry. */
    reply(0x20); reply(0x22); reply(5); tick=900; bt_hid_host_handshake_tick();
    assert(sent_count==5 && sent[4]==0x70);
    reply(0x70); bt_hid_host_handshake_tick(); assert(hid_ctx.state==BT_HID_STATE_CONNECTED && hid_ctx.is_dse && edge_unlocks==1);
    assert(cached[0x20]==1 && cached[5]==1 && !disconnected);
    assert(event_count==9 && events[6]==0x65 && events[7]==0x80 && events[8]==0x100);

    setup(); fail_count[9]=2; bt_hid_host_handshake_tick();
    assert(sent_count==5 && sent[4]==0x70 && !(hid_ctx.handshake.sent&1));
    tick=100; bt_hid_host_handshake_tick(); tick=200; bt_hid_host_handshake_tick();
    assert(sent_count==7 && sent[5]==9 && sent[6]==9 && (hid_ctx.handshake.sent&1));
    setup(); fail_count[9]=99;
    for(tick=0;tick<=1000;tick+=100) bt_hid_host_handshake_tick();
    assert(hid_ctx.handshake.attempts[0]==3 && sent[sent_count-1]==0x70);

    /* Match DS5Dongle's check_dse window: enable it immediately before GET
     * 0x70 and classify DS5 on its first INVALID_ID, without another probe. */
    setup(); reject();
    assert(hid_ctx.state==BT_HID_STATE_HANDSHAKE && !hid_ctx.check_dse);
    bt_hid_host_handshake_tick(); reject(); bt_hid_host_handshake_tick();
    assert(hid_ctx.state==BT_HID_STATE_CONNECTED && !hid_ctx.is_dse && !edge_unlocks);
    assert(!hid_ctx.check_dse && hid_ctx.handshake.probe_attempts==1);
    reject(); reply(0x70); tick=1000; bt_hid_host_handshake_tick();
    assert(!hid_ctx.is_dse && !edge_unlocks && sent_count==5);

    setup(); reject_initialization=reject_probe=true; bt_hid_host_handshake_tick();
    assert(sent_count==5 && hid_ctx.state==BT_HID_STATE_CONNECTED && !hid_ctx.is_dse);
    assert(event_count==7 && !memcmp(events,initial,sizeof(initial)) && events[6]==0x100);

    setup(); reject_initialization=immediate_response=true; bt_hid_host_handshake_tick();
    assert(sent_count==5 && hid_ctx.state==BT_HID_STATE_CONNECTED && hid_ctx.is_dse);

    setup(); optional_response=true; bt_hid_host_handshake_tick(); reject(); bt_hid_host_handshake_tick();
    assert(hid_ctx.state==BT_HID_STATE_CONNECTED && !hid_ctx.is_dse);

    setup(); immediate_response=true; bt_hid_host_handshake_tick();
    assert(sent_count==5 && hid_ctx.state==BT_HID_STATE_CONNECTED && hid_ctx.is_dse);
    assert(event_count==9 && !memcmp(events,initial,sizeof(initial)) &&
           events[6]==0x65 && events[7]==0x80 && events[8]==0x100);
    reply(0x70); reject(); handshake_finish_detection();
    assert(edge_unlocks==1 && primers==1 && event_count==9);
    setup(); reply_during_primer=true; bt_hid_host_handshake_tick();
    assert(hid_ctx.state==BT_HID_STATE_CONNECTED && edge_unlocks==1 && primers==1);
    setup(); immediate_response=disconnect_during_primer=true; bt_hid_host_handshake_tick();
    assert(hid_ctx.state==BT_HID_STATE_IDLE && !edge_unlocks && !hid_ctx.handshake.primer_done);
    setup(); immediate_response=disconnect_on_unlock=true; bt_hid_host_handshake_tick();
    assert(edge_unlocks==1 && hid_ctx.state==BT_HID_STATE_IDLE && !hid_ctx.conn);
    setup(); bt_hid_host_handshake_tick(); tick=1000; fail_count[0x70]=1;
    bt_hid_host_handshake_tick(); assert(hid_ctx.check_dse);
    tick=2000; bt_hid_host_handshake_tick(); reply(0x70); bt_hid_host_handshake_tick();
    assert(hid_ctx.state==BT_HID_STATE_CONNECTED && hid_ctx.is_dse);

    setup(); for(tick=0;tick<10000;tick+=100) bt_hid_host_handshake_tick();
    assert(sent_count==10 && hid_ctx.handshake.probe_attempts==6);
    assert(primers==1); /* Retries do not replay the initial state/0x32. */
    assert(hid_ctx.state==BT_HID_STATE_DISCONNECTING && hid_ctx.disconnect_requested); /* never guess DS5 */
    assert(!disconnected); bt_hid_host_poll_control(); assert(disconnected==1 && !deleted_keys);
    setup(); fail_count[0x70]=1; bt_hid_host_handshake_tick();
    assert(primers==1 && hid_ctx.state==BT_HID_STATE_HANDSHAKE);
    tick=1000; immediate_response=true; bt_hid_host_handshake_tick();
    assert(primers==1 && edge_unlocks==1 && hid_ctx.state==BT_HID_STATE_CONNECTED);
    setup(); reset_on_send=true; reset_report=0x70; bt_hid_host_handshake_tick();
    assert(sent_count==5 && !primers && !hid_ctx.handshake.primer_done);
    event_count=0; bt_hid_host_handshake_tick();
    assert(primers==1 && event_count==6 && !memcmp(events,initial,sizeof(initial)));
    setup(); reset_on_send=true; bt_hid_host_handshake_tick();
    assert(sent_count==1 && hid_ctx.handshake.sent==0);
    assert(!primers);
    bt_hid_host_handshake_tick(); assert(sent_count==6);
    control.conn=&other; reply(0x70); reject();
    assert(!edge_unlocks && hid_ctx.state==BT_HID_STATE_HANDSHAKE && hid_ctx.check_dse);

    setup(); hid_ctx.state=BT_HID_STATE_CONNECTING;
    tick=7999; assert(!bt_hid_host_poll_security_ready() && !deleted_keys);
    tick=8000; assert(!bt_hid_host_poll_security_ready());
    tick=60000; assert(!bt_hid_host_poll_security_ready());
    assert(!disconnected && !deleted_keys && hid_ctx.state==BT_HID_STATE_CONNECTING);
    setup(); hid_ctx.state=BT_HID_STATE_CONNECTING; tick=8000;
    current.encrypt=true; current.sec_level=BT_SECURITY_L2; hid_ctx.outgoing=true;
    assert(bt_hid_host_poll_security_ready() && hid_ctx.security_done);
    assert(hid_ctx.security_done_tick==8000 && hid_ctx.sdp_pending && !disconnected && !deleted_keys);

    setup(); auth_event(BT_HCI_ERR_AUTH_FAIL,1);
    assert(notifications==1 && deleted_keys==1 && disconnected==1 && current.refs==1);
    setup(); auth_event(BT_HCI_ERR_PIN_OR_KEY_MISSING,1);
    assert(deleted_keys==1 && disconnected==1);
    setup(); auth_event(8,1); /* connection timeout */
    assert(notifications==1 && !deleted_keys && disconnected==1);
    setup(); auth_event(BT_HCI_ERR_PAIRING_NOT_ALLOWED,1);
    assert(!deleted_keys && disconnected==1);
    setup(); auth_event(BT_HCI_ERR_AUTH_FAIL,2);
    assert(!deleted_keys && !disconnected && other.refs==1);
    setup(); auth_event(0,1); assert(encryptions==1 && !notifications && !deleted_keys);
    setup(); auth_pairing_failed(&current,BT_SECURITY_ERR_PIN_OR_KEY_MISSING); bt_hid_host_poll_control();
    assert(deleted_keys==1 && disconnected==1);
    setup(); auth_pairing_failed(&current,BT_SECURITY_ERR_UNSPECIFIED); bt_hid_host_poll_control();
    assert(!deleted_keys && disconnected==1);
    setup(); security_changed(&current,BT_SECURITY_L1,0); assert(!hid_ctx.security_done);
    setup(); immediate_response=true; primer_failures=1;
    bt_hid_host_handshake_tick();
    assert(!primers && !hid_ctx.handshake.primer_done && hid_ctx.handshake.detection_ready);
    tick=100; bt_hid_host_handshake_tick();
    assert(primers==1 && hid_ctx.state==BT_HID_STATE_CONNECTED && sent_count==5);
    setup(); primer_failures=10;
    for(unsigned i=0;i<6;i++) { tick=i*1000; bt_hid_host_handshake_tick(); }
    assert(!primers && hid_ctx.handshake.probe_attempts==6);
    primer_failures=0; tick=6000; bt_hid_host_handshake_tick();
    assert(primers==1 && hid_ctx.handshake.primer_done);
    reply(0x70); bt_hid_host_handshake_tick(); assert(hid_ctx.state==BT_HID_STATE_CONNECTED);
    puts("PASS: GET batch -> 0x32 -> detection/unlock, synchronous replies, reconnects, retries, SDK auth errors and timeout bond preservation");
}
'''

original = SDK / "components/wireless/bluetooth/blestack/src/host/hci_core.c"
before = original.read_bytes()
with tempfile.TemporaryDirectory(prefix="ds5-handshake-test-") as directory:
    work = Path(directory)
    patched = work / "hci_core.c"
    script = work / "patch.cmake"
    script.write_text(f'include("{ROOT / "cmake/bt_auth_notify.cmake"}")\n'
                      f'ds5_patch_bt_auth("{original}" "{patched}")\n')
    subprocess.run(["cmake", "-P", str(script)], check=True)
    patched_text = patched.read_text()
    actual_auth = extract(patched_text, "static void auth_complete(")
    old_auth = extract(before.decode(), "static void auth_complete(")
    actual_io = extract(patched_text, "static void io_capa_req(")
    old_io = extract(before.decode(), "static void io_capa_req(")
    assert patched_text.replace(actual_auth, old_auth).replace(actual_io, old_io) == before.decode()
    assert "cp->authentication = BT_HCI_GENERAL_BONDING;" in actual_io
    assert actual_auth.count("bt_conn_security_changed(conn, security_err_get(evt->status));") == 1
    again = work / "again.c"
    script.write_text(f'include("{ROOT / "cmake/bt_auth_notify.cmake"}")\n'
                      f'ds5_patch_bt_auth("{patched}" "{again}")\n')
    subprocess.run(["cmake", "-P", str(script)], check=True)
    assert again.read_bytes() == patched.read_bytes()
    c = work / "test.c"
    c.write_text(support + "\n" + extract(patched_text, "static enum bt_security_err security_err_get(")
                 + "\n" + actual_auth + tests)
    subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                    "-fsanitize=address,undefined", str(c), "-o", str(work / "test")], check=True)
    subprocess.run([str(work / "test")], check=True)
assert original.read_bytes() == before
print("PASS: build-local SDK patch is isolated/idempotent; original SDK is unchanged")

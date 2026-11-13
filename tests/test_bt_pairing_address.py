"""Exercise BR pairing, key-store synchronization and active-address migration."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "src/bt_hid_host.c").read_text()


def extract(signature):
    start = SOURCE.index(signature)
    end = SOURCE.index("{", start) + 1
    depth = 1
    while depth:
        depth += (SOURCE[end] == "{") - (SOURCE[end] == "}")
        end += 1
    return SOURCE[start:end]


harness = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef struct { uint8_t val[6]; } bt_addr_t;
typedef struct { uint8_t type; bt_addr_t a; } bt_addr_le_t;
/* Relevant SDK union layout: LE has a type byte before the address;
 * BR has the address first, followed by the peer's IO capability. */
struct bt_conn {
    unsigned refs;
    union {
        struct { bt_addr_le_t dst; } le;
        struct { bt_addr_t dst; uint8_t remote_io_capa; } br;
    };
};
struct bt_conn_info { struct { const bt_addr_t *dst; } br; };
#define LOG_INF(...) ((void)0)
#define LOG_WRN(...) ((void)0)
static unsigned critical_depth;
#define taskENTER_CRITICAL() (++critical_depth)
#define taskEXIT_CRITICAL() do { assert(critical_depth); --critical_depth; } while (0)
struct bt_br_bond_info { const bt_addr_t *addr; };
static bt_addr_t keys[8], saved_address;
static unsigned key_count, writes;
static uint8_t legacy_index;
static bool have_address, have_legacy, info_error, fail_write;
static void (*during_write)(void);
#define BT_L2CAP_DISCONNECTED 0
struct bt_l2cap_br_chan { struct { struct bt_conn *conn; unsigned state, psm; } chan; };
enum bt_hid_host_state { BT_HID_STATE_IDLE, BT_HID_STATE_SCANNING, BT_HID_STATE_DISCONNECTING };
static struct {
    struct bt_conn *conn;
    bool acl_disconnected;
    struct bt_l2cap_br_chan ctrl_chan, intr_chan;
    unsigned connect_pending_tick;
    bool has_target;
    bt_addr_t target_addr;
    enum bt_hid_host_state state;
    bool ctrl_connected, intr_connected, ctrl_accepting, intr_accepting;
    bool outgoing, is_dse, check_dse, security_pending, security_done;
    bool sdp_pending, connect_pending;
    unsigned handshake;
} hid_ctx;
#define BT_HCI_ERR_REMOTE_USER_TERM_CONN 0x13
#define BT_ADDR_STR_LEN 18
static int blacklist_count, cached_rssi;
static bool switch_pending, first_intr_logged, first_output_logged, clear_bonds_pending;
static void clear_bond_storage(void);
void bt_hid_host_disconnect(void);
static void bt_br_discovery_stop(void) {}
static int bt_conn_disconnect(struct bt_conn *conn, int reason) { (void)conn; (void)reason; return 0; }
static struct bt_conn *bt_conn_ref(struct bt_conn *conn) { ++conn->refs; return conn; }
static void bt_conn_unref(struct bt_conn *conn) { assert(conn->refs); --conn->refs; }
static void bt_br_set_connectable(bool enabled) { (void)enabled; }
static void bt_br_set_discoverable(bool enabled) { (void)enabled; }
static void feature_cache_clear(void) {}
static void dse_reset(void) {}
static void app_tx_fifo_reset(void) {}
static void set_state(int state) { hid_ctx.state=state; }
static void bt_addr_to_str(const bt_addr_t *addr, char *out, size_t len) { out[0]=0; }
enum bt_security_err { BT_SECURITY_ERR_AUTH_FAIL, BT_SECURITY_ERR_PIN_OR_KEY_MISSING,
                       BT_SECURITY_ERR_UNSPECIFIED };
static int bt_settings_get_bin(const char *name, void *value, size_t len, size_t *actual) {
    assert(!critical_depth);
    if (!strcmp(name,"active_addr") && have_address) {
        assert(len>=6); memcpy(value,&saved_address,6); *actual=6; return 0;
    }
    if (!strcmp(name,"active_ctrl") && have_legacy) {
        assert(len>=1); *(uint8_t *)value=legacy_index; *actual=1; return 0;
    }
    *actual=0; return -1;
}
static int bt_settings_set_bin(const char *name, const void *value, size_t len) {
    assert(!critical_depth && !strcmp(name,"active_addr") && len==6);
    if (fail_write) return -1;
    memcpy(&saved_address,value,6); have_address=true; ++writes;
    if (during_write) { void (*callback)(void)=during_write; during_write=NULL; callback(); }
    return 0;
}
static int settings_delete(const char *name) {
    assert(!critical_depth && !strcmp(name,"active_ctrl")); have_legacy=false; return 0;
}
static void bt_br_foreach_bond(void (*cb)(const struct bt_br_bond_info *, void *), void *arg) {
    assert(critical_depth);
    for(unsigned i=0;i<key_count;i++) { struct bt_br_bond_info info={&keys[i]}; cb(&info,arg); }
}
void bt_keys_link_key_clear_addr(const bt_addr_t *addr) {
    if (!addr) { key_count=0; return; }
    for(unsigned i=0;i<key_count;i++) {
        if (!memcmp(addr,&keys[i],6)) {
            memmove(&keys[i],&keys[i+1],(--key_count-i)*sizeof(keys[0])); return;
        }
    }
}
static bool blacklist_dirty;
static void blacklist_persist(void) { assert(!critical_depth); }
static int bt_conn_get_info(const struct bt_conn *conn, struct bt_conn_info *info) {
    if (info_error) return -1;
    info->br.dst = &conn->br.dst;
    return 0;
}
/* Match the SDK API so the old callback compiles but fails the address test. */
static const bt_addr_le_t *bt_conn_get_dst(const struct bt_conn *conn) {
    return &conn->le.dst;
}
static int bt_addr_cmp(const bt_addr_t *a, const bt_addr_t *b) {
    return memcmp(a, b, sizeof(*a));
}
static void bt_addr_copy(bt_addr_t *a, const bt_addr_t *b) { *a = *b; }
'''
harness += SOURCE[SOURCE.index("#define MAX_BONDED_CONTROLLERS 8"):
                  SOURCE.index("/* ---------- internal state ---------- */")]
for signature in (
    "static const bt_addr_t *conn_get_br_addr(",
    "static void auth_pairing_complete(",
    "static void clear_rejected_key(",
    "void bt_hid_host_persist_if_dirty(",
    "int bt_hid_host_try_reconnect(",
    "int bt_hid_host_switch_next(",
    "void bt_hid_host_disconnect(",
    "static void conn_disconnected(",
    "static void clear_bond_storage(void)\n{",
    "static bool hid_channel_released(",
    "bool bt_hid_host_poll_disconnect(",
    "void bt_hid_host_clear_bonds(",
):
    harness += "\n" + extract(signature) + "\n"
harness += r'''
static void expect_active(bt_addr_t addr) {
    assert(bt_addr_cmp(&active_addr,&addr)==0);
    if (bonded_count) assert(bt_addr_cmp(&bonded_list[active_idx],&addr)==0);
    else assert(active_idx==0);
}
static void select_first_during_write(void) {
    taskENTER_CRITICAL(); bonded_list_select_index(0); taskEXIT_CRITICAL();
}
int main(void) {
    struct bt_conn first = {.refs=1}, second = {.refs=1};
    const bt_addr_t first_addr = {{1, 2, 3, 4, 5, 6}};
    const bt_addr_t second_addr = {{0xa1, 0xb2, 0xc3, 0xd4, 0xe5, 0xf6}};
    const bt_addr_t zero = {{0}};
    first.br.dst = first_addr; first.br.remote_io_capa = 3;
    second.br.dst = second_addr; second.br.remote_io_capa = 0;

    /* Migrate the previous index once, then survive SDK slot reordering. */
    keys[0]=first_addr; keys[1]=second_addr; key_count=2;
    have_legacy=true; legacy_index=1;
    bonded_list_load(); expect_active(second_addr);
    assert(writes==1 && !have_legacy && !memcmp(&saved_address,&second_addr,6));
    keys[0]=second_addr; keys[1]=first_addr;
    have_legacy=true; legacy_index=1; /* Stale legacy state must not override address. */
    bonded_list_load(); expect_active(second_addr);
    assert(active_idx==0 && writes==1);

    /* Removing a different bond keeps the same selected identity. */
    hid_ctx.conn=&first;
    clear_rejected_key(&first,BT_SECURITY_ERR_AUTH_FAIL);
    assert(bonded_count==1 && bonded_list_find_addr(&first_addr)==-1);
    expect_active(second_addr);
    bt_hid_host_persist_if_dirty(); assert(writes==1);
    keys[1]=first_addr; key_count=2;
    hid_ctx.conn=&second;
    clear_rejected_key(&second,BT_SECURITY_ERR_PIN_OR_KEY_MISSING);
    expect_active(first_addr);
    assert(bonded_count==1 && hid_ctx.has_target && !memcmp(&hid_ctx.target_addr,&first_addr,6));
    bt_hid_host_persist_if_dirty(); assert(!memcmp(&saved_address,&first_addr,6));
    hid_ctx.conn=&first;
    clear_rejected_key(&first,BT_SECURITY_ERR_AUTH_FAIL);
    expect_active(zero); assert(!bonded_count && !hid_ctx.has_target);
    bt_hid_host_persist_if_dirty(); assert(!memcmp(&saved_address,&zero,6));
    bonded_list_load(); expect_active(zero);

    /* Pairing callback precedes Link Key Notify: no phantom bond or slot. */
    hid_ctx.conn=&first;
    auth_pairing_complete(&first,true);
    bt_hid_host_persist_if_dirty();
    assert(!bonded_count && pairing_addr_pending);
    keys[0]=first_addr; key_count=1;
    bt_hid_host_persist_if_dirty();
    assert(bonded_count==1 && !pairing_addr_pending);
    expect_active(first_addr);
    assert(bonded_list_find_addr(&first_addr)==0 && !memcmp(&saved_address,&first_addr,6));

    keys[1]=second_addr; key_count=2; hid_ctx.conn=&second;
    auth_pairing_complete(&second,true); bt_hid_host_persist_if_dirty();
    expect_active(second_addr);
    first.br.remote_io_capa=1; hid_ctx.conn=&first;
    auth_pairing_complete(&first,true); bt_hid_host_persist_if_dirty();
    expect_active(first_addr); assert(bonded_count==2);
    unsigned before=writes;
    auth_pairing_complete(&second,true); /* Not the owned ACL. */
    auth_pairing_complete(&first,false);
    info_error=true; auth_pairing_complete(&first,true); info_error=false;
    bt_hid_host_persist_if_dirty();
    assert(writes==before && !pairing_addr_pending);

    /* A full SDK key table replaces a slot: choose the actual new peer, not 0. */
    for(unsigned i=0;i<8;i++) keys[i]=(bt_addr_t){{(uint8_t)(0x20+i),2,3,4,5,6}};
    key_count=8; active_addr=keys[4]; bonded_list_refresh();
    expect_active(keys[4]);
    hid_ctx.conn=&second; auth_pairing_complete(&second,true);
    bt_hid_host_persist_if_dirty(); assert(active_idx==4 && pairing_addr_pending);
    bt_addr_t evicted=keys[6]; keys[6]=second_addr;
    bt_hid_host_persist_if_dirty();
    assert(bonded_count==8 && active_idx==6 && bonded_list_find_addr(&evicted)==-1);
    expect_active(second_addr);
    bt_addr_t swap=keys[0]; keys[0]=keys[6]; keys[6]=swap;
    bonded_list_load(); expect_active(second_addr); assert(active_idx==0);

    /* SDK deletion outside our callback is picked up by a requested refresh. */
    bt_keys_link_key_clear_addr(&second_addr); bonded_refresh_pending=true;
    bt_hid_host_persist_if_dirty();
    assert(bonded_count==7 && bonded_list_find_addr(&second_addr)==-1);
    expect_active(keys[0]);

    /* Failed saves retry; a selection change during Flash write is not lost. */
    taskENTER_CRITICAL(); bonded_list_select_index(1); taskEXIT_CRITICAL();
    fail_write=true; bt_hid_host_persist_if_dirty(); assert(active_addr_dirty);
    fail_write=false; during_write=select_first_during_write;
    bt_hid_host_persist_if_dirty(); assert(active_addr_dirty);
    bt_hid_host_persist_if_dirty();
    assert(!active_addr_dirty && !memcmp(&saved_address,&keys[0],6));

    /* Public switch/reconnect paths use refreshed keys and persist the address. */
    hid_ctx.conn=NULL; hid_ctx.state=BT_HID_STATE_IDLE;
    assert(bt_hid_host_switch_next()==1); expect_active(keys[1]);
    assert(!memcmp(&saved_address,&keys[1],6));
    swap=keys[1]; keys[1]=keys[3]; keys[3]=swap;
    assert(bt_hid_host_try_reconnect()==0 && active_idx==3);
    assert(hid_ctx.has_target && !memcmp(&hid_ctx.target_addr,&swap,6));

    /* Clearing bonds cancels an in-flight selection and clears its persistence. */
    hid_ctx.conn=&first; auth_pairing_complete(&first,true);
    assert(pairing_addr_pending);
    bt_hid_host_clear_bonds();
    assert(hid_ctx.conn==&first && key_count && clear_bonds_pending);
    assert(hid_ctx.state==BT_HID_STATE_DISCONNECTING && !bt_hid_host_poll_disconnect());
    conn_disconnected(&first,0x13);
    assert(bt_hid_host_poll_disconnect() && first.refs==0);
    assert(!key_count && !bonded_count && !pairing_addr_pending && !hid_ctx.has_target);
    expect_active(zero); assert(!memcmp(&saved_address,&zero,6));
    auth_pairing_complete(&first,true); /* Late callback from the cleared ACL. */
    bt_hid_host_persist_if_dirty(); assert(!pairing_addr_pending && !bonded_count);
    assert(bt_hid_host_try_reconnect()==-1 && bt_hid_host_switch_next()==-1);
    bonded_list_load(); expect_active(zero);
    assert(!critical_depth);
    puts("PASS: BR address, deferred keys, full-table replacement, deletion, address migration/reorder, save retry and concurrent selection");
}

'''
with tempfile.TemporaryDirectory(prefix="ds5-pairing-address-") as directory:
    work = Path(directory)
    source = work / "test.c"
    executable = work / "test"
    source.write_text(harness)
    subprocess.run(["cc", "-std=c11", "-fsanitize=address,undefined",
                    str(source), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)

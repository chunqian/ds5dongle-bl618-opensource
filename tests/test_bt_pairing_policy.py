"""Exercise patched SDK SSP replies and production legacy PIN callback."""
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


harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
typedef uint8_t u8_t;
typedef struct { uint8_t val[6]; } bt_addr_t;
struct bt_conn { unsigned refs, flags; } current, other;
struct bt_hci_evt_io_capa_req { bt_addr_t bdaddr; };
struct bt_hci_cp_io_capability_reply { bt_addr_t bdaddr; uint8_t capability, oob_data, authentication; };
struct net_buf { void *data; };
#define BT_DBG(...) ((void)0)
#define BT_ERR(...) ((void)0)
#define LOG_INF(...) ((void)0)
#define LOG_WRN(...) ((void)0)
#define BT_HCI_GENERAL_BONDING 4
#define BT_HCI_OP_IO_CAPABILITY_REPLY 0x042b
static bool fail_alloc, no_peer;
static unsigned sends, pin_replies, confirms, disconnects;
static int pin_error;
static struct bt_hci_cp_io_capability_reply reply;
static struct net_buf response={&reply};
static struct bt_conn *bt_conn_lookup_addr_br(const bt_addr_t *addr) {
    (void)addr; if(no_peer) return NULL; ++current.refs; return &current;
}
static void bt_conn_unref(struct bt_conn *conn) { assert(conn->refs>1); --conn->refs; }
static struct net_buf *bt_hci_cmd_create(unsigned op, unsigned size) {
    assert(op==BT_HCI_OP_IO_CAPABILITY_REPLY && size==sizeof(reply));
    return fail_alloc ? NULL : &response;
}
static void *net_buf_add(struct net_buf *buf, unsigned size) { assert(size==sizeof(reply)); return buf->data; }
static void bt_addr_copy(bt_addr_t *dst,const bt_addr_t *src) { *dst=*src; }
static uint8_t bt_conn_get_io_capa(void) { return 1; } /* Display Yes/No. */
static int bt_hci_cmd_send_sync(unsigned op,struct net_buf *buf,void *rsp) {
    assert(op==BT_HCI_OP_IO_CAPABILITY_REPLY && buf==&response && !rsp); ++sends; return 0;
}
enum { BT_HID_STATE_CONNECTING, BT_HID_STATE_DISCONNECTING };
static struct { struct bt_conn *conn; unsigned state; } hid_ctx;
static int bt_conn_auth_pincode_entry(struct bt_conn *conn,const char *pin) {
    assert(conn==&current && !strcmp(pin,"0000")); ++pin_replies; return pin_error;
}
static void bt_conn_auth_passkey_confirm(struct bt_conn *conn) { assert(conn==&current); ++confirms; }
static void bt_hid_host_disconnect(void) { ++disconnects; }
'''
tests = r'''
int main(void) {
    current.refs=1;
    /* Both initiator flags: the wire reply always has General Bonding/no MITM. */
    for(unsigned initiator=0; initiator<2; ++initiator) {
        current.flags=initiator;
        struct bt_hci_evt_io_capa_req evt={{{1,2,3,4,5,6}}};
        struct net_buf request={&evt};
        memset(&reply,0xff,sizeof(reply));
        io_capa_req(&request);
        assert(reply.authentication==4 && reply.capability==1 && reply.oob_data==0);
        assert(!memcmp(&reply.bdaddr,&evt.bdaddr,sizeof(bt_addr_t)) && current.refs==1);
    }
    assert(sends==2);
    struct bt_hci_evt_io_capa_req evt={0}; struct net_buf request={&evt};
    fail_alloc=true; io_capa_req(&request);
    assert(current.refs==1 && sends==2);
    no_peer=true; io_capa_req(&request); assert(sends==2);

    hid_ctx.conn=&current; hid_ctx.state=BT_HID_STATE_CONNECTING;
    auth_pincode_entry(&current,false); assert(pin_replies==1 && !disconnects);
    auth_passkey_confirm(&current,123456); assert(confirms==1);
    auth_pincode_entry(&other,false); auth_passkey_confirm(&other,123456);
    assert(pin_replies==1 && confirms==1);
    hid_ctx.state=BT_HID_STATE_DISCONNECTING;
    auth_pincode_entry(&current,false); auth_passkey_confirm(&current,123456);
    assert(pin_replies==1 && confirms==1);
    hid_ctx.state=BT_HID_STATE_CONNECTING; pin_error=-EPERM;
    auth_pincode_entry(&current,true);
    assert(pin_replies==2 && disconnects==1); /* Unsupported secure legacy PIN fails cleanly. */
    puts("PASS: General Bonding/no MITM SSP wire reply, legacy PIN 0000, stale auth guards and failed PIN cleanup");
}
'''
assert ".pincode_entry     = auth_pincode_entry," in SOURCE
original = SDK / "components/wireless/bluetooth/blestack/src/host/hci_core.c"
before = original.read_bytes()
with tempfile.TemporaryDirectory(prefix="ds5-pairing-policy-") as directory:
    work = Path(directory)
    patched = work / "hci_core.c"
    script = work / "patch.cmake"
    script.write_text(f'include("{ROOT / "cmake/bt_auth_notify.cmake"}")\n'
                      f'ds5_patch_bt_auth("{original}" "{patched}")\n')
    subprocess.run(["cmake", "-P", str(script)], check=True)
    harness += extract(patched.read_text(), "static void io_capa_req(") + "\n"
    harness += extract(SOURCE, "static void auth_pincode_entry(") + "\n"
    harness += extract(SOURCE, "static void auth_passkey_confirm(") + "\n"
    (work / "test.c").write_text(harness + tests)
    subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-unused-parameter", "-fsanitize=address,undefined",
                    str(work / "test.c"), "-o", str(work / "test")], check=True)
    subprocess.run([str(work / "test")], check=True)
assert original.read_bytes() == before

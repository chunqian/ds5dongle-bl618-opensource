"""Run the SDK's actual Authentication Complete handler through firmware cleanup.

python3 tests/test_bt_auth_notify.py
Requires the sibling bouffalo_sdk checkout (or BL_SDK_BASE), CMake and a C compiler.
The build-time SDK patch is applied only to temporary copies, just as in a build.
"""
from pathlib import Path
import os
import runpy
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
sdk = Path(os.environ.get('BL_SDK_BASE', root.parent / 'bouffalo_sdk'))
original = sdk / 'components/wireless/bluetooth/blestack/src/host/hci_core.c'
before = original.read_bytes()
fixture = runpy.run_path(str(root / 'tests/test_bt_reconnect.py'))
harness = fixture['harness'].split('int main(void) {', 1)[0]


def function(source, signature):
    start = source.index(signature)
    return source[start:source.index('\n}\n', start) + 3]


support = r'''
#define BT_DBG(...) ((void)0)
#define BT_ERR(...) ((void)0)
#define BT_HCI_ERR_SUCCESS 0
#define BT_HCI_ERR_PIN_OR_KEY_MISSING 6
#define BT_HCI_ERR_PAIRING_NOT_SUPPORTED 0x29
#define BT_HCI_ERR_PAIRING_NOT_ALLOWED 0x18
#define BT_HCI_ERR_INVALID_PARAM 0x12
#define BT_SECURITY_ERR_PAIR_NOT_SUPPORTED ((enum bt_security_err)4)
#define BT_SECURITY_ERR_PAIR_NOT_ALLOWED ((enum bt_security_err)5)
#define BT_SECURITY_ERR_INVALID_PARAM ((enum bt_security_err)6)
typedef uint8_t u8_t;
typedef uint16_t u16_t;
#define sys_le16_to_cpu(v) (v)
struct bt_hci_evt_auth_complete { uint8_t status; uint16_t handle; };
static unsigned security_notifications, encryption_requests;
static bool fail_channel_first;
static struct bt_conn *bt_conn_lookup_handle(uint16_t handle) {
    return handle==1 ? bt_conn_ref(&current) : handle==2 ? bt_conn_ref(&other) : NULL;
}
static void reset_pairing(struct bt_conn *c) { c->required_sec_level=c->sec_level; }
static void bt_l2cap_encrypt_change(struct bt_conn *c, uint8_t status) {
    (void)c; (void)status;
    if (fail_channel_first) l2cap_ctrl_disconnected(&hid_ctx.ctrl_chan.chan);
}
static void bt_conn_security_changed(struct bt_conn *c, enum bt_security_err err) {
    ++security_notifications;
    security_changed(c,c->sec_level,err);
}
static void link_encr(uint16_t handle) { assert(handle==1); ++encryption_requests; }
'''
cases = r'''
static void event(unsigned status, unsigned handle) {
    struct net_buf packet={0};
    struct bt_hci_evt_auth_complete evt={.status=status,.handle=handle};
    memcpy(packet.data,&evt,sizeof(evt)); auth_complete(&packet);
}
int main(void) {
    const unsigned failures[]={5,6,8,0x18,0x29,0x12};
    for (unsigned early=0;early<2;early++) {
        for (unsigned i=0;i<sizeof(failures)/sizeof(failures[0]);i++) {
            setup(); hid_ctx.state=BT_HID_STATE_CONNECTING;
            current.sec_level=1; current.required_sec_level=2;
            fail_channel_first=early; security_notifications=0;
            event(failures[i],1);
            assert(security_notifications==1 && current.ref==2);
            assert(hid_ctx.teardown_pending && !deleted_keys);
            bool rejected_key=failures[i]==5 || failures[i]==6;
            assert(hid_ctx.clear_key_pending==rejected_key);
            bt_hid_host_poll_disconnect();
            assert(deleted_keys==(unsigned)rejected_key);
            if (rejected_key) assert(deleted_addr.val[0]==1);
            sdk_finish(); bt_hid_host_poll_disconnect();
            assert(!hid_ctx.conn && !current.ref && !resets);
        }
    }
    setup(); fail_channel_first=false; security_notifications=encryption_requests=0;
    event(0,1); assert(encryption_requests==1 && !security_notifications);
    assert(!hid_ctx.teardown_pending && !deleted_keys && current.ref==2);
    event(5,2); assert(!hid_ctx.clear_key_pending && !hid_ctx.teardown_pending && other.ref==2);
    unsigned notified=security_notifications;
    event(6,99); assert(security_notifications==notified && !hid_ctx.clear_key_pending);
    puts("PASS: real SDK AUTH_COMPLETE success, rejected keys, non-key errors, foreign/stale ACL and L2CAP-first teardown");
    return 0;
}
'''
with tempfile.TemporaryDirectory(prefix='ds5-auth-notify-') as directory:
    tmp = Path(directory)
    patched = tmp / 'hci_core.c'
    script = tmp / 'patch.cmake'
    script.write_text(f'include("{root / "cmake/bt_auth_notify.cmake"}")\n'
                      f'ds5_patch_bt_auth("${{SOURCE}}" "{patched}")\n')
    subprocess.run(['cmake', f'-DSOURCE={original}', '-P', str(script)], check=True)
    result = patched.read_bytes()
    mtime = patched.stat().st_mtime_ns
    subprocess.run(['cmake', f'-DSOURCE={patched}', '-P', str(script)], check=True)
    assert patched.read_bytes() == result and patched.stat().st_mtime_ns == mtime
    assert original.read_bytes() == before
    source = result.decode()
    actual_sdk = function(source, 'static enum bt_security_err security_err_get(')
    actual_sdk += function(source, 'static void auth_complete(')
    (tmp / 'test.c').write_text(harness + support + actual_sdk + cases)
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-function', '-Wno-unused-parameter', '-Wno-unused-variable',
                    '-fsanitize=address,undefined', '-I', str(root / 'src'),
                    str(tmp / 'test.c'), str(root / 'src/ds5_protocol.c'),
                    '-o', str(tmp / 'test')], check=True)
    subprocess.run([str(tmp / 'test')], check=True)

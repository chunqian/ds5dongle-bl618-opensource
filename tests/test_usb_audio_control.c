#include "usb_audio_control.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static void test_control(void)
{
    struct usb_audio_control p = {0};
    uint8_t cmd[63] = {4, 'D', 'A', 'U', 'D', 1, 1};
    uint8_t status[8];
    assert(!p.visible && !p.pending);
    usb_audio_control_configured(&p);
    for (size_t n = 0; n < 7; n++)
        assert(!usb_audio_control_command(&p, cmd, n, 0));
    cmd[5] = 2;
    assert(!usb_audio_control_command(&p, cmd, sizeof(cmd), 0));
    cmd[5] = 1; cmd[6] = 2;
    assert(!usb_audio_control_command(&p, cmd, sizeof(cmd), 0));
    cmd[6] = 1;
    assert(usb_audio_control_command(&p, cmd, sizeof(cmd), 100));
    assert(!p.visible && p.pending && p.requested);
    assert(!usb_audio_control_ready(&p, 100099));
    /* Duplicate SET must not delay the disconnect beyond the original grace. */
    assert(usb_audio_control_command(&p, cmd, sizeof(cmd), 100000));
    assert(usb_audio_control_ready(&p, 100100));
    usb_audio_control_status(&p, status);
    assert(!memcmp(status, "DAUD\1\0\1\1", 8));
    usb_audio_control_apply(&p, true, p.generation, 400000);
    usb_audio_control_reset(&p, 410000);
    usb_audio_control_reset(&p, 420000);
    assert(p.visible && p.requested && !p.pending);
    usb_audio_control_configured(&p);
    assert(!usb_audio_control_expired(&p, 10000000));
    assert(usb_audio_control_command(&p, cmd, sizeof(cmd), 10000000));
    assert(!p.pending); /* App sees the reconnected device: no reconnect loop. */
    cmd[6] = 0;
    assert(usb_audio_control_command(&p, cmd, sizeof(cmd), 11000000));
    assert(p.visible && p.pending && !p.requested);
    cmd[6] = 1;
    assert(usb_audio_control_command(&p, cmd, sizeof(cmd), 11000001));
    assert(!p.pending); /* opposite command can cancel a pending transition */
    cmd[6] = 0;
    usb_audio_control_command(&p, cmd, sizeof(cmd), 11000002);
    usb_audio_control_apply(&p, false, p.generation, 11400000);
    usb_audio_control_reset(&p, 11410000);
    assert(!p.visible);
    usb_audio_control_apply(&p, true, p.generation, 12000000);
    assert(usb_audio_control_expired(&p, 15000000));
    usb_audio_control_reset(&p, 15000000);
    assert(p.visible && p.revoke_pending); /* Keep layout until detached. */
    usb_audio_control_apply(&p, false, p.generation, 15000001);
    assert(!p.visible);
    usb_audio_control_apply(&p, true, p.generation, 16000000);
    usb_audio_control_configured(&p);
    usb_audio_control_reset(&p, 16100000);
    assert(p.visible && !p.requested && p.revoke_pending);
    assert(usb_audio_control_ready(&p, 16100000)); /* even before CONFIGURED */
    cmd[6] = 1;
    assert(!usb_audio_control_command(&p, cmd, sizeof(cmd), 16100001));
    usb_audio_control_configured(&p);
    assert(p.revoke_pending); /* Late SET_CONFIGURATION cannot cancel revoke. */
    usb_audio_control_apply(&p, false, p.generation, 16400000);
    assert(!p.visible && !p.requested); /* New host via powered hub. */
    usb_audio_control_apply(&p, true, p.generation, 17000000);
    usb_audio_control_disconnect(&p);
    usb_audio_control_reset(&p, 17100000);
    assert(!p.visible); /* Physical unplug always revokes permission. */
}

static void test_stale_authorization(void)
{
    struct usb_audio_control p = {0};
    uint8_t cmd[] = {4, 'D', 'A', 'U', 'D', 1, 1};
    for (unsigned event = 0; event < 3; event++) {
        usb_audio_control_disconnect(&p);
        cmd[6] = 1;
        assert(usb_audio_control_command(&p, cmd, sizeof(cmd), 0));
        uint32_t token = p.generation;
        if (event == 0)
            usb_audio_control_reset(&p, 200000);
        else if (event == 1)
            usb_audio_control_disconnect(&p);
        else {
            cmd[6] = 0;
            usb_audio_control_command(&p, cmd, sizeof(cmd), 200000);
        }
        assert(p.generation != token);
        usb_audio_control_apply(&p, true, token, 350000);
        assert(!p.visible && !p.requested && !p.pending);
        assert(!p.grant_until_us);
        /* A new command after the new HID enumeration still works. */
        cmd[6] = 1;
        assert(usb_audio_control_command(&p, cmd, sizeof(cmd), 400000));
        usb_audio_control_apply(&p, true, p.generation, 750000);
        assert(p.visible && p.requested && !p.pending);
    }
}

static void test_descriptors(void)
{
    /* Audio with alt settings + gamepad + optional keyboard, as on this dongle. */
    uint8_t full[] = {
        9,2,0,0,5,1,0,0xe0,250,
        9,4,0,0,0,1,1,0,0, 9,0x24,1,0,1,9,0,1,1,
        9,4,1,0,0,1,2,0,0,
        9,4,1,1,1,1,2,0,0, 7,5,1,1,192,0,1,
        9,4,2,0,0,1,2,0,0,
        9,4,2,1,1,1,2,0,0, 7,5,0x82,1,192,0,1,
        9,4,3,0,1,3,0,0,0, 9,0x21,0x11,1,0,1,0x22,0xbd,1,
        7,5,0x84,3,64,0,3,
        9,4,4,0,1,3,0,0,0, 9,0x21,0x11,1,0,1,0x22,138,0,
        7,5,0x83,3,9,0,10
    };
    full[2] = sizeof(full);
    uint8_t out[64];
    size_t n = usb_audio_hid_descriptor(full, sizeof(full), out, sizeof(out));
    assert(n == 59 && out[2] == 59 && out[3] == 0 && out[4] == 2);
    assert(out[7] == 0xe0 && out[11] == 0 && out[36] == 1);
    assert(out[25] == 0xbd && out[26] == 1); /* Edge report length preserved */
    assert(out[29] == 0x84 && out[33] == 3);
    assert(out[54] == 0x83 && out[58] == 10);
    for (size_t pos = 9; pos < n; pos += out[pos]) {
        assert(out[pos + 1] != 0x24 && out[pos + 1] != 0x25);
        if (out[pos + 1] == 4) assert(out[pos + 5] == 3);
        if (out[pos + 1] == 5) assert((out[pos + 3] & 3) == 3);
    }
    /* Keyboard disabled: header length bounds, not backing array length. */
    full[2] -= 25;
    full[4] = 4;
    assert(usb_audio_hid_descriptor(full, sizeof(full), out, sizeof(out)) == 34);
    assert(out[4] == 1);
    assert(usb_audio_hid_descriptor(full, sizeof(full), out, 33) == 0);
    full[9] = 0; /* malformed descriptor must not leak an audio configuration */
    assert(usb_audio_hid_descriptor(full, sizeof(full), out, sizeof(out)) == 0);
    assert(usb_audio_hid_descriptor(NULL, 0, out, sizeof(out)) == 0);
}

int main(void)
{
    test_control();
    test_stale_authorization();
    test_descriptors();
    puts("PASS: HID audio commands, session resets, idempotency, HID-only descriptors");
}

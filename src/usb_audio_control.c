#include "usb_audio_control.h"
#include <string.h>

void usb_audio_control_disconnect(struct usb_audio_control *p)
{
    uint32_t generation = p->generation + 1;
    memset(p, 0, sizeof(*p));
    p->generation = generation;
}

void usb_audio_control_reset(struct usb_audio_control *p, uint64_t now)
{
    /* Only resets within our explicit re-enumeration may retain audio.
     * CONFIGURED consumes this grant. Revoke it on a later reset, but keep
     * the current descriptor layout until a deliberate detach/reconnect. */
    uint64_t grant = p->grant_until_us;
    bool visible = p->visible;
    usb_audio_control_disconnect(p);
    p->visible = visible;
    if (grant && now < grant) {
        p->visible = p->requested = visible;
        p->grant_until_us = grant;
    } else if (visible) {
        p->pending = p->revoke_pending = true;
        p->request_us = now;
    }
}

void usb_audio_control_configured(struct usb_audio_control *p)
{
    p->grant_until_us = 0;
}

bool usb_audio_control_command(struct usb_audio_control *p,
                               const uint8_t *payload, size_t len, uint64_t now)
{
    /* F6 body: 04 'D' 'A' 'U' 'D' 01 enabled; HID padding is permitted. */
    static const uint8_t prefix[] = {4, 'D', 'A', 'U', 'D', 1};
    if (!payload || len < 7 || memcmp(payload, prefix, sizeof(prefix)) || payload[6] > 1)
        return false;
    /* A fresh command may authorize audio only after the revoked session
     * has completed its forced HID-only reconnect. */
    if (p->revoke_pending)
        return false;
    bool enabled = payload[6] != 0;
    if (p->requested != enabled)
        p->generation++;
    /* Repeated writes must not postpone a pending transition indefinitely. */
    if (p->requested != enabled || !p->pending)
        p->request_us = now;
    p->requested = enabled;
    p->pending = p->visible != enabled;
    return true;
}

bool usb_audio_control_ready(const struct usb_audio_control *p, uint64_t now)
{
    /* Let EP0's SET_REPORT status stage complete before disconnecting. */
    return p->revoke_pending ||
           (p->pending && now - p->request_us >= 100000ULL);
}

void usb_audio_control_apply(struct usb_audio_control *p, bool enabled,
                             uint32_t generation, uint64_t now)
{
    /* RESET, disconnect or a superseding command during the detached wait
     * invalidates the snapshot. Never carry its old authorization forward. */
    if (generation != p->generation)
        enabled = false;
    usb_audio_control_disconnect(p);
    p->visible = p->requested = enabled;
    p->grant_until_us = enabled ? now + 3000000ULL : 0;
}

bool usb_audio_control_expired(const struct usb_audio_control *p, uint64_t now)
{
    return p->visible && p->grant_until_us && now >= p->grant_until_us;
}

void usb_audio_control_status(const struct usb_audio_control *p, uint8_t out[8])
{
    const uint8_t status[] = {'D', 'A', 'U', 'D', 1,
                             p->visible, p->requested, p->pending};
    memcpy(out, status, sizeof(status));
}

size_t usb_audio_hid_descriptor(const uint8_t *src, size_t size,
                               uint8_t *dst, size_t capacity)
{
    if (!src || !dst || size < 9 || capacity < 9 || src[0] != 9 || src[1] != 2)
        return 0;
    size_t total = src[2] | ((size_t)src[3] << 8);
    if (total < 9 || total > size)
        return 0;
    memcpy(dst, src, 9);
    size_t used = 9;
    uint8_t interfaces = 0;
    bool keep = false;
    for (size_t pos = 9; pos < total; ) {
        uint8_t len = src[pos];
        if (len < 2 || pos + len > total)
            return 0;
        if (src[pos + 1] == 4) {
            if (len < 9)
                return 0;
            keep = src[pos + 5] == 3;
            /* This project's HID interfaces have only alternate setting 0. */
            if (keep && src[pos + 3] != 0)
                return 0;
        }
        if (keep) {
            if (used + len > capacity)
                return 0;
            memcpy(dst + used, src + pos, len);
            if (src[pos + 1] == 4)
                dst[used + 2] = interfaces++;
            used += len;
        }
        pos += len;
    }
    if (!interfaces)
        return 0;
    dst[2] = (uint8_t)used;
    dst[3] = (uint8_t)(used >> 8);
    dst[4] = interfaces;
    return used;
}

#ifndef USB_AUDIO_CONTROL_H
#define USB_AUDIO_CONTROL_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* USB visibility is session-only: a new host starts with HID, never audio. */
struct usb_audio_control {
    bool visible;
    bool requested;
    bool pending;
    bool revoke_pending;
    uint32_t generation;
    uint64_t request_us;
    uint64_t grant_until_us;
};
void usb_audio_control_reset(struct usb_audio_control *p, uint64_t now);
void usb_audio_control_disconnect(struct usb_audio_control *p);
void usb_audio_control_configured(struct usb_audio_control *p);
bool usb_audio_control_command(struct usb_audio_control *p,
                               const uint8_t *payload, size_t len, uint64_t now);
bool usb_audio_control_ready(const struct usb_audio_control *p, uint64_t now);
/* Called only after detaching; a stale generation always falls back to HID. */
void usb_audio_control_apply(struct usb_audio_control *p, bool enabled,
                             uint32_t generation, uint64_t now);
bool usb_audio_control_expired(const struct usb_audio_control *p, uint64_t now);
void usb_audio_control_status(const struct usb_audio_control *p, uint8_t out[8]);
size_t usb_audio_hid_descriptor(const uint8_t *src, size_t size,
                                uint8_t *dst, size_t capacity);
#endif

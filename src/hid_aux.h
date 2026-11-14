#ifndef HID_AUX_H
#define HID_AUX_H
#include <stdbool.h>
#include <stdint.h>
/* Caller serializes all access with the USB endpoint/IRQ lock. */
enum hid_aux_source { HID_AUX_REMAP, HID_AUX_PS, HID_AUX_WAKE, HID_AUX_SOURCES };
struct hid_aux {
    uint8_t keys[HID_AUX_SOURCES][8], sent_keys[8];
    uint8_t consumer, sent_consumer, buttons, sent_buttons, next;
    int32_t x, y, wheel;
    uint8_t pending[3][9], pending_len[3];
};
typedef int (*hid_aux_write_fn)(const uint8_t *, uint8_t);
void hid_aux_reset(struct hid_aux *s);
void hid_aux_release(struct hid_aux *s);
void hid_aux_keyboard(struct hid_aux *s, enum hid_aux_source source, const uint8_t *keys);
bool hid_aux_keyboard_sent(const struct hid_aux *s);
void hid_aux_consumer(struct hid_aux *s, uint8_t bits);
void hid_aux_mouse(struct hid_aux *s, uint8_t buttons, int8_t x, int8_t y, int8_t wheel);
/* One fair submission per call; failed writes leave all pending state intact. */
void hid_aux_flush(struct hid_aux *s, hid_aux_write_fn write);
#endif

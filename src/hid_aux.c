#include "hid_aux.h"
#include <string.h>

static void merged_keys(const struct hid_aux *s, uint8_t out[8])
{
    memset(out, 0, 8);
    unsigned used = 2;
    for (unsigned i = 0; i < HID_AUX_SOURCES; ++i) {
        out[0] |= s->keys[i][0];
        for (unsigned j = 2; j < 8; ++j) {
            uint8_t key = s->keys[i][j];
            if (!key) continue;
            bool duplicate = false;
            for (unsigned k = 2; k < used && k < 8; ++k)
                if (out[k] == key) duplicate = true;
            if (!duplicate) {
                if (used < 8) out[used] = key;
                ++used;
            }
        }
    }
    if (used > 8) memset(out + 2, 1, 6); /* HID ErrorRollOver */
}

static void capture(struct hid_aux *s, unsigned kind);

void hid_aux_reset(struct hid_aux *s) { memset(s, 0, sizeof(*s)); }
void hid_aux_release(struct hid_aux *s)
{
    memset(s->keys, 0, sizeof(s->keys));
    s->consumer = s->buttons = 0;
    s->x = s->y = s->wheel = 0;
    memset(s->pending_len, 0, sizeof(s->pending_len));
    for (unsigned i = 0; i < 3; ++i) capture(s, i);
    /* Keep the submitted state: releases must survive a busy endpoint. */
}
void hid_aux_keyboard(struct hid_aux *s, enum hid_aux_source source, const uint8_t *keys)
{
    if ((unsigned)source < HID_AUX_SOURCES) memcpy(s->keys[source], keys, 8);
    capture(s, 0);
}
bool hid_aux_keyboard_sent(const struct hid_aux *s)
{
    uint8_t keys[8]; merged_keys(s, keys);
    return memcmp(keys, s->sent_keys, 8) == 0;
}
static int32_t accumulate(int32_t a, int8_t b)
{
    /* Bound prolonged host stalls without overflowing or wrapping direction. */
    int32_t sum = a + b;
    return sum > 32767 ? 32767 : sum < -32767 ? -32767 : sum;
}
void hid_aux_consumer(struct hid_aux *s, uint8_t bits)
{
    s->consumer = bits;
    capture(s, 1);
}
void hid_aux_mouse(struct hid_aux *s, uint8_t buttons, int8_t x, int8_t y, int8_t wheel)
{
    s->buttons = buttons;
    s->x = accumulate(s->x, x); s->y = accumulate(s->y, y);
    s->wheel = accumulate(s->wheel, wheel);
    capture(s, 2);
}
static int8_t chunk(int32_t n) { return n > 127 ? 127 : n < -127 ? -127 : n; }
/* Preserve the first unsent transition as well as the latest desired state.
 * In particular a press followed by release while busy must not disappear.
 * Additional motion accumulates separately until a successful submission. */
static void capture(struct hid_aux *s, unsigned kind)
{
    if (s->pending_len[kind]) return;
    uint8_t *report = s->pending[kind];
    memset(report, 0, 9); report[0] = kind + 1;
    if (kind == 0) {
        merged_keys(s, report + 1);
        if (memcmp(report + 1, s->sent_keys, 8)) s->pending_len[kind] = 9;
    } else if (kind == 1) {
        report[1] = s->consumer;
        if (s->consumer != s->sent_consumer) s->pending_len[kind] = 2;
    } else {
        report[1] = s->buttons; report[2] = chunk(s->x);
        report[3] = chunk(s->y); report[4] = chunk(s->wheel);
        if (s->buttons != s->sent_buttons || s->x || s->y || s->wheel)
            s->pending_len[kind] = 5;
    }
}
void hid_aux_flush(struct hid_aux *s, hid_aux_write_fn write)
{
    for (unsigned n = 0; n < 3; ++n) {
        unsigned kind = (s->next + n) % 3;
        capture(s, kind);
        uint8_t len = s->pending_len[kind];
        if (!len) continue;
        const uint8_t *report = s->pending[kind];
        if (write(report, len) < 0) return;
        if (kind == 0) memcpy(s->sent_keys, report + 1, 8);
        else if (kind == 1) s->sent_consumer = report[1];
        else {
            s->sent_buttons = report[1];
            s->x -= (int8_t)report[2]; s->y -= (int8_t)report[3];
            s->wheel -= (int8_t)report[4];
        }
        s->pending_len[kind] = 0;
        capture(s, kind);
        s->next = (kind + 1) % 3;
        return;
    }
}

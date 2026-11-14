#include "boot_button.h"

static void finish_clicks(struct boot_button *b)
{
    b->pending = b->clicks == 1 ? BOOT_BUTTON_SINGLE : BOOT_BUTTON_DOUBLE;
    b->clicks = 0;
}

enum boot_button_event boot_button_update(struct boot_button *b,
                                          uint64_t now, bool pressed)
{
    if (pressed != b->candidate) {
        b->candidate = pressed;
        b->candidate_since = now;
    }
    if (b->candidate != b->stable &&
        now - b->candidate_since >= BOOT_BUTTON_DEBOUNCE_US) {
        b->stable = b->candidate;
        b->stable_since = b->candidate_since;
    }
    if (b->pending != BOOT_BUTTON_NONE)
        return b->pending;

    if (b->stable != b->processed) {
        b->processed = b->stable;
        if (b->stable) {
            /* A late second press starts a separate gesture. A press whose
             * debounce crosses the deadline still belongs to the old one. */
            if (b->clicks && b->stable_since - b->released_since > BOOT_BUTTON_CLICK_US)
                finish_clicks(b);
            b->pressed_since = b->stable_since;
        } else if (b->held) {
            b->held = false;
            b->pending = BOOT_BUTTON_RELEASE;
        } else if (b->stable_since - b->pressed_since >= BOOT_BUTTON_HOLD_US) {
            /* Delayed sampling can see release before the hold notification.
             * Preserve HOLD -> RELEASE order; never turn it into a click. */
            b->held = true;
            b->clicks = 0;
            b->pending = BOOT_BUTTON_HOLD;
        } else {
            if (b->clicks < 2) ++b->clicks;
            b->released_since = b->stable_since;
        }
    }
    if (b->pending != BOOT_BUTTON_NONE)
        return b->pending;
    if (b->held && !b->stable) {
        b->held = false;
        b->pending = BOOT_BUTTON_RELEASE;
    } else if (b->stable && b->candidate && !b->held &&
               now - b->pressed_since >= BOOT_BUTTON_HOLD_US) {
        b->held = true;
        b->clicks = 0;
        b->pending = BOOT_BUTTON_HOLD;
    } else if (!b->stable && !b->candidate && b->clicks &&
               now - b->released_since >= BOOT_BUTTON_CLICK_US) {
        finish_clicks(b);
    }
    return b->pending;
}

void boot_button_ack(struct boot_button *b)
{
    b->pending = BOOT_BUTTON_NONE;
}

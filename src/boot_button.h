#ifndef BOOT_BUTTON_H
#define BOOT_BUTTON_H
#include <stdbool.h>
#include <stdint.h>

#define BOOT_BUTTON_SAMPLE_MS 5
#define BOOT_BUTTON_DEBOUNCE_US 15000ULL
#define BOOT_BUTTON_CLICK_US 500000ULL
#define BOOT_BUTTON_HOLD_US 3000000ULL

enum boot_button_event {
    BOOT_BUTTON_NONE, BOOT_BUTTON_SINGLE, BOOT_BUTTON_DOUBLE,
    BOOT_BUTTON_HOLD, BOOT_BUTTON_RELEASE
};

/* Owned by the sampler. The pending event is retained until queue submission
 * succeeds. During backpressure, debounce continues but new gestures wait. */
struct boot_button {
    bool candidate, stable, processed, held;
    uint8_t clicks;
    uint64_t candidate_since, stable_since, pressed_since, released_since;
    enum boot_button_event pending;
};
enum boot_button_event boot_button_update(struct boot_button *button,
                                          uint64_t now, bool pressed);
void boot_button_ack(struct boot_button *button);
#endif

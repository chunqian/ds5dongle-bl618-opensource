#include "led_status.h"
#include "bflb_gpio.h"
#include "board.h"
#include "board_config.h"
#include <stdint.h>
#include "debug_log.h"

#define BLINK_SLOW_PERIOD   10   /* 500ms per toggle → 1Hz */
#define BLINK_FAST_PERIOD   3    /* 150ms per toggle → ~3Hz */

/* After disconnect blink, auto-transition to slow blink
 * after ~3 seconds (60 ticks at 50ms/tick). */
#define RED_BLINK_DURATION  60

static struct bflb_device_s *gpio_dev;
static enum led_pattern current_pattern = LED_OFF;
static uint32_t tick_count = 0;
static uint32_t pattern_age = 0;
static volatile bool led_locked = false;

/* ── Active-low GPIO helpers ── */

static inline void led_pin_on(uint32_t pin)
{
    bflb_gpio_reset(gpio_dev, pin);
}

static inline void led_pin_off(uint32_t pin)
{
    bflb_gpio_set(gpio_dev, pin);
}

/* ══════════════════════════════════════════════════════════════
 * 1 user LED (GPIO27), active-low
 * Board: LCTech BL616
 *
 * Pattern mapping (single LED, differentiated by blink cadence):
 *   Waiting/Pairing (purple slow) → slow blink
 *   Scanning (purple fast)        → fast blink
 *   Connected (green)             → solid
 *   Disconnect (red blink)        → medium blink → auto slow blink
 *   Event ack (blink once)        → single flash
 *   Bonds cleared (triple)        → triple flash
 *   Settings/storage failure      → rapid blink
 * ══════════════════════════════════════════════════════════════ */

static bool led_on = false;

static void set_led(bool on)
{
    led_on = on;
    if (on) led_pin_on(LED_PIN);
    else    led_pin_off(LED_PIN);
}

static void all_off(void)
{
    set_led(false);
}

void led_status_init(void)
{
    gpio_dev = bflb_device_get_by_name("gpio");
    const uint32_t cfg = GPIO_OUTPUT | GPIO_PULLUP | GPIO_SMT_EN | GPIO_DRV_0;
    bflb_gpio_init(gpio_dev, LED_PIN, cfg);
    all_off();
}

void led_status_set(enum led_pattern pattern)
{
    if (led_locked && pattern != LED_BLUE_SOLID)
        return;

    current_pattern = pattern;
    tick_count = 0;
    pattern_age = 0;
    all_off();

    switch (pattern) {
    case LED_PURPLE_BLINK_SLOW:
    case LED_PURPLE_BLINK_FAST:
    case LED_RED_BLINK:
    case LED_BLINK_ERROR:
        set_led(true);
        break;
    case LED_GREEN_SOLID:
    case LED_BLUE_SOLID:
        set_led(true);
        break;
    default:
        break;
    }
}

void led_status_tick(void)
{
    tick_count++;
    pattern_age++;

    switch (current_pattern) {
    case LED_OFF:
    case LED_GREEN_SOLID:
    case LED_BLUE_SOLID:
        break;

    case LED_PURPLE_BLINK_SLOW:
        if (tick_count >= BLINK_SLOW_PERIOD) {
            tick_count = 0;
            set_led(!led_on);
        }
        break;

    case LED_PURPLE_BLINK_FAST:
        if (tick_count >= BLINK_FAST_PERIOD) {
            tick_count = 0;
            set_led(!led_on);
        }
        break;

    case LED_RED_BLINK:
        if (pattern_age >= RED_BLINK_DURATION) {
            LOG_INF("[LED] Disconnect blink done, switching to slow blink\n");
            led_status_set(LED_PURPLE_BLINK_SLOW);
            return;
        }
        if (tick_count >= BLINK_SLOW_PERIOD) {
            tick_count = 0;
            set_led(!led_on);
        }
        break;

    case LED_BLINK_ONCE:
        if (tick_count == 1)
            set_led(true);
        else if (tick_count >= 4) {
            all_off();
            current_pattern = LED_OFF;
        }
        break;

    case LED_BLINK_DOUBLE:
        if (tick_count <= 8) {
            bool on = ((tick_count % 4) < 2);
            set_led(on);
        } else {
            all_off();
            current_pattern = LED_OFF;
        }
        break;

    case LED_BLINK_TRIPLE:
        if (tick_count <= 12) {
            bool on = ((tick_count % 4) < 2);
            set_led(on);
        } else {
            all_off();
            current_pattern = LED_OFF;
        }
        break;

    case LED_BLINK_ERROR:
        if (tick_count >= BLINK_FAST_PERIOD) {
            tick_count = 0;
            set_led(!led_on);
        }
        break;
    }
}

/* ── Shared API (board-independent) ── */

enum led_pattern led_status_get(void)
{
    return current_pattern;
}

bool led_status_can_auto_off(void)
{
    return current_pattern == LED_GREEN_SOLID ||
           current_pattern == LED_PURPLE_BLINK_SLOW ||
           current_pattern == LED_PURPLE_BLINK_FAST;
}

void led_status_lock(void)
{
    led_locked = true;
}

void led_status_unlock(void)
{
    led_locked = false;
}

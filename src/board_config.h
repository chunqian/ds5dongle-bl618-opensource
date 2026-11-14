#ifndef BOARD_CONFIG_H
#define BOARD_CONFIG_H

#include "bflb_gpio.h"

/* LCTech BL616: native Type-C USB and one active-low blue user LED.
 * The power LED is hardwired. No board-selection macro is required. */
#define BOARD_NAME          "LCTech-616"
#define BOOT_BUTTON_PIN     GPIO_PIN_2
#define LED_PIN             GPIO_PIN_27

#endif /* BOARD_CONFIG_H */

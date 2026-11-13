#ifndef FACTORY_RESET_H
#define FACTORY_RESET_H

#include <stdbool.h>

enum factory_reset_result {
    FACTORY_RESET_NONE,
    FACTORY_RESET_DONE,
    FACTORY_RESET_FAILED,
};

enum factory_reset_request_result {
    FACTORY_RESET_REQUEST_CANCELLED, /* No committed intent; normal use is safe. */
    FACTORY_RESET_REQUEST_ARMED,     /* Durable intent; release BOOT to reboot. */
    FACTORY_RESET_REQUEST_UNCERTAIN, /* Commit may have reached flash; reboot to resolve. */
};

/* BT task only. Stage and atomically publish intent; never erase live settings. */
enum factory_reset_request_result factory_reset_request(void);
/* Also true during storage I/O and an uncertain commit: defer other resets. */
bool factory_reset_is_pending(void);

/* Immediately after easyflash_init(), before config/BT/USB and any tasks. */
enum factory_reset_result factory_reset_apply_on_boot(void);

#endif

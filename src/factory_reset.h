#ifndef FACTORY_RESET_H
#define FACTORY_RESET_H

#include <stdbool.h>

enum factory_reset_result {
    FACTORY_RESET_NONE,
    FACTORY_RESET_DONE,
    FACTORY_RESET_FAILED,
};

/* Task context. Persist and verify intent; do not erase while tasks are active. */
bool factory_reset_request(void);
bool factory_reset_is_pending(void);

/* Immediately after easyflash_init(), before config/BT/USB and any tasks. */
enum factory_reset_result factory_reset_apply_on_boot(void);

#endif

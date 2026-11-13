#ifndef FACTORY_RESET_H
#define FACTORY_RESET_H

#include <stdbool.h>

/* Persist the request; reboot only after BOOT is released. */
bool factory_reset_request(void);
/* Call after storage init, before loading settings or starting USB/Bluetooth. */
bool factory_reset_apply_pending(void);

#endif

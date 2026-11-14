#ifndef DEFERRED_STORE_H
#define DEFERRED_STORE_H
#include <stddef.h>
#include <stdint.h>
/* Internal controller type/profile selection writes only. Bluetooth key storage remains SDK-owned. */
enum deferred_store_key {
    STORE_CONTROLLER_TYPE, STORE_REMAP_ACTIVE, STORE_COUNT
};
/* IRQ-safe: snapshot at the request, not when the worker eventually runs. */
void deferred_store_request(enum deferred_store_key key, const void *data, size_t len);
/* Single worker, task context only; failures retry after one second. */
void deferred_store_process(void);
#endif

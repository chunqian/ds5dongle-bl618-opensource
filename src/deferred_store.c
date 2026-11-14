#include "deferred_store.h"
#include "bflb_irq.h"
#include "bflb_mtimer.h"
#include <stdbool.h>
#include <string.h>
#define STORE_MAX_BYTES 128
static const char *const keys[STORE_COUNT] = {
    "detected_type", "remap_prof"
};
static struct {
    uint8_t data[STORE_MAX_BYTES];
    size_t len;
    uint32_t version;
    bool pending;
    uint64_t retry_at;
} pending[STORE_COUNT];
extern int bt_settings_set_bin(const char *, const uint8_t *, size_t);

void deferred_store_request(enum deferred_store_key key, const void *data, size_t len)
{
    if ((unsigned)key >= STORE_COUNT || !data || !len || len > STORE_MAX_BYTES) return;
    uintptr_t irq = bflb_irq_save();
    memcpy(pending[key].data, data, len);
    pending[key].len = len;
    ++pending[key].version;
    pending[key].pending = true;
    bflb_irq_restore(irq);
}

void deferred_store_process(void)
{
    for (unsigned key = 0; key < STORE_COUNT; ++key) {
        uint8_t snapshot[STORE_MAX_BYTES];
        uintptr_t irq = bflb_irq_save();
        uint64_t now = bflb_mtimer_get_time_us();
        if (!pending[key].pending || now < pending[key].retry_at) {
            bflb_irq_restore(irq);
            continue;
        }
        uint32_t version = pending[key].version;
        size_t len = pending[key].len;
        memcpy(snapshot, pending[key].data, len);
        bflb_irq_restore(irq);
        int result = bt_settings_set_bin(keys[key], snapshot, len);
        irq = bflb_irq_save();
        if (result == 0) {
            if (version == pending[key].version) pending[key].pending = false;
            pending[key].retry_at = 0;
        } else pending[key].retry_at = bflb_mtimer_get_time_us() + 1000000ULL;
        bflb_irq_restore(irq);
    }
}

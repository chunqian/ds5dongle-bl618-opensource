#include "factory_reset.h"
#include "easyflash.h"
#include "debug_log.h"
#include <stdint.h>
#include <errno.h>

#define RESET_KEY "factory_reset"
#define RESET_REQUESTED 0xA5

/* All application settings plus BR/EDR and BLE bonds. Keep the device's own
 * Bluetooth identity and factory RF data; these are not user preferences. */
static const char *const reset_keys[] = {
    "dongle_cfg", "detected_type", "btn_remap", "btn_remap_1", "remap_prof",
    "link_key", "KEY_POOL", "DS5_BLK", "active_ctrl", "active_addr",
};

static bool delete_verified(const char *key)
{
    uint8_t byte;
    size_t length = 0;
    if (ef_del_env(key) != EF_NO_ERR)
        return false;
    /* This SDK's ef_del_env ignores lfs_remove errors. Read back so a failed
     * deletion cannot be reported as a completed factory reset. */
    errno = 0;
    size_t read = ef_get_env_blob(key, &byte, sizeof(byte), &length);
    return read == 0 && length == 0 && (errno == 0 || errno == ENOENT);
}

bool factory_reset_request(void)
{
    const uint8_t requested = RESET_REQUESTED;
    uint8_t stored = 0;
    size_t length = 0;
    if (ef_set_env_blob(RESET_KEY, &requested, sizeof(requested)) != EF_NO_ERR)
        return false;
    return ef_get_env_blob(RESET_KEY, &stored, sizeof(stored), &length) == 1 &&
           length == 1 && stored == requested;
}

bool factory_reset_apply_pending(void)
{
    uint8_t requested = 0;
    size_t length = 0;
    errno = 0;
    ef_get_env_blob(RESET_KEY, &requested, sizeof(requested), &length);
    if (errno != 0 && errno != ENOENT)
        return false;
    if (length == 0)
        return true;
    /* Any nonempty marker means reset was requested, including a damaged one.
     * Delete it last so an interrupted reset resumes on the next boot. */
    LOG_INF("[RESET] Restoring all user settings and clearing bonds\n");
    for (size_t i = 0; i < sizeof(reset_keys) / sizeof(reset_keys[0]); ++i) {
        if (!delete_verified(reset_keys[i])) {
            LOG_ERR("[RESET] Failed to clear %s; request retained\n", reset_keys[i]);
            return false;
        }
    }
    if (!delete_verified(RESET_KEY))
        return false;
    LOG_INF("[RESET] Complete; loading defaults (real-time polling)\n");
    return true;
}

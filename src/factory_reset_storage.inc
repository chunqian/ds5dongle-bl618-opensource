/* Included in a build-local copy of the SDK LittleFS/EasyFlash adapter.
 * Keeping the reset marker outside the deletion loop makes recovery resumable.
 * Access to the adapter's lfs handle also preserves real storage error codes;
 * ef_del_env() in this SDK discards lfs_remove() failures.
 */
#include "factory_reset.h"
#include <string.h>

#define RESET_REQUEST_KEY "dongle_factory_reset"
#define RESET_REQUEST_PATH LFS_EF_NAMESPACE "/" RESET_REQUEST_KEY
#define RESET_STAGING_KEY "dongle_factory_reset_tmp"
#define RESET_STAGING_PATH LFS_EF_NAMESPACE "/" RESET_STAGING_KEY

static volatile enum factory_reset_request_result reset_request_state;

bool factory_reset_is_pending(void)
{
    return reset_request_state != FACTORY_RESET_REQUEST_CANCELLED;
}

enum factory_reset_request_result factory_reset_request(void)
{
    const uint8_t requested = 1;
    uint8_t saved = 0;
    lfs_file_t file = {0};
    if (lfs == NULL)
        return FACTORY_RESET_REQUEST_CANCELLED;
    if (factory_reset_is_pending())
        return reset_request_state;

    /* Other tasks must not reset while BOOT is held, including while we wait
     * for the settings mutex or commit to flash. Only the BT task calls us. */
    reset_request_state = FACTORY_RESET_REQUEST_UNCERTAIN;
    EF_GIANT_LOCK();
    /* Never create the authoritative marker until the staged write is fully
     * verified. An abandoned staging file cannot trigger cleanup on boot. */
    int ret = lfs_file_open(lfs, &file, RESET_STAGING_PATH, LFS_O_WRONLY | LFS_O_CREAT | LFS_O_TRUNC);
    if (ret != LFS_ERR_OK) {
        goto cancel_staging;
    }
    lfs_ssize_t written = lfs_file_write(lfs, &file, &requested, sizeof(requested));
    /* The stock ef_set_env_blob() ignores close errors. Check the metadata
     * commit explicitly before promising that the reset request survived. */
    ret = lfs_file_close(lfs, &file);
    if (written != sizeof(requested) || ret != LFS_ERR_OK) {
        goto cancel_staging;
    }

    ret = lfs_file_open(lfs, &file, RESET_STAGING_PATH, LFS_O_RDONLY);
    if (ret != LFS_ERR_OK) {
        goto cancel_staging;
    }
    lfs_ssize_t read_len = lfs_file_read(lfs, &file, &saved, sizeof(saved));
    ret = lfs_file_close(lfs, &file);
    if (read_len != sizeof(saved) || saved != requested || ret != LFS_ERR_OK) {
        goto cancel_staging;
    }

    ret = lfs_rename(lfs, RESET_STAGING_PATH, RESET_REQUEST_PATH);
    if (ret != LFS_ERR_OK) {
        /* A failed commit can still reach flash. Do not cancel or continue
         * ordinary use; leave pending set and resolve after BOOT is released. */
        EF_GIANT_UNLOCK(FACTORY_RESET_REQUEST_UNCERTAIN);
    }
    reset_request_state = FACTORY_RESET_REQUEST_ARMED;
    EF_GIANT_UNLOCK(FACTORY_RESET_REQUEST_ARMED);

cancel_staging:
    /* Best effort only: even if deletion fails, this filename is never intent.
     * A later request can overwrite it; a committed reset deletes it too. */
    (void)lfs_remove(lfs, RESET_STAGING_PATH);
    reset_request_state = FACTORY_RESET_REQUEST_CANCELLED;
    EF_GIANT_UNLOCK(FACTORY_RESET_REQUEST_CANCELLED);
}

enum factory_reset_result factory_reset_apply_on_boot(void)
{
    struct lfs_info info;
    lfs_dir_t dir = {0};
    int ret;

    if (lfs == NULL)
        return FACTORY_RESET_FAILED;

    EF_GIANT_LOCK();
    ret = lfs_stat(lfs, RESET_REQUEST_PATH, &info);
    if (ret == LFS_ERR_NOENT) {
        EF_GIANT_UNLOCK(FACTORY_RESET_NONE);
    }
    if (ret != LFS_ERR_OK || info.type != LFS_TYPE_REG) {
        EF_GIANT_UNLOCK(FACTORY_RESET_FAILED);
    }

    /* Presence is the durable intent. Even a malformed marker must not allow
     * normal startup with partially cleared settings after an interruption. */
    ret = lfs_dir_open(lfs, &dir, LFS_EF_NAMESPACE);
    if (ret != LFS_ERR_OK) {
        EF_GIANT_UNLOCK(FACTORY_RESET_FAILED);
    }

    while ((ret = lfs_dir_read(lfs, &dir, &info)) > 0) {
        if (strcmp(info.name, ".") == 0 || strcmp(info.name, "..") == 0 ||
            strcmp(info.name, RESET_REQUEST_KEY) == 0)
            continue;
        if (info.type != LFS_TYPE_REG) {
            ret = LFS_ERR_ISDIR;
            break;
        }
        int length = snprintf(path_buffer, sizeof(path_buffer), "%s/%s",
                              LFS_EF_NAMESPACE, info.name);
        if (length < 0 || (size_t)length >= sizeof(path_buffer)) {
            ret = LFS_ERR_NAMETOOLONG;
            break;
        }
        ret = lfs_remove(lfs, path_buffer);
        if (ret != LFS_ERR_OK)
            break;
    }
    int close_ret = lfs_dir_close(lfs, &dir);
    if (ret < 0 || close_ret != LFS_ERR_OK) {
        EF_GIANT_UNLOCK(FACTORY_RESET_FAILED);
    }

    /* Delete intent last, only after every user/BT key was removed. No active
     * application or SDK task exists yet, so old RAM state cannot be re-saved. */
    ret = lfs_remove(lfs, RESET_REQUEST_PATH);
    if (ret != LFS_ERR_OK) {
        EF_GIANT_UNLOCK(FACTORY_RESET_FAILED);
    }
    reset_request_state = FACTORY_RESET_REQUEST_CANCELLED;
    EF_GIANT_UNLOCK(FACTORY_RESET_DONE);
}

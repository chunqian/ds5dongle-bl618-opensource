/* Included in a build-local copy of the SDK LittleFS/EasyFlash adapter.
 * Keeping the reset marker outside the deletion loop makes recovery resumable.
 * Access to the adapter's lfs handle also preserves real storage error codes;
 * ef_del_env() in this SDK discards lfs_remove() failures.
 */
#include "factory_reset.h"
#include <string.h>

#define RESET_REQUEST_KEY "dongle_factory_reset"
#define RESET_REQUEST_PATH LFS_EF_NAMESPACE "/" RESET_REQUEST_KEY

static volatile bool factory_reset_pending;

bool factory_reset_is_pending(void)
{
    return factory_reset_pending;
}

bool factory_reset_request(void)
{
    const uint8_t requested = 1;
    uint8_t saved = 0;
    lfs_file_t file = {0};
    if (lfs == NULL)
        return false;

    EF_GIANT_LOCK();
    int ret = lfs_file_open(lfs, &file, RESET_REQUEST_PATH, LFS_O_WRONLY | LFS_O_CREAT | LFS_O_TRUNC);
    if (ret != LFS_ERR_OK) {
        EF_GIANT_UNLOCK(false);
    }
    lfs_ssize_t written = lfs_file_write(lfs, &file, &requested, sizeof(requested));
    /* The stock ef_set_env_blob() ignores close errors. Check the metadata
     * commit explicitly before promising that the reset request survived. */
    ret = lfs_file_close(lfs, &file);
    if (written != sizeof(requested) || ret != LFS_ERR_OK) {
        EF_GIANT_UNLOCK(false);
    }

    ret = lfs_file_open(lfs, &file, RESET_REQUEST_PATH, LFS_O_RDONLY);
    if (ret != LFS_ERR_OK) {
        EF_GIANT_UNLOCK(false);
    }
    lfs_ssize_t read_len = lfs_file_read(lfs, &file, &saved, sizeof(saved));
    ret = lfs_file_close(lfs, &file);
    if (read_len != sizeof(saved) || saved != requested || ret != LFS_ERR_OK) {
        EF_GIANT_UNLOCK(false);
    }

    factory_reset_pending = true;
    EF_GIANT_UNLOCK(true);
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
    factory_reset_pending = false;
    EF_GIANT_UNLOCK(FACTORY_RESET_DONE);
}

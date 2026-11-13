/* Real LittleFS, 32 KiB simulated PSM, and torn program/erase operations. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "lfs.h"

static int test_file_open(lfs_t *, lfs_file_t *, const char *, int);
static lfs_ssize_t test_file_read(lfs_t *, lfs_file_t *, void *, lfs_size_t);
static int test_file_close(lfs_t *, lfs_file_t *);
static int test_rename(lfs_t *, const char *, const char *);
#define lfs_file_open test_file_open
#define lfs_file_read test_file_read
#define lfs_file_close test_file_close
#define lfs_rename test_rename
#include "lfs_easyflash.c"
#undef lfs_file_open
#undef lfs_file_read
#undef lfs_file_close
#undef lfs_rename

enum { READBACK_OK, READBACK_OPEN_FAIL, READBACK_READ_FAIL, READBACK_CLOSE_FAIL };
static int readback_fault;
static lfs_file_t *readback_file;
enum { RENAME_OK, RENAME_FAIL_BEFORE, RENAME_FAIL_AFTER };
static int rename_fault;

static int test_rename(lfs_t *fs, const char *from, const char *to)
{
    if (rename_fault == RENAME_FAIL_BEFORE) return LFS_ERR_IO;
    int ret = lfs_rename(fs, from, to);
    return !ret && rename_fault == RENAME_FAIL_AFTER ? LFS_ERR_IO : ret;
}

static int test_file_open(lfs_t *fs, lfs_file_t *file, const char *path, int flags)
{
    bool readback = !strcmp(path, RESET_STAGING_PATH) && flags == LFS_O_RDONLY;
    if (readback && readback_fault == READBACK_OPEN_FAIL) return LFS_ERR_IO;
    int ret = lfs_file_open(fs, file, path, flags);
    if (!ret && readback) readback_file = file;
    return ret;
}

static lfs_ssize_t test_file_read(lfs_t *fs, lfs_file_t *file, void *data, lfs_size_t size)
{
    if (file == readback_file && readback_fault == READBACK_READ_FAIL) return LFS_ERR_IO;
    return lfs_file_read(fs, file, data, size);
}

static int test_file_close(lfs_t *fs, lfs_file_t *file)
{
    int ret = lfs_file_close(fs, file);
    if (file == readback_file) {
        readback_file = NULL;
        if (readback_fault == READBACK_CLOSE_FAIL) return LFS_ERR_IO;
    }
    return ret;
}

static uint8_t flash[8 * 4096], snapshot[sizeof(flash)];
static int fail_after = -1, mutations;
static bool fresh, request_in_progress, one_shot_fault;

static bool write_fails(void)
{
    if (request_in_progress) assert(factory_reset_is_pending());
    ++mutations;
    if (fail_after < 0) return false;
    if (fail_after == 0) {
        if (one_shot_fault) fail_after = -1;
        return true;
    }
    --fail_after;
    return false;
}

static int read_block(const struct lfs_config *c, lfs_block_t block,
                      lfs_off_t off, void *data, lfs_size_t size)
{
    assert(block < c->block_count && off + size <= c->block_size);
    memcpy(data, flash + block * c->block_size + off, size);
    return 0;
}

static int program_block(const struct lfs_config *c, lfs_block_t block,
                         lfs_off_t off, const void *data, lfs_size_t size)
{
    assert(block < c->block_count && off + size <= c->block_size);
    bool fail = write_fails();
    uint8_t *dest = flash + block * c->block_size + off;
    const uint8_t *src = data;
    for (unsigned i = 0; i < (fail ? size / 2 : size); ++i) {
        assert((dest[i] & src[i]) == src[i]);
        dest[i] &= src[i];
    }
    return fail ? LFS_ERR_IO : 0;
}

static int erase_block(const struct lfs_config *c, lfs_block_t block)
{
    assert(block < c->block_count);
    bool fail = write_fails();
    memset(flash + block * c->block_size, 0xff, fail ? c->block_size / 2 : c->block_size);
    return fail ? LFS_ERR_IO : 0;
}

static int sync_blocks(const struct lfs_config *c) { (void)c; return 0; }

lfs_t *lfs_xip_init(struct lfs_context *ctx, struct lfs_config *cfg)
{
    cfg->read = read_block;
    cfg->prog = program_block;
    cfg->erase = erase_block;
    cfg->sync = sync_blocks;
    cfg->block_count = sizeof(flash) / cfg->block_size;
    if (fresh) {
        assert(lfs_format(&ctx->lfs, cfg) == 0);
        fresh = false;
    }
    /* Never auto-format a failed mount: recovery must preserve the marker. */
    assert(lfs_mount(&ctx->lfs, cfg) == 0);
    return &ctx->lfs;
}

static void unmount(void)
{
    assert(mutex_depth == 0);
    assert(lfs_unmount(lfs) == 0);
    lfs = NULL;
    reset_request_state = FACTORY_RESET_REQUEST_CANCELLED;
}

static void mount(void)
{
    assert(easyflash_init() == EF_NO_ERR);
    assert(!factory_reset_is_pending() && mutex_depth == 0);
}

static bool exists(const char *path)
{
    struct lfs_info info;
    int result = lfs_stat(lfs, path, &info);
    assert(result == 0 || result == LFS_ERR_NOENT);
    return result == 0;
}

static unsigned setting_count(void)
{
    lfs_dir_t dir = {0};
    struct lfs_info info;
    unsigned count = 0;
    int ret;
    assert(lfs_dir_open(lfs, &dir, LFS_EF_NAMESPACE) == 0);
    while ((ret = lfs_dir_read(lfs, &dir, &info)) > 0) {
        if (info.name[0] != '.' && strcmp(info.name, RESET_REQUEST_KEY) &&
            strcmp(info.name, RESET_STAGING_KEY)) ++count;
    }
    assert(ret == 0 && lfs_dir_close(lfs, &dir) == 0);
    return count;
}

static void populate(void)
{
    const char *keys[] = {"dongle_cfg", "remap_0", "remap_1", "remap_2", "remap_prof",
        "DS5_BLK", "active_idx", "bt/keys/123456789abc", "aaa", "zzz_future_setting"};
    memset(flash, 0xff, sizeof(flash));
    fail_after = -1;
    fresh = true;
    mount();
    for (unsigned i = 0; i < sizeof(keys) / sizeof(keys[0]); ++i) {
        uint8_t data[512];
        memset(data, i, sizeof(data));
        assert(ef_set_env_blob(keys[i], data, sizeof(data)) == EF_NO_ERR);
    }
    /* An unrelated file outside the settings namespace must survive. */
    lfs_file_t file = {0};
    assert(lfs_file_open(lfs, &file, "/other", LFS_O_WRONLY | LFS_O_CREAT) == 0);
    assert(lfs_file_write(lfs, &file, "keep", 4) == 4);
    assert(lfs_file_close(lfs, &file) == 0);
    assert(setting_count() == 10);
}

static void check_cleared(void)
{
    char data[4];
    lfs_file_t file = {0};
    assert(setting_count() == 0 && !exists(RESET_REQUEST_PATH) && !exists(RESET_STAGING_PATH));
    assert(!factory_reset_is_pending() && mutex_depth == 0);
    assert(lfs_file_open(lfs, &file, "/other", LFS_O_RDONLY) == 0);
    assert(lfs_file_read(lfs, &file, data, 4) == 4 && memcmp(data, "keep", 4) == 0);
    assert(lfs_file_close(lfs, &file) == 0);
    assert(factory_reset_apply_on_boot() == FACTORY_RESET_NONE);
}

int main(void)
{
    populate();
    assert(factory_reset_apply_on_boot() == FACTORY_RESET_NONE);
    assert(setting_count() == 10 && mutex_depth == 0);
    unmount();
    memcpy(snapshot, flash, sizeof(flash));

    /* Request is durable, but must not erase data while tasks are running. */
    mount(); mutations = 0;
    request_in_progress = true;
    assert(factory_reset_request() == FACTORY_RESET_REQUEST_ARMED && factory_reset_is_pending());
    request_in_progress = false;
    int request_writes = mutations;
    assert(setting_count() == 10 && mutex_depth == 0);
    unmount(); mount();
    assert(exists(RESET_REQUEST_PATH));
    assert(factory_reset_apply_on_boot() == FACTORY_RESET_DONE);
    check_cleared(); unmount();

    unsigned cancelled = 0, uncertain = 0;
    /* Failure before rename must never publish intent, even if the abandoned
     * staging file cannot be removed. Once rename starts, keep pending set. */
    for (unsigned once = 0; once < 2; ++once) {
        one_shot_fault = once;
        for (int n = 0; n < request_writes; ++n) {
            memcpy(flash, snapshot, sizeof(flash)); mount();
            fail_after = n; request_in_progress = true;
            enum factory_reset_request_result result = factory_reset_request();
            request_in_progress = false;
            assert(result != FACTORY_RESET_REQUEST_ARMED && mutex_depth == 0);
            assert(factory_reset_is_pending() == (result == FACTORY_RESET_REQUEST_UNCERTAIN));
            fail_after = -1;
            if (result == FACTORY_RESET_REQUEST_CANCELLED) {
                ++cancelled;
                /* The caller resumes normal use only in this state. */
                assert(ef_set_env_blob("new_config_after_failure", "keep", 4) == EF_NO_ERR);
            } else {
                ++uncertain;
            }
            unmount(); mount();
            if (result == FACTORY_RESET_REQUEST_CANCELLED) {
                char saved[4];
                assert(!exists(RESET_REQUEST_PATH));
                assert(factory_reset_apply_on_boot() == FACTORY_RESET_NONE);
                assert(setting_count() == 11);
                assert(ef_get_env_blob("new_config_after_failure", saved, 4, NULL) == 4);
                assert(memcmp(saved, "keep", 4) == 0);
                /* A later retry may safely replace the abandoned staging file. */
                assert(factory_reset_request() == FACTORY_RESET_REQUEST_ARMED);
                unmount(); mount();
                assert(factory_reset_apply_on_boot() == FACTORY_RESET_DONE);
                check_cleared();
            } else if (exists(RESET_REQUEST_PATH)) {
                assert(factory_reset_apply_on_boot() == FACTORY_RESET_DONE);
                check_cleared();
            } else {
                assert(factory_reset_apply_on_boot() == FACTORY_RESET_NONE);
                assert(setting_count() == 10);
            }
            unmount();
        }
    }
    one_shot_fault = false;
    assert(cancelled && uncertain);

    /* Both possible durable outcomes of an ambiguous final commit must keep
     * normal operation blocked until the caller reboots on BOOT release. */
    for (int fault = RENAME_FAIL_BEFORE; fault <= RENAME_FAIL_AFTER; ++fault) {
        memcpy(flash, snapshot, sizeof(flash)); mount();
        rename_fault = fault;
        assert(factory_reset_request() == FACTORY_RESET_REQUEST_UNCERTAIN);
        assert(factory_reset_is_pending() && mutex_depth == 0);
        assert(factory_reset_request() == FACTORY_RESET_REQUEST_UNCERTAIN);
        rename_fault = RENAME_OK;
        unmount(); mount();
        if (fault == RENAME_FAIL_AFTER) {
            assert(factory_reset_apply_on_boot() == FACTORY_RESET_DONE);
            check_cleared();
        } else {
            assert(factory_reset_apply_on_boot() == FACTORY_RESET_NONE && setting_count() == 10);
        }
        unmount();
    }

    /* Readback failures can occur after the staging file is fully durable. */
    for (int fault = READBACK_OPEN_FAIL; fault <= READBACK_CLOSE_FAIL; ++fault) {
        memcpy(flash, snapshot, sizeof(flash)); mount();
        readback_fault = fault;
        assert(factory_reset_request() == FACTORY_RESET_REQUEST_CANCELLED);
        assert(!factory_reset_is_pending() && mutex_depth == 0);
        readback_fault = READBACK_OK;
        unmount(); mount();
        assert(!exists(RESET_REQUEST_PATH));
        assert(factory_reset_apply_on_boot() == FACTORY_RESET_NONE && setting_count() == 10);
        unmount();
    }

    populate(); assert(factory_reset_request() == FACTORY_RESET_REQUEST_ARMED); unmount();
    memcpy(snapshot, flash, sizeof(flash));
    mount(); mutations = 0;
    assert(factory_reset_apply_on_boot() == FACTORY_RESET_DONE);
    int cleanup_writes = mutations;
    check_cleared(); unmount();

    /* Interrupt every deletion, including the final marker deletion. */
    for (int n = 0; n < cleanup_writes; ++n) {
        memcpy(flash, snapshot, sizeof(flash)); mount();
        fail_after = n;
        assert(factory_reset_apply_on_boot() == FACTORY_RESET_FAILED);
        assert(mutex_depth == 0);
        fail_after = -1; unmount(); mount();
        if (exists(RESET_REQUEST_PATH)) {
            assert(factory_reset_apply_on_boot() == FACTORY_RESET_DONE);
        } else {
            /* A torn write can still contain the entire final commit; absence
             * of the marker is safe only when every setting is already gone. */
            assert(setting_count() == 0);
            assert(factory_reset_apply_on_boot() == FACTORY_RESET_NONE);
        }
        check_cleared(); unmount();
    }

    /* Even an empty marker records intent after an interrupted request. */
    populate();
    lfs_file_t file = {0};
    assert(lfs_file_open(lfs, &file, RESET_REQUEST_PATH, LFS_O_WRONLY | LFS_O_CREAT) == 0);
    assert(lfs_file_close(lfs, &file) == 0);
    unmount(); mount();
    assert(factory_reset_apply_on_boot() == FACTORY_RESET_DONE);
    check_cleared(); unmount();
    printf("PASS: all settings/bonds removed, unrelated data preserved, no-request startup, "
           "legacy marker, staged intent, cancelled/new settings preserved, uncertain commit, readback errors, "
           "mutex balance; %u cancelled + %u uncertain requests, %d cleanup write failures recovered\n",
           cancelled, uncertain, cleanup_writes);
    return 0;
}

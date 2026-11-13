/* Real LittleFS, 32 KiB simulated PSM, and torn program/erase operations. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "lfs_easyflash.c"

static uint8_t flash[8 * 4096], snapshot[sizeof(flash)];
static int fail_after = -1, mutations;
static bool fresh;

static bool write_fails(void)
{
    ++mutations;
    if (fail_after < 0) return false;
    if (fail_after == 0) return true;
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
    factory_reset_pending = false;
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
        if (info.name[0] != '.' && strcmp(info.name, RESET_REQUEST_KEY)) ++count;
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
    assert(setting_count() == 0 && !exists(RESET_REQUEST_PATH));
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
    assert(factory_reset_request() && factory_reset_is_pending());
    int request_writes = mutations;
    assert(setting_count() == 10 && mutex_depth == 0);
    unmount(); mount();
    assert(exists(RESET_REQUEST_PATH));
    assert(factory_reset_apply_on_boot() == FACTORY_RESET_DONE);
    check_cleared(); unmount();

    /* Fail each write in request creation, including close/metadata commit. */
    for (int n = 0; n < request_writes; ++n) {
        memcpy(flash, snapshot, sizeof(flash)); mount();
        fail_after = n;
        assert(!factory_reset_request() && !factory_reset_is_pending());
        assert(mutex_depth == 0);
        fail_after = -1; unmount(); mount();
        if (exists(RESET_REQUEST_PATH)) {
            assert(factory_reset_apply_on_boot() == FACTORY_RESET_DONE);
            check_cleared();
        } else {
            assert(factory_reset_apply_on_boot() == FACTORY_RESET_NONE);
            assert(setting_count() == 10);
        }
        unmount();
    }

    populate(); assert(factory_reset_request()); unmount();
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
           "durable intent, empty marker, mutex balance, %d request and %d cleanup write failures recovered\n",
           request_writes, cleanup_writes);
    return 0;
}

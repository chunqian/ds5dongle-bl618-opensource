"""Compile production reset/config code and exercise persistence offline; gesture coverage is in test_boot_button.py."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
main = (ROOT / 'src/main.c').read_text()
storage = r'''
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include "easyflash.h"
#include "factory_reset.h"
#include "config.h"
#include "deferred_store.h"
void deferred_store_request(enum deferred_store_key key, const void *data, size_t len) { (void)key;(void)data;(void)len; }
struct item { const char *key; uint8_t data[64]; size_t len; } items[] = {
    {"dongle_cfg"}, {"btn_remap"}, {"btn_remap_1"}, {"remap_prof"},
    {"link_key"}, {"KEY_POOL"}, {"DS5_BLK"}, {"active_ctrl"}, {"active_addr"}, {"detected_type"},
    {"LOCAL_ID_ADDR"}, {"RF_FACTORY"}, {"factory_reset"}
};
static const char *fail_delete, *fail_read;
static bool fail_write, silent_delete;
static unsigned deletes;
static struct item *find(const char *key) {
    for (unsigned i=0; i<sizeof(items)/sizeof(items[0]); i++)
        if (!strcmp(key, items[i].key)) return &items[i];
    assert(0); return NULL;
}
size_t ef_get_env_blob(const char *key, void *data, size_t cap, size_t *len) {
    *len=0;
    if (fail_read && !strcmp(key,fail_read)) {errno=EIO;return 0;}
    struct item *p=find(key);
    if (!p->len) {errno=ENOENT;return 0;}
    *len=p->len;size_t n=cap<p->len?cap:p->len;memcpy(data,p->data,n);return n;
}
EfErrCode ef_set_env_blob(const char *key, const void *data, size_t len) {
    if (fail_write) return EF_WRITE_ERR;
    struct item *p=find(key);assert(len<=sizeof(p->data));memcpy(p->data,data,len);p->len=len;return EF_NO_ERR;
}
EfErrCode ef_del_env(const char *key) {
    deletes++;
    if (fail_delete && !strcmp(key,fail_delete)) return silent_delete?EF_NO_ERR:EF_WRITE_ERR;
    find(key)->len=0;return EF_NO_ERR;
}
int bt_settings_get_bin(const char *key,uint8_t *out,size_t cap,size_t *len) {
    ef_get_env_blob(key,out,cap,len);return 0;
}
int bt_settings_set_bin(const char *key,const uint8_t *data,size_t len) {
    return ef_set_env_blob(key,data,len);
}
static void seed(void) {
    for (unsigned i=0;i<sizeof(items)/sizeof(items[0]);i++) {
        items[i].len=1;items[i].data[0]=0x55;
    }
    find("factory_reset")->len=0;deletes=0;
    fail_delete=fail_read=NULL;fail_write=silent_delete=false;
}
int main(void) {
    seed();assert(factory_reset_apply_pending());assert(deletes==0);
    fail_write=true;assert(!factory_reset_request());assert(!find("factory_reset")->len);
    fail_write=false;
    for (unsigned i=0;i<11;i++) {
        seed();assert(factory_reset_request());
        fail_delete=i==10?"factory_reset":items[i].key;
        silent_delete=(i%2)==0;
        assert(!factory_reset_apply_pending());assert(find("factory_reset")->len==1);
        fail_delete=NULL;assert(factory_reset_apply_pending());
        for(unsigned j=0;j<10;j++) assert(items[j].len==0);
        assert(find("LOCAL_ID_ADDR")->len==1 && find("RF_FACTORY")->len==1);
        assert(!find("factory_reset")->len);
    }
    seed();assert(factory_reset_request());fail_read="factory_reset";
    assert(!factory_reset_apply_pending());assert(deletes==0);
    fail_read="dongle_cfg";assert(!factory_reset_apply_pending());
    assert(find("factory_reset")->len==1);fail_read=NULL;
    assert(factory_reset_apply_pending());
    config_load();assert(config_polling_mode()==2);
    assert(config_get()->tp_mode==0 && config_get()->tp_mode_enabled_mask==3);
    assert(config_get()->dse_detected==0 && config_get()->trigger_reduce==0);
    for(unsigned mode=0;mode<3;mode++) {
        config_get()->polling_rate_mode=mode;
        assert(!bt_settings_set_bin("dongle_cfg",(const uint8_t *)config_get(),sizeof(struct config_body)));
        config_load();assert(config_polling_mode()==mode); /* keep existing choices */
    }
    assert(factory_reset_request());assert(factory_reset_apply_pending());
    config_load();assert(config_polling_mode()==2);
    config_get()->config_version=0;config_validate();assert(config_polling_mode()==2);
    puts("PASS: complete settings reset, retained hardware identity, write/delete/read failures, retry and real-time defaults");
}
'''

with tempfile.TemporaryDirectory(prefix='ds5-factory-reset-') as directory:
    temp = Path(directory)
    (temp / 'bflb_irq.h').write_text('#include <stdint.h>\nstatic inline uintptr_t bflb_irq_save(void) { return 0; }\nstatic inline void bflb_irq_restore(uintptr_t x) { (void)x; }\n')
    (temp / 'easyflash.h').write_text('''#include <stddef.h>
typedef enum {EF_NO_ERR, EF_WRITE_ERR} EfErrCode;
size_t ef_get_env_blob(const char*,void*,size_t,size_t*);
EfErrCode ef_set_env_blob(const char*,const void*,size_t);
EfErrCode ef_del_env(const char*);
''')
    for name, code, sources in [('storage', storage, ['src/factory_reset.c', 'src/config.c'])]:
        path = temp / (name + '.c')
        path.write_text(code)
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-variable', '-Wno-missing-field-initializers',
                        '-DLOG_LEVEL=0', '-I' + str(temp), '-I' + str(ROOT / 'src'),
                        str(path), *(str(ROOT / source) for source in sources),
                        '-o', str(temp / name)], check=True)
        subprocess.run([str(temp / name)], check=True)

startup = main[main.index('int main(void)'):]
assert startup.index('factory_reset_apply_pending()') < startup.index('config_load()')
assert startup.index('factory_reset_apply_pending()') < startup.index('xTaskCreateStatic(')
print('PASS: pending reset completes before settings are loaded and tasks are created')

"""Exercise legacy keyboard/mouse migration and surviving gamepad remaps."""
from pathlib import Path
import subprocess
import tempfile
root = Path(__file__).resolve().parents[1]
harness = r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "remap.h"
#include "config.h"
#include "deferred_store.h"
static struct config_body cfg;
struct config_body *config_get(void) { return &cfg; }
static remap_entry_t saved[REMAP_PROFILE_COUNT][REMAP_BTN_COUNT];
size_t ef_get_env_blob(const char *key, void *out, size_t cap, size_t *len) {
    if (!strcmp(key,"remap_prof")) { *(uint8_t *)out=0; *len=1; return 1; }
    unsigned profile=!strcmp(key,"btn_remap_1");
    assert(cap==sizeof(saved[profile]));
    memcpy(out,saved[profile],cap); *len=cap; return cap;
}
int ef_set_env_blob(const char *key,const void *data,size_t len) {
    (void)key; (void)data; (void)len; return 0;
}
void deferred_store_request(enum deferred_store_key id,const void *data,size_t len) {
    (void)id; (void)data; (void)len;
}
'''+ '#include "'+str(root/'src/remap.c')+'"\n'+r'''
int main(void) {
    _Static_assert(sizeof(remap_entry_t)==4,"preserve old on-wire entry layout");
    remap_init();
    for (unsigned p=0;p<REMAP_PROFILE_COUNT;p++) {
        memcpy(saved[p],g_profiles[p],sizeof(saved[p]));
        saved[p][0]=(remap_entry_t){1,4,8,3}; /* old suppressed keyboard shortcut */
        saved[p][1]=(remap_entry_t){2,0,0,1}; /* old suppressed mouse click */
        saved[p][2]=(remap_entry_t){0,3,0,0}; /* legitimate circle -> triangle */
    }
    remap_load();
    for (unsigned p=0;p<REMAP_PROFILE_COUNT;p++) {
        const remap_entry_t *t=g_profiles[p];
        assert(t[0].type==0 && t[0].value==0 && !t[0].modifier && !t[0].flags);
        assert(t[1].type==0 && t[1].value==1 && !t[1].flags);
        assert(t[2].type==0 && t[2].value==3);
        remap_switch_profile(p);
        uint8_t input[63]={0}; input[7]=0x78; input[32]=input[36]=0x80;
        remap_apply(input);
        assert(input[7]==0xb8); /* original square/cross survive; circle becomes triangle */
    }
    cfg.tp_mode=0;cfg.tp_mode_enabled_mask=0x1f;
    remap_tp_cycle_mode(); assert(cfg.tp_mode==1);
    remap_tp_cycle_mode(); assert(cfg.tp_mode==4);
    remap_tp_cycle_mode(); assert(cfg.tp_mode==0);
    puts("PASS: both legacy stored profiles restore original buttons; gamepad remap and non-mouse touch modes survive");
}
'''
with tempfile.TemporaryDirectory() as d:
    p=Path(d)
    (p/'bflb_irq.h').write_text('#include <stdint.h>\nstatic inline uintptr_t bflb_irq_save(void){return 0;}\nstatic inline void bflb_irq_restore(uintptr_t v){(void)v;}\n')
    (p/'easyflash.h').write_text('#include <stddef.h>\nsize_t ef_get_env_blob(const char *,void *,size_t,size_t *);\nint ef_set_env_blob(const char *,const void *,size_t);\n')
    (p/'test.c').write_text(harness)
    subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-DLOG_LEVEL=0',
                    '-fsanitize=address,undefined','-I'+str(p),'-I'+str(root/'src'),
                    str(p/'test.c'),'-o',str(p/'test')],check=True)
    subprocess.run([str(p/'test')],check=True)

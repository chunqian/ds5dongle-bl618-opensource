"""Exercise production config/store and remap requests with interrupted/failed Flash writes."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
remap = (root / 'src/remap.c').read_text()

def extract(source, signature):
    start = source.index(signature)
    end = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]

harness = r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "config.h"
#include "remap.h"
#include "deferred_store.h"
#include "bflb_irq.h"
#define LOG_INF(...) ((void)0)
unsigned irq_depth;
static uint64_t now;
uint64_t bflb_mtimer_get_time_us(void) { return now; }
static struct { const char *key; uint8_t data[128]; size_t len; unsigned calls; } records[] = {
    {"dongle_cfg"}, {"detected_type"}, {"btn_remap"}, {"btn_remap_1"}, {"remap_prof"}
};
static unsigned index_for(const char *key) {
    for (unsigned i=0;i<STORE_COUNT;i++) if (!strcmp(key,records[i].key)) return i;
    assert(0); return 0;
}
static int fail_key=-1;
static bool inject_request;
int bt_settings_set_bin(const char *key,const uint8_t *data,size_t len) {
    assert(!irq_depth); /* no Flash operation in USB ISR/locked snapshot copy */
    unsigned i=index_for(key); records[i].calls++;
    if (inject_request && i==STORE_CONFIG) {
        inject_request=false;
        config_get()->speaker_volume=66;
        config_request_save(); /* a newer SAVE arrives during this write */
        config_get()->speaker_volume=77; /* later unsaved edit */
    }
    if ((int)i==fail_key) return -1;
    assert(len<=sizeof(records[i].data));
    memcpy(records[i].data,data,len); records[i].len=len; return 0;
}
int bt_settings_get_bin(const char *key,uint8_t *data,size_t cap,size_t *len) {
    unsigned i=index_for(key); *len=records[i].len;
    if (*len>cap) *len=cap;
    memcpy(data,records[i].data,*len); return 0;
}
static struct config_body saved_config(void) {
    struct config_body c; assert(records[STORE_CONFIG].len==sizeof(c));
    memcpy(&c,records[STORE_CONFIG].data,sizeof(c)); return c;
}
static remap_entry_t g_profiles[REMAP_PROFILE_COUNT][REMAP_BTN_COUNT];
static uint8_t active_profile;
static void update_identity_cache(void) { assert(irq_depth); }
'''
for sig in ('void remap_request_save(', 'void remap_switch_profile('):
    harness += extract(remap, sig) + '\n'
harness += r'''
int main(void) {
    config_load();
    config_get()->speaker_volume=12; config_request_save();
    config_get()->speaker_volume=24; /* never saved, must not leak */
    assert(!records[STORE_CONFIG].calls); deferred_store_process();
    assert(saved_config().speaker_volume==12);
    config_note_controller(true); deferred_store_process();
    assert(records[STORE_CONTROLLER_TYPE].data[0]==1);
    assert(records[STORE_CONFIG].calls==1 && saved_config().speaker_volume==12);
    config_note_controller(true); deferred_store_process();
    assert(records[STORE_CONTROLLER_TYPE].calls==1);
    config_load(); assert(config_get()->speaker_volume==12 && config_get()->dse_detected==1);

    /* Host writes (even invalid versions) cannot overwrite remembered type. */
    struct config_body host=*config_get(); host.dse_detected=0; host.config_version=0;
    config_set((uint8_t *)&host,sizeof(host)); assert(config_get()->dse_detected==1);
    config_get()->speaker_volume=33; config_request_save(); inject_request=true;
    deferred_store_process(); assert(saved_config().speaker_volume==33);
    deferred_store_process(); assert(saved_config().speaker_volume==66);
    assert(config_get()->speaker_volume==77);

    /* Retry timing, immutable queued data and independent keys. */
    now=100; fail_key=STORE_CONFIG;
    config_get()->speaker_volume=44; config_request_save(); deferred_store_process();
    unsigned calls=records[STORE_CONFIG].calls;
    config_get()->speaker_volume=55;
    config_note_controller(false); deferred_store_process();
    assert(records[STORE_CONTROLLER_TYPE].data[0]==0 && records[STORE_CONFIG].calls==calls);
    now=1000099; deferred_store_process(); assert(records[STORE_CONFIG].calls==calls);
    now++; fail_key=-1; deferred_store_process();
    assert(records[STORE_CONFIG].calls==calls+1 && saved_config().speaker_volume==44);
    config_load(); assert(config_get()->dse_detected==0); /* independent record wins */

    /* Two profiles requested before a worker tick must both survive. */
    memset(g_profiles[0],0x31,sizeof(g_profiles[0]));
    memset(g_profiles[1],0x52,sizeof(g_profiles[1]));
    remap_request_save(0); remap_request_save(1); remap_switch_profile(1);
    assert(!records[STORE_REMAP_0].calls && !records[STORE_REMAP_ACTIVE].calls);
    memset(g_profiles,0,sizeof(g_profiles));
    deferred_store_process();
    assert(records[STORE_REMAP_0].data[0]==0x31 && records[STORE_REMAP_1].data[0]==0x52);
    assert(records[STORE_REMAP_0].len==sizeof(g_profiles[0]));
    assert(records[STORE_REMAP_ACTIVE].data[0]==1);
    fail_key=STORE_REMAP_ACTIVE; remap_switch_profile(0); deferred_store_process();
    now+=1000000; fail_key=-1; deferred_store_process();
    assert(records[STORE_REMAP_ACTIVE].data[0]==0);

    /* Legacy config migrates the detected type without saving other edits. */
    records[STORE_CONTROLLER_TYPE].len=0;
    host=saved_config(); host.dse_detected=1;
    memcpy(records[STORE_CONFIG].data,&host,sizeof(host)); config_load();
    assert(config_get()->dse_detected==1);
    calls=records[STORE_CONFIG].calls;
    config_get()->speaker_volume=88; config_note_controller(true); deferred_store_process();
    assert(records[STORE_CONTROLLER_TYPE].data[0]==1 && records[STORE_CONFIG].calls==calls);
    assert(!irq_depth);
    puts("PASS: save-time snapshots, isolated type memory/migration, failed-write retry, concurrent SAVE, two remap profiles and deferred selection");
}
'''
with tempfile.TemporaryDirectory(prefix='ds5-persistence-') as directory:
    path = Path(directory)
    (path / 'bflb_irq.h').write_text('''#pragma once
#include <stdint.h>
extern unsigned irq_depth;
static inline uintptr_t bflb_irq_save(void) { return irq_depth++; }
static inline void bflb_irq_restore(uintptr_t old) { irq_depth=old; }
''')
    (path / 'bflb_mtimer.h').write_text('#include <stdint.h>\nuint64_t bflb_mtimer_get_time_us(void);\n')
    (path / 'test.c').write_text(harness)
    subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-Wno-missing-field-initializers',
                    '-fsanitize=address,undefined','-DLOG_LEVEL=0','-I',str(path),'-I',str(root/'src'),
                    str(path/'test.c'),str(root/'src/config.c'),str(root/'src/deferred_store.c'),
                    '-o',str(path/'test')],check=True)
    subprocess.run([str(path/'test')],check=True)

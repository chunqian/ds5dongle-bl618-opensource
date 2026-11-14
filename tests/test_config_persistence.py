"""Internal type/profile persistence, legacy config loading and failed-write retries."""
from pathlib import Path
import subprocess,tempfile
root=Path(__file__).resolve().parents[1]
s=(root/'src/remap.c').read_text()
a=s.index('void remap_switch_profile(');b=s.index('\n}\n',a)+3
h=r'''
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
uint64_t bflb_mtimer_get_time_us(void){return now;}
static struct config_body legacy;
static bool legacy_present,have_type,fail_write,inject;
static uint8_t type_value,profile_value,active_profile;
static unsigned writes,type_writes,profile_writes;
static void update_identity_cache(void){assert(irq_depth);}
int bt_settings_get_bin(const char *key,uint8_t *out,size_t cap,size_t *len){
 *len=0;
 if(!strcmp(key,"dongle_cfg") && legacy_present){assert(cap>=sizeof(legacy));memcpy(out,&legacy,sizeof(legacy));*len=sizeof(legacy);}
 if(!strcmp(key,"detected_type") && have_type){*out=type_value;*len=1;}
 return 0;
}
int bt_settings_set_bin(const char *key,const uint8_t *p,size_t len){
 assert(!irq_depth && len==1);writes++;
 if(fail_write)return -1;
 if(!strcmp(key,"detected_type")){
  type_value=*p;have_type=true;type_writes++;
  if(inject){inject=false;config_note_controller(false);}
 }else{assert(!strcmp(key,"remap_prof"));profile_value=*p;profile_writes++;}
 return 0;
}
'''+s[a:b]+r'''
int main(void){
 config_load();assert(config_polling_mode()==2);
 legacy=*config_get();legacy.speaker_volume=42;legacy_present=true;
 config_load();assert(config_get()->speaker_volume==42);
 config_get()->speaker_volume=99;config_note_controller(true);remap_switch_profile(1);
 assert(!writes);deferred_store_process();
 assert(type_value==1 && profile_value==1 && type_writes==1 && profile_writes==1);
 config_load();assert(config_get()->speaker_volume==42 && config_get()->dse_detected==1);
 config_note_controller(true);deferred_store_process();assert(type_writes==1);
 fail_write=true;config_note_controller(false);deferred_store_process();unsigned attempts=writes;
 now=999999;deferred_store_process();assert(writes==attempts);
 fail_write=false;now++;deferred_store_process();assert(type_value==0);
 inject=true;config_note_controller(true);deferred_store_process();assert(type_value==1);
 deferred_store_process();assert(type_value==0); /* newer request during persistence survives */
 remap_switch_profile(0);remap_switch_profile(1);deferred_store_process();assert(profile_value==1);
 legacy.ps_shortcut_enabled=1;legacy.tp_mode=2;legacy.tp_mode_enabled_mask=0x1f;
 config_load();assert(!config_get()->ps_shortcut_enabled && !config_get()->tp_mode);
 assert(config_get()->tp_mode_enabled_mask==0x13 && !irq_depth);
 puts("PASS: legacy preferences load, independent type/profile writes, retry and concurrent update; no user-config write path");
}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d)
 (p/'bflb_irq.h').write_text('#pragma once\n#include <stdint.h>\nextern unsigned irq_depth;\nstatic inline uintptr_t bflb_irq_save(void){return irq_depth++;}\nstatic inline void bflb_irq_restore(uintptr_t old){irq_depth=old;}\n')
 (p/'bflb_mtimer.h').write_text('#include <stdint.h>\nuint64_t bflb_mtimer_get_time_us(void);\n')
 (p/'test.c').write_text(h)
 subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-DLOG_LEVEL=0','-I'+str(p),'-I'+str(root/'src'),str(p/'test.c'),str(root/'src/config.c'),str(root/'src/deferred_store.c'),'-o',str(p/'test')],check=True)
 subprocess.run([str(p/'test')],check=True)

"""Interrupt-injected tests of the production state manager's complete transactions."""
from pathlib import Path
import subprocess,tempfile
ROOT=Path(__file__).resolve().parents[1]
h=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include "config.h"
#include "state_mgr.h"
static struct config_body cfg;
struct config_body *config_get(void) { return &cfg; }
static uintptr_t mask;
static bool inject, pending;
static unsigned interrupts;
uintptr_t bflb_irq_save(void) { uintptr_t old=mask; mask=1; return old; }
void bflb_irq_restore(uintptr_t old) {
 mask=old;
 if(!mask && pending) {
  pending=false; interrupts++; state_mgr_apply_config(91,92,3,4);
  state_mgr_set_led_color(1,2,3);
 }
}
static void *checked_copy(void *out,const void *in,size_t n) {
 assert(mask);
 memcpy(out,in,n/2);
 if(inject) { inject=false; pending=true; }
 memcpy((uint8_t *)out+n/2,(const uint8_t *)in+n/2,n-n/2);
 return out;
}
#define memcpy checked_copy
'''
h+='#include "'+str(ROOT/'src/state_mgr.c')+'"\n'
h+=r'''
#undef memcpy
int main(void) {
 uint8_t init[47]={0xff,0xff},snapshot[47],next[47];
 init[4]=5;init[5]=6;init[44]=7;init[45]=8;init[46]=9;
 state_mgr_init(init,47);
 inject=true; state_mgr_get(snapshot,47);
 assert(interrupts==1 && !memcmp(snapshot,init,47));
 state_mgr_get(next,47); assert(next[4]==92 && next[5]==91 && next[44]==1);
 /* The pending ISR must wait through BOTH merge and output snapshot. */
 uint8_t host[47]; memset(host,0x55,47);host[0]=host[1]=0xff;
 inject=true; state_mgr_merge_snapshot(host,47);
 assert(interrupts==2 && host[4]==0x55 && host[5]==0x55 && host[44]==0x55);
 for(unsigned i=10;i<32;i++) assert(host[i]==0x55);
 state_mgr_get(next,47);assert(next[4]==92 && next[5]==91 && next[44]==1);
 mask=1;state_mgr_init(init,47);assert(mask==1);
 state_mgr_set_led_color(10,20,30);state_mgr_get(snapshot,47);
 assert(mask==1 && snapshot[44]==10 && snapshot[45]==20 && snapshot[46]==30);
 state_mgr_merge_snapshot(snapshot,47);assert(mask==1);mask=0;
 puts("PASS: full state copy/merge atomicity, deferred configuration ISR and nested IRQ mask preservation");
}
'''
with tempfile.TemporaryDirectory(prefix='ds5-state-snapshot-') as tmp:
 p=Path(tmp)
 (p/'bflb_irq.h').write_text('#include <stdint.h>\nuintptr_t bflb_irq_save(void);\nvoid bflb_irq_restore(uintptr_t);\n')
 (p/'test.c').write_text(h)
 subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-I'+str(p),'-I'+str(ROOT/'src'),str(p/'test.c'),'-o',str(p/'test')],check=True)
 subprocess.run([str(p/'test')],check=True)

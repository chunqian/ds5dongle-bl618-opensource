"""Compile real Feature cache writer/reader; inject an interrupt during copying."""
from pathlib import Path
import subprocess,tempfile
root=Path(__file__).resolve().parents[1]
s=(root/'src/bt_hid_host.c').read_text()
def fn(sig):
    a=s.index(sig); b=s.index('{',a)+1; d=1
    while d:
        d+=(s[b]=='{')-(s[b]=='}'); b+=1
    return s[a:b]+'\n'
h=r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>
static uintptr_t masked;
static bool inject, interrupt_pending;
static void interrupt_read(void);
static uintptr_t bflb_irq_save(void){uintptr_t old=masked;masked=1;return old;}
static void bflb_irq_restore(uintptr_t old){masked=old;if(!masked && interrupt_pending){interrupt_pending=false;interrupt_read();}}
static void *copy(void *to,const void *from,size_t n){
    assert(masked);
    memcpy(to,from,n/2);
    if(inject){inject=false;interrupt_pending=true;assert(masked);}
    memcpy((uint8_t*)to+n/2,(const uint8_t*)from+n/2,n-n/2);return to;
}
#define memcpy copy
'''
a=s.index('#define FEATURE_CACHE_SLOTS');b=s.index('/* ---------- persistent blacklist',a)
h+=s[a:b]+fn('bool bt_hid_host_copy_feature(')
h+=r'''
#undef memcpy
static uint8_t observed[256];
static void interrupt_read(void){
    uint16_t n=0;masked=1;
    assert(bt_hid_host_copy_feature(0x70,observed,sizeof(observed),&n));
    assert(n==256 && masked==1);
    for(unsigned i=0;i<n;i++)assert(observed[i]==0x22);
    masked=0;
}
int main(void){
    uint8_t old[256],newer[256],snapshot[256];uint16_t n;
    memset(old,0x11,256);memset(newer,0x22,256);
    feature_cache_clear();feature_cache_store(0x70,old,256);
    inject=true;feature_cache_store(0x70,newer,256);
    assert(!masked && !interrupt_pending && observed[0]==0x22);
    assert(bt_hid_host_copy_feature(0x70,snapshot,61,&n) && n==61);
    feature_cache_store(0x70,old,256);assert(snapshot[0]==0x22); /* owned snapshot */
    feature_cache_clear();assert(!bt_hid_host_copy_feature(0x70,snapshot,256,&n) && !n);
    assert(!masked);puts("PASS: coherent Feature snapshot, ISR mask preservation, bounded copy, owned lifetime and clear");
}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d);(p/'t.c').write_text(h)
 subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined',str(p/'t.c'),'-o',str(p/'t')],check=True)
 subprocess.run([str(p/'t')],check=True)

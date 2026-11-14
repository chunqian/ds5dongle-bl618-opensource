"""Run the actual USB Feature response branch with cache/fallback/Edge fixtures."""
from pathlib import Path
import subprocess
import tempfile
ROOT=Path(__file__).resolve().parents[1]
source=(ROOT/'src/usb_gamepad.c').read_text()
start=source.index('    /* DSE profile reports (0x70-0x7B):')
end=source.index('\n}\n',start)
branch=source[start:end]
harness=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define LOG_INF(...) ((void)0)
#define FEATURE_DATA_MAX 256
static uint8_t feature_resp_buf[257], cache[20]={0x42};
static bool ready, cached, fallback;
static unsigned requests, requested;
static bool dse_is_profile_report(uint8_t id) { return id>=0x70 && id<=0x7b; }
static bool dse_profiles_ready(void) { return ready; }
static void bt_hid_host_refresh_feature(uint8_t id) { ++requests; requested=id; }
static bool bt_hid_host_copy_feature(uint8_t id,uint8_t *data,uint16_t cap,uint16_t *len) {
    (void)id; assert(cap>=sizeof(cache)); memcpy(data,cache,sizeof(cache)); *len=sizeof(cache); return cached;
}
static bool ds5_feature_fallback(uint8_t id,uint8_t *data,uint32_t *len) {
    data[0]=id; data[1]=0x55; *len=2; return fallback;
}
static void get(uint8_t report_id,uint8_t **data,uint32_t *len) {
'''+branch+r'''
}
int main(void) {
    for(unsigned id=1;id<256;id++)
    for(unsigned has_cache=0;has_cache<2;has_cache++) {
        uint8_t *data=NULL; uint32_t len=0;
        ready=true; cached=has_cache; fallback=false; requests=0;
        get(id,&data,&len);
        bool mutable=id==0x81 || id==0x63 || id==0x64 || id==0x65 || (id>=0x70 && id<=0x7b);
        assert(requests==(!has_cache || mutable));
        if(requests) assert(requested==id);
        if(has_cache) assert(len==21 && data[0]==id && data[1]==0x42);
        else assert(len==0);
    }
    uint8_t *data=NULL; uint32_t len;
    cached=false; fallback=true; requests=0;
    get(0x05,&data,&len);
    assert(len==2 && data[1]==0x55 && requests==1 && requested==0x05);
    ready=false; cached=true; requests=0;
    get(0x70,&data,&len);
    assert(len==0 && requests==1 && requested==0x70);
    puts("PASS: mutable Feature refresh, immutable cache, cache-miss fallback fetch and Edge prefetch gate");
}
'''
with tempfile.TemporaryDirectory(prefix='ds5-feature-') as directory:
    path=Path(directory); (path/'test.c').write_text(harness)
    subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined',str(path/'test.c'),'-o',str(path/'test')],check=True)
    subprocess.run([str(path/'test')],check=True)

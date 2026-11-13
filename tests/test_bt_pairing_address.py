"""Exercise the production pairing callback with the SDK's BR/LE address layout."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "src/bt_hid_host.c").read_text()


def extract(signature):
    start = SOURCE.index(signature)
    end = SOURCE.index("{", start) + 1
    depth = 1
    while depth:
        depth += (SOURCE[end] == "{") - (SOURCE[end] == "}")
        end += 1
    return SOURCE[start:end]


harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef struct { uint8_t val[6]; } bt_addr_t;
typedef struct { uint8_t type; bt_addr_t a; } bt_addr_le_t;
/* Relevant SDK union layout: LE has a type byte before the address;
 * BR has the address first, followed by the peer's IO capability. */
struct bt_conn {
    union {
        struct { bt_addr_le_t dst; } le;
        struct { bt_addr_t dst; uint8_t remote_io_capa; } br;
    };
};
struct bt_conn_info { struct { const bt_addr_t *dst; } br; };
#define LOG_INF(...) ((void)0)
#define MAX_BONDED_CONTROLLERS 8
static bt_addr_t bonded_list[MAX_BONDED_CONTROLLERS];
static uint8_t bonded_count, active_idx;
static unsigned writes;
static uint8_t saved_idx;
static bool info_error;
static int bt_conn_get_info(const struct bt_conn *conn, struct bt_conn_info *info) {
    if (info_error) return -1;
    info->br.dst = &conn->br.dst;
    return 0;
}
/* Match the SDK API so the old callback compiles but fails the address test. */
static const bt_addr_le_t *bt_conn_get_dst(const struct bt_conn *conn) {
    return &conn->le.dst;
}
static int bt_addr_cmp(const bt_addr_t *a, const bt_addr_t *b) {
    return memcmp(a, b, sizeof(*a));
}
static void bt_addr_copy(bt_addr_t *a, const bt_addr_t *b) { *a = *b; }
static void bonded_list_persist_idx(void) { ++writes; saved_idx = active_idx; }
'''
for signature in (
    "static int bonded_list_find_addr(",
    "static const bt_addr_t *conn_get_br_addr(",
    "static void auth_pairing_complete(",
):
    harness += "\n" + extract(signature) + "\n"
harness += r'''
int main(void) {
    struct bt_conn first = {0}, second = {0};
    const bt_addr_t first_addr = {{1, 2, 3, 4, 5, 6}};
    const bt_addr_t second_addr = {{0xa1, 0xb2, 0xc3, 0xd4, 0xe5, 0xf6}};
    first.br.dst = first_addr; first.br.remote_io_capa = 3;
    second.br.dst = second_addr; second.br.remote_io_capa = 0;

    auth_pairing_complete(&first, true);
    assert(bonded_count == 1 && writes == 1 && saved_idx == 0);
    assert(bt_addr_cmp(&bonded_list[0], &first_addr) == 0);
    /* The same lookup used to recognize an existing bond must find the peer. */
    assert(bonded_list_find_addr(&first_addr) == 0);

    auth_pairing_complete(&second, true);
    assert(bonded_count == 2 && active_idx == 1 && saved_idx == 1);
    assert(bt_addr_cmp(&bonded_list[1], &second_addr) == 0);

    /* Re-pair the first peer with a different capability: no phantom entry. */
    first.br.remote_io_capa = 1;
    auth_pairing_complete(&first, true);
    assert(bonded_count == 2 && active_idx == 0 && saved_idx == 0 && writes == 3);

    auth_pairing_complete(&second, false);
    assert(bonded_count == 2 && active_idx == 0 && writes == 3);
    info_error = true;
    auth_pairing_complete(&second, true);
    assert(bonded_count == 2 && active_idx == 0 && writes == 3);
    puts("PASS: exact BR address, bond lookup, repeated pairing, active peer, no-bond and lookup failure");
}
'''
with tempfile.TemporaryDirectory(prefix="ds5-pairing-address-") as directory:
    work = Path(directory)
    source = work / "test.c"
    executable = work / "test"
    source.write_text(harness)
    subprocess.run(["cc", "-std=c11", "-fsanitize=address,undefined",
                    str(source), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)

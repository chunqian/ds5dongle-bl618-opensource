"""Exercise the firmware's fallback report with host-side sensor conversion.

Run: python3 tests/test_ds5_calibration.py [path/to/usb_gamepad.c]
Extract the hardware-independent function so this test needs no Bouffalo SDK.
"""

from pathlib import Path
import subprocess
import sys
import tempfile


source_path = (Path(sys.argv[1]) if len(sys.argv) > 1 else
               Path(__file__).resolve().parents[1] / "src/usb_gamepad.c")
source = source_path.read_text()
start = source.index("static bool ds5_feature_fallback(")
end = source.index("\n}\n", start) + 3

harness = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define DS5_FEATURE_REPORT_CALIBRATION 0x05
#define DS5_FEATURE_REPORT_PAIRING 0x09
#define DS5_FEATURE_REPORT_FIRMWARE 0x20
""" + source[start:end] + r"""
static int le16(const uint8_t *p)
{
    unsigned n = p[0] | ((unsigned)p[1] << 8);
    return n >= 32768 ? (int)n - 65536 : (int)n;
}

int main(void)
{
    /* Exercise both alignments and check the complete report boundary. */
    for (unsigned offset = 0; offset < 2; offset++) {
        uint8_t storage[64];
        memset(storage, 0xa5, sizeof(storage));
        uint8_t *r = storage + offset;
        uint32_t length = 0;
        assert(ds5_feature_fallback(0x05, r, &length));
        assert(length == 41 && r[0] == 0x05);
        if (offset) assert(storage[0] == 0xa5);
        for (unsigned i = offset + length; i < sizeof(storage); i++)
            assert(storage[i] == 0xa5);
        for (unsigned i = 35; i < length; i++) assert(r[i] == 0);

        int speed_sum = le16(r + 19) + le16(r + 21);
        for (unsigned axis = 0; axis < 3; axis++) {
            int bias = le16(r + 1 + 2 * axis);
            int plus = le16(r + 7 + 4 * axis);
            int minus = le16(r + 9 + 4 * axis);
            int denominator = abs(plus - bias) + abs(minus - bias);
            assert(denominator > 0);
            /* Sony/Linux normalization, followed by conversion to deg/s.
             * Nominal DualSense raw gyro resolution is 16 counts/deg/s. */
            double gain = speed_sum * 1024.0 / denominator;
            assert(gain == 64.0);
            assert((1600 - bias) * gain / 1024.0 == 100.0);
            assert((-1600 - bias) * gain / 1024.0 == -100.0);
            assert((0 - bias) * gain == 0.0);
            /* SDL's signed-range calculation must agree. */
            assert(speed_sum * 1024.0 / (plus - minus) == gain);

            plus = le16(r + 23 + 4 * axis);
            minus = le16(r + 25 + 4 * axis);
            int range = plus - minus;
            bias = plus - range / 2;
            assert(range > 0);
            double accel_gain = 2.0 * 8192 / range;
            assert((8192 - bias) * accel_gain / 8192 == 1.0);
            assert((-8192 - bias) * accel_gain / 8192 == -1.0);
        }
    }
    puts("PASS: fallback gyro scale/sign/zero, acceleration and report bounds");
}
"""

with tempfile.TemporaryDirectory(prefix="ds5-calibration-") as directory:
    path = Path(directory)
    (path / "test.c").write_text(harness)
    subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(path / "test.c"),
                    "-o", str(path / "test")], check=True)
    subprocess.run([str(path / "test")], check=True)

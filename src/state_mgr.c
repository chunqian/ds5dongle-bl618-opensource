#include "state_mgr.h"
#include "config.h"
#include <stdbool.h>
#include <string.h>
#include "debug_log.h"

/*
 * SetStateData byte layout (47 bytes):
 *
 *  [0]  flags0:  EnableRumbleEmulation(0), UseRumbleNotHaptics(1),
 *                AllowRightTriggerFFB(2), AllowLeftTriggerFFB(3),
 *                AllowHeadphoneVolume(4), AllowSpeakerVolume(5),
 *                AllowMicVolume(6), AllowAudioControl(7)
 *  [1]  flags1:  AllowMuteLight(0), AllowAudioMute(1), AllowLedColor(2),
 *                ResetLights(3), AllowPlayerIndicators(4),
 *                AllowHapticLowPassFilter(5), AllowMotorPowerLevel(6),
 *                AllowAudioControl2(7)
 *  [2]  RumbleRight
 *  [3]  RumbleLeft
 *  [4]  VolumeHeadphones
 *  [5]  VolumeSpeaker
 *  [6]  VolumeMic
 *  [7]  AudioControl
 *  [8]  MuteLightMode
 *  [9]  MuteControl
 *  [10..20]  RightTriggerFFB  (11 bytes)
 *  [21..31]  LeftTriggerFFB   (11 bytes)
 *  [32..35]  HostTimestamp    (4 bytes)
 *  [36] MotorPowerLevel
 *  [37] AudioControl2
 *  [38] Flags: AllowLightBrightnessChange(0), AllowColorLightFadeAnimation(1),
 *              EnableImprovedRumbleEmulation(2), UseRumbleNotHaptics2(3)
 *  [39] HapticLowPassFilter + UNKBIT
 *  [40] UNK
 *  [41] LightFadeAnimation
 *  [42] LightBrightness
 *  [43] PlayerIndicators
 *  [44] LedRed
 *  [45] LedGreen
 *  [46] LedBlue
 */

static uint8_t state[SET_STATE_SIZE];
static volatile bool spk_active;
static volatile bool vol_dirty;

void state_mgr_init(const uint8_t *init_data, uint8_t len)
{
    memset(state, 0, SET_STATE_SIZE);
    if (len > SET_STATE_SIZE)
        len = SET_STATE_SIZE;
    memcpy(state, init_data, len);
    spk_active = false;
    vol_dirty = false;
}

void state_mgr_update(const uint8_t *data, uint8_t len)
{
    if (len < SET_STATE_SIZE)
        return;

    uint8_t f0 = data[0], f1 = data[1];
    struct config_body *cfg = config_get();
    /* Keep the initialized Allow flags; only the rumble mode bits follow
     * every host report. Unflagged fields retain this session's state. */
    state[0] = (state[0] & ~0x03) | (f0 & 0x03);
    state[38] = (state[38] & ~0x0c) | (data[38] & 0x0c);
    bool rumble = (f0 & 0x02) || (data[38] & 0x08);
    state[2] = rumble ? data[2] : 0;
    state[3] = rumble ? data[3] : 0;
    if (f0 & 0x04) memcpy(state + 10, data + 10, 11);
    if (f0 & 0x08) memcpy(state + 21, data + 21, 11);
    if (!cfg->lock_volume && (f0 & 0x10))
        cfg->headset_volume = state[4] = data[4];
    if (!cfg->lock_volume && (f0 & 0x20))
        cfg->speaker_volume = state[5] = data[5];
    if (f0 & 0x40) state[6] = data[6];
    if (f0 & 0x80) state[7] = data[7];
    if (f1 & 0x01) state[8] = data[8];
    if (f1 & 0x02) state[9] = data[9];
    if (f1 & 0x40) {
        state[36] = (state[36] & 0xf0) | (data[36] & 0x0f);
        if (!cfg->trigger_reduce)
            state[36] = (state[36] & 0x0f) | (data[36] & 0xf0);
    }
    if (f1 & 0x80) {
        if (!cfg->speaker_gain)
            state[37] = (state[37] & 0xf8) | (data[37] & 0x07);
        state[37] = (state[37] & 0x07) | (data[37] & 0xf8);
    }
    if (f1 & 0x20) state[39] = data[39];
    if (data[38] & 0x02) state[41] = data[41];
    if (data[38] & 0x01) state[42] = data[42];
    if (f1 & 0x10) state[43] = data[43];
    if (f1 & 0x04) memcpy(state + 44, data + 44, 3);
}

void state_mgr_get(uint8_t *out, uint8_t size)
{
    if (size > SET_STATE_SIZE)
        size = SET_STATE_SIZE;
    memcpy(out, state, size);
}

void state_mgr_apply_config(uint8_t spk_vol, uint8_t hp_vol, uint8_t spk_gain,
                            uint8_t trigger_reduce)
{
    state[4] = hp_vol;          /* VolumeHeadphones */
    state[5] = spk_vol;         /* VolumeSpeaker */
    if (spk_gain)
        state[37] = (state[37] & 0xf8) | (spk_gain & 0x07);
    if (trigger_reduce > 0) {
        /* state[36] high nibble = TriggerMotorPowerReduction [0..10] */
        state[36] = (state[36] & 0x0F) | ((trigger_reduce & 0x0F) << 4);
    }
}

void state_mgr_set_spk_active(bool active)
{
    spk_active = active;
}

bool state_mgr_is_spk_active(void)
{
    return spk_active;
}

void state_mgr_set_volume(uint8_t spk_vol, uint8_t hp_vol)
{
    if (config_get()->lock_volume)
        return;
    state[0] |= 0x30;
    state[4] = hp_vol;
    state[5] = spk_vol;
    config_get()->headset_volume = hp_vol;
    config_get()->speaker_volume = spk_vol;
    vol_dirty = true;
}

void state_mgr_set_mute(bool mute)
{
    if (config_get()->lock_volume)
        return;
    state[1] |= 0x02;
    if (mute)
        state[9] |= 0x60;
    else
        state[9] &= ~0x60;
    vol_dirty = true;
}

void state_mgr_restore_config_volume(void)
{
    struct config_body *cfg = config_get();
    state[0] |= 0x30;
    state[1] |= 0x02;
    state[4] = cfg->headset_volume;
    state[5] = cfg->speaker_volume;
    state[9] &= ~0x60;
    vol_dirty = true;
}

bool state_mgr_vol_dirty(void) { return vol_dirty; }
void state_mgr_vol_ack(void)   { vol_dirty = false; }

void state_mgr_set_led_color(uint8_t r, uint8_t g, uint8_t b)
{
    state[44] = r;
    state[45] = g;
    state[46] = b;
}

#ifndef STATE_MGR_H
#define STATE_MGR_H

#include <stdbool.h>
#include <stdint.h>

#define SET_STATE_SIZE 47

/**
 * Initialize state manager with default SetStateData.
 * Called on each new controller connection.
 */
void state_mgr_init(const uint8_t *init_data, uint8_t len);

/**
 * Selectively merge incoming USB SetStateData into persistent state.
 * Only fields whose Allow flags are set in the incoming data are updated;
 * unflagged fields retain their previous values.
 * Matches DS5Dongle's state_update logic.
 */
void state_mgr_update(const uint8_t *data, uint8_t len);

/**
 * Copy the current merged state into the output buffer.
 * Initialized Allow flags are retained, with rumble mode bits updated from
 * host reports, matching DS5Dongle.
 */
void state_mgr_get(uint8_t *out, uint8_t size);

/**
 * Apply saved config values to the initial state.
 * Called once after state_mgr_init to override default volumes/gain
 * with user-configured values (matches DS5Dongle's state_init).
 */
void state_mgr_apply_config(uint8_t spk_vol, uint8_t hp_vol, uint8_t spk_gain,
                            uint8_t trigger_reduce);

/**
 * Set speaker/headphone volume directly (from UAC SET_CUR).
 * Values are DualSense range [0, 127].
 */
void state_mgr_set_volume(uint8_t spk_vol, uint8_t hp_vol);

/**
 * Set speaker+headphone mute (from UAC SET_CUR).
 */
void state_mgr_set_mute(bool mute);

/**
 * Check if a UAC volume/mute change is pending.
 * If true, the caller should send a BT output report with state_mgr_get()
 * to push the update to the controller, then acknowledge the matching snapshot version.
 */
bool state_mgr_vol_dirty(void);

/**
 * Clear the volume-dirty flag after the update has been sent.
 */
bool state_mgr_volume_snapshot(uint8_t *out, uint8_t size, uint32_t *version);
void state_mgr_vol_ack(uint32_t version);

/**
 * Persist custom LED color into internal state so subsequent
 * state_mgr_get() calls (e.g. volume updates) keep the color.
 */
void state_mgr_set_led_color(uint8_t r, uint8_t g, uint8_t b);

void state_mgr_merge_snapshot(uint8_t *data, uint8_t size);

/* Latest desired state survives congestion; ACK only the exact accepted version. */
bool state_mgr_output_pending(void);
bool state_mgr_output_snapshot(uint8_t *out, uint8_t size, uint32_t *version);
void state_mgr_output_ack(uint32_t version);
void state_mgr_set_mic_light(bool muted);
void state_mgr_usb_reset(void);

#endif /* STATE_MGR_H */

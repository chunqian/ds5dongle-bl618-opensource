#ifndef AUDIO_H
#define AUDIO_H

#include <stdint.h>
#include <stdbool.h>

/**
 * Initialize the audio processing pipeline (Opus encoder, buffers).
 * Must be called once from main before starting audio_task.
 * Returns 0 on success, -1 on Opus init failure.
 */
int audio_init(void);

/**
 * Audio processing task entry point (FreeRTOS).
 * Blocks on USB audio semaphore, processes PCM, encodes Opus,
 * accumulates 2 frames and sends BT report 0x39 (double-frame).
 */
void audio_task(void *arg);

/**
 * Set headset plug state (parsed from BT input report byte[56] bit0).
 * When plugged, speaker tag switches from 0x93 to 0x96.
 */
void audio_set_headset(bool plugged);

/**
 * Discard controller-session audio and request codec resets.
 * USB host alternate settings are retained for Bluetooth reconnect.
 */
void audio_reset(void);

/**
 * Request a speaker encoder reset (safe from USB ISR or Bluetooth task).
 * The audio task alone resets the codec and its frame buffers before reuse.
 */
void audio_reset_encoder(void);

/**
 * Feed a mic Opus frame from the controller (called from BT callback context).
 * @param opus_data  pointer to the Opus-encoded frame
 * @param len        available byte length (must be >= 71)
 */
void audio_mic_feed(const uint8_t *opus_data, uint16_t len);

/**
 * Set mic active state (called when USB host opens/closes mic interface).
 * Sends a 0x32 status report to the controller to start/stop mic streaming.
 */
void audio_set_mic_active(bool active);

/**
 * Check if mic streaming is active.
 */
bool audio_mic_active(void);

/**
 * Mic decode task entry point (FreeRTOS).
 * Blocks on mic_queue, decodes Opus frames, writes to USB mic ring buffer.
 * Must run at lower priority than audio_task to avoid blocking speaker path.
 */
void audio_mic_task(void *arg);

/**
 * Check if the audio task was killed by the encode watchdog and respawn it.
 * Call periodically from bt_task or main loop. Returns true if respawned.
 */
bool audio_check_respawn(void);

/* Start workers; failed allocations are retried by the USB task. */
void audio_start_tasks(void);

#endif /* AUDIO_H */

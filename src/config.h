#ifndef CONFIG_H
#define CONFIG_H

#include <stdint.h>
#include <stdbool.h>

/* Default controller lightbar color: dim blue (#000040). */
#define CONFIG_DEFAULT_LED_R 0x00
#define CONFIG_DEFAULT_LED_G 0x00
#define CONFIG_DEFAULT_LED_B 0x40

/*
 * Configuration body — preserve the legacy Flash layout when loading
 * settings saved by earlier firmware. No host configuration write API.
 */
struct __attribute__((packed)) config_body {
    uint8_t config_version;
    float   haptics_gain;         /* [1.0,2.0]  — haptics amplitude scaling */
    uint8_t speaker_volume;       /* [0,127]    — speaker volume level */
    uint8_t headset_volume;       /* [0,127]    — headset volume level */
    uint8_t speaker_gain;         /* [0,7]      — speaker gain (SpeakerCompPreGain) */
    uint8_t inactive_time;        /* [0,60] min  (0 = disable) */
    uint8_t disable_led;          /* bool */
    uint8_t polling_rate_mode;    /* 0: 250Hz, 1: 500Hz, 2: real-time */
    uint8_t audio_buffer_length;  /* [16,127]   — controller audio buffer depth */
    uint8_t controller_mode;      /* 0: DS5, 1: DSE, 2: Auto */
    uint8_t enable_usb_sn;        /* bool */
    uint8_t ps_shortcut_enabled;  /* reserved legacy byte, always 0 */
    uint8_t disable_mic;          /* bool       — disable mic passthrough */
    uint8_t disable_speaker;      /* bool       — disable Opus speaker encoding */
    uint8_t enable_wake;          /* bool */
    uint8_t trigger_reduce;       /* [0,10]     — trigger motor power reduction */
    uint8_t lock_volume;          /* bool       — ignore host volume changes */
    uint8_t dse_detected;         /* bool — Auto mode remembers last detection */
    uint8_t usb_stealth;          /* bool — hide USB until BT controller connects */
    uint8_t led_r;                /* [0-255] custom LED red   (default 0x00) */
    uint8_t led_g;                /* [0-255] custom LED green (default 0x00) */
    uint8_t led_b;                /* [0-255] custom LED blue  (default 0x40) */
    uint8_t tp_mode;              /* 0=off, 1=dpad, 4=split; legacy mouse modes disabled */
    uint8_t tp_mode_enabled_mask; /* enabled modes: bits 0, 1, 4 */
    uint8_t tp_mouse_sensitivity; /* reserved legacy byte, always 0 */
    uint8_t audio_haptic;         /* 0=off, 1=auto(game priority), 2=force */
};

#define CONFIG_VERSION  2

void config_load(void);
/* Independent persisted detection hint; never saves other unsaved preferences. */
void config_note_controller(bool edge);
void config_validate(void);

struct config_body *config_get(void);

/* Convenience accessors for hot-path fields */
static inline bool config_wake_enabled(void)       { return config_get()->enable_wake; }
static inline bool config_led_disabled(void)        { return config_get()->disable_led; }
static inline uint8_t config_inactive_minutes(void) { return config_get()->inactive_time; }
static inline uint8_t config_polling_mode(void)     { return config_get()->polling_rate_mode; }
static inline uint8_t config_controller_mode(void)  { return config_get()->controller_mode; }
static inline bool config_dse_detected(void)        { return config_get()->dse_detected; }
static inline bool config_speaker_disabled(void)    { return config_get()->disable_speaker; }
static inline bool config_mic_disabled(void)         { return config_get()->disable_mic; }
static inline uint8_t config_audio_buf_len(void)    { return config_get()->audio_buffer_length; }
static inline bool config_usb_stealth(void)         { return config_get()->usb_stealth; }

#endif /* CONFIG_H */

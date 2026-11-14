#include "remap.h"
#include "debug_log.h"
#include "easyflash.h"
#include "config.h"
#include "deferred_store.h"
#include "bflb_irq.h"
#include <string.h>
#include <stdbool.h>

/* ------------------------------------------------------------------ */
/* Bit extraction helpers: indices into the 63-byte USB input payload  */
/* p[7]:  dpad[3:0]  ■[4] ✕[5] ○[6] △[7]                           */
/* p[8]:  L1[0] R1[1] L2[2] R2[3] Create[4] Options[5] L3[6] R3[7]  */
/* p[9]:  PS[0] TP_click[1] Mute[2]                                   */
/* ------------------------------------------------------------------ */

static const struct { uint8_t off; uint8_t mask; } BTN_LOC[REMAP_LEGACY_BTN_COUNT] = {
    [REMAP_BTN_SQUARE]   = { 7, 0x10 },
    [REMAP_BTN_CROSS]    = { 7, 0x20 },
    [REMAP_BTN_CIRCLE]   = { 7, 0x40 },
    [REMAP_BTN_TRIANGLE] = { 7, 0x80 },
    [REMAP_BTN_L1]       = { 8, 0x01 },
    [REMAP_BTN_R1]       = { 8, 0x02 },
    [REMAP_BTN_L2]       = { 8, 0x04 },
    [REMAP_BTN_R2]       = { 8, 0x08 },
    [REMAP_BTN_CREATE]   = { 8, 0x10 },
    [REMAP_BTN_OPTIONS]  = { 8, 0x20 },
    [REMAP_BTN_L3]       = { 8, 0x40 },
    [REMAP_BTN_R3]       = { 8, 0x80 },
    [REMAP_BTN_PS]       = { 9, 0x01 },
    [REMAP_BTN_TP_CLICK] = { 9, 0x02 },
    [REMAP_BTN_MUTE]     = { 9, 0x04 },
};

/* Touchpad split-zone parameters */
#define TP_SPLIT_X      960
#define TP_L_CENTER_X   480
#define TP_R_CENTER_X   1440
#define TP_CENTER_Y     540
#define TP_DEADZONE     120

/* ------------------------------------------------------------------ */
/* Module state                                                         */
/* ------------------------------------------------------------------ */

static remap_entry_t g_profiles[REMAP_PROFILE_COUNT][REMAP_BTN_COUNT];
static uint8_t active_profile = 0;
static bool g_remap_is_identity = true;

static const char *profile_keys[REMAP_PROFILE_COUNT] = {
    "btn_remap",      /* profile 0 — backward compatible key */
    "btn_remap_1",    /* profile 1 */
};

/* ------------------------------------------------------------------ */
/* Internal helpers                                                     */
/* ------------------------------------------------------------------ */

static bool validate_entry(const remap_entry_t *e)
{
    if (e->type == REMAP_TYPE_BTN) {
        return e->value < REMAP_BTN_COUNT;
    }
    return false;
}


static void sanitize_entry(remap_entry_t *e, uint8_t src_id)
{
    if (!validate_entry(e))
        *e = (remap_entry_t){ REMAP_TYPE_BTN, src_id, 0, 0 };
    e->modifier = e->flags = 0; /* Legacy keyboard metadata is reserved now. */
}

static void reset_table(remap_entry_t *table)
{
    for (int i = 0; i < REMAP_BTN_COUNT; i++)
        table[i] = (remap_entry_t){ REMAP_TYPE_BTN, (uint8_t)i, 0, 0 };
}

static bool is_table_identity(const remap_entry_t *table)
{
    for (int i = 0; i < REMAP_BTN_COUNT; i++) {
        if (table[i].type   != REMAP_TYPE_BTN ||
            table[i].value  != (uint8_t)i     ||
            table[i].modifier != 0             ||
            table[i].flags  != 0)
            return false;
    }
    return true;
}

static void update_identity_cache(void)
{
    g_remap_is_identity = is_table_identity(g_profiles[active_profile]);
}

static inline uint8_t get_src_bit(const uint8_t *p, int id)
{
    if (id < REMAP_LEGACY_BTN_COUNT)
        return (p[BTN_LOC[id].off] & BTN_LOC[id].mask) ? 1u : 0u;

    if (id >= REMAP_BTN_DPAD_UP && id <= REMAP_BTN_DPAD_RIGHT) {
        uint8_t hat = p[7] & 0x0F;
        switch (id) {
        case REMAP_BTN_DPAD_UP:    return (hat == 0 || hat == 1 || hat == 7) ? 1u : 0u;
        case REMAP_BTN_DPAD_DOWN:  return (hat == 3 || hat == 4 || hat == 5) ? 1u : 0u;
        case REMAP_BTN_DPAD_LEFT:  return (hat == 5 || hat == 6 || hat == 7) ? 1u : 0u;
        case REMAP_BTN_DPAD_RIGHT: return (hat == 1 || hat == 2 || hat == 3) ? 1u : 0u;
        }
    }

    if (id >= REMAP_BTN_TP_L_TOUCH && id <= REMAP_BTN_TP_R_CLICK) {
        uint8_t mode = config_get()->tp_mode;

        /* mode 0 (off): all zone buttons suppressed */
        if (mode == 0) return 0u;

        /* Determine which button groups are active and their centers */
        bool left_active  = (mode == 1 || mode == 4);
        bool right_active = (mode == 1 || mode == 4);

        /* Suppress inactive groups */
        if (!left_active && id >= REMAP_BTN_TP_L_TOUCH && id <= REMAP_BTN_TP_L_CLICK)
            return 0u;
        if (!right_active && id >= REMAP_BTN_TP_R_TOUCH && id <= REMAP_BTN_TP_R_CLICK)
            return 0u;

        const uint8_t *tp = &p[32];
        if (tp[0] & 0x80) return 0u; /* finger 0 not active */
        int16_t x = tp[1] | ((tp[2] & 0x0F) << 8);
        int16_t y = ((tp[2] & 0xF0) >> 4) | (tp[3] << 4);

        /* Center point depends on mode */
        int16_t cx_l, cx_r;
        if (mode == 1) {
            cx_l = 960; cx_r = 960; /* whole-pad center */
        } else {
            cx_l = TP_L_CENTER_X; cx_r = TP_R_CENTER_X;
        }

        /* In split mode (4), restrict to the correct half */
        bool left_half = (x < TP_SPLIT_X);
        bool need_half_check = (mode != 1);

        /* Left-half sources (19-24) */
        if (id >= REMAP_BTN_TP_L_TOUCH && id <= REMAP_BTN_TP_L_CLICK) {
            if (need_half_check && !left_half) return 0u;
            if (id == REMAP_BTN_TP_L_TOUCH) return 1u;
            if (id == REMAP_BTN_TP_L_CLICK)
                return (p[9] & 0x02) ? 1u : 0u;
            int16_t dx = x - cx_l;
            int16_t dy = y - TP_CENTER_Y;
            int16_t adx = dx < 0 ? -dx : dx;
            int16_t ady = dy < 0 ? -dy : dy;
            if (adx < TP_DEADZONE && ady < TP_DEADZONE) return 0u;
            switch (id) {
            case REMAP_BTN_TP_L_UP:    return (ady >= adx && dy < 0) ? 1u : 0u;
            case REMAP_BTN_TP_L_DOWN:  return (ady >= adx && dy > 0) ? 1u : 0u;
            case REMAP_BTN_TP_L_LEFT:  return (adx > ady  && dx < 0) ? 1u : 0u;
            case REMAP_BTN_TP_L_RIGHT: return (adx > ady  && dx > 0) ? 1u : 0u;
            }
        }

        /* Right-half sources (25-30) */
        if (id >= REMAP_BTN_TP_R_TOUCH && id <= REMAP_BTN_TP_R_CLICK) {
            if (need_half_check && left_half) return 0u;
            if (id == REMAP_BTN_TP_R_TOUCH) return 1u;
            if (id == REMAP_BTN_TP_R_CLICK)
                return (p[9] & 0x02) ? 1u : 0u;
            int16_t dx = x - cx_r;
            int16_t dy = y - TP_CENTER_Y;
            int16_t adx = dx < 0 ? -dx : dx;
            int16_t ady = dy < 0 ? -dy : dy;
            if (adx < TP_DEADZONE && ady < TP_DEADZONE) return 0u;
            switch (id) {
            case REMAP_BTN_TP_R_UP:    return (ady >= adx && dy < 0) ? 1u : 0u;
            case REMAP_BTN_TP_R_DOWN:  return (ady >= adx && dy > 0) ? 1u : 0u;
            case REMAP_BTN_TP_R_LEFT:  return (adx > ady  && dx < 0) ? 1u : 0u;
            case REMAP_BTN_TP_R_RIGHT: return (adx > ady  && dx > 0) ? 1u : 0u;
            }
        }
    }
    return 0u;
}

static void load_single_profile(uint8_t idx)
{
    size_t len = 0;
    ef_get_env_blob(profile_keys[idx], g_profiles[idx],
                    sizeof(g_profiles[idx]), &len);
    size_t n_loaded = len / sizeof(remap_entry_t);
    for (size_t i = 0; i < n_loaded; i++)
        sanitize_entry(&g_profiles[idx][i], (uint8_t)i);
    /* Old format (23 entries): indices 19-22 had different meaning (TP_UP/DOWN/LEFT/RIGHT).
     * Reset them to identity since the touchpad model has changed. */
    if (n_loaded > 0 && n_loaded <= REMAP_OLD_BTN_COUNT) {
        for (size_t i = REMAP_BTN_TP_L_TOUCH; i < REMAP_BTN_COUNT; i++)
            g_profiles[idx][i] = (remap_entry_t){ REMAP_TYPE_BTN, (uint8_t)i, 0, 0 };
    } else {
        for (size_t i = n_loaded; i < REMAP_BTN_COUNT; i++)
            g_profiles[idx][i] = (remap_entry_t){ REMAP_TYPE_BTN, (uint8_t)i, 0, 0 };
    }
}

/* ------------------------------------------------------------------ */
/* Public API                                                           */
/* ------------------------------------------------------------------ */

void remap_init(void)
{
    for (int p = 0; p < REMAP_PROFILE_COUNT; p++)
        reset_table(g_profiles[p]);
    active_profile = 0;
    g_remap_is_identity = true;
}

void remap_load(void)
{
    for (int p = 0; p < REMAP_PROFILE_COUNT; p++)
        load_single_profile(p);

    size_t len = 0;
    uint8_t saved_prof = 0;
    ef_get_env_blob("remap_prof", &saved_prof, 1, &len);
    if (len == 1 && saved_prof < REMAP_PROFILE_COUNT)
        active_profile = saved_prof;
    else
        active_profile = 0;

    update_identity_cache();
    LOG_INF("[REMAP] Loaded profiles, active=%d, identity=%d\n",
            active_profile, (int)g_remap_is_identity);
}

void remap_switch_profile(uint8_t profile)
{
    if (profile >= REMAP_PROFILE_COUNT) return;
    uintptr_t irq = bflb_irq_save();
    active_profile = profile;
    update_identity_cache();
    deferred_store_request(STORE_REMAP_ACTIVE, &active_profile, 1);
    bflb_irq_restore(irq);
    LOG_INF("[REMAP] Switched to profile %d, identity=%d\n",
            active_profile, (int)g_remap_is_identity);
}

uint8_t remap_get_active_profile(void)
{
    return active_profile;
}

void remap_apply(uint8_t *p)
{
    if (g_remap_is_identity)
        return;

    const remap_entry_t *remap = g_profiles[active_profile];

    uint8_t src[REMAP_BTN_COUNT];
    for (int i = 0; i < REMAP_BTN_COUNT; i++)
        src[i] = get_src_bit(p, i);
    uint8_t analog_l2 = p[4];
    uint8_t analog_r2 = p[5];

    uint8_t dst[REMAP_BTN_COUNT];
    memset(dst, 0, sizeof(dst));

    for (int i = 0; i < REMAP_BTN_COUNT; i++) {
        if (!src[i])
            continue;
        if (remap[i].type == REMAP_TYPE_BTN) {
            dst[remap[i].value] |= 1;
        }
    }

    if (remap[REMAP_BTN_L2].type  == REMAP_TYPE_BTN &&
        remap[REMAP_BTN_L2].value == REMAP_BTN_R2   &&
        remap[REMAP_BTN_R2].type  == REMAP_TYPE_BTN &&
        remap[REMAP_BTN_R2].value == REMAP_BTN_L2) {
        p[4] = analog_r2;
        p[5] = analog_l2;
    }

    /* Reconstruct D-pad HAT from virtual direction bits */
    uint8_t du = dst[REMAP_BTN_DPAD_UP],   dd = dst[REMAP_BTN_DPAD_DOWN];
    uint8_t dl = dst[REMAP_BTN_DPAD_LEFT], dr = dst[REMAP_BTN_DPAD_RIGHT];
    uint8_t hat;
    if      (du && dr) hat = 1; /* NE */
    else if (dr && dd) hat = 3; /* SE */
    else if (dd && dl) hat = 5; /* SW */
    else if (dl && du) hat = 7; /* NW */
    else if (du)       hat = 0; /* N  */
    else if (dr)       hat = 2; /* E  */
    else if (dd)       hat = 4; /* S  */
    else if (dl)       hat = 6; /* W  */
    else               hat = 8; /* none */

    p[7] = hat
         | (dst[REMAP_BTN_SQUARE]   ? 0x10 : 0)
         | (dst[REMAP_BTN_CROSS]    ? 0x20 : 0)
         | (dst[REMAP_BTN_CIRCLE]   ? 0x40 : 0)
         | (dst[REMAP_BTN_TRIANGLE] ? 0x80 : 0);

    p[8] = (dst[REMAP_BTN_L1]      ? 0x01 : 0)
         | (dst[REMAP_BTN_R1]      ? 0x02 : 0)
         | (dst[REMAP_BTN_L2]      ? 0x04 : 0)
         | (dst[REMAP_BTN_R2]      ? 0x08 : 0)
         | (dst[REMAP_BTN_CREATE]  ? 0x10 : 0)
         | (dst[REMAP_BTN_OPTIONS] ? 0x20 : 0)
         | (dst[REMAP_BTN_L3]      ? 0x40 : 0)
         | (dst[REMAP_BTN_R3]      ? 0x80 : 0);

    p[9] = (p[9] & 0xF8)
         | (dst[REMAP_BTN_PS]       ? 0x01 : 0)
         | (dst[REMAP_BTN_TP_CLICK] ? 0x02 : 0)
         | (dst[REMAP_BTN_MUTE]     ? 0x04 : 0);
}

void remap_tp_cycle_mode(void)
{
    struct config_body *cfg = config_get();
    uint8_t mask = cfg->tp_mode_enabled_mask & 0x13;
    if (mask == 0) mask = 0x01;

    uint8_t cur = cfg->tp_mode;
    for (int i = 0; i < 5; i++) {
        cur = (cur + 1) % 5;
        if (mask & (1u << cur)) break;
    }
    cfg->tp_mode = cur;

}

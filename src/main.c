#include "board.h"
#include "bflb_mtimer.h"
#include "bflb_gpio.h"
#include "FreeRTOS.h"
#include "task.h"
#include "queue.h"

#include "bt_hid_host.h"
#include "ds5_protocol.h"
#include "usb_gamepad.h"
#include "ds5_usb_audio.h"
#include "audio.h"
#include "usb_wake.h"
#include "led_status.h"
#include "state_mgr.h"
#include "config.h"
#include "factory_reset.h"
#include "dse.h"
#include "remap.h"

#include "bl616_glb.h"
#include "btble_lib_api.h"
#include "rfparam_adapter.h"
#include "hci_driver.h"
#include "bflb_mtd.h"
#include "easyflash.h"
#include "board_config.h"
#include "debug_log.h"
#include <string.h>

#define STACK_WORDS(bytes) \
    (((bytes) + sizeof(StackType_t) - 1) / sizeof(StackType_t))

#define BT_TASK_STACK_SIZE    STACK_WORDS(1024*24)
#define BT_TASK_PRIORITY      (configMAX_PRIORITIES - 3)
#define USB_TASK_STACK_SIZE   STACK_WORDS(1024*12)
#define USB_TASK_PRIORITY     (configMAX_PRIORITIES - 1)
#define LED_TASK_STACK_SIZE   STACK_WORDS(1024*2)
#define LED_TASK_PRIORITY     (tskIDLE_PRIORITY + 1)
#define BOOT_HOLD_US         (3ULL * 1000000ULL)
#define BOOT_CLICK_WINDOW    5    /* 500ms window between clicks */

#define INPUT_QUEUE_ITEM_SZ  sizeof(struct input_message)

struct input_message { uint32_t session; uint8_t data[DS5_BT_INPUT_REPORT_SIZE]; };


static QueueHandle_t input_queue;
static QueueHandle_t output_queue;
/* Critical workers and queues never depend on runtime heap availability. */
static StaticTask_t bt_tcb, usb_tcb, led_tcb;
static StackType_t bt_stack[BT_TASK_STACK_SIZE], usb_stack[USB_TASK_STACK_SIZE],
                   led_stack[LED_TASK_STACK_SIZE];
static StaticQueue_t input_queue_cb, output_queue_cb;
static uint8_t input_queue_storage[INPUT_QUEUE_ITEM_SZ];
static uint8_t output_queue_storage[1]; /* Coalesced wake signal, not report storage. */
static volatile bool ds5_connected = false;
static volatile bool bt_disconnect_pending_usb = false;
static volatile bool bt_init_done = false;
static volatile bool bt_init_failed;

/* LED auto-off timers */
#define LED_DISABLE_OFF_US   (60ULL * 1000000ULL)  /* 1 minute */
static volatile uint64_t conn_led_start_us = 0;
static volatile bool conn_led_off = false;
static volatile uint64_t handshake_start_us;
static volatile TickType_t connecting_start_tick = 0;
static volatile bool prev_led_disabled = false;
static volatile bool scan_after_disconnect = false;
static uint8_t output_seq = 0;  /* sequence counter for 0x31 BT output */
static bool first_input_logged = false;
static uint32_t usb_fwd_count = 0;


/* Build a standard 0x31 BT output report (78 bytes) for ongoing output.
 * set_state_data: 47 bytes of SetStateData
 * seq: sequence counter
 * out: 78-byte output buffer */
static void build_bt_output(const uint8_t *set_state_data, uint16_t len,
                            uint8_t seq, uint8_t *out)
{
    memset(out, 0, DS5_BT_OUTPUT_REPORT_SIZE);
    out[0] = DS5_BT_OUTPUT_REPORT_ID;
    out[1] = seq << 4;
    out[2] = DS5_BT_OUTPUT_TAG;
    if (len > DS5_USB_OUTPUT_PAYLOAD_LEN)
        len = DS5_USB_OUTPUT_PAYLOAD_LEN;
    memcpy(out + 3, set_state_data, len);

    uint32_t crc = ds5_crc32(DS5_BT_OUTPUT_CRC_SEED, out,
                             DS5_BT_OUTPUT_REPORT_SIZE - 4);
    ds5_write_le32(&out[DS5_BT_OUTPUT_REPORT_SIZE - 4], crc);
}

/* Build an extended 0x32 BT output report (142 bytes).
 * Matches DS5Dongle exactly: pkt[1]=0x10 hardcoded (PS5 native BT format). */
static void build_bt_output_ext(const uint8_t *set_state_data, uint16_t len,
                                uint8_t *out)
{
    memset(out, 0, DS5_BT_OUTPUT_EXT_SIZE);
    out[0] = DS5_BT_OUTPUT_REPORT_ID_EXT;
    out[1] = 0x10;  /* hardcoded — matches PS5 native BT format */
    out[2] = DS5_BT_OUTPUT_EXT_TAG;
    out[3] = DS5_BT_OUTPUT_EXT_PAYLOAD;
    if (len > DS5_BT_OUTPUT_EXT_PAYLOAD)
        len = DS5_BT_OUTPUT_EXT_PAYLOAD;
    memcpy(out + 4, set_state_data, len);

    uint32_t crc = ds5_crc32(DS5_BT_OUTPUT_CRC_SEED, out,
                             DS5_BT_OUTPUT_EXT_SIZE - 4);
    ds5_write_le32(&out[DS5_BT_OUTPUT_EXT_SIZE - 4], crc);
}

/* DS5Dongle state_init_data, with the user's configured LED color retained.
 * Reset the complete controller state once per connection; no old-session
 * trigger replay or delayed LED primer that could overwrite host output. */
static const uint8_t init_set_state[DS5_USB_OUTPUT_PAYLOAD_LEN] = {
    [0] = 0xfd, [1] = 0xf7,
    [6] = 0xff,                 /* Mic volume */
    [7] = 0x09,                 /* Internal mic, noise cancellation */
    [9] = 0x0f,                 /* Touch/motion/haptic/audio power save */
    [37] = 0x01,                /* SpeakerCompPreGain */
    [38] = 0x07,                /* Brightness/fade + improved rumble */
    [41] = 0x02, [42] = 0x01,   /* FadeOut, Mid */
    [44] = CONFIG_DEFAULT_LED_R,
    [45] = CONFIG_DEFAULT_LED_G,
    [46] = CONFIG_DEFAULT_LED_B,
};

/* ---- BT HID callbacks ---- */

static int send_led_primer(uint32_t session)
{
    uintptr_t irq = bflb_irq_save();
    state_mgr_init(init_set_state, DS5_USB_OUTPUT_PAYLOAD_LEN);
    struct config_body *cfg = config_get();
    state_mgr_apply_config(cfg->speaker_volume, cfg->headset_volume,
                           cfg->speaker_gain, cfg->trigger_reduce);
    state_mgr_set_led_color(cfg->led_r, cfg->led_g, cfg->led_b);
    uint8_t merged[DS5_USB_OUTPUT_PAYLOAD_LEN];
    state_mgr_get(merged, sizeof(merged));
    bflb_irq_restore(irq);
    uint8_t buf[DS5_BT_OUTPUT_EXT_SIZE];
    build_bt_output_ext(merged, sizeof(merged), buf);
    return bt_hid_host_send_output_reliable_session(buf, sizeof(buf), session);
}

/* Called after the initial Feature GET batch, before completing detection. */
static int on_hid_primer(uint32_t session)
{
    int ret = send_led_primer(session);
    if (!ret) LOG_INF("[MAIN] Initial 0x32 queued after Feature GETs\n");
    return ret;
}

static void on_hid_input(const uint8_t *data, uint16_t len)
{
    if (len < 3) return;

    /* data[0] = report ID (0x31), data[1] = flags */
    if ((data[1] >> 1) & 1) {
        /* Bit1 set: mic Opus frame at data[3], length = len - 3 */
        if (len > 3)
            audio_mic_feed(data + 3, (uint16_t)(len - 3));
        return;
    }

    if (len >= DS5_BT_INPUT_REPORT_SIZE) {
        if (!first_input_logged) {
            first_input_logged = true;
            LOG_INF("[MAIN] First input report received (%d bytes)\n", len);
        }
        struct input_message report;
        report.session = bt_hid_host_session();
        memcpy(report.data, data, sizeof(report.data));
        xQueueOverwrite(input_queue, &report);
    }
}

static void on_hid_state(enum bt_hid_host_state state)
{
    switch (state) {
    case BT_HID_STATE_IDLE: {
        handshake_start_us = 0;
        connecting_start_tick = 0;
        bool was_connected = ds5_connected;
        if (input_queue) xQueueReset(input_queue);
        if (output_queue) xQueueReset(output_queue);
        if (ds5_connected) {
            ds5_connected = false;
            uint8_t neutral[DS5_USB_INPUT_PAYLOAD_LEN];
            memset(neutral, 0, sizeof(neutral));
            neutral[0] = 0x7F; /* LX center */
            neutral[1] = 0x7F; /* LY center */
            neutral[2] = 0x7F; /* RX center */
            neutral[3] = 0x7F; /* RY center */
            neutral[7] = DS5_DPAD_NONE; /* D-pad neutral */
            neutral[32] = neutral[36] = 0x80; /* both touch contacts released */
            usb_gamepad_send_raw_input(neutral);
        }
        ds5_connected = false;
        usb_fwd_count = 0;
        first_input_logged = false;
        usb_wake_on_bt_disconnect();
        if (bt_disconnect_pending_usb && !config_wake_enabled() &&
            !usb_wake_host_suspended()) {
            usb_gamepad_set_bt_visible(false);
            LOG_INF("[MAIN] BT disconnected: USB hidden\n");
        }
        bt_disconnect_pending_usb = false;
        dse_reset();
        audio_reset();
        if (was_connected) {
            conn_led_start_us = bflb_mtimer_get_time_us();
            conn_led_off = false;
        }
        if (!conn_led_off)
            led_status_set(was_connected ? LED_RED_BLINK : LED_PURPLE_BLINK_SLOW);
        if (scan_after_disconnect) {
            scan_after_disconnect = false;
            LOG_INF("[MAIN] State: IDLE — button-triggered rescan\n");
            bt_hid_host_scan_start(0);
        } else {
            LOG_INF("[MAIN] State: IDLE\n");
        }
        break;
    }

    case BT_HID_STATE_SCANNING:
        if (!conn_led_off)
            led_status_set(LED_PURPLE_BLINK_FAST);
        LOG_INF("[MAIN] State: SCANNING\n");
        break;

    case BT_HID_STATE_CONNECTING:
    case BT_HID_STATE_SDP_QUERY:
        connecting_start_tick = xTaskGetTickCount();
        if (!conn_led_off)
            led_status_set(LED_PURPLE_BLINK_FAST);
        LOG_INF("[MAIN] State: CONNECTING (%d)\n", state);
        break;

    case BT_HID_STATE_L2CAP_CONTROL:
    case BT_HID_STATE_L2CAP_INTERRUPT:
        if (!conn_led_off)
            led_status_set(LED_PURPLE_BLINK_FAST);
        LOG_INF("[MAIN] State: CONNECTING (%d)\n", state);
        break;

    case BT_HID_STATE_HANDSHAKE: {
        connecting_start_tick = 0;
        handshake_start_us = bflb_mtimer_get_time_us();
        LOG_INF("[MAIN] State: HANDSHAKE\n");
        break;
    }

    case BT_HID_STATE_CONNECTED: {
        connecting_start_tick = 0;
        bool is_dse = bt_hid_host_is_dse();
        uint8_t mode = config_controller_mode();
        if (mode == 2) config_note_controller(is_dse);

        usb_gamepad_set_dse_mode(mode == 1 || (mode == 2 && is_dse));
        handshake_start_us = 0;

        /* DS5Dongle reattaches only while the host is awake. Keeping the
         * existing attachment during suspend avoids an unintended USB wake. */
        if (!usb_wake_host_suspended())
            usb_gamepad_set_bt_visible(true);
        bt_disconnect_pending_usb = false;

        ds5_connected = true;
        usb_wake_on_bt_connect();
        conn_led_start_us = bflb_mtimer_get_time_us();
        conn_led_off = false;
        led_status_set(LED_GREEN_SOLID);
        LOG_INF("[MAIN] State: CONNECTED%s\n", is_dse ? " (Edge)" : "");
        break;
    }

    case BT_HID_STATE_DISCONNECTING:
        /* This event marks a real ACL teardown; ordinary scan/connect errors
         * can also enter IDLE and must not hide the configuration device. */
        bt_disconnect_pending_usb = true;
        break;

    default:
        LOG_INF("[MAIN] State: %d\n", state);
        break;
    }
}


/* Merge before acknowledging USB. A saturated transport cannot discard a
 * flagged field or final release. The queue is only a one-byte wake signal. */
static void on_usb_output(const uint8_t *data, uint16_t len)
{
    uintptr_t irq = bflb_irq_save();
    if (!ds5_connected || len < 2 || !usb_gamepad_host_active() ||
        bt_hid_host_get_state() != BT_HID_STATE_CONNECTED ||
        data[0] != DS5_USB_REPORT_ID_OUTPUT) {
        bflb_irq_restore(irq);
        return;
    }
    uint8_t payload[DS5_USB_OUTPUT_PAYLOAD_LEN] = {0};
    uint16_t copy = len - 1;
    if (copy > sizeof(payload)) copy = sizeof(payload);
    memcpy(payload, data + 1, copy);
    state_mgr_update(payload, sizeof(payload));
    uint8_t wake = 1;
    xQueueOverwriteFromISR(output_queue, &wake, NULL);
    bflb_irq_restore(irq);
}

static void process_state_output(void)
{
    uint8_t merged[DS5_USB_OUTPUT_PAYLOAD_LEN];
    uint32_t version;
    uintptr_t irq = bflb_irq_save();
    uint32_t session = bt_hid_host_session();
    uint32_t usb_session = usb_gamepad_host_session();
    bool pending = state_mgr_output_snapshot(merged, sizeof(merged), &version);
    bflb_irq_restore(irq);
    if (!pending) return;
    uint8_t packet[DS5_BT_OUTPUT_REPORT_SIZE];
    build_bt_output(merged, sizeof(merged), output_seq, packet);
    if (bt_hid_host_send_state_session(packet, sizeof(packet), session, usb_session) == 0) {
        output_seq = (output_seq + 1) & 0x0f;
        state_mgr_output_ack(version);
    }
}

/* ---- FreeRTOS tasks ---- */

static struct bflb_device_s *btn_gpio;

static void boot_button_init(void)
{
    btn_gpio = bflb_device_get_by_name("gpio");
    /* GPIO2 is the bootstrap pin — the board's external pull-down keeps
     * it LOW for normal boot; pressing the BOOT button pulls it HIGH.
     * Do NOT add an internal pull-up (conflicts with external circuit). */
    bflb_gpio_init(btn_gpio, BOOT_BUTTON_PIN,
                   GPIO_INPUT | GPIO_FLOAT | GPIO_SMT_EN);
    LOG_INF("[BTN] GPIO%d init, raw=%d\n", BOOT_BUTTON_PIN,
           (int)bflb_gpio_read(btn_gpio, BOOT_BUTTON_PIN));
}

static bool boot_button_pressed(void)
{
    /* Active HIGH: BOOT button connects GPIO2 to VCC (HIGH = pressed) */
    return bflb_gpio_read(btn_gpio, BOOT_BUTTON_PIN) != 0;
}

static void bt_task(void *arg)
{
    (void)arg;
    LOG_INF("[MAIN] Board: %s\n", BOARD_NAME);

    LOG_INF("[BT_TASK] btble_controller_init...\n");
    btble_controller_init(configMAX_PRIORITIES - 1);
    LOG_INF("[BT_TASK] hci_driver_init...\n");
    hci_driver_init();
    LOG_INF("[BT_TASK] bt_hid_host_init...\n");

    struct bt_hid_host_config cfg = {
        .input_cb   = on_hid_input,
        .state_cb   = on_hid_state,
        .primer_cb  = on_hid_primer,
        .target_vid = DS5_VID,
        .target_pid = DS5_PID,
    };

    int err = bt_hid_host_init(&cfg);
    if (err) {
        LOG_ERR("[MAIN] BT init failed: %d\n", err);
    }
    bt_init_failed = err != 0;

    /* End the wait on success or failure; BT setup may have disabled USB clocks. */
    GLB_Set_USB_CLK_From_WIFIPLL(1);
    bt_init_done = true;
    LOG_INF("[BT_TASK] BT init result=%d; USB clock restored, releasing usb_task\n", err);

    led_status_set(err ? LED_RED_BLINK : LED_PURPLE_BLINK_SLOW);
    LOG_INF("[BT_TASK] led OK\n");

    /* DS5Dongle performs one startup inquiry, including with saved bonds.
     * Page scan remains enabled so a paired controller may reconnect first.
     * Enter the task loop immediately to process authentication and HID. */
    LOG_INF("[BT_TASK] Startup inquiry\n");
    if (!err) bt_hid_host_scan_start(0);
    LOG_INF("[BT_TASK] Entering main loop\n");

    uint8_t output_wake;
    uint16_t loop_count = 0;

    /*
     * Button gesture FSM (polled at 10 Hz, independent of loop speed).
     *   single click  → switch to next bonded controller
     *   double click  → disconnect + fresh scan (search new controller)
     *   hold >= 3s    → request full settings reset; reboot on release
     */
    enum { BTN_IDLE, BTN_PRESSING, BTN_HELD, BTN_WAIT } btn_fsm = BTN_IDLE;
    uint8_t  btn_press_samples = 0;
    uint8_t  btn_wait_samples  = 0;
    uint8_t  btn_click_count   = 0;
    uint64_t btn_last_poll_us  = 0;
    uint64_t btn_press_start_us = 0;
    bool     btn_reset_requested = false;
    uint8_t  btn_debug_counter = 0;

    prev_led_disabled = config_led_disabled();
    conn_led_start_us = bflb_mtimer_get_time_us();

    for (;;) {
        loop_count++;
        if ((loop_count % 50) == 1) {
            LOG_DBG("[BT_TASK] loop #%u state=%d conn=%d\n",
                   loop_count, bt_hid_host_get_state(), ds5_connected);
        }

        /* ---- BOOT button gesture FSM (10 Hz cadence) ----
         * Placed BEFORE any continue statements so the button is
         * always responsive regardless of BT state machine activity. */
        uint64_t btn_now = bflb_mtimer_get_time_us();
        if (btn_now - btn_last_poll_us >= 100000ULL) {
            btn_last_poll_us = btn_now;
            bool pressed = boot_button_pressed();

            if (++btn_debug_counter >= 50) {
                btn_debug_counter = 0;
                LOG_DBG("[BTN] GPIO%d raw=%d fsm=%d\n",
                       BOOT_BUTTON_PIN,
                       (int)bflb_gpio_read(btn_gpio, BOOT_BUTTON_PIN),
                       btn_fsm);
            }

            switch (btn_fsm) {
            case BTN_IDLE:
                if (pressed) {
                    btn_fsm = BTN_PRESSING;
                    btn_press_samples = 1;
                    btn_press_start_us = btn_now;
                    LOG_DBG("[BTN] IO2 pressed (raw=%d)\n",
                           (int)bflb_gpio_read(btn_gpio, BOOT_BUTTON_PIN));
                }
                break;

            case BTN_PRESSING:
                if (pressed) {
                    btn_press_samples++;
                    /* LED feedback while holding: fast red blink to show
                     * "keep holding" (revert to previous pattern on release) */
                    if (btn_press_samples == 3) {
                        LOG_DBG("[BTN] IO2 holding...\n");
                        led_status_set(LED_RED_BLINK);
                    }
                    if (btn_now - btn_press_start_us >= BOOT_HOLD_US) {
                        btn_click_count = 0;
                        btn_fsm = BTN_HELD;
                        btn_reset_requested = factory_reset_request();
                        if (btn_reset_requested) {
                            LOG_INF("[BTN] IO2 held 3s — full reset saved; release to reboot\n");
                            led_status_set(LED_BLINK_TRIPLE);
                        } else {
                            LOG_ERR("[BTN] Failed to save reset request; release and retry\n");
                            led_status_set(LED_RED_BLINK);
                        }
                    }
                } else {
                    btn_click_count++;
                    btn_fsm = BTN_WAIT;
                    btn_wait_samples = 0;
                    /* Restore LED to current BT state since hold was cancelled */
                    if (btn_press_samples >= 3) {
                        led_status_set(ds5_connected ? LED_GREEN_SOLID
                                                     : LED_PURPLE_BLINK_SLOW);
                    }
                    LOG_DBG("[BTN] IO2 released (click #%d)\n", btn_click_count);
                }
                break;

            case BTN_HELD:
                if (!pressed) {
                    btn_fsm = BTN_IDLE;
                    btn_press_samples = 0;
                    if (btn_reset_requested) {
                        LOG_INF("[BTN] IO2 released after hold — resetting all settings\n");
                        vTaskDelay(pdMS_TO_TICKS(300));
                        GLB_SW_System_Reset();
                    }
                }
                break;

            case BTN_WAIT:
                if (pressed) {
                    btn_fsm = BTN_PRESSING;
                    btn_press_samples = 1;
                    btn_press_start_us = btn_now;
                } else if (++btn_wait_samples >= BOOT_CLICK_WINDOW) {
                    uint8_t clicks = btn_click_count;
                    btn_click_count = 0;
                    btn_fsm = BTN_IDLE;
                    btn_press_samples = 0;

                    if (bt_init_failed && clicks) {
                        /* Do not re-enter a partly initialized SDK. A user
                         * click retries through a complete system restart. */
                        GLB_SW_System_Reset();
                    } else if (clicks == 1) {
                        if (bt_hid_host_get_bonded_count() > 1) {
                            int next = bt_hid_host_switch_next();
                            LOG_INF("[BTN] Single click — switching to controller %d\n", next);
                        } else {
                            LOG_INF("[BTN] Single click — only 1 bonded, fresh scan\n");
                            if (ds5_connected) {
                                scan_after_disconnect = true;
                                bt_hid_host_disconnect();
                            } else {
                                bt_hid_host_scan_start(0);
                            }
                        }
                    } else if (clicks >= 2) {
                        LOG_INF("[BTN] Double click — disconnect + fresh scan\n");
                        if (ds5_connected) {
                            scan_after_disconnect = true;
                            bt_hid_host_disconnect();
                        } else {
                            bt_hid_host_scan_start(0);
                        }
                    }
                }
                break;
            }
        }

        if (bt_init_failed) {
            /* Keep USB and BOOT recovery available. */
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        bt_hid_host_persist_if_dirty();

        bt_hid_host_poll_disconnect();
        bt_hid_host_poll_control();

        if (bt_hid_host_poll_scan_early()) {
            continue;
        }

        if (bt_hid_host_poll_connect()) {
            continue;
        }

        if (bt_hid_host_poll_security()) {
            continue;
        }

        if (bt_hid_host_poll_sdp()) {
            continue;
        }

        bt_hid_host_poll_security_ready();
        bt_hid_host_poll_tx();
        usb_gamepad_process_feature_sets();
        dse_task();

        /* React to disable_led config toggle in real-time */
        bool cur_led_disabled = config_led_disabled();
        if (cur_led_disabled != prev_led_disabled) {
            prev_led_disabled = cur_led_disabled;
            if (cur_led_disabled) {
                conn_led_start_us = bflb_mtimer_get_time_us();
                conn_led_off = false;
                LOG_DBG("[LED-DBG] toggle ON: start 1min timer, conn=%d\n",
                       ds5_connected);
            } else {
                conn_led_off = false;
                if (ds5_connected) {
                    led_status_set(LED_GREEN_SOLID);
                    LOG_DBG("[LED-DBG] toggle OFF: restored green, conn=1\n");
                } else {
                    enum bt_hid_host_state s = bt_hid_host_get_state();
                    if (s == BT_HID_STATE_SCANNING ||
                        s == BT_HID_STATE_CONNECTING ||
                        s == BT_HID_STATE_SDP_QUERY ||
                        s == BT_HID_STATE_L2CAP_CONTROL ||
                        s == BT_HID_STATE_L2CAP_INTERRUPT)
                        led_status_set(LED_PURPLE_BLINK_FAST);
                    else
                        led_status_set(LED_PURPLE_BLINK_SLOW);
                    LOG_DBG("[LED-DBG] toggle OFF: restored purple, conn=0\n");
                }
            }
        }

        /* 1-min auto-off: applies to any pattern in the whitelist */
        if (config_led_disabled() && !conn_led_off &&
            led_status_can_auto_off()) {
            uint64_t now_us = bflb_mtimer_get_time_us();
            uint64_t elapsed = now_us - conn_led_start_us;
            if (elapsed >= LED_DISABLE_OFF_US) {
                led_status_set(LED_OFF);
                conn_led_off = true;
                LOG_DBG("[LED-DBG] auto-off fired: elapsed=%llus\n",
                       (unsigned long long)(elapsed / 1000000ULL));
            }
        }

        if (ds5_connected) {
            /* Wake immediately on host output; retry pending final state even
             * if no more host reports arrive. Do not replay queued raw reports. */
            xQueueReceive(output_queue, &output_wake,
                          pdMS_TO_TICKS(state_mgr_output_pending() ? 1 : 24));
            process_state_output();
        } else {
            vTaskDelay(pdMS_TO_TICKS(100));

            if (bt_hid_host_get_state() == BT_HID_STATE_HANDSHAKE)
                bt_hid_host_handshake_tick();
        }
    }
}

static bool should_use_dse(void)
{
    uint8_t mode = config_controller_mode();
    if (mode == 0) return false;
    if (mode == 1) return true;
    return config_dse_detected();
}

static void usb_task(void *arg)
{
    (void)arg;

    /* Wait for BT init to complete — it reconfigures clocks and kills USB */
    LOG_INF("[USB_TASK] Waiting for BT init to complete...\n");
    while (!bt_init_done) {
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    LOG_INF("[USB_TASK] BT init done, starting USB init\n");

    usb_gamepad_set_dse_mode(should_use_dse());
    usb_gamepad_set_polling_rate(config_polling_mode());
    usb_gamepad_init(on_usb_output);
    usb_gamepad_set_suspend_hooks(usb_wake_on_suspend,
                                  usb_wake_on_resume,
                                  usb_wake_on_configured);

    if (config_usb_stealth() && !bt_init_failed) {
        vTaskDelay(pdMS_TO_TICKS(150));
        taskENTER_CRITICAL();
        if (!ds5_connected && !usb_wake_host_suspended())
            usb_gamepad_set_bt_visible(false);
        taskEXIT_CRITICAL();
    }

    struct input_message input;
    uint8_t *raw_report = input.data;
    uint32_t usb_check_cnt = 0;
    uint32_t usb_session = bt_hid_host_session();
    uint8_t prev_mic_bit = 0xFF;

    /* Remap profile switch: Create + D-pad Left/Right */
    bool     remap_combo_fired = false;

    /* Touchpad mode cycle: Create + Touchpad Press */
    bool     tp_mode_combo_fired = false;

    for (;;) {
        /* Periodic USB status check (every ~10 seconds) */
        if (++usb_check_cnt >= 10000) {
            usb_check_cnt = 0;
            LOG_DBG("[USB-MON] UNPLUG=%d GLINT=%d "
                   "DEV_CTL=0x%08lx GLB_INT=0x%08lx\n",
                   (int)(*(volatile uint32_t *)(USB_BASE + 0x114) & 1),
                   (int)((*(volatile uint32_t *)(USB_BASE + 0x100) >> 2) & 1),
                   (unsigned long)*(volatile uint32_t *)(USB_BASE + 0x100),
                   (unsigned long)*(volatile uint32_t *)(USB_BASE + 0x0C4));
        }
        audio_check_respawn(); /* Run before input paths that may continue early. */
        usb_gamepad_process_deferred();

        uint32_t session = bt_hid_host_session();
        if (session != usb_session) {
            usb_session = session;
            prev_mic_bit = 0xFF;
            remap_combo_fired = tp_mode_combo_fired = false;
        }

        TickType_t wait = pdMS_TO_TICKS(10);
        if (xQueueReceive(input_queue, &input, wait) == pdTRUE) {
            if (input.session != bt_hid_host_session() ||
                bt_hid_host_get_state() == BT_HID_STATE_DISCONNECTING)
                continue;
            /* Wake sees original buttons, before shortcuts/remapping. */
            usb_wake_on_bt_input(raw_report + 2, DS5_USB_INPUT_PAYLOAD_LEN);
            usb_fwd_count++;
            if (usb_fwd_count == 1)
                LOG_DBG("[USB-FWD] First BT→USB input forwarded\n");
            else if ((usb_fwd_count % 500) == 0)
                LOG_DBG("[USB-FWD] %lu reports forwarded\n",
                       (unsigned long)usb_fwd_count);

            /* Remap profile switch: Create + D-pad Left → profile 0, Right → profile 1 */
            {
                bool create_held = (raw_report[2 + DS5_BTN_BYTE] & DS5_BTN_CREATE_BIT) != 0;
                uint8_t dpad = raw_report[2 + DS5_DPAD_BYTE] & DS5_DPAD_MASK;

                if (create_held && !remap_combo_fired) {
                    int target = -1;
                    if (dpad == DS5_DPAD_W)
                        target = 0;
                    else if (dpad == DS5_DPAD_E)
                        target = 1;

                    if (target >= 0 && target != remap_get_active_profile()) {
                        remap_switch_profile((uint8_t)target);
                        led_status_set(target == 0 ? LED_BLINK_ONCE : LED_BLINK_DOUBLE);
                        LOG_INF("[REMAP] Combo → profile %d\n", target);
                        remap_combo_fired = true;
                    }
                }
                if (!create_held)
                    remap_combo_fired = false;
            }

            /* Touchpad mode cycle: Create + Touchpad Press */
            {
                bool create_held = (raw_report[2 + DS5_BTN_BYTE] & DS5_BTN_CREATE_BIT) != 0;
                bool tp_pressed  = (raw_report[2 + 9] & 0x02) != 0;

                static uint8_t tp_dbg_cnt = 0;
                if (create_held && tp_pressed && tp_dbg_cnt < 5) {
                    LOG_INF("[TP-DBG] Create+TP detected, fired=%d mode=%u mask=0x%02x\n",
                            tp_mode_combo_fired, config_get()->tp_mode,
                            config_get()->tp_mode_enabled_mask);
                    tp_dbg_cnt++;
                }

                if (create_held && tp_pressed && !tp_mode_combo_fired) {
                    remap_tp_cycle_mode();
                    uint8_t new_mode = config_get()->tp_mode;
                    static const uint8_t mode_blinks[] = {
                        LED_BLINK_ONCE, LED_BLINK_DOUBLE, LED_BLINK_TRIPLE,
                        LED_BLINK_TRIPLE, LED_BLINK_TRIPLE
                    };
                    led_status_set(mode_blinks[new_mode]);
                    LOG_INF("[TP] Combo → mode %u\n", new_mode);
                    tp_mode_combo_fired = true;
                }
                if (!create_held || !tp_pressed)
                    tp_mode_combo_fired = false;

                /* Suppress Create+TP from reaching game */
                if (create_held && tp_pressed) {
                    raw_report[2 + DS5_BTN_BYTE] &= ~DS5_BTN_CREATE_BIT;
                    raw_report[2 + 9] &= ~0x02;
                }
            }

            remap_apply(raw_report + 2);
            if (usb_gamepad_send_input_session(raw_report + 2, input.session) != 0)
                continue;

            /* Headset plug detection for audio tag switching (0x93 vs 0x96) */
            audio_set_headset((raw_report[2 + DS5_HEADSET_BYTE] & 1) != 0);

            /* Mic mute button → toggle MuteLight on controller.
             * Controller toggles status_bit only when a game has activated
             * the mic — this is normal PS5 behavior. */
            {
                uint8_t cur_mic_bit = (raw_report[2 + DS5_HEADSET_BYTE] >> 2) & 1;
                if (prev_mic_bit != 0xFF && cur_mic_bit != prev_mic_bit) {
                    uintptr_t irq = bflb_irq_save();
                    if (input.session == bt_hid_host_session() && ds5_connected)
                        state_mgr_set_mic_light(cur_mic_bit != 0);
                    bflb_irq_restore(irq);
                }
                prev_mic_bit = cur_mic_bit;
            }
        }

        usb_wake_task();
    }
}

static void led_task(void *arg)
{
    (void)arg;
    for (;;) {
        led_status_tick();
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

int main(void)
{
    board_init();

    printf("\n=== DS5Dongle BL618 ===\n");
    LOG_INF("DualSense Wireless Bridge\n");

    /* Flash speed diagnostic */
    {
        volatile uint32_t *glb_sf_cfg0 = (volatile uint32_t *)0x20000170;
        uint32_t sf_val  = *glb_sf_cfg0;
        uint8_t clk_sel  = (sf_val >> 12) & 0x3;
        uint8_t clk_sel2 = (sf_val >> 14) & 0x3;
        uint8_t clk_div  = (sf_val >> 8) & 0x7;
        unsigned mhz = 0;
        const char *src = "?";
        if (clk_sel == 0) {
            if (clk_sel2 == 1)      { src = "XTAL";   mhz = 40; }
            else if (clk_sel2 == 0) { src = "PLL120"; mhz = 120; }
            else if (clk_sel2 == 2) { src = "PLL96";  mhz = 96; }
        } else if (clk_sel == 1) { src = "PLL80";  mhz = 80; }
        else if (clk_sel == 2)   { src = "BCLK";   mhz = 0; }
        else if (clk_sel == 3)   { src = "PLL160"; mhz = 160; }
        if (clk_div > 0 && mhz > 0) mhz /= (clk_div + 1);
        LOG_INF("[B] Flash: %s/%u = %uMHz\n", src, clk_div, mhz);
    }

    /* Configure Exchange Memory for BT controller (required for BR/EDR) */
    {
        extern uint8_t __LD_CONFIG_EM_SEL;
        volatile uint32_t em_size = (uint32_t)&__LD_CONFIG_EM_SEL;
        if (em_size == 0)
            GLB_Set_EM_Sel(GLB_WRAM160KB_EM0KB);
        else if (em_size == 32 * 1024)
            GLB_Set_EM_Sel(GLB_WRAM128KB_EM32KB);
        else
            GLB_Set_EM_Sel(GLB_WRAM96KB_EM64KB);
        LOG_INF("[B] EM: %lu KB\n", em_size / 1024);
    }

    bflb_mtd_init();
    if (easyflash_init() != EF_NO_ERR || !factory_reset_apply_pending()) {
        LOG_ERR("[B] Settings reset/storage failed; reconnect power to retry\n");
        /* Never start Bluetooth/USB with partially reset settings. */
        led_status_init();
        led_status_set(LED_BLINK_ERROR);
        while (1) {
            led_status_tick();
            bflb_mtimer_delay_ms(10);
        }
    }
    if (rfparam_init(0, NULL, 0) != 0)
        LOG_ERR("[B] RF init FAIL\n");
    bflb_mtimer_delay_ms(3000);

    config_load();
    remap_init();
    remap_load();
    led_status_init();
    led_status_set(LED_PURPLE_BLINK_SLOW);
    boot_button_init();
    usb_wake_init();

    usb_audio_early_init();
    bool audio_ok = (audio_init() == 0);
    LOG_INF("[B] audio: %s\n", audio_ok ? "OK" : "FAIL");

    input_queue = xQueueCreateStatic(1, INPUT_QUEUE_ITEM_SZ, input_queue_storage, &input_queue_cb);
    /* Both queues coalesce notifications. Full output state lives in state_mgr,
     * so queue saturation cannot lose a field update or release. */
    output_queue = xQueueCreateStatic(1, sizeof(uint8_t), output_queue_storage, &output_queue_cb);
    if (!input_queue || !output_queue) {
        LOG_ERR("[B] queue alloc FAIL\n");
        vAssertCalled();
    }

    if (!xTaskCreateStatic(bt_task, "bt", BT_TASK_STACK_SIZE, NULL,
                           BT_TASK_PRIORITY, bt_stack, &bt_tcb) ||
        !xTaskCreateStatic(usb_task, "usb", USB_TASK_STACK_SIZE, NULL,
                           USB_TASK_PRIORITY, usb_stack, &usb_tcb) ||
        !xTaskCreateStatic(led_task, "led", LED_TASK_STACK_SIZE, NULL,
                           LED_TASK_PRIORITY, led_stack, &led_tcb))
        vAssertCalled();
    if (audio_ok) audio_start_tasks();
    LOG_INF("[B] init done, starting scheduler\n");
    vTaskStartScheduler();

    vAssertCalled(); /* Scheduler returning means startup failed. */
    return 0;
}

void vApplicationIdleHook(void)
{
    __asm volatile("wfi");
}

/* The SDK defaults abort here, leaving the dongle stuck. Never return into
 * callers that may dereference a failed allocation. These hooks also work
 * before the scheduler starts and with interrupts masked. */
__attribute__((noreturn)) void vAssertCalled(void)
{
    GLB_SW_System_Reset();
    for (;;) {} /* Reset is not expected to return. */
}

void vApplicationMallocFailedHook(void)
{
    vAssertCalled();
}

void vApplicationStackOverflowHook(TaskHandle_t task, char *name)
{
    (void)task;
    (void)name;
    vAssertCalled();
}

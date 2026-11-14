#include "usb_gamepad.h"
#include "usb_audio_control.h"
#include "usb_wake.h"
#include "compiler/compiler_ld.h"
#include "ds5_usb_audio.h"
#include "audio.h"
#include "bt_hid_host.h"
#include "ds5_protocol.h"
#include "dse.h"
#include "config.h"
#include "deferred_store.h"
#include "state_mgr.h"
#include "usbd_core.h"
#include "usbd_hid.h"
#include "bl616_glb.h"
#include "bflb_efuse.h"
#include "bflb_irq.h"
#include "bflb_mtimer.h"
#include "board.h"
#include <string.h>
#include <stdio.h>
#include "board_config.h"
#include "debug_log.h"
#include "FreeRTOS.h"
#include "task.h"
#include "queue.h"

/* Retained for startup identification and firmware-package verification. */
#define FIRMWARE_VERSION "LCT616-DS5 3.18H-HidAudio-R5"

#define USBD_MAX_POWER      250
#define USBD_LANGID         0x0409

/* Config(9) + Audio(186) + Gamepad(9+9+7) = 220 */
#define USB_HID_ONLY_SIZE (9 + 9 + 7)
#define USB_AUDIO_DESC_SIZE 186
#define USB_HID_CONFIG_SIZE (9 + USB_AUDIO_DESC_SIZE + USB_HID_ONLY_SIZE)
#define HID_REPORT_DESC_SIZE_DS  305
#define HID_REPORT_DESC_SIZE_DSE 421

static bool current_dse_mode = false;
static bool requested_dse_mode, usb_initialized;
static bool usb_profile_pending;
static uint64_t usb_profile_request_us;
static uint8_t applied_polling_mode;
static void apply_polling_rate(uint8_t mode);
static void apply_usb_profile(void);
static bool usb_profile_matches_config(void);
static void request_usb_profile(void);

static struct usb_audio_control audio_control;

static bool first_usb_send_logged = false;

#define SET_REPORT_MAX_DATA    64
#define SET_REPORT_QUEUE_DEPTH 4

typedef struct {
    uint8_t  report_id;
    uint32_t session;
    uint32_t usb_session; /* Origin only: accepted Edge transactions are durable. */
    uint16_t len;
    bool     is_dse;
    uint8_t  data[SET_REPORT_MAX_DATA];
} set_report_entry_t;

static QueueHandle_t set_report_queue = NULL;
/* CherryUSB's SET callback is void. Propagate rejected critical writes through
 * its class handler so EP0 stalls instead of acknowledging a dropped command. */
static bool feature_set_rejected;
static usbd_request_handler hid_class_handler;
static int gamepad_class_request(uint8_t busid, struct usb_setup_packet *setup,
                                 uint8_t **data, uint32_t *len)
{
    feature_set_rejected = false;
    int result = hid_class_handler(busid, setup, data, len);
    if (feature_set_rejected) return -1;
    if (setup->bRequest == HID_REQUEST_GET_REPORT &&
        (setup->wValue >> 8) == 3 &&
        (dse_is_profile_report(setup->wValue & 0xff) ||
         (setup->wValue & 0xff) >= 0xF6) && *len == 0)
        return -1; /* Unavailable profiles and unsupported private reads stall. */
    return result;
}

/* DualSense USB HID Report Descriptor (305 bytes) */
static const uint8_t hid_report_desc_ds[HID_REPORT_DESC_SIZE_DS] = {
    0x05, 0x01,       /* Usage Page (Generic Desktop Ctrls) */
    0x09, 0x05,       /* Usage (Game Pad) */
    0xA1, 0x01,       /* Collection (Application) */

    /* Input Report 0x01 -------------------------------------------- */
    0x85, 0x01,       /*   Report ID (1) */
    0x09, 0x30,       /*   Usage (X)  — LX */
    0x09, 0x31,       /*   Usage (Y)  — LY */
    0x09, 0x32,       /*   Usage (Z)  — RX */
    0x09, 0x35,       /*   Usage (Rz) — RY */
    0x09, 0x33,       /*   Usage (Rx) — L2 trigger */
    0x09, 0x34,       /*   Usage (Ry) — R2 trigger */
    0x15, 0x00,       /*   Logical Minimum (0) */
    0x26, 0xFF, 0x00, /*   Logical Maximum (255) */
    0x75, 0x08,       /*   Report Size (8) */
    0x95, 0x06,       /*   Report Count (6) */
    0x81, 0x02,       /*   Input (Data,Var,Abs) */
    0x06, 0x00, 0xFF, /*   Usage Page (Vendor 0xFF00) */
    0x09, 0x20,       /*   Usage (0x20) — counter byte */
    0x95, 0x01,       /*   Report Count (1) */
    0x81, 0x02,       /*   Input (Data,Var,Abs) */
    0x05, 0x01,       /*   Usage Page (Generic Desktop Ctrls) */
    0x09, 0x39,       /*   Usage (Hat switch) */
    0x15, 0x00,       /*   Logical Minimum (0) */
    0x25, 0x07,       /*   Logical Maximum (7) */
    0x35, 0x00,       /*   Physical Minimum (0) */
    0x46, 0x3B, 0x01, /*   Physical Maximum (315) */
    0x65, 0x14,       /*   Unit (Degrees) */
    0x75, 0x04,       /*   Report Size (4) */
    0x95, 0x01,       /*   Report Count (1) */
    0x81, 0x42,       /*   Input (Data,Var,Abs,Null) */
    0x65, 0x00,       /*   Unit (None) */
    0x05, 0x09,       /*   Usage Page (Button) */
    0x19, 0x01,       /*   Usage Minimum (0x01) */
    0x29, 0x0F,       /*   Usage Maximum (0x0F) — 15 buttons */
    0x15, 0x00,       /*   Logical Minimum (0) */
    0x25, 0x01,       /*   Logical Maximum (1) */
    0x75, 0x01,       /*   Report Size (1) */
    0x95, 0x0F,       /*   Report Count (15) */
    0x81, 0x02,       /*   Input (Data,Var,Abs) */
    0x06, 0x00, 0xFF, /*   Usage Page (Vendor 0xFF00) */
    0x09, 0x21,       /*   Usage (0x21) — 13 vendor bits (padding+flags) */
    0x95, 0x0D,       /*   Report Count (13) */
    0x81, 0x02,       /*   Input (Data,Var,Abs) */
    0x06, 0x00, 0xFF, /*   Usage Page (Vendor 0xFF00) */
    0x09, 0x22,       /*   Usage (0x22) — sensor/touch/battery blob */
    0x15, 0x00,       /*   Logical Minimum (0) */
    0x26, 0xFF, 0x00, /*   Logical Maximum (255) */
    0x75, 0x08,       /*   Report Size (8) */
    0x95, 0x34,       /*   Report Count (52) */
    0x81, 0x02,       /*   Input (Data,Var,Abs) */

    /* Output Report 0x02 (47 bytes) -------------------------------- */
    0x85, 0x02,       /*   Report ID (2) */
    0x09, 0x23,       /*   Usage (0x23) */
    0x95, 0x2F,       /*   Report Count (47) */
    0x91, 0x02,       /*   Output (Data,Var,Abs) */

    /* Feature Reports ---------------------------------------------- */
    0x85, 0x05,       /*   Report ID (5) — calibration */
    0x09, 0x33,
    0x95, 0x28,       /*   Report Count (40) */
    0xB1, 0x02,
    0x85, 0x08,       /*   Report ID (8) */
    0x09, 0x34,
    0x95, 0x2F,       /*   Report Count (47) */
    0xB1, 0x02,
    0x85, 0x09,       /*   Report ID (9) — pairing info */
    0x09, 0x24,
    0x95, 0x13,       /*   Report Count (19) */
    0xB1, 0x02,
    0x85, 0x0A,       /*   Report ID (10) */
    0x09, 0x25,
    0x95, 0x1A,       /*   Report Count (26) */
    0xB1, 0x02,
    0x85, 0x0B,       /*   Report ID (11) */
    0x09, 0x41,
    0x95, 0x29,       /*   Report Count (41) */
    0xB1, 0x02,
    0x85, 0x0C,       /*   Report ID (12) */
    0x09, 0x42,
    0x95, 0x29,       /*   Report Count (41) */
    0xB1, 0x02,
    0x85, 0x20,       /*   Report ID (32) — firmware info */
    0x09, 0x26,
    0x95, 0x3F,       /*   Report Count (63) */
    0xB1, 0x02,
    0x85, 0x21,       /*   Report ID (33) */
    0x09, 0x27,
    0x95, 0x04,       /*   Report Count (4) */
    0xB1, 0x02,
    0x85, 0x22,       /*   Report ID (34) */
    0x09, 0x40,
    0x95, 0x3F,       /*   Report Count (63) */
    0xB1, 0x02,
    0x85, 0x80,       /*   Report ID (128) */
    0x09, 0x28,
    0x95, 0x3F,
    0xB1, 0x02,
    0x85, 0x81,       /*   Report ID (129) */
    0x09, 0x29,
    0x95, 0x3F,
    0xB1, 0x02,
    0x85, 0x82,       /*   Report ID (130) */
    0x09, 0x2A,
    0x95, 0x09,       /*   Report Count (9) */
    0xB1, 0x02,
    0x85, 0x83,       /*   Report ID (131) */
    0x09, 0x2B,
    0x95, 0x3F,
    0xB1, 0x02,
    0x85, 0x84,       /*   Report ID (132) */
    0x09, 0x2C,
    0x95, 0x3F,
    0xB1, 0x02,
    0x85, 0x85,       /*   Report ID (133) */
    0x09, 0x2D,
    0x95, 0x02,       /*   Report Count (2) */
    0xB1, 0x02,
    0x85, 0xA0,       /*   Report ID (160) */
    0x09, 0x2E,
    0x95, 0x01,       /*   Report Count (1) */
    0xB1, 0x02,
    0x85, 0xE0,       /*   Report ID (224) */
    0x09, 0x2F,
    0x95, 0x3F,
    0xB1, 0x02,
    0x85, 0xF0,       /*   Report ID (240) */
    0x09, 0x30,
    0x95, 0x3F,
    0xB1, 0x02,
    0x85, 0xF1,       /*   Report ID (241) */
    0x09, 0x31,
    0x95, 0x3F,
    0xB1, 0x02,
    0x85, 0xF2,       /*   Report ID (242) */
    0x09, 0x32,
    0x95, 0x0F,       /*   Report Count (15) */
    0xB1, 0x02,
    0x85, 0xF4,       /*   Report ID (244) */
    0x09, 0x35,
    0x95, 0x3F,
    0xB1, 0x02,
    0x85, 0xF5,       /*   Report ID (245) */
    0x09, 0x36,
    0x95, 0x03,       /*   Report Count (3) */
    0xB1, 0x02,
    0x85, 0xF6,       /*   Report ID (246) */
    0x09, 0x37,
    0x95, 0x3F,
    0xB1, 0x02,
    0x85, 0xF9,       /*   Report ID (249) */
    0x09, 0x3A,
    0x95, 0x3F,
    0xB1, 0x02,

    0xC0,             /* End Collection */
};

/* DualSense Edge USB HID Report Descriptor (421 bytes)
 * Differences from DS: Output 0x02 count 63 (vs 47), Feature 0xF2 count 52
 * (vs 15), plus DSE-specific Feature Reports 0x60-0x65, 0x68, 0x70-0x7B. */
static const uint8_t hid_report_desc_dse[HID_REPORT_DESC_SIZE_DSE] = {
    0x05, 0x01, 0x09, 0x05, 0xA1, 0x01,
    0x85, 0x01,
    0x09, 0x30, 0x09, 0x31, 0x09, 0x32, 0x09, 0x35, 0x09, 0x33, 0x09, 0x34,
    0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x06, 0x81, 0x02,
    0x06, 0x00, 0xFF, 0x09, 0x20, 0x95, 0x01, 0x81, 0x02,
    0x05, 0x01, 0x09, 0x39,
    0x15, 0x00, 0x25, 0x07, 0x35, 0x00, 0x46, 0x3B, 0x01, 0x65, 0x14,
    0x75, 0x04, 0x95, 0x01, 0x81, 0x42,
    0x65, 0x00, 0x05, 0x09,
    0x19, 0x01, 0x29, 0x0F, 0x15, 0x00, 0x25, 0x01,
    0x75, 0x01, 0x95, 0x0F, 0x81, 0x02,
    0x06, 0x00, 0xFF, 0x09, 0x21, 0x95, 0x0D, 0x81, 0x02,
    0x06, 0x00, 0xFF, 0x09, 0x22,
    0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x34, 0x81, 0x02,
    /* Output Report 0x02 — 63 bytes (DSE) */
    0x85, 0x02, 0x09, 0x23, 0x95, 0x3F, 0x91, 0x02,
    /* Feature Reports (shared with DS) */
    0x85, 0x05, 0x09, 0x33, 0x95, 0x28, 0xB1, 0x02,
    0x85, 0x08, 0x09, 0x34, 0x95, 0x2F, 0xB1, 0x02,
    0x85, 0x09, 0x09, 0x24, 0x95, 0x13, 0xB1, 0x02,
    0x85, 0x0A, 0x09, 0x25, 0x95, 0x1A, 0xB1, 0x02,
    0x85, 0x0B, 0x09, 0x41, 0x95, 0x29, 0xB1, 0x02,
    0x85, 0x0C, 0x09, 0x42, 0x95, 0x29, 0xB1, 0x02,
    0x85, 0x20, 0x09, 0x26, 0x95, 0x3F, 0xB1, 0x02,
    0x85, 0x21, 0x09, 0x27, 0x95, 0x04, 0xB1, 0x02,
    0x85, 0x22, 0x09, 0x40, 0x95, 0x3F, 0xB1, 0x02,
    0x85, 0x80, 0x09, 0x28, 0x95, 0x3F, 0xB1, 0x02,
    0x85, 0x81, 0x09, 0x29, 0x95, 0x3F, 0xB1, 0x02,
    0x85, 0x82, 0x09, 0x2A, 0x95, 0x09, 0xB1, 0x02,
    0x85, 0x83, 0x09, 0x2B, 0x95, 0x3F, 0xB1, 0x02,
    0x85, 0x84, 0x09, 0x2C, 0x95, 0x3F, 0xB1, 0x02,
    0x85, 0x85, 0x09, 0x2D, 0x95, 0x02, 0xB1, 0x02,
    0x85, 0xA0, 0x09, 0x2E, 0x95, 0x01, 0xB1, 0x02,
    0x85, 0xE0, 0x09, 0x2F, 0x95, 0x3F, 0xB1, 0x02,
    0x85, 0xF0, 0x09, 0x30, 0x95, 0x3F, 0xB1, 0x02,
    0x85, 0xF1, 0x09, 0x31, 0x95, 0x3F, 0xB1, 0x02,
    /* Feature 0xF2 — 52 bytes in DSE (vs 15 in DS) */
    0x85, 0xF2, 0x09, 0x32, 0x95, 0x34, 0xB1, 0x02,
    0x85, 0xF4, 0x09, 0x35, 0x95, 0x3F, 0xB1, 0x02,
    0x85, 0xF5, 0x09, 0x36, 0x95, 0x03, 0xB1, 0x02,
    /* DSE-specific Feature Reports */
    0x85, 0x60, 0x09, 0x41, 0x95, 0x3F, 0xB1, 0x02,
    0x85, 0x61, 0x09, 0x42, 0xB1, 0x02,
    0x85, 0x62, 0x09, 0x43, 0xB1, 0x02,
    0x85, 0x63, 0x09, 0x44, 0xB1, 0x02,
    0x85, 0x64, 0x09, 0x45, 0xB1, 0x02,
    0x85, 0x65, 0x09, 0x46, 0xB1, 0x02,
    0x85, 0x68, 0x09, 0x47, 0xB1, 0x02,
    0x85, 0x70, 0x09, 0x48, 0xB1, 0x02,
    0x85, 0x71, 0x09, 0x49, 0xB1, 0x02,
    0x85, 0x72, 0x09, 0x4A, 0xB1, 0x02,
    0x85, 0x73, 0x09, 0x4B, 0xB1, 0x02,
    0x85, 0x74, 0x09, 0x4C, 0xB1, 0x02,
    0x85, 0x75, 0x09, 0x4D, 0xB1, 0x02,
    0x85, 0x76, 0x09, 0x4E, 0xB1, 0x02,
    0x85, 0x77, 0x09, 0x4F, 0xB1, 0x02,
    0x85, 0x78, 0x09, 0x50, 0xB1, 0x02,
    0x85, 0x79, 0x09, 0x51, 0xB1, 0x02,
    0x85, 0x7A, 0x09, 0x52, 0xB1, 0x02,
    0x85, 0x7B, 0x09, 0x53, 0xB1, 0x02,
    /* Private DAUD audio control/status reports (shared with CR). */
    0x85, 0xF6, 0x09, 0x37, 0x95, 0x3F, 0xB1, 0x02,
    0x85, 0xF9, 0x09, 0x3A, 0x95, 0x3F, 0xB1, 0x02,
    0xC0,
};

static uint8_t device_desc[] = {
    0x12,                    /* bLength */
    0x01,                    /* bDescriptorType: Device */
    0x00, 0x02,              /* bcdUSB: 2.00 */
    0x00,                    /* bDeviceClass */
    0x00,                    /* bDeviceSubClass */
    0x00,                    /* bDeviceProtocol */
    0x40,                    /* bMaxPacketSize0 */
    (USB_GAMEPAD_VID & 0xFF), (USB_GAMEPAD_VID >> 8),
    (USB_GAMEPAD_PID & 0xFF), (USB_GAMEPAD_PID >> 8),
    0x00, 0x01,              /* bcdDevice: 1.00 */
    0x01,                    /* iManufacturer */
    0x02,                    /* iProduct */
    0x00,                    /* iSerialNumber: none */
    0x01,                    /* bNumConfigurations */
};

#define DEVICE_DESC_PID_OFF         10   /* idProduct low byte in device_desc */

/* Byte offsets within config_desc for dynamic modification.
 * Audio desc = 186 bytes at offset 9, HID starts at 9+186 = 195.
 * Gamepad intf(9) at 195, HID desc(9) at 204, wDescriptorLength at byte 7 of HID = 211. */
#define AUDIO_DESC_OFFSET           9    /* audio descriptor starts after config header */
#define HID_SECTION_OFFSET          (AUDIO_DESC_OFFSET + USB_AUDIO_DESC_SIZE)  /* 195 */
#define CONFIG_DESC_WDLEN_OFF       (HID_SECTION_OFFSET + 9 + 7)  /* 211: gamepad HID wDescriptorLength */
#define BINTERVAL_GAMEPAD_EP_IN_OFF (HID_SECTION_OFFSET + 9 + 9 + 6) /* 219 */

static uint8_t config_desc[USB_HID_CONFIG_SIZE] = {
    /* Configuration Descriptor (9 bytes) */
    0x09, 0x02,
    USB_HID_CONFIG_SIZE & 0xFF, (USB_HID_CONFIG_SIZE >> 8) & 0xFF,
    0x04,                    /* bNumInterfaces: AC + AS_SPK + AS_MIC + Gamepad */
    0x01,                    /* bConfigurationValue */
    0x00,                    /* iConfiguration */
    0xE0,                    /* bmAttributes: self-powered + remote wakeup */
    USBD_MAX_POWER,          /* bMaxPower: 500mA */

    /* ---- Audio descriptors (194 bytes) injected at init ---- */
    /* Placeholder: filled by memcpy from usb_audio_get_desc() in init */
#define AUDIO_PLACEHOLDER_BYTES 186
    [AUDIO_DESC_OFFSET ... AUDIO_DESC_OFFSET + AUDIO_PLACEHOLDER_BYTES - 1] = 0,

    /* ---- Interface 2: DualSense Gamepad ---- */
    0x09, 0x04,
    USB_INTF_GAMEPAD,        /* bInterfaceNumber: 2 */
    0x00,                    /* bAlternateSetting */
    0x01,                    /* bNumEndpoints: IN only (OUT via EP0 SET_REPORT) */
    0x03,                    /* bInterfaceClass: HID */
    0x00,                    /* bInterfaceSubClass */
    0x00,                    /* bInterfaceProtocol */
    0x00,                    /* iInterface */

    /* HID Descriptor (Gamepad) */
    0x09, 0x21,
    0x11, 0x01,              /* bcdHID: 1.11 */
    0x00,                    /* bCountryCode */
    0x01,                    /* bNumDescriptors */
    0x22,                    /* bDescriptorType: Report */
    HID_REPORT_DESC_SIZE_DS & 0xFF, (HID_REPORT_DESC_SIZE_DS >> 8) & 0xFF,

    /* EP IN (Gamepad) */
    0x07, 0x05,
    USB_GAMEPAD_EP_IN,       /* bEndpointAddress */
    0x03,                    /* bmAttributes: Interrupt */
    USB_GAMEPAD_EP_MPS, 0x00,
    USB_GAMEPAD_INTERVAL_MS,
};

/* ---- Advance-mode descriptor callbacks ---- */

static const uint8_t *desc_device_cb(uint8_t speed)
{
    (void)speed;
    return device_desc;
}

static const uint8_t *desc_config_cb(uint8_t speed)
{
    (void)speed;
    static uint8_t hid_only_desc[9 + USB_HID_ONLY_SIZE];
    if (!audio_control.visible) {
        size_t len = usb_audio_hid_descriptor(config_desc, sizeof(config_desc),
                                               hid_only_desc, sizeof(hid_only_desc));
        return len ? hid_only_desc : NULL;
    }
    return config_desc;
}

static const uint8_t device_qualifier_desc[] = {
    0x0A,        /* bLength */
    0x06,        /* bDescriptorType: Device Qualifier */
    0x00, 0x02,  /* bcdUSB: 2.00 */
    0x00, 0x00, 0x00,
    0x40,        /* bMaxPacketSize0: 64 */
    0x01, 0x00,
};

static const uint8_t *desc_qualifier_cb(uint8_t speed)
{
    (void)speed;
    return device_qualifier_desc;
}

static uint8_t other_speed_desc[USB_HID_CONFIG_SIZE];

/* HS devices still expose the USB other-speed configuration descriptor.
 * This describes the alternate bus speed; it is not an FS firmware variant. */
static const uint8_t *desc_other_speed_cb(uint8_t speed)
{
    (void)speed;
    const uint8_t *selected = desc_config_cb(speed);
    if (!selected) return NULL;
    uint16_t selected_len = selected[2] | ((uint16_t)selected[3] << 8);
    if (selected_len > sizeof(other_speed_desc)) return NULL;
    memset(other_speed_desc, 0, sizeof(other_speed_desc));
    memcpy(other_speed_desc, selected, selected_len);
    other_speed_desc[1] = 0x07;
    uint16_t total = other_speed_desc[2] | (other_speed_desc[3] << 8);
    for (uint16_t i = 0; i + 2 < total && i + 2 < sizeof(other_speed_desc); ) {
        uint8_t len = other_speed_desc[i];
        if (len < 2) break;
        if (other_speed_desc[i + 1] == 0x05 && len >= 7) {
            uint8_t bmAttr = other_speed_desc[i + 3];
            if ((bmAttr & 0x03) == 0x01)
                other_speed_desc[i + 6] = 1;   /* ISO: FS bInterval=1 (1ms) */
            else if ((bmAttr & 0x03) == 0x03) {
                other_speed_desc[i + 6] = (applied_polling_mode == 0 ? 4 : applied_polling_mode == 1 ? 2 : 1);
            }
        }
        i += len;
    }
    return other_speed_desc;
}

static char serial_str[24]; /* "xxxxxxxxxxxx xxxx" max 16 hex + nul */
static bool serial_init = false;

static const char *desc_string_cb(uint8_t speed, uint8_t index)
{
    (void)speed;
    switch (index) {
    case 0: return "\x09\x04";
    case 1: return "Sony Interactive Entertainment";
    case 2: return current_dse_mode
                 ? "DualSense Edge Wireless Controller"
                 : "DualSense Wireless Controller";
    case 3:
        if (!serial_init) {
            uint8_t chipid[8];
            bflb_efuse_get_chipid(chipid);
            snprintf(serial_str, sizeof(serial_str),
                     "%02X%02X%02X%02X%02X%02X%02X%02X",
                     chipid[0], chipid[1], chipid[2], chipid[3],
                     chipid[4], chipid[5], chipid[6], chipid[7]);
            serial_init = true;
            LOG_INF("[USB] Serial: %s\n", serial_str);
        }
        return serial_str;
    default: return NULL;
    }
}

/* BOS descriptor: declare USB 2.0 Extension with LPM NOT supported.
 * Prevents Linux from attempting L1 power-state transitions. */
static const uint8_t bos_desc_raw[] = {
    /* BOS Header */
    0x05,                   /* bLength */
    0x0F,                   /* bDescriptorType: BOS */
    0x0C, 0x00,             /* wTotalLength: 12 */
    0x01,                   /* bNumDeviceCaps: 1 */

    /* USB 2.0 Extension Device Capability */
    0x07,                   /* bLength */
    0x10,                   /* bDescriptorType: DEVICE_CAPABILITY */
    0x02,                   /* bDevCapabilityType: USB 2.0 Extension */
    0x00, 0x00, 0x00, 0x00, /* bmAttributes: bit1=0 → LPM not supported */
};

static const struct usb_bos_descriptor bos_desc = {
    .string     = bos_desc_raw,
    .string_len = sizeof(bos_desc_raw),
};

static const struct usb_descriptor usb_desc = {
    .device_descriptor_callback          = desc_device_cb,
    .config_descriptor_callback          = desc_config_cb,
    .device_quality_descriptor_callback  = desc_qualifier_cb,
    .other_speed_descriptor_callback     = desc_other_speed_cb,
    .string_descriptor_callback          = desc_string_cb,
    .bos_descriptor                      = NULL,
};

/* Gamepad interface / endpoints */
static struct usbd_interface hid_intf;
static struct usbd_endpoint  hid_ep_in;
/* Output reports arrive via EP0 SET_REPORT; no interrupt OUT endpoint. */

/* Only called before enumeration, during reset, or while detached. */

static void apply_audio_profile(void)
{
    hid_intf.intf_num = audio_control.visible ? USB_INTF_GAMEPAD : 0;
    usb_audio_set_visible(audio_control.visible);
}

static volatile bool usb_configured = false;
static volatile uint32_t host_session = 1;
static volatile bool host_session_open;

/* Idempotent across soft detach -> RESET -> DISCONNECTED notifications. */
static void usb_host_session_end(void)
{
    uintptr_t irq = bflb_irq_save();
    if (host_session_open) {
        host_session_open = false;
        usb_configured = false;
        if (++host_session == 0) ++host_session;
        state_mgr_usb_reset();
        usb_audio_stop();
        usb_audio_mic_stop();
        audio_reset_encoder();
    }
    bflb_irq_restore(irq);
}

/* CherryUSB does not emit an event for SET_CONFIGURATION(0). Observe its
 * actual configuration before accepting/submitting host output as well. */
uint32_t usb_gamepad_host_session(void)
{
    uintptr_t irq = bflb_irq_save();
    if (host_session_open && !usb_device_is_configured(0)) usb_host_session_end();
    uint32_t session = host_session;
    bflb_irq_restore(irq);
    return session;
}

bool usb_gamepad_host_active(void)
{
    usb_gamepad_host_session();
    return host_session_open && usb_configured;
}

/* BT presentation policy is independent of the HID-controlled audio profile. */
static volatile bool usb_bt_hidden = false;
static volatile bool usb_reenumerating = false;
static volatile bool ep_in_busy = false;
static usb_gamepad_output_cb_t output_callback = NULL;

/* Suspend/resume hooks for wake module */
static void (*hook_suspend)(void)    = NULL;
static void (*hook_resume)(void)     = NULL;
static void (*hook_configured)(void) = NULL;

static USB_NOCACHE_RAM_SECTION uint8_t usb_in_buf[64];

static volatile uint8_t pending_payload[DS5_USB_INPUT_PAYLOAD_LEN];
/* Valid cache and unsent update are distinct: periodic modes may replay a
 * valid frame, while real-time mode consumes each newly published update. */
static volatile bool    pending_valid = false;
static volatile bool    pending_active = false;
static uint32_t pending_session;
static bool pending_session_checked;

ATTR_TCM_SECTION
static void try_send_pending(void)
{
    /* Called from both the USB task and endpoint ISR. Keep the snapshot,
     * endpoint ownership and dirty flag in one interrupt-safe transaction. */
    uint8_t first_report[10];
    bool log_first = false;
    uintptr_t irq_state = bflb_irq_save();
    if (pending_session_checked &&
        (pending_session != bt_hid_host_session() ||
         bt_hid_host_get_state() == BT_HID_STATE_DISCONNECTING)) {
        pending_valid = pending_active = false;
    }
    bool realtime = config_polling_mode() == 2;
    if (!pending_valid || (realtime && !pending_active) ||
        !usb_configured || ep_in_busy) {
        bflb_irq_restore(irq_state);
        return;
    }
    usb_in_buf[0] = DS5_USB_REPORT_ID_INPUT;
    memcpy(usb_in_buf + 1, (const void *)pending_payload,
           DS5_USB_INPUT_PAYLOAD_LEN);
    ep_in_busy = true;
    int ret = usbd_ep_start_write(0, USB_GAMEPAD_EP_IN, usb_in_buf, 64);
    if (ret < 0) {
        ep_in_busy = false;
        /* Keep this frame pending; the task will retry even without new BT input. */
    } else {
        /* Consume only after the driver accepts it. Real-time completion
         * waits for a new update; periodic modes may reuse the valid cache. */
        pending_active = false;
        if (!first_usb_send_logged) {
            first_usb_send_logged = true;
            memcpy(first_report, usb_in_buf, sizeof(first_report));
            log_first = true;
        }
    }
    bflb_irq_restore(irq_state);
    if (log_first) {
        LOG_INF("[USB] First input: [%02x %02x %02x %02x %02x %02x %02x %02x %02x %02x]\n",
               first_report[0], first_report[1], first_report[2], first_report[3],
               first_report[4], first_report[5], first_report[6], first_report[7],
               first_report[8], first_report[9]);
    }
}

ATTR_TCM_SECTION
static void hid_ep_in_handler(uint8_t busid, uint8_t ep, uint32_t nbytes)
{
    (void)busid; (void)ep; (void)nbytes;
    ep_in_busy = false;
    try_send_pending();
}

static void usbd_event_handler(uint8_t busid, uint8_t event)
{
    switch (event) {
    case USBD_EVENT_CONFIGURED:
        usb_host_session_end(); /* Re-selecting a configuration resets its state too. */
        host_session_open = true;
        usb_audio_control_configured(&audio_control);
        usb_configured = true;
        ep_in_busy = false;
        LOG_INF("[USB-EVT] CONFIGURED — host enumeration complete!\n");
        if (hook_configured)
            hook_configured();
        try_send_pending();
        break;
    case USBD_EVENT_RESET:
        usb_host_session_end();
        usb_audio_control_reset(&audio_control, bflb_mtimer_get_time_us());
        /* A reset alone need not recreate the host's interface objects.
         * Force a real removal before replacing the audio configuration;
         * the task changes descriptors only after the detached interval. */
        if (audio_control.revoke_pending)
            usb_soft_disconnect();
        usb_configured = false;
        ep_in_busy = false;
        pending_valid = false;
        pending_active = false;
        first_usb_send_logged = false;
        usb_audio_stop();
        usb_audio_mic_stop();
        audio_set_mic_active(false);
        audio_reset_encoder();
        LOG_INF("[USB-EVT] RESET — host detected device, bus reset sent\n");
        break;
    case USBD_EVENT_SUSPEND:
        usb_configured = false;
        ep_in_busy = false;
        usb_audio_suspend();
        LOG_INF("[USB-EVT] SUSPEND\n");
        /* Our own detach must not masquerade as a sleeping host, otherwise
         * BT reconnect would refuse to reattach USB indefinitely. */
        if (!usb_bt_hidden && !usb_reenumerating &&
            !audio_control.revoke_pending && hook_suspend)
            hook_suspend();
        break;
    case USBD_EVENT_RESUME:
        usb_configured = true;
        usb_audio_resume();
        ep_in_busy = false;
        LOG_INF("[USB-EVT] RESUME\n");
        if (hook_resume)
            hook_resume();
        try_send_pending();
        break;
    case USBD_EVENT_CONNECTED:
        LOG_INF("[USB-EVT] CONNECTED (VBUS detected)\n");
        break;
    case USBD_EVENT_DISCONNECTED:
        usb_host_session_end();
        usb_audio_control_disconnect(&audio_control);
        apply_audio_profile();
        usb_configured = false;
        ep_in_busy = false;
        pending_valid = false;
        pending_active = false;
        usb_audio_stop();
        usb_audio_mic_stop();
        audio_set_mic_active(false);
        LOG_INF("[USB-EVT] DISCONNECTED (VBUS lost)\n");
        break;
    case USBD_EVENT_SET_REMOTE_WAKEUP:
        LOG_INF("[USB-EVT] SET_REMOTE_WAKEUP\n");
        break;
    case USBD_EVENT_CLR_REMOTE_WAKEUP:
        LOG_INF("[USB-EVT] CLR_REMOTE_WAKEUP\n");
        break;
    default:
        LOG_INF("[USB-EVT] UNKNOWN event=%d\n", event);
        break;
    }
}

static void apply_dse_mode(bool dse)
{
    current_dse_mode = dse;

    uint16_t pid = dse ? DS5_EDGE_PID : DS5_PID;
    device_desc[DEVICE_DESC_PID_OFF]     = pid & 0xFF;
    device_desc[DEVICE_DESC_PID_OFF + 1] = (pid >> 8) & 0xFF;

    uint16_t rdl = dse ? HID_REPORT_DESC_SIZE_DSE : HID_REPORT_DESC_SIZE_DS;
    config_desc[CONFIG_DESC_WDLEN_OFF]     = rdl & 0xFF;
    config_desc[CONFIG_DESC_WDLEN_OFF + 1] = (rdl >> 8) & 0xFF;

    hid_intf.hid_report_descriptor = dse ? hid_report_desc_dse : hid_report_desc_ds;
    hid_intf.hid_report_descriptor_len = rdl;
    LOG_INF("[USB] DSE mode: %s (PID=0x%04X, rdl=%u)\n",
           dse ? "Edge" : "Standard", pid, rdl);
}

static bool usb_profile_matches_config(void)
{
    const struct config_body *cfg = config_get();
    return current_dse_mode == requested_dse_mode &&
           !!(config_desc[7] & 0x20) == !!cfg->enable_wake &&
           device_desc[16] == (cfg->enable_usb_sn ? 3 : 0) &&
           applied_polling_mode == cfg->polling_rate_mode;
}

static void request_usb_profile(void)
{
    if (!usb_profile_pending) usb_profile_request_us = bflb_mtimer_get_time_us();
    usb_profile_pending = true;
}

/* Apply one coherent descriptor set only before startup or while detached. */
static void apply_usb_profile(void)
{
    const struct config_body *cfg = config_get();
    apply_dse_mode(requested_dse_mode);
    uint16_t total = USB_HID_CONFIG_SIZE;
    config_desc[2] = total & 0xff;
    config_desc[3] = total >> 8;
    config_desc[4] = 4;
    config_desc[7] = cfg->enable_wake ? 0xe0 : 0xc0;
    device_desc[16] = cfg->enable_usb_sn ? 3 : 0;
    apply_polling_rate(cfg->polling_rate_mode);
    applied_polling_mode = cfg->polling_rate_mode;
    apply_audio_profile();
}

void usb_gamepad_set_dse_mode(bool dse)
{
    taskENTER_CRITICAL();
    requested_dse_mode = dse;
    if (!usb_initialized) apply_dse_mode(dse);
    else if (current_dse_mode != dse) request_usb_profile();
    taskEXIT_CRITICAL();
}

/* One consumer. Peek keeps failed work ahead of later writes; the USB ISR
 * may append but never removes/reorders it. Bound each task pass. */
void usb_gamepad_process_feature_sets(void)
{
    static uint64_t retry_at;
    set_report_entry_t entry;
    for (unsigned n = 0; n < SET_REPORT_QUEUE_DEPTH && set_report_queue &&
         xQueuePeek(set_report_queue, &entry, 0) == pdTRUE; ++n) {
        uint32_t session = bt_hid_host_session();
        if (entry.session != session || bt_hid_host_get_state() != BT_HID_STATE_CONNECTED) {
            xQueueReceive(set_report_queue, &entry, 0);
            retry_at = 0;
            continue;
        }
        /* USB epoch changes do not revoke an already acknowledged profile
         * transaction. FIFO order also keeps its unlock ahead of later writes. */
        uint64_t now = bflb_mtimer_get_time_us();
        if (now < retry_at) break;
        int ret = bt_hid_host_set_feature_crc_session(entry.report_id, entry.data,
                                                      entry.len, entry.session);
        if (ret) {
            retry_at = now + 10000ULL;
            break;
        }
        xQueueReceive(set_report_queue, &entry, 0);
        retry_at = 0;
        if (entry.is_dse) dse_on_profile_write(entry.report_id, entry.session);
    }
}

ATTR_TCM_SECTION
void usb_gamepad_process_deferred(void)
{
    taskENTER_CRITICAL();
    /* Sample time and ISR-written request state in the same critical section. */
    uint64_t now = bflb_mtimer_get_time_us();
    if (!usb_profile_matches_config()) request_usb_profile();
    else if (usb_profile_pending) {
        /* A second request may restore the already-applied profile after a
         * BT reconnect was held off. Release that gate without resetting USB. */
        usb_profile_pending = false;
        if (!usb_wake_host_suspended()) usb_soft_connect();
    }
    bool profile_change = usb_profile_pending &&
                          !usb_wake_host_suspended() && now >= usb_profile_request_us &&
                          now - usb_profile_request_us >= 100000ULL &&
                          (!audio_control.pending || usb_audio_control_ready(&audio_control, now));
    bool change = (usb_configured || audio_control.revoke_pending) &&
                  usb_audio_control_ready(&audio_control, now);
    bool expired = usb_audio_control_expired(&audio_control, now);
    bool enabled = !expired &&
                   (change ? audio_control.requested : (profile_change && audio_control.visible));
    uint32_t generation = audio_control.generation;
    if (change || expired || profile_change) {
        usb_reenumerating = true;
        usb_soft_disconnect();
        usb_configured = false;
    }
    taskEXIT_CRITICAL();
    if (change || expired || profile_change) {
        usb_audio_stop();
        usb_audio_mic_stop();
        audio_set_mic_active(false);
        audio_reset_encoder();
        vTaskDelay(pdMS_TO_TICKS(250));
        taskENTER_CRITICAL();
        usb_audio_control_apply(&audio_control, enabled, generation,
                                bflb_mtimer_get_time_us());
        apply_usb_profile();
        usb_profile_pending = false;
        ep_in_busy = false;
        pending_valid = false;
        pending_active = false;
        enabled = audio_control.visible;
        usb_reenumerating = false;
        usb_soft_connect();
        taskEXIT_CRITICAL();
        LOG_INF("[USB-AUDIO] Profile applied: audio=%d, BT hidden=%d\n",
                enabled, usb_bt_hidden);
    }

    deferred_store_process();

    usb_audio_poll();

    /* Recover a failed queue attempt even when the controller stops sending. */
    try_send_pending();
}

int usb_gamepad_init(usb_gamepad_output_cb_t output_cb)
{
    printf("[USB] Firmware: %s\n", FIRMWARE_VERSION);
    output_callback = output_cb;
    memset(usb_in_buf, 0, sizeof(usb_in_buf));

    if (!set_report_queue)
        set_report_queue = xQueueCreate(SET_REPORT_QUEUE_DEPTH,
                                        sizeof(set_report_entry_t));

    LOG_INF("[USB-INIT] === USB Initialization Start ===\n");

    /* Inject audio descriptor bytes into config_desc */
    uint16_t audio_len;
    const uint8_t *audio_bytes = usb_audio_get_desc(&audio_len);
    memcpy(&config_desc[AUDIO_DESC_OFFSET], audio_bytes, audio_len);
    LOG_INF("[USB-INIT] Audio desc injected: %u bytes at offset %d\n",
           audio_len, AUDIO_DESC_OFFSET);

    /* Register optional interfaces once; their visibility/IDs are selected
     * atomically with the descriptor profile on each USB re-enumeration. */
    apply_usb_profile();

    usbd_desc_register(0, &usb_desc);
    LOG_INF("[USB-INIT] Descriptors registered (dev=%uB, cfg=%uB)\n",
           (unsigned)sizeof(device_desc), (unsigned)sizeof(config_desc));

    /* Interface 0,1,2: Audio Control + Streaming OUT + Streaming IN */
    usb_audio_register(0);
    LOG_INF("[USB-INIT] Audio interfaces registered\n");

    /* Interface 3: Gamepad — select descriptor based on DSE mode */
    const uint8_t *rd  = current_dse_mode ? hid_report_desc_dse : hid_report_desc_ds;
    uint16_t rd_len    = current_dse_mode ? sizeof(hid_report_desc_dse) : sizeof(hid_report_desc_ds);
    usbd_hid_init_intf(0, &hid_intf, rd, rd_len);
    hid_class_handler = hid_intf.class_interface_handler;
    hid_intf.class_interface_handler = gamepad_class_request;
    usbd_add_interface(0, &hid_intf);
    LOG_INF("[USB-INIT] Gamepad HID intf added (report_desc=%uB, DSE=%d)\n",
           rd_len, current_dse_mode);

    hid_ep_in.ep_addr = USB_GAMEPAD_EP_IN;
    hid_ep_in.ep_cb = hid_ep_in_handler;
    usbd_add_endpoint(0, &hid_ep_in);

    /* Read registers BEFORE usbd_initialize */
    {
        volatile uint32_t *dev_ctl = (volatile uint32_t *)(USB_BASE + 0x100);
        volatile uint32_t *phy_tst = (volatile uint32_t *)(USB_BASE + 0x114);
        LOG_INF("[USB-INIT] PRE-init: DEV_CTL=0x%08lx PHY_TST=0x%08lx\n",
               (unsigned long)*dev_ctl, (unsigned long)*phy_tst);
    }

    LOG_INF("[USB-INIT] Calling usbd_initialize(0, 0x%08lx, handler)...\n",
           (unsigned long)USB_BASE);
    usb_audio_control_disconnect(&audio_control);
    apply_audio_profile();
    usb_initialized = true;
    usbd_initialize(0, USB_BASE, usbd_event_handler);
    LOG_INF("[USB-INIT] usbd_initialize returned\n");

    /* Read registers AFTER usbd_initialize (before fixup) */
    {
        volatile uint32_t *dev_ctl = (volatile uint32_t *)(USB_BASE + 0x100);
        volatile uint32_t *phy_tst = (volatile uint32_t *)(USB_BASE + 0x114);
        LOG_INF("[USB-INIT] POST-init (raw): DEV_CTL=0x%08lx PHY_TST=0x%08lx\n",
               (unsigned long)*dev_ctl, (unsigned long)*phy_tst);
        LOG_INF("[USB-INIT]   GLINT_EN=%d CHIP_EN=%d UNPLUG=%d\n",
               (int)((*dev_ctl >> 2) & 1), (int)((*dev_ctl >> 5) & 1),
               (int)(*phy_tst & 1));
    }

    {
        volatile uint32_t *dev_ctl = (volatile uint32_t *)(USB_BASE + 0x100);
        uint32_t v = *dev_ctl;
        v |= (1 << 2);  /* USB_GLINT_EN_HOV */
        v &= ~(1 << 25); /* Clear USB_LPM_EN — refuse L1 power state */
        v &= ~(1 << 26); /* Clear USB_LPM_ACCEPT — NYET all LPM requests */
        *dev_ctl = v;
    }

    /* Final register state after fixup */
    {
        volatile uint32_t *pds_usb_ctl  = (volatile uint32_t *)(0x2000e000 + 0x500);
        volatile uint32_t *usb_dev_ctl  = (volatile uint32_t *)(USB_BASE + 0x100);
        volatile uint32_t *usb_phy_tst  = (volatile uint32_t *)(USB_BASE + 0x114);
        volatile uint32_t *usb_glb_int  = (volatile uint32_t *)(USB_BASE + 0x0C4);
        volatile uint32_t *usb_otg_csr  = (volatile uint32_t *)(USB_BASE + 0x080);

        LOG_INF("[USB-INIT] POST-fixup final state:\n");
        LOG_INF("[USB-INIT]   PDS_USB_CTL=0x%08lx\n", (unsigned long)*pds_usb_ctl);
        LOG_INF("[USB-INIT]   DEV_CTL=0x%08lx (GLINT_EN=%d CHIP_EN=%d)\n",
               (unsigned long)*usb_dev_ctl,
               (int)((*usb_dev_ctl >> 2) & 1), (int)((*usb_dev_ctl >> 5) & 1));
        LOG_INF("[USB-INIT]   PHY_TST=0x%08lx (UNPLUG=%d) — expect 0\n",
               (unsigned long)*usb_phy_tst, (int)(*usb_phy_tst & 1));
        LOG_INF("[USB-INIT]   GLB_INT=0x%08lx OTG_CSR=0x%08lx\n",
               (unsigned long)*usb_glb_int, (unsigned long)*usb_otg_csr);
    }

    LOG_INF("[USB-INIT] === USB Ready. Waiting for host to send RESET ===\n");
    LOG_INF("[USB-INIT] USB Type-C native — no wiring needed\n");
    return 0;
}

void usb_gamepad_set_suspend_hooks(void (*on_suspend)(void),
                                   void (*on_resume)(void),
                                   void (*on_configured)(void))
{
    hook_suspend    = on_suspend;
    hook_resume     = on_resume;
    hook_configured = on_configured;
}

static void apply_polling_rate(uint8_t mode)
{
    uint8_t interval;
    /* HS: bInterval = 2^(N-1) microframes */
    switch (mode) {
    case 0:  interval = 6; break;  /* HS 4ms = 250Hz */
    case 1:  interval = 5; break;  /* HS 2ms = 500Hz */
    default: interval = 3; break;  /* HS 2^2=4µf  = 0.5ms → 2000Hz, effective ~750Hz */
    }
    config_desc[BINTERVAL_GAMEPAD_EP_IN_OFF]  = interval;
    LOG_INF("[USB] Polling rate: mode=%d bInterval=%d\n", mode, interval);
}

void usb_gamepad_set_polling_rate(uint8_t mode)
{
    taskENTER_CRITICAL();
    if (!usb_initialized) apply_polling_rate(mode);
    else if (applied_polling_mode != mode) request_usb_profile();
    taskEXIT_CRITICAL();
}

ATTR_TCM_SECTION
int usb_gamepad_send_raw_input(const uint8_t *payload)
{
    /* Never let the endpoint ISR read a partly updated pending report.
     * While busy, newer frames replace older unsent ones to avoid a backlog. */
    uintptr_t irq_state = bflb_irq_save();
    pending_session_checked = false; /* explicit neutral/maintenance report */
    memcpy((void *)pending_payload, payload, DS5_USB_INPUT_PAYLOAD_LEN);
    pending_valid = true;
    pending_active = true;
    bflb_irq_restore(irq_state);
    try_send_pending();
    return 0;
}

int usb_gamepad_send_input_session(const uint8_t *payload, uint32_t session)
{
    uintptr_t irq = bflb_irq_save();
    if (session != bt_hid_host_session() ||
        bt_hid_host_get_state() == BT_HID_STATE_DISCONNECTING) {
        bflb_irq_restore(irq);
        return -1;
    }
    memcpy((void *)pending_payload, payload, DS5_USB_INPUT_PAYLOAD_LEN);
    pending_session = session;
    pending_session_checked = true;
    pending_valid = pending_active = true;
    try_send_pending();
    bflb_irq_restore(irq);
    return 0;
}

bool usb_gamepad_is_ready(void)
{
    return usb_configured && !ep_in_busy;
}

/* ---------- HID class request callbacks (Feature Report forwarding) ---------- */

/* +1 for Report ID prefix required by CherryUSB */
static uint8_t feature_resp_buf[FEATURE_DATA_MAX + 1];

/*
 * Static fallback for DualSense feature reports 0x09/0x20/0x05.
 * Used when BT cache is empty (controller not yet connected) so that
 * the Linux hid-playstation driver can probe successfully.
 * Returns true if a fallback was provided.
 */
static bool ds5_feature_fallback(uint8_t report_id, uint8_t *buf, uint32_t *len)
{
    switch (report_id) {

    case DS5_FEATURE_REPORT_PAIRING: {
        /* Report 0x09 — Pairing info (20 bytes)
         * bytes[0]   = report ID
         * bytes[1:6] = controller BT MAC (placeholder)
         * bytes[7:19]= reserved */
        memset(buf, 0, 20);
        buf[0] = 0x09;
        buf[1] = 0x00; buf[2] = 0x05; buf[3] = 0xD5;
        buf[4] = 0xBE; buf[5] = 0x18; buf[6] = 0x61;
        *len = 20;
        return true;
    }

    case DS5_FEATURE_REPORT_FIRMWARE: {
        /* Report 0x20 — Firmware info (64 bytes)
         * bytes[0]     = report ID
         * bytes[24:27] = hw_version  (uint32 LE)
         * bytes[28:31] = fw_version  (uint32 LE)
         * bytes[44:45] = update_version (uint16 LE, >= 0x0215 for vibration_v2) */
        memset(buf, 0, 64);
        buf[0] = 0x20;
        buf[24] = 0x00; buf[25] = 0x02; buf[26] = 0x00; buf[27] = 0x00;
        buf[28] = 0x56; buf[29] = 0x02; buf[30] = 0x00; buf[31] = 0x00;
        buf[44] = 0x21; buf[45] = 0x02;
        *len = 64;
        return true;
    }

    case DS5_FEATURE_REPORT_CALIBRATION: {
        /* Report 0x05 — Calibration data (41 bytes)
         * Nominal calibration until the real controller data is cached.
         * Both gyro speed fields are positive magnitudes in degrees/s.
         * With +/-8192 raw counts at +/-512 degrees/s, the host obtains
         * 16 counts per degree/s (normalized gain: 1024 / 16 = 64).
         * A zero speed sum would silently zero all calibrated gyro axes;
         * denominator-only validation does not reject that case. */
        memset(buf, 0, 41);
        buf[0] = 0x05;
        int16_t cal[17];
        cal[0]  = 0;       /* gyro_pitch_bias */
        cal[1]  = 0;       /* gyro_yaw_bias   */
        cal[2]  = 0;       /* gyro_roll_bias  */
        cal[3]  = 8192;    /* gyro_pitch_plus  */
        cal[4]  = -8192;   /* gyro_pitch_minus */
        cal[5]  = 8192;    /* gyro_yaw_plus    */
        cal[6]  = -8192;   /* gyro_yaw_minus   */
        cal[7]  = 8192;    /* gyro_roll_plus   */
        cal[8]  = -8192;   /* gyro_roll_minus  */
        cal[9]  = 512;     /* gyro_speed_plus  */
        cal[10] = 512;     /* gyro_speed_minus */
        cal[11] = 8192;    /* acc_x_plus   */
        cal[12] = -8192;   /* acc_x_minus  */
        cal[13] = 8192;    /* acc_y_plus   */
        cal[14] = -8192;   /* acc_y_minus  */
        cal[15] = 8192;    /* acc_z_plus   */
        cal[16] = -8192;   /* acc_z_minus  */
        /* The report ID makes these fields unaligned; encode explicitly. */
        for (unsigned i = 0; i < 17; i++) {
            uint16_t value = (uint16_t)cal[i];
            buf[1 + 2 * i] = (uint8_t)value;
            buf[2 + 2 * i] = (uint8_t)(value >> 8);
        }
        *len = 41;
        return true;
    }

    default:
        return false;
    }
}

ATTR_TCM_SECTION
void usbd_hid_get_report(uint8_t busid, uint8_t intf, uint8_t report_id,
                         uint8_t report_type, uint8_t **data, uint32_t *len)
{
    (void)busid;
    (void)intf;

    if (report_type != 0x03) {
        *len = 0;
        return;
    }

    /* Keep CR's DAUD status ABI; legacy web telemetry bytes are reserved. */
    if (report_id >= 0xF6) {
        *len = 0;
        if (report_id == 0xF9) {
            memset(feature_resp_buf, 0, 22);
            feature_resp_buf[0] = 0xF9;
            usb_audio_control_status(&audio_control, feature_resp_buf + 14);
            *data = feature_resp_buf;
            *len = 22;
        }
        return; /* Never forward removed private reports to the controller. */
    }

    /* DSE profile reports (0x70-0x7B): the class wrapper stalls absent
     * snapshots rather than returning a fabricated all-zero profile. */
    if (dse_is_profile_report(report_id) && !dse_profiles_ready()) {
        /* DSE owns paced requests until real responses complete the snapshot. */
        *len = 0;
        return;
    }

    /* DualSense Feature Reports — forwarded to/from BT */
    uint8_t *cached = feature_resp_buf + 1;
    uint16_t cached_len;
    bool has_cache = bt_hid_host_copy_feature(report_id, cached, FEATURE_DATA_MAX, &cached_len);
    /* DS5Dongle returns cached data immediately and refreshes mutable reports
     * in the background. Cache misses also fetch, even if a fallback is used. */
    if (!has_cache || report_id == 0x81 || report_id == 0x63 ||
        report_id == 0x64 || report_id == 0x65 || dse_is_profile_report(report_id))
        bt_hid_host_refresh_feature(report_id);
    if (has_cache) {
        if (cached_len > FEATURE_DATA_MAX)
            cached_len = FEATURE_DATA_MAX;
        feature_resp_buf[0] = report_id;
        /* Snapshot was copied directly into the response buffer. */
        *data = feature_resp_buf;
        *len  = 1 + cached_len;
        LOG_INF("[USB] GET_REPORT(Feature 0x%02x) → %u bytes from cache\n",
               report_id, cached_len);
    } else if (ds5_feature_fallback(report_id, feature_resp_buf, len)) {
        *data = feature_resp_buf;
        LOG_INF("[USB] GET_REPORT(Feature 0x%02x) → %u bytes from fallback\n",
               report_id, *len);
    } else {
        *len = 0;
        LOG_INF("[USB] GET_REPORT(Feature 0x%02x) → not cached, deferred to task\n",
               report_id);
    }
}

ATTR_TCM_SECTION
void usbd_hid_set_report(uint8_t busid, uint8_t intf, uint8_t report_id,
                         uint8_t report_type, uint8_t *report,
                         uint32_t report_len)
{
    (void)busid;
    (void)intf;

    const uint8_t *payload = report;
    uint32_t payload_len = report_len;
    if (report_len > 0 && report[0] == report_id) {
        payload++;
        payload_len--;
    }

    /* Only the DAUD audio command remains in the private HID range. */
    if (report_id >= 0xF6) {
        feature_set_rejected = report_type != 0x03 || report_id != 0xF6 ||
            !usb_audio_control_command(&audio_control, payload, payload_len,
                                       bflb_mtimer_get_time_us());
        return;
    }

    /* Accepted Edge writes/unlock form durable, ordered transactions tied to
     * this controller. Finish them across USB re-enumeration, including the
     * unlock prerequisite; only a Bluetooth session change discards them. */
    if (report_type == 0x03 &&
        (report_id == 0x60 || report_id == 0x61 ||
         report_id == 0x62 || report_id == 0x80)) {
        if (!usb_gamepad_host_active() || !set_report_queue ||
            !payload_len || payload_len > SET_REPORT_MAX_DATA ||
            bt_hid_host_get_state() != BT_HID_STATE_CONNECTED) {
            feature_set_rejected = true;
            return;
        }
        set_report_entry_t entry;
        entry.report_id = report_id;
        entry.session = bt_hid_host_session();
        entry.usb_session = usb_gamepad_host_session();
        entry.is_dse    = true;
        entry.len = payload_len;
        memcpy(entry.data, payload, entry.len);
        BaseType_t ok = xQueueSendFromISR(set_report_queue, &entry, NULL);
        feature_set_rejected = ok != pdTRUE;
        LOG_ISR("[USB-ISR] SET_REPORT(DSE 0x%02x) queued=%d len=%lu\n",
               report_id, (int)(ok == pdTRUE), (unsigned long)payload_len);
        return;
    }

    /* DS5Dongle only forwards DSE profile reports (0x60-0x62, 0x80) to BT.
     * All other Feature SET_REPORTs (0x08, 0x09 etc.) are dropped — forwarding
     * 0x08 (BT control) would cause the controller to power-off or re-pair. */
    if (report_type == 0x02 && payload_len > 0) {
        if (output_callback)
            output_callback(report, report_len);
    }
}

/* ---- USB soft plug/unplug via PHY UNPLUG bit ---- */

void usb_soft_disconnect(void)
{
    usb_host_session_end();
    usb_wake_note_usb_reconnect();
    volatile uint32_t *phy_tst = (volatile uint32_t *)(USB_BASE + 0x114);
    *phy_tst |= 1u;
    LOG_INF("[USB] Soft disconnect (stealth)\n");
}

void usb_soft_connect(void)
{
    if (usb_bt_hidden || usb_reenumerating || usb_profile_pending || audio_control.revoke_pending)
        return;
    volatile uint32_t *phy_tst = (volatile uint32_t *)(USB_BASE + 0x114);
    *phy_tst &= ~1u;
    LOG_INF("[USB] Soft connect (stealth)\n");
}

bool usb_gamepad_set_bt_visible(bool visible)
{
    taskENTER_CRITICAL();
    bool changed = usb_bt_hidden == visible;
    usb_bt_hidden = !visible;
    if (visible) {
        usb_soft_connect();
    } else {
        usb_soft_disconnect();
        usb_configured = false;
        ep_in_busy = false;
        pending_valid = false;
        pending_active = false;
    }
    taskEXIT_CRITICAL();
    return changed;
}

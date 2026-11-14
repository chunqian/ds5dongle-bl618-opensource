#ifndef BT_HID_HOST_H
#define BT_HID_HOST_H

#include <stdint.h>
#include <stdbool.h>

#define HID_PSM_CONTROL    0x0011
#define HID_PSM_INTERRUPT  0x0013

#define HID_TRANS_HANDSHAKE    0x0
#define HID_TRANS_HID_CONTROL  0x1
#define HID_TRANS_GET_REPORT   0x4
#define HID_TRANS_SET_REPORT   0x5
#define HID_TRANS_GET_PROTOCOL 0x6
#define HID_TRANS_SET_PROTOCOL 0x7
#define HID_TRANS_DATA         0xA
#define HID_TRANS_DATC         0xB

#define HID_REPORT_INPUT   0x01
#define HID_REPORT_OUTPUT  0x02
#define HID_REPORT_FEATURE 0x03

#define HID_HANDSHAKE_OK            0x00
#define HID_HANDSHAKE_NOT_READY     0x01
#define HID_HANDSHAKE_INVALID_ID    0x02
#define HID_HANDSHAKE_UNSUPPORTED   0x03
#define HID_HANDSHAKE_INVALID_PARAM 0x04
#define HID_HANDSHAKE_ERR_UNKNOWN   0x0E
#define HID_HANDSHAKE_ERR_FATAL     0x0F

#define HID_PROTOCOL_BOOT   0x00
#define HID_PROTOCOL_REPORT 0x01

#define HID_HEADER(trans, param) (((trans) << 4) | ((param) & 0x0F))

enum bt_hid_host_state {
    BT_HID_STATE_IDLE,
    BT_HID_STATE_SCANNING,
    BT_HID_STATE_CONNECTING,
    BT_HID_STATE_SDP_QUERY,
    BT_HID_STATE_L2CAP_CONTROL,
    BT_HID_STATE_L2CAP_INTERRUPT,
    BT_HID_STATE_HANDSHAKE,
    BT_HID_STATE_CONNECTED,
    BT_HID_STATE_DISCONNECTING,
};

typedef void (*bt_hid_input_cb_t)(const uint8_t *data, uint16_t len);
typedef void (*bt_hid_state_cb_t)(enum bt_hid_host_state state);
/* Called once in the BT task after the initial GET 09/20/22/05/70 batch.
 * Initialize state and send 0x32 here, before type detection is finalized,
 * matching DS5Dongle's init_feature() -> state_init() -> bt_write(0x32). */
typedef int (*bt_hid_primer_cb_t)(uint32_t session);

struct bt_hid_host_config {
    bt_hid_input_cb_t  input_cb;
    bt_hid_state_cb_t  state_cb;
    bt_hid_primer_cb_t primer_cb;
    uint16_t           target_vid;
    uint16_t           target_pid;
};

/**
 * Initialize BT HID Host subsystem.
 * @param config  callbacks and target device filters
 * @return 0 on success
 */
int bt_hid_host_init(const struct bt_hid_host_config *config);

/**
 * Start scanning for BT Classic HID devices.
 * @param inquiry_length  HCI units of 1.28 seconds, 1..0x30;
 *                        0 uses DS5Dongle's default of 30 units (38.4 seconds).
 * @return 0 on success
 */
int bt_hid_host_scan_start(uint8_t inquiry_length);

/**
 * Stop scanning.
 */
void bt_hid_host_scan_stop(void);

/**
 * Connect to a discovered device by address.
 * @param addr  6-byte Bluetooth address
 * @return 0 on success
 */
int bt_hid_host_connect(const uint8_t *addr);

/**
 * Request disconnect; retain the connection until real teardown completes.
 */
void bt_hid_host_disconnect(void);
void bt_hid_host_poll_control(void);

/**
 * Finalize cleanup after ACL disconnection and SDK L2CAP release.
 * Call from the BT task. Returns true when a session was cleaned up.
 */
bool bt_hid_host_poll_disconnect(void);

/**
 * Send an output report (e.g. LED, rumble) to the connected device.
 * Sent via HID Interrupt channel as DATA|OUTPUT.
 * @param data  report data including Report ID
 * @param len   data length
 * @return 0 on success
 */
int bt_hid_host_send_output(const uint8_t *data, uint16_t len);
/* Tag work before queueing/encoding; old sessions cannot send to a new peer. */
uint32_t bt_hid_host_session(void);
/* State stays with the caller until accepted; no FIFO backlog of old host effects. */
int bt_hid_host_send_state_session(const uint8_t *data, uint16_t len,
                                   uint32_t session, uint32_t usb_session);
int bt_hid_host_send_output_session(const uint8_t *data, uint16_t len, uint32_t session);
int bt_hid_host_set_feature_crc_session(uint8_t report_id, const uint8_t *data,
                                        uint16_t len, uint32_t session);

/* Optional revocation check immediately before transport submission. */
int bt_hid_host_set_feature_crc_guarded(uint8_t report_id, const uint8_t *data,
        uint16_t len, uint32_t session, bool (*allow)(uint32_t), uint32_t token);

/* Accepted critical output is retained on transient transport failure. */
int bt_hid_host_send_output_reliable_session(const uint8_t *data, uint16_t len, uint32_t session);
int bt_hid_host_set_feature_session(uint8_t report_id, const uint8_t *data, uint16_t len, uint32_t session);
int bt_hid_host_get_feature_session(uint8_t report_id, uint32_t session);

/* Continue queued output after failed submissions; call from the BT task. */
void bt_hid_host_poll_tx(void);

/**
 * Send a SET_REPORT (Feature) on the control channel.
 * @param report_id  HID report ID
 * @param data       report payload (after report ID)
 * @param len        payload length
 * @return 0 on success
 */
int bt_hid_host_set_feature(uint8_t report_id, const uint8_t *data, uint16_t len);

/**
 * Send a GET_REPORT (Feature) on the control channel.
 * Response arrives via the control channel callback.
 * @param report_id  HID report ID to request
 * @return 0 on success
 */
int bt_hid_host_get_feature(uint8_t report_id);

/* Coalesce a host Feature refresh for the BT task; safe from USB callbacks.
 * Pending requests are discarded with the Bluetooth session. */
void bt_hid_host_refresh_feature(uint8_t report_id);

/**
 * Get current connection state.
 */
enum bt_hid_host_state bt_hid_host_get_state(void);

/**
 * Get the address of the connected device.
 * @param addr  output 6-byte buffer
 * @return true if a device is connected
 */
bool bt_hid_host_get_connected_addr(uint8_t *addr);

/**
 * Select the saved BR/EDR peer and enable passive reconnection.
 * The controller initiates ACL and both HID channels; no outgoing paging.
 * @return 0 if a bonded device was found and page scan enabled,
 *         -1 if no bonded device exists
 */
int bt_hid_host_try_reconnect(void);

/**
 * Switch to the next bonded controller. Disconnects current, advances
 * the selected peer, persists its address, and waits for it to reconnect.
 * @return new active_idx on success, -1 if <= 1 bonded controller
 */
int bt_hid_host_switch_next(void);

/**
 * Check if a controller switch is in progress (waiting for old ACL teardown).
 */
bool bt_hid_host_is_switching(void);

/**
 * Get the number of bonded controllers in memory.
 */
uint8_t bt_hid_host_get_bonded_count(void);

/**
 * Get the active controller index (0-based).
 */
uint8_t bt_hid_host_get_active_idx(void);

/**
 * Clear all BR/EDR bonding information and disconnect if connected.
 * After clearing, the device will need to be re-paired via inquiry scan.
 */
void bt_hid_host_clear_bonds(void);

/**
 * Check if the connected controller is a DualSense Edge.
 * Detected by receiving feature report 0x70 from the controller.
 * @return true if Edge controller is connected
 */
bool bt_hid_host_is_dse(void);

/**
 * Copy a coherent cached Feature snapshot; no borrowed storage escapes.
 * @param report_id  HID report ID
 * @param data       caller-owned output buffer (excludes report_id)
 * @param capacity   size of output buffer
 * @param len        output: cached data length
 * @return true if found in cache
 */
bool bt_hid_host_copy_feature(uint8_t report_id, uint8_t *data,
                               uint16_t capacity, uint16_t *len);

/**
 * Send a SET_REPORT (Feature) with BT CRC to the connected controller.
 * @param report_id  HID report ID
 * @param data       report payload (after report ID, without CRC)
 * @param len        payload length (without CRC)
 * @return 0 on success
 */
int bt_hid_host_set_feature_crc(uint8_t report_id, const uint8_t *data,
                                uint16_t len);

/**
 * Persist blacklist to flash if dirty. Call from task context (not ISR/BT
 * callback) to avoid blocking the BT stack during hot paths.
 */
void bt_hid_host_persist_if_dirty(void);

/**
 * Dispatch initialization GETs and retry the type probe. Call from bt_task when state
 * is HANDSHAKE to recover from transient TX pool exhaustion.
 */
void bt_hid_host_handshake_tick(void);

/**
 * Check for and execute a deferred connect (set by discovery callback).
 * Must be called from task context (bt_task main loop), not from BT
 * callbacks, because bt_conn_create_br blocks on HCI cmd_send_sync.
 * @return true if a deferred connect was processed
 */
bool bt_hid_host_poll_connect(void);

/**
 * Poll for a Peripheral-class device. Confirm inquiry cancellation before
 * scheduling a connection; also stop inquiry after an incoming reconnect.
 * Call from task context at ~10 Hz.
 * @return true if early termination triggered
 */
bool bt_hid_host_poll_scan_early(void);

/**
 * Execute deferred security establishment (auth+encrypt).
 * Must be called from task context because bt_conn_set_security uses
 * bt_hci_cmd_send_sync, which may block in a BT callback.
 * @return true if a deferred security request was processed
 */
bool bt_hid_host_poll_security(void);

/**
 * Start deferred L2CAP channel creation (for outgoing connections
 * after security_changed fires). Must be called from task context.
 * @return true if a deferred L2CAP was processed
 */
bool bt_hid_host_poll_sdp(void);

/**
 * Observe actual encryption if the SDK notification was missed.
 * No application deadline or inferred disconnect.
 * @return true if encrypted readiness was recovered
 */
bool bt_hid_host_poll_security_ready(void);

/**
 * Read RSSI from the connected device via HCI.
 * Must be called from task context (uses bt_hci_cmd_send_sync).
 * @return 0 on success, negative on error
 */
int bt_hid_host_read_rssi(int8_t *rssi);

/**
 * Get the last cached RSSI value (safe to call from ISR context).
 * @return last RSSI in dBm, or 0 if not available
 */
int8_t bt_hid_host_get_cached_rssi(void);

#endif /* BT_HID_HOST_H */

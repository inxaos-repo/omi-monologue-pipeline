/*
 * Pure (hardware-free) Omi CV1 fw 3.0.24 protocol logic for the ESP32-S3 bridge.
 * Mirrors src/omi_pipeline/omi_protocol.py; see docs/omi-ble-protocol.md.
 * Everything here is unit-tested on the host (test/test_host.c).
 */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* storage service 30295780-4301-EABD-2904-2849ADFEAE43, chr 30295781 */
#define OMI_RING_CMD_STOP 0x03
#define OMI_RING_CMD_INFO 0x10
#define OMI_RING_CMD_READ 0x11
#define OMI_RING_CMD_CUSTODY 0x14
#define OMI_RING_CMD_ADVANCE_ID 0x15

#define OMI_NOTIFY_ACK 0x01
#define OMI_NOTIFY_INFO 0x02
#define OMI_NOTIFY_DATA 0x03
#define OMI_NOTIFY_DONE 0x04
#define OMI_NOTIFY_BEGIN 0x05
#define OMI_NOTIFY_LIVE_MARK 0x06

#define OMI_ST_OK 0
#define OMI_ST_INVALID 6
#define OMI_ST_NOT_READY 9
#define OMI_ST_SEQ_RANGE 10
#define OMI_ST_ID_MISMATCH 11

#define OMI_CAP_APP_ACK_RECLAIM 0x01
#define OMI_CAP_LIVE_PERSIST 0x02
#define OMI_CAP_ADVANCE_IDEMPOTENT 0x04
#define OMI_CAP_RING_ID 0x08
#define OMI_CUSTODY_VERSION 1

#define OMI_RING_PACKET 444 /* [ts u32 BE][440-byte record] */
#define OMI_RING_RECORD 440
#define OMI_AUDIO_HEADER 3 /* live: [packet_id u16 LE][frag idx u8] */
#define OMI_MAX_FRAME 255

typedef struct {
    uint64_t read_seq, write_seq, dropped, ring_id;
    uint32_t capacity;
    uint16_t packet_bytes;
    uint8_t caps, custody_version;
    bool has_ring_id;
} omi_ring_info_t;

typedef enum {
    OMI_EV_NONE = 0,
    OMI_EV_ACK,       /* status */
    OMI_EV_CUSTODY,   /* granted caps */
    OMI_EV_INFO,
    OMI_EV_DATA,
    OMI_EV_DONE,      /* status, seq */
    OMI_EV_BEGIN,     /* seq=start, count */
    OMI_EV_LIVE_MARK, /* ring_id, seq=ring_seq, live_index */
} omi_ev_type_t;

typedef struct {
    omi_ev_type_t type;
    uint8_t status;
    uint64_t seq, ring_id;
    uint32_t count;
    uint16_t live_index;
    omi_ring_info_t info;
    const uint8_t *data;
    size_t data_len;
} omi_event_t;

/* command encoders; return bytes written (0 if buf too small) */
size_t omi_cmd_info(uint8_t *buf, size_t cap);
size_t omi_cmd_read(uint8_t *buf, size_t cap, uint64_t start, uint32_t count);
size_t omi_cmd_custody(uint8_t *buf, size_t cap, uint8_t caps);
size_t omi_cmd_advance_id(uint8_t *buf, size_t cap, uint64_t ring_id, uint64_t seq);
size_t omi_cmd_stop(uint8_t *buf, size_t cap);

/* parse a storage notification; false if unknown/short */
bool omi_parse_notify(const uint8_t *d, size_t n, omi_event_t *ev);

/* custody ack target: start + whole packets (capped at requested); 0 => nothing to ack */
bool omi_custody_target(uint64_t start, uint32_t packets, uint32_t requested, uint64_t *out);

/* ---- ring packet assembler: DATA byte stream -> 444-byte packets ---- */
typedef struct {
    uint8_t buf[OMI_RING_PACKET];
    size_t have;
    uint32_t packets;
} omi_pkt_asm_t;
typedef void (*omi_pkt_cb)(void *ctx, uint32_t ts, const uint8_t *record /*440*/);
void omi_pkt_asm_reset(omi_pkt_asm_t *a);
void omi_pkt_asm_feed(omi_pkt_asm_t *a, const uint8_t *d, size_t n, omi_pkt_cb cb, void *ctx);

/* ---- live frame assembler (19B10001 notifications) ---- */
typedef struct {
    uint8_t buf[OMI_MAX_FRAME + 64];
    size_t len;
    bool have_last, overflow;
    uint16_t last_id;
    uint8_t last_idx;
    uint16_t frame_last_id; /* packet_id of the last fragment in buf */
    uint32_t frames, dropped;
} omi_live_asm_t;
typedef void (*omi_frame_cb)(void *ctx, const uint8_t *frame, size_t len, uint16_t last_packet_id);
void omi_live_asm_reset(omi_live_asm_t *a);
void omi_live_asm_feed(omi_live_asm_t *a, const uint8_t *d, size_t n, omi_frame_cb cb, void *ctx);
void omi_live_asm_flush(omi_live_asm_t *a, omi_frame_cb cb, void *ctx);

/* u16 wrap-aware a <= b (within half range) */
bool omi_u16_le(uint16_t a, uint16_t b);

/* ---- byte FIFO for buffered upload payloads (caller supplies storage, e.g. PSRAM) ---- */
typedef struct {
    uint8_t *mem;
    size_t cap, head, len; /* head = read position */
    uint64_t dropped_bytes;
} omi_fifo_t;
void omi_fifo_init(omi_fifo_t *f, uint8_t *mem, size_t cap);
size_t omi_fifo_free(const omi_fifo_t *f);
bool omi_fifo_push(omi_fifo_t *f, const uint8_t *d, size_t n); /* all-or-nothing */
size_t omi_fifo_peek(const omi_fifo_t *f, uint8_t *out, size_t n);
void omi_fifo_drop(omi_fifo_t *f, size_t n);

/* lp16 record ([u16 LE len][frame]) used by receiver POST /v1/frames?format=lp16 */
size_t omi_lp16(uint8_t *out, size_t cap, const uint8_t *frame, size_t len);

/* exponential backoff with cap and +-jitter_pct; rnd in [0,1) supplied by caller */
uint32_t omi_backoff_ms(uint32_t attempt, uint32_t base_ms, uint32_t max_ms, uint32_t jitter_pct, double rnd);

/* is http status a success that permits a custody ack? */
static inline bool omi_http_ok(int status) { return status >= 200 && status < 300; }

#ifdef __cplusplus
}
#endif

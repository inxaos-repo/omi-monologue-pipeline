#include "omi_proto.h"

#include <string.h>

static void be64(uint8_t *p, uint64_t v) { for (int i = 7; i >= 0; i--) { p[i] = (uint8_t)v; v >>= 8; } }
static void be32(uint8_t *p, uint32_t v) { for (int i = 3; i >= 0; i--) { p[i] = (uint8_t)v; v >>= 8; } }
static uint64_t rd64(const uint8_t *p) { uint64_t v = 0; for (int i = 0; i < 8; i++) v = (v << 8) | p[i]; return v; }
static uint32_t rd32(const uint8_t *p) { return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3]; }
static uint16_t rd16(const uint8_t *p) { return (uint16_t)((p[0] << 8) | p[1]); }

size_t omi_cmd_info(uint8_t *b, size_t cap) { if (cap < 1) return 0; b[0] = OMI_RING_CMD_INFO; return 1; }
size_t omi_cmd_stop(uint8_t *b, size_t cap) { if (cap < 1) return 0; b[0] = OMI_RING_CMD_STOP; return 1; }

size_t omi_cmd_read(uint8_t *b, size_t cap, uint64_t start, uint32_t count)
{
    if (cap < 13) return 0;
    b[0] = OMI_RING_CMD_READ;
    be64(b + 1, start);
    be32(b + 9, count);
    return 13;
}

size_t omi_cmd_custody(uint8_t *b, size_t cap, uint8_t caps)
{
    if (cap < 3) return 0;
    b[0] = OMI_RING_CMD_CUSTODY;
    b[1] = OMI_CUSTODY_VERSION;
    b[2] = caps;
    return 3;
}

size_t omi_cmd_advance_id(uint8_t *b, size_t cap, uint64_t ring_id, uint64_t seq)
{
    if (cap < 17) return 0;
    b[0] = OMI_RING_CMD_ADVANCE_ID;
    be64(b + 1, ring_id);
    be64(b + 9, seq);
    return 17;
}

bool omi_parse_notify(const uint8_t *d, size_t n, omi_event_t *ev)
{
    memset(ev, 0, sizeof(*ev));
    if (n < 1) return false;
    switch (d[0]) {
    case OMI_NOTIFY_ACK:
        if (n >= 3 && d[1] == OMI_ST_OK) { ev->type = OMI_EV_CUSTODY; ev->status = d[2]; return true; }
        if (n >= 2) { ev->type = OMI_EV_ACK; ev->status = d[1]; return true; }
        return false;
    case OMI_NOTIFY_INFO: {
        if (n < 31) return false;
        omi_ring_info_t *i = &ev->info;
        i->read_seq = rd64(d + 1);
        i->write_seq = rd64(d + 9);
        i->capacity = rd32(d + 17);
        i->dropped = rd64(d + 21);
        i->packet_bytes = rd16(d + 29);
        if (n >= 41) {
            i->caps = d[31];
            i->custody_version = d[32];
            i->ring_id = rd64(d + 33);
            i->has_ring_id = (i->caps & OMI_CAP_RING_ID) != 0;
        }
        ev->type = OMI_EV_INFO;
        return true;
    }
    case OMI_NOTIFY_DATA:
        ev->type = OMI_EV_DATA; ev->data = d + 1; ev->data_len = n - 1; return true;
    case OMI_NOTIFY_DONE:
        if (n < 10) return false;
        ev->type = OMI_EV_DONE; ev->status = d[1]; ev->seq = rd64(d + 2); return true;
    case OMI_NOTIFY_BEGIN:
        if (n < 13) return false;
        ev->type = OMI_EV_BEGIN; ev->seq = rd64(d + 1); ev->count = rd32(d + 9); return true;
    case OMI_NOTIFY_LIVE_MARK:
        if (n < 19) return false;
        ev->type = OMI_EV_LIVE_MARK; ev->ring_id = rd64(d + 1); ev->seq = rd64(d + 9);
        ev->live_index = rd16(d + 17);
        return true;
    default:
        return false;
    }
}

bool omi_custody_target(uint64_t start, uint32_t packets, uint32_t requested, uint64_t *out)
{
    uint32_t n = packets < requested ? packets : requested;
    if (n == 0) return false;
    *out = start + n;
    return true;
}

void omi_pkt_asm_reset(omi_pkt_asm_t *a) { a->have = 0; a->packets = 0; }

void omi_pkt_asm_feed(omi_pkt_asm_t *a, const uint8_t *d, size_t n, omi_pkt_cb cb, void *ctx)
{
    while (n) {
        size_t take = OMI_RING_PACKET - a->have;
        if (take > n) take = n;
        memcpy(a->buf + a->have, d, take);
        a->have += take; d += take; n -= take;
        if (a->have == OMI_RING_PACKET) {
            a->packets++;
            if (cb) cb(ctx, rd32(a->buf), a->buf + 4);
            a->have = 0;
        }
    }
}

void omi_live_asm_reset(omi_live_asm_t *a) { memset(a, 0, sizeof(*a)); }

static void emit(omi_live_asm_t *a, omi_frame_cb cb, void *ctx)
{
    if (a->len && !a->overflow) { a->frames++; if (cb) cb(ctx, a->buf, a->len, a->frame_last_id); }
    else if (a->overflow) a->dropped++;
    a->len = 0; a->overflow = false;
}

void omi_live_asm_feed(omi_live_asm_t *a, const uint8_t *d, size_t n, omi_frame_cb cb, void *ctx)
{
    if (n < OMI_AUDIO_HEADER) return;
    uint16_t id = (uint16_t)(d[0] | (d[1] << 8));
    uint8_t idx = d[2];
    const uint8_t *p = d + OMI_AUDIO_HEADER;
    size_t pl = n - OMI_AUDIO_HEADER;
    bool contiguous = a->have_last && id == (uint16_t)(a->last_id + 1);
    if (idx == 0) {
        emit(a, cb, ctx);
    } else if (!(contiguous && idx == (uint8_t)(a->last_idx + 1) && a->len)) {
        if (a->len) a->dropped++;
        a->len = 0; a->overflow = false;
        a->have_last = true; a->last_id = id; a->last_idx = idx;
        return;
    }
    if (a->len + pl > sizeof(a->buf)) a->overflow = true;
    else { memcpy(a->buf + a->len, p, pl); a->len += pl; }
    a->frame_last_id = id;
    a->have_last = true; a->last_id = id; a->last_idx = idx;
}

void omi_live_asm_flush(omi_live_asm_t *a, omi_frame_cb cb, void *ctx) { emit(a, cb, ctx); }

bool omi_u16_le(uint16_t a, uint16_t b) { return (uint16_t)(b - a) < 0x8000; }

void omi_fifo_init(omi_fifo_t *f, uint8_t *mem, size_t cap) { f->mem = mem; f->cap = cap; f->head = f->len = 0; f->dropped_bytes = 0; }
size_t omi_fifo_free(const omi_fifo_t *f) { return f->cap - f->len; }

bool omi_fifo_push(omi_fifo_t *f, const uint8_t *d, size_t n)
{
    if (n > omi_fifo_free(f)) { f->dropped_bytes += n; return false; }
    size_t tail = (f->head + f->len) % f->cap;
    size_t first = f->cap - tail < n ? f->cap - tail : n;
    memcpy(f->mem + tail, d, first);
    memcpy(f->mem, d + first, n - first);
    f->len += n;
    return true;
}

size_t omi_fifo_peek(const omi_fifo_t *f, uint8_t *out, size_t n)
{
    if (n > f->len) n = f->len;
    size_t first = f->cap - f->head < n ? f->cap - f->head : n;
    memcpy(out, f->mem + f->head, first);
    memcpy(out + first, f->mem, n - first);
    return n;
}

void omi_fifo_drop(omi_fifo_t *f, size_t n)
{
    if (n > f->len) n = f->len;
    f->head = (f->head + n) % (f->cap ? f->cap : 1);
    f->len -= n;
}

size_t omi_lp16(uint8_t *out, size_t cap, const uint8_t *frame, size_t len)
{
    if (len > 0xFFFF || cap < len + 2) return 0;
    out[0] = (uint8_t)len; out[1] = (uint8_t)(len >> 8);
    memcpy(out + 2, frame, len);
    return len + 2;
}

uint32_t omi_backoff_ms(uint32_t attempt, uint32_t base_ms, uint32_t max_ms, uint32_t jitter_pct, double rnd)
{
    uint64_t v = base_ms;
    for (uint32_t i = 0; i < attempt && v < max_ms; i++) v <<= 1;
    if (v > max_ms) v = max_ms;
    double j = ((rnd * 2.0) - 1.0) * (double)jitter_pct / 100.0;
    double r = (double)v * (1.0 + j);
    if (r < 0) r = 0;
    return (uint32_t)r;
}

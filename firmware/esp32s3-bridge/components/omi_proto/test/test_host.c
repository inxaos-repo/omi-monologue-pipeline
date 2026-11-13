/* Host unit tests for omi_proto (no ESP-IDF). Build: make -C test */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "omi_proto.h"

static int frames_seen; static size_t last_len; static uint16_t last_pid;
static void on_frame(void *c, const uint8_t *f, size_t n, uint16_t pid) { (void)c; (void)f; frames_seen++; last_len = n; last_pid = pid; }
static int pkts; static uint32_t last_ts;
static void on_pkt(void *c, uint32_t ts, const uint8_t *r) { (void)c; (void)r; pkts++; last_ts = ts; }

static void put64(uint8_t *p, uint64_t v) { for (int i = 7; i >= 0; i--) { p[i] = (uint8_t)v; v >>= 8; } }

int main(void)
{
    uint8_t b[64];
    assert(omi_cmd_custody(b, sizeof b, 0x0F) == 3 && b[0] == 0x14 && b[1] == 1 && b[2] == 0x0F);
    assert(omi_cmd_advance_id(b, sizeof b, 0x1122334455667788ULL, 42) == 17 && b[0] == 0x15 && b[1] == 0x11 && b[8] == 0x88 && b[16] == 42);
    assert(omi_cmd_advance_id(b, 16, 1, 1) == 0);
    assert(omi_cmd_read(b, sizeof b, 5, 7) == 13 && b[8] == 5 && b[12] == 7);

    omi_event_t ev;
    uint8_t info[41] = {2}; put64(info + 1, 5); put64(info + 9, 9); info[29] = 0x01; info[30] = 0xBC;
    info[31] = 0x0F; info[32] = 1; put64(info + 33, 0xBEEF);
    assert(omi_parse_notify(info, 41, &ev) && ev.type == OMI_EV_INFO && ev.info.ring_id == 0xBEEF && ev.info.has_ring_id && ev.info.packet_bytes == 444 && ev.info.write_seq == 9);
    assert(omi_parse_notify(info, 31, &ev) && !ev.info.has_ring_id);
    uint8_t ack[2] = {1, 11}; assert(omi_parse_notify(ack, 2, &ev) && ev.type == OMI_EV_ACK && ev.status == 11);
    uint8_t cus[3] = {1, 0, 0x0F}; assert(omi_parse_notify(cus, 3, &ev) && ev.type == OMI_EV_CUSTODY && ev.status == 0x0F);
    uint8_t lm[19] = {6}; put64(lm + 1, 7); put64(lm + 9, 1234); lm[17] = 0xFF; lm[18] = 0xFE;
    assert(omi_parse_notify(lm, 19, &ev) && ev.type == OMI_EV_LIVE_MARK && ev.ring_id == 7 && ev.seq == 1234 && ev.live_index == 0xFFFE);
    assert(!omi_parse_notify(lm, 18, &ev));

    uint64_t t;
    assert(!omi_custody_target(100, 0, 10, &t));
    assert(omi_custody_target(100, 3, 10, &t) && t == 103);
    assert(omi_custody_target(100, 50, 10, &t) && t == 110);

    /* packet assembler across odd chunk sizes */
    omi_pkt_asm_t pa; omi_pkt_asm_reset(&pa);
    uint8_t stream[444 * 2 + 10] = {0}; stream[0] = 0x65; stream[444] = 0x66; stream[447] = 1;
    for (size_t o = 0; o < sizeof stream; o += 37) omi_pkt_asm_feed(&pa, stream + o, sizeof stream - o < 37 ? sizeof stream - o : 37, on_pkt, NULL);
    assert(pkts == 2 && last_ts == 0x66000001 && pa.have == 10);

    /* live assembler: frame of 2 fragments, then a gap drops the partial */
    omi_live_asm_t la; omi_live_asm_reset(&la);
    uint8_t f0[13] = {10, 0, 0}, f1[8] = {11, 0, 1}, g0[5] = {12, 0, 0}, g2[5] = {14, 0, 2}, h0[5] = {15, 0, 0};
    omi_live_asm_feed(&la, f0, 13, on_frame, NULL);
    omi_live_asm_feed(&la, f1, 8, on_frame, NULL);
    omi_live_asm_feed(&la, g0, 5, on_frame, NULL);
    assert(frames_seen == 1 && last_len == 15 && last_pid == 11);
    omi_live_asm_feed(&la, g2, 5, on_frame, NULL);
    omi_live_asm_feed(&la, h0, 5, on_frame, NULL);
    assert(frames_seen == 1 && la.dropped == 1);
    omi_live_asm_flush(&la, on_frame, NULL);
    assert(frames_seen == 2 && last_pid == 15);

    assert(omi_u16_le(65530, 3) && !omi_u16_le(3, 65530) && omi_u16_le(5, 5));

    /* fifo wrap */
    uint8_t mem[8], out[8]; omi_fifo_t f; omi_fifo_init(&f, mem, 8);
    assert(omi_fifo_push(&f, (const uint8_t *)"abcde", 5));
    omi_fifo_drop(&f, 4);
    assert(omi_fifo_push(&f, (const uint8_t *)"fghijk", 6));
    assert(!omi_fifo_push(&f, (const uint8_t *)"zz", 2) && f.dropped_bytes == 2);
    assert(omi_fifo_peek(&f, out, 8) == 7 && memcmp(out, "efghijk", 7) == 0);

    assert(omi_lp16(b, sizeof b, (const uint8_t *)"xyz", 3) == 5 && b[0] == 3 && b[1] == 0 && b[2] == 'x');
    assert(omi_backoff_ms(0, 500, 60000, 0, 0.5) == 500);
    assert(omi_backoff_ms(3, 500, 60000, 0, 0.5) == 4000);
    assert(omi_backoff_ms(30, 500, 60000, 0, 0.5) == 60000);
    assert(omi_backoff_ms(1, 1000, 60000, 20, 0.0) == 1600);
    assert(omi_http_ok(201) && !omi_http_ok(401) && !omi_http_ok(500));
    puts("omi_proto host tests OK");
    return 0;
}

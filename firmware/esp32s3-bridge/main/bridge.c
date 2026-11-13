/*
 * Custody state machine (fw 3.0.24). Rule: a RING_ADVANCE_ID is sent ONLY for audio the
 * receiver has answered 2xx for. Everything else (disconnect, WiFi down, HTTP error, buffer
 * overflow, lost BLE fragments) just means the pendant keeps the data and we re-read it on
 * the next connection (at-least-once: duplicates possible, loss not).
 *
 * Tasks:
 *  - bridge task: owns all state and every BLE write; consumes pendant + uploader events.
 *  - uploader task: does the (slow) HTTP POSTs with backoff and reports results back.
 */
#include <stddef.h>
#include <string.h>
#include <sys/time.h>

#include "bridge.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "omi_proto.h"
#include "sdkconfig.h"

static const char *TAG = "bridge";

enum { BEV_BACKLOG_UPLOADED = 100, BEV_LIVE_UPLOADED };
typedef struct {
    int kind; /* pev_kind_t or BEV_* */
    uint32_t gen;
    uint32_t live_pid; /* unwrapped */
    pev_t pev;
} bev_t;

typedef enum { S_IDLE, S_INFO1, S_CUSTODY, S_INFO2, S_READING, S_UPLOADING, S_ADVANCING, S_LIVE } state_t;

/* ---- shared with uploader (guarded by s_mx) ---- */
#define MAX_SEGS 1024
/* one segment: SEGMENT_FRAMES worst-case lp16 records */
#define TMP_CAP ((size_t)CONFIG_OMI_LIVE_SEGMENT_FRAMES * (OMI_MAX_FRAME + 64 + 2))
typedef struct {
    double start;
    size_t bytes;
    uint32_t last_pid; /* unwrapped packet id of last fragment of the last frame */
    uint32_t frames;
} seg_t;
static SemaphoreHandle_t s_mx;
static omi_fifo_t s_live;
static seg_t s_segs[MAX_SEGS];
static uint32_t s_seg_head, s_seg_count; /* last seg (head+count-1) is the open one */
static uint8_t *s_backlog;               /* batch * 440 */
static size_t s_backlog_len;
static double s_backlog_start;
static volatile bool s_backlog_ready;
static volatile uint32_t s_gen;

static QueueHandle_t s_q;
static TaskHandle_t s_uploader;

/* ---- bridge-task-only state ---- */
static state_t s_state = S_IDLE;
static omi_ring_info_t s_info;
static uint64_t s_ring_id, s_backlog_end, s_batch_start, s_acked_seq, s_pending_target;
static uint32_t s_batch_count;
static bool s_backlog_done, s_live_gap, s_live_persist;
static omi_pkt_asm_t s_pa;
static omi_live_asm_t s_la;
static bool s_pid_init;
static uint32_t s_pid_unwrapped, s_live_uploaded_pid;
static bool s_live_uploaded_any;
static TickType_t s_deadline;
typedef struct {
    uint64_t ring_id, seq;
    uint32_t pid;
} mark_t;
#define MAX_MARKS 16
static mark_t s_marks[MAX_MARKS];
static int s_nmarks;

static void *big_alloc(size_t n)
{
    void *p = heap_caps_malloc(n, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    return p ? p : heap_caps_malloc(n, MALLOC_CAP_8BIT);
}

static double now_epoch(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return tv.tv_sec + tv.tv_usec / 1e6;
}

void bridge_post_event(const pev_t *ev)
{
    bev_t b = {.kind = ev->kind, .gen = s_gen};
    memcpy(&b.pev, ev, offsetof(pev_t, data) + ev->len);
    if (xQueueSend(s_q, &b, ev->kind == PEV_AUDIO ? 0 : pdMS_TO_TICKS(50)) != pdTRUE) {
        if (ev->kind == PEV_AUDIO) s_live_gap = true; /* dropped audio: never ack live marks this session */
        else pendant_disconnect();                     /* lost control event: resync from scratch */
    }
}

static void write_cmd(const uint8_t *b, size_t n, state_t next)
{
    if (!pendant_storage_write(b, n)) { ESP_LOGW(TAG, "storage write failed"); pendant_disconnect(); return; }
    s_state = next;
    s_deadline = xTaskGetTickCount() + pdMS_TO_TICKS(next == S_READING ? 30000 : 10000);
}

static void send_info(state_t next) { uint8_t b[1]; write_cmd(b, omi_cmd_info(b, 1), next); }

/* ---------------- live path ---------------- */
static uint32_t unwrap_pid(uint16_t id)
{
    if (!s_pid_init) { s_pid_init = true; s_pid_unwrapped = id; return id; }
    s_pid_unwrapped += (uint16_t)(id - (uint16_t)s_pid_unwrapped);
    return s_pid_unwrapped;
}

static void on_live_frame(void *ctx, const uint8_t *frame, size_t len, uint16_t pid16)
{
    (void)ctx;
    uint8_t rec[OMI_MAX_FRAME + 64 + 2];
    size_t n = omi_lp16(rec, sizeof rec, frame, len);
    uint32_t pid = s_pid_unwrapped - (uint16_t)((uint16_t)s_pid_unwrapped - pid16);
    xSemaphoreTake(s_mx, portMAX_DELAY);
    bool ok = n && omi_fifo_push(&s_live, rec, n);
    if (ok) {
        seg_t *open = s_seg_count ? &s_segs[(s_seg_head + s_seg_count - 1) % MAX_SEGS] : NULL;
        if (!open || open->frames >= CONFIG_OMI_LIVE_SEGMENT_FRAMES) {
            if (s_seg_count == MAX_SEGS) ok = false;
            else {
                open = &s_segs[(s_seg_head + s_seg_count++) % MAX_SEGS];
                *open = (seg_t){.start = now_epoch()};
            }
        }
        if (ok) { open->bytes += n; open->frames++; open->last_pid = pid; }
    }
    xSemaphoreGive(s_mx);
    if (!ok) s_live_gap = true; /* buffer full: never ack marks for this session */
    if (s_uploader) xTaskNotifyGive(s_uploader);
}

static void try_ack_live(void)
{
    if (!s_live_persist || !s_backlog_done || s_live_gap || !s_live_uploaded_any || s_state != S_LIVE) return;
    uint64_t best = 0;
    int keep = 0;
    for (int i = 0; i < s_nmarks; i++) {
        mark_t *m = &s_marks[i];
        if (m->ring_id != s_ring_id) continue; /* stale incarnation */
        if ((int32_t)(m->pid - s_live_uploaded_pid) <= 0) { if (m->seq > best) best = m->seq; }
        else s_marks[keep++] = *m;
    }
    s_nmarks = keep;
    if (best > s_acked_seq) {
        uint8_t b[17];
        s_pending_target = best;
        write_cmd(b, omi_cmd_advance_id(b, sizeof b, s_ring_id, best), S_ADVANCING);
        ESP_LOGI(TAG, "live custody ack -> seq %llu", (unsigned long long)best);
    }
}

/* ---------------- backlog path ---------------- */
static void on_ring_packet(void *ctx, uint32_t ts, const uint8_t *rec)
{
    (void)ctx;
    if (s_pa.packets > s_batch_count) return; /* never exceed requested */
    if (s_pa.packets == 1)
        s_backlog_start = (ts & 0x80000000u) || ts < 1600000000u ? now_epoch() : (double)ts;
    memcpy(s_backlog + s_backlog_len, rec, OMI_RING_RECORD);
    s_backlog_len += OMI_RING_RECORD;
}

static void next_backlog_step(void)
{
    if (s_info.read_seq >= s_backlog_end || s_info.write_seq <= s_info.read_seq) {
        if (!s_backlog_done) ESP_LOGI(TAG, "backlog synced (read_seq=%llu)", (unsigned long long)s_info.read_seq);
        s_backlog_done = true;
        s_state = S_LIVE;
        try_ack_live();
        return;
    }
    uint64_t left = s_backlog_end - s_info.read_seq;
    s_batch_start = s_info.read_seq;
    s_batch_count = left < CONFIG_OMI_BACKLOG_BATCH_PACKETS ? (uint32_t)left : CONFIG_OMI_BACKLOG_BATCH_PACKETS;
    s_backlog_len = 0;
    omi_pkt_asm_reset(&s_pa);
    uint8_t b[13];
    write_cmd(b, omi_cmd_read(b, sizeof b, s_batch_start, s_batch_count), S_READING);
}

static void reset_session(void)
{
    s_state = S_IDLE;
    s_backlog_done = s_live_gap = s_live_uploaded_any = s_pid_init = false;
    s_nmarks = 0;
    s_acked_seq = 0;
    s_backlog_ready = false;
    omi_live_asm_reset(&s_la);
}

static void on_storage(const uint8_t *d, size_t n)
{
    omi_event_t ev;
    if (!omi_parse_notify(d, n, &ev)) return;
    switch (ev.type) {
    case OMI_EV_INFO:
        if (s_state == S_INFO1) {
            s_info = ev.info;
            if (!ev.info.has_ring_id) {
                ESP_LOGW(TAG, "pendant has no custody support (fw < 3.0.24): live only, ring untouched");
                s_state = S_LIVE;
                return;
            }
            uint8_t caps = OMI_CAP_APP_ACK_RECLAIM | OMI_CAP_ADVANCE_IDEMPOTENT | OMI_CAP_RING_ID;
#if CONFIG_OMI_LIVE_PERSIST
            caps |= OMI_CAP_LIVE_PERSIST;
#endif
            uint8_t b[3];
            write_cmd(b, omi_cmd_custody(b, sizeof b, caps), S_CUSTODY);
        } else if (s_state == S_INFO2) {
            s_info = ev.info;
            if (s_ring_id != ev.info.ring_id) { s_ring_id = ev.info.ring_id; s_nmarks = 0; s_backlog_done = false; }
            if (!s_backlog_done) s_backlog_end = ev.info.write_seq; /* snapshot: newer data = live session */
            ESP_LOGI(TAG, "ring id=%016llx read=%llu write=%llu dropped=%llu", (unsigned long long)s_ring_id,
                     (unsigned long long)ev.info.read_seq, (unsigned long long)ev.info.write_seq,
                     (unsigned long long)ev.info.dropped);
            next_backlog_step();
        }
        return;
    case OMI_EV_CUSTODY:
        if (s_state != S_CUSTODY) return;
        s_live_persist = (ev.status & OMI_CAP_LIVE_PERSIST) != 0;
        ESP_LOGI(TAG, "custody granted caps=0x%02x", ev.status);
        send_info(S_INFO2);
        return;
    case OMI_EV_ACK:
        if (s_state == S_CUSTODY && ev.status != OMI_ST_OK) {
            ESP_LOGW(TAG, "custody refused (%u); live only", ev.status);
            s_state = S_LIVE;
        } else if (s_state == S_ADVANCING) {
            if (ev.status == OMI_ST_OK) {
                s_acked_seq = s_pending_target;
                send_info(S_INFO2);
            } else if (ev.status == OMI_ST_ID_MISMATCH) {
                ESP_LOGW(TAG, "ring_id changed on pendant; nothing freed, re-syncing");
                s_backlog_done = false;
                send_info(S_INFO2);
            } else {
                ESP_LOGW(TAG, "advance status %u; retry later", ev.status);
                s_state = s_backlog_done ? S_LIVE : S_IDLE;
                if (!s_backlog_done) pendant_disconnect();
            }
        } else if (s_state == S_READING && ev.status != OMI_ST_OK) {
            ESP_LOGW(TAG, "read refused %u", ev.status);
            pendant_disconnect();
        }
        return;
    case OMI_EV_BEGIN:
        if (s_state == S_READING && ev.seq != s_batch_start) {
            ESP_LOGW(TAG, "READ_BEGIN seq mismatch");
            pendant_disconnect();
        }
        return;
    case OMI_EV_DATA:
        if (s_state == S_READING) omi_pkt_asm_feed(&s_pa, ev.data, ev.data_len, on_ring_packet, NULL);
        return;
    case OMI_EV_DONE: {
        if (s_state != S_READING) return;
        uint64_t target;
        if (!omi_custody_target(s_batch_start, s_pa.packets, s_batch_count, &target)) {
            ESP_LOGW(TAG, "empty batch (done status %u)", ev.status);
            s_backlog_done = true;
            s_state = S_LIVE;
            return;
        }
        s_pending_target = target;
        s_state = S_UPLOADING;
        s_backlog_ready = true;
        xTaskNotifyGive(s_uploader);
        return;
    }
    case OMI_EV_LIVE_MARK:
        if (!s_live_persist) return;
        {
            /* unwrap the u16 live_index relative to the newest packet id we have seen */
            uint32_t pid = s_pid_unwrapped - (uint16_t)((uint16_t)s_pid_unwrapped - ev.live_index);
            mark_t m = {ev.ring_id, ev.seq, pid};
            if (s_nmarks == MAX_MARKS) memmove(s_marks, s_marks + 1, sizeof(mark_t) * --s_nmarks);
            s_marks[s_nmarks++] = m;
        }
        try_ack_live();
        return;
    default:
        return;
    }
}

static void bridge_task(void *arg)
{
    (void)arg;
    bev_t b;
    for (;;) {
        if (!xQueueReceive(s_q, &b, pdMS_TO_TICKS(500))) {
            if (s_state != S_IDLE && s_state != S_LIVE && s_state != S_UPLOADING &&
                (int32_t)(xTaskGetTickCount() - s_deadline) > 0) {
                ESP_LOGW(TAG, "pendant timeout in state %d; reconnecting", s_state);
                pendant_disconnect();
                s_state = S_IDLE;
            }
            continue;
        }
        switch (b.kind) {
        case PEV_CONNECTED:
            reset_session();
            s_gen++;
            send_info(S_INFO1);
            led_set(LED_CONNECTED);
            break;
        case PEV_DISCONNECTED:
            omi_live_asm_flush(&s_la, on_live_frame, NULL);
            reset_session();
            s_gen++;
            led_set(LED_SCANNING);
            break;
        case PEV_STORAGE:
            on_storage(b.pev.data, b.pev.len);
            break;
        case PEV_AUDIO:
            if (b.pev.len >= OMI_AUDIO_HEADER) {
                uint32_t before = s_la.dropped;
                unwrap_pid((uint16_t)(b.pev.data[0] | (b.pev.data[1] << 8)));
                omi_live_asm_feed(&s_la, b.pev.data, b.pev.len, on_live_frame, NULL);
                if (s_la.dropped != before) s_live_gap = true;
            }
            break;
        case BEV_BACKLOG_UPLOADED:
            if (b.gen != s_gen || s_state != S_UPLOADING) break; /* stale: pendant keeps it, re-read later */
            {
                uint8_t c[17];
                write_cmd(c, omi_cmd_advance_id(c, sizeof c, s_ring_id, s_pending_target), S_ADVANCING);
                ESP_LOGI(TAG, "backlog custody ack -> seq %llu", (unsigned long long)s_pending_target);
            }
            break;
        case BEV_LIVE_UPLOADED:
            if (b.gen != s_gen) break;
            s_live_uploaded_pid = b.live_pid;
            s_live_uploaded_any = true;
            try_ack_live();
            break;
        }
    }
}

/* ---------------- uploader ---------------- */
static bool post_retry(const char *fmt, double start, const uint8_t *body, size_t len, uint32_t gen)
{
    for (uint32_t attempt = 0;; attempt++) {
        int st = uplink_post_frames(fmt, start, body, len);
        if (omi_http_ok(st)) { if (attempt) led_set(LED_CONNECTED); return true; }
        led_set(LED_UPLOAD_RETRY);
        if (st == 400 || st == 413) { ESP_LOGE(TAG, "receiver rejected %s (%d); dropping (pendant keeps ring copy)", fmt, st); return false; }
        if (gen != s_gen && body == s_backlog) return false; /* connection changed: batch obsolete */
        vTaskDelay(pdMS_TO_TICKS(omi_backoff_ms(attempt, CONFIG_OMI_BACKOFF_BASE_MS, CONFIG_OMI_BACKOFF_MAX_MS, 20,
                                                (double)esp_random() / 4294967296.0)));
    }
}

static void uploader_task(void *arg)
{
    (void)arg;
    uint8_t *tmp = big_alloc(TMP_CAP);
    for (;;) {
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(2000));
        if (s_backlog_ready) {
            uint32_t gen = s_gen;
            bool ok = post_retry("omi-sd", s_backlog_start, s_backlog, s_backlog_len, gen);
            s_backlog_ready = false;
            if (ok) { bev_t e = {.kind = BEV_BACKLOG_UPLOADED, .gen = gen}; xQueueSend(s_q, &e, portMAX_DELAY); }
        }
        /* upload live segments oldest-first; an idle open segment is closed first */
        for (;;) {
            xSemaphoreTake(s_mx, portMAX_DELAY);
            if (s_seg_count == 1 && s_segs[s_seg_head].frames &&
                now_epoch() - s_segs[s_seg_head].start > CONFIG_OMI_LIVE_SEGMENT_FRAMES * 0.02 + 2)
                s_segs[s_seg_head].frames = CONFIG_OMI_LIVE_SEGMENT_FRAMES; /* close it */
            seg_t *h = &s_segs[s_seg_head];
            bool have = s_seg_count && h->frames >= CONFIG_OMI_LIVE_SEGMENT_FRAMES && tmp && h->bytes <= TMP_CAP;
            seg_t sg = have ? *h : (seg_t){0};
            if (have) omi_fifo_peek(&s_live, tmp, sg.bytes);
            uint32_t gen = s_gen;
            xSemaphoreGive(s_mx);
            if (!have) break;
            bool ok = post_retry("lp16", sg.start, tmp, sg.bytes, gen);
            xSemaphoreTake(s_mx, portMAX_DELAY);
            omi_fifo_drop(&s_live, sg.bytes);
            s_seg_head = (s_seg_head + 1) % MAX_SEGS;
            s_seg_count--;
            xSemaphoreGive(s_mx);
            if (ok) { bev_t e = {.kind = BEV_LIVE_UPLOADED, .gen = gen, .live_pid = sg.last_pid}; xQueueSend(s_q, &e, portMAX_DELAY); }
            else s_live_gap = true;
        }
    }
}

void bridge_start(void)
{
    s_mx = xSemaphoreCreateMutex();
    s_q = xQueueCreate(48, sizeof(bev_t));
    size_t live = (size_t)CONFIG_OMI_LIVE_BUFFER_KB * 1024;
    uint8_t *lm = big_alloc(live);
    if (!lm) { live = 64 * 1024; lm = big_alloc(live); }
    omi_fifo_init(&s_live, lm, live);
    s_backlog = big_alloc((size_t)CONFIG_OMI_BACKLOG_BATCH_PACKETS * OMI_RING_RECORD);
    configASSERT(s_q && lm && s_backlog);
    ESP_LOGI(TAG, "buffers: live %u KiB, backlog %u packets, psram free %u KiB", (unsigned)(live / 1024),
             CONFIG_OMI_BACKLOG_BATCH_PACKETS, (unsigned)(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024));
    xTaskCreatePinnedToCore(uploader_task, "omi_up", 8192, NULL, 4, &s_uploader, 1);
    xTaskCreatePinnedToCore(bridge_task, "omi_bridge", 6144, NULL, 6, NULL, 1);
}

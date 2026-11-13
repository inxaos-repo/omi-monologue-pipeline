/*
 * NimBLE central: scan for the pendant, connect, MTU exchange, discover audio + storage
 * characteristics, subscribe, and forward notifications to bridge.c.
 *
 * Multiple-bridge safety: the pendant firmware is built with CONFIG_BT_MAX_CONN=1, so it
 * stops advertising while any central (phone or another bridge) holds it. A bridge only
 * ever connects to an advertising pendant and never forces a disconnect of someone else.
 * Custody acks are idempotent (stale seq = no-op, stale ring_id = rejected), so even a
 * hand-over between bridges cannot free audio that was not uploaded.
 */
#include <string.h>

#include "bridge.h"
#include "esp_log.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "omi_proto.h"
#include "sdkconfig.h"
#include "services/gap/ble_svc_gap.h"

static const char *TAG = "pendant";

/* 19B10000-E8F2-537E-4F6C-D104768A1214 (little-endian byte order for BLE_UUID128_INIT) */
static const ble_uuid128_t UUID_AUDIO_SVC = BLE_UUID128_INIT(0x14, 0x12, 0x8a, 0x76, 0x04, 0xd1, 0x6c, 0x4f, 0x7e, 0x53,
                                                             0xf2, 0xe8, 0x00, 0x00, 0xb1, 0x19);
static const ble_uuid128_t UUID_AUDIO_DATA = BLE_UUID128_INIT(0x14, 0x12, 0x8a, 0x76, 0x04, 0xd1, 0x6c, 0x4f, 0x7e,
                                                              0x53, 0xf2, 0xe8, 0x01, 0x00, 0xb1, 0x19);
/* 30295780-4301-EABD-2904-2849ADFEAE43 */
static const ble_uuid128_t UUID_STOR_SVC = BLE_UUID128_INIT(0x43, 0xae, 0xfe, 0xad, 0x49, 0x28, 0x04, 0x29, 0xbd, 0xea,
                                                            0x01, 0x43, 0x80, 0x57, 0x29, 0x30);
static const ble_uuid128_t UUID_STOR_DATA = BLE_UUID128_INIT(0x43, 0xae, 0xfe, 0xad, 0x49, 0x28, 0x04, 0x29, 0xbd,
                                                             0xea, 0x01, 0x43, 0x81, 0x57, 0x29, 0x30);

static uint16_t s_conn = BLE_HS_CONN_HANDLE_NONE;
static uint16_t s_mtu = 23;
static uint8_t s_own_addr_type;
static uint32_t s_attempt;

typedef struct {
    uint16_t start, end;
} range_t;
static range_t s_audio_svc, s_stor_svc;
static uint16_t s_audio_val, s_stor_val;
static uint16_t s_audio_cccd, s_stor_cccd;

static void scan_start(void);
static int gap_cb(struct ble_gap_event *ev, void *arg);

uint16_t pendant_mtu(void) { return s_mtu; }

bool pendant_storage_write(const uint8_t *d, size_t n)
{
    if (s_conn == BLE_HS_CONN_HANDLE_NONE || !s_stor_val) return false;
    return ble_gattc_write_flat(s_conn, s_stor_val, d, n, NULL, NULL) == 0;
}

void pendant_disconnect(void)
{
    if (s_conn != BLE_HS_CONN_HANDLE_NONE) ble_gap_terminate(s_conn, BLE_ERR_REM_USER_CONN_TERM);
}

static void fail(const char *what, int rc)
{
    ESP_LOGW(TAG, "%s failed rc=%d; disconnecting", what, rc);
    pendant_disconnect();
}

static void ready(void)
{
    ESP_LOGI(TAG, "pendant ready (mtu=%u)", s_mtu);
    s_attempt = 0;
    pev_t ev = {.kind = PEV_CONNECTED};
    bridge_post_event(&ev);
}

static int sub_cb(uint16_t c, const struct ble_gatt_error *e, struct ble_gatt_attr *a, void *arg);

static void subscribe(uint16_t cccd, uintptr_t step)
{
    static const uint8_t on[2] = {1, 0};
    int rc = ble_gattc_write_flat(s_conn, cccd, on, 2, sub_cb, (void *)step);
    if (rc) fail("subscribe", rc);
}

static int sub_cb(uint16_t c, const struct ble_gatt_error *e, struct ble_gatt_attr *a, void *arg)
{
    (void)c; (void)a;
    if (e->status) { fail("cccd write", e->status); return 0; }
    if ((uintptr_t)arg == 0) subscribe(s_audio_cccd, 1);
    else ready();
    return 0;
}

static int dsc_cb(uint16_t c, const struct ble_gatt_error *e, uint16_t chr_val, const struct ble_gatt_dsc *d, void *arg);

static int dsc_cb(uint16_t c, const struct ble_gatt_error *e, uint16_t chr_val, const struct ble_gatt_dsc *d, void *arg)
{
    (void)c;
    uintptr_t which = (uintptr_t)arg; /* 0 = storage, 1 = audio */
    if (e->status == 0) {
        if (ble_uuid_cmp(&d->uuid.u, BLE_UUID16_DECLARE(BLE_GATT_DSC_CLT_CFG_UUID16)) == 0) {
            if (which == 0) s_stor_cccd = d->handle;
            else s_audio_cccd = d->handle;
        }
        return 0;
    }
    if (e->status != BLE_HS_EDONE) { fail("dsc disc", e->status); return 0; }
    if (which == 0) {
        int rc = ble_gattc_disc_all_dscs(s_conn, s_audio_val, s_audio_svc.end, dsc_cb, (void *)1);
        if (rc) fail("dsc disc audio", rc);
    } else if (!s_stor_cccd || !s_audio_cccd) {
        fail("missing cccd", 0);
    } else {
        subscribe(s_stor_cccd, 0); /* storage first, then audio */
    }
    (void)chr_val;
    return 0;
}

static int chr_cb(uint16_t c, const struct ble_gatt_error *e, const struct ble_gatt_chr *chr, void *arg)
{
    (void)c;
    uintptr_t which = (uintptr_t)arg;
    if (e->status == 0) {
        if (which == 0 && ble_uuid_cmp(&chr->uuid.u, &UUID_STOR_DATA.u) == 0) s_stor_val = chr->val_handle;
        if (which == 1 && ble_uuid_cmp(&chr->uuid.u, &UUID_AUDIO_DATA.u) == 0) s_audio_val = chr->val_handle;
        return 0;
    }
    if (e->status != BLE_HS_EDONE) { fail("chr disc", e->status); return 0; }
    int rc;
    if (which == 0) {
        rc = ble_gattc_disc_all_chrs(s_conn, s_audio_svc.start, s_audio_svc.end, chr_cb, (void *)1);
    } else if (!s_stor_val || !s_audio_val) {
        fail("missing chr", 0);
        return 0;
    } else {
        /* descriptor range: value handle .. service end (good enough: CCCD follows value) */
        rc = ble_gattc_disc_all_dscs(s_conn, s_stor_val, s_stor_svc.end, dsc_cb, (void *)0);
    }
    if (rc) fail("disc", rc);
    return 0;
}

static int svc_cb(uint16_t c, const struct ble_gatt_error *e, const struct ble_gatt_svc *s, void *arg)
{
    (void)c; (void)arg;
    if (e->status == 0) {
        if (ble_uuid_cmp(&s->uuid.u, &UUID_AUDIO_SVC.u) == 0) s_audio_svc = (range_t){s->start_handle, s->end_handle};
        if (ble_uuid_cmp(&s->uuid.u, &UUID_STOR_SVC.u) == 0) s_stor_svc = (range_t){s->start_handle, s->end_handle};
        return 0;
    }
    if (e->status != BLE_HS_EDONE) { fail("svc disc", e->status); return 0; }
    if (!s_audio_svc.start || !s_stor_svc.start) { fail("pendant lacks audio/storage svc", 0); return 0; }
    int rc = ble_gattc_disc_all_chrs(s_conn, s_stor_svc.start, s_stor_svc.end, chr_cb, (void *)0);
    if (rc) fail("chr disc", rc);
    return 0;
}

static int mtu_cb(uint16_t c, const struct ble_gatt_error *e, uint16_t mtu, void *arg)
{
    (void)c; (void)arg;
    if (e->status == 0) s_mtu = mtu;
    if (s_mtu < 47) ESP_LOGW(TAG, "MTU %u < 44+3: RING_INFO may fail", s_mtu);
    int rc = ble_gattc_disc_all_svcs(s_conn, svc_cb, NULL);
    if (rc) fail("svc disc", rc);
    return 0;
}

static bool addr_matches(const ble_addr_t *a)
{
    if (!g_cfg.pendant_addr[0]) return true;
    char s[18];
    snprintf(s, sizeof s, "%02x:%02x:%02x:%02x:%02x:%02x", a->val[5], a->val[4], a->val[3], a->val[2], a->val[1],
             a->val[0]);
    return strcasecmp(s, g_cfg.pendant_addr) == 0;
}

static bool adv_matches(const struct ble_gap_disc_desc *d)
{
    if (!addr_matches(&d->addr)) return false;
    struct ble_hs_adv_fields f;
    if (ble_hs_adv_parse_fields(&f, d->data, d->length_data)) return false;
    if (g_cfg.pendant_addr[0]) return true;
    size_t pl = strlen(g_cfg.pendant_name);
    return f.name && f.name_len >= pl && memcmp(f.name, g_cfg.pendant_name, pl) == 0;
}

static void schedule_rescan(void)
{
    uint32_t ms = omi_backoff_ms(s_attempt++, CONFIG_OMI_BACKOFF_BASE_MS, CONFIG_OMI_BACKOFF_MAX_MS / 4, 30,
                                 (double)esp_random() / 4294967296.0);
    vTaskDelay(pdMS_TO_TICKS(ms)); /* runs in NimBLE host task; short and rare */
    scan_start();
}

static int gap_cb(struct ble_gap_event *ev, void *arg)
{
    (void)arg;
    switch (ev->type) {
    case BLE_GAP_EVENT_DISC:
        if (!adv_matches(&ev->disc)) return 0;
        ble_gap_disc_cancel();
        ESP_LOGI(TAG, "found pendant, connecting");
        if (ble_gap_connect(s_own_addr_type, &ev->disc.addr, 10000, NULL, gap_cb, NULL)) schedule_rescan();
        return 0;
    case BLE_GAP_EVENT_DISC_COMPLETE:
        if (s_conn == BLE_HS_CONN_HANDLE_NONE) scan_start();
        return 0;
    case BLE_GAP_EVENT_CONNECT:
        if (ev->connect.status) { ESP_LOGW(TAG, "connect failed %d", ev->connect.status); schedule_rescan(); return 0; }
        s_conn = ev->connect.conn_handle;
        s_mtu = 23;
        s_audio_svc = s_stor_svc = (range_t){0, 0};
        s_audio_val = s_stor_val = s_audio_cccd = s_stor_cccd = 0;
        if (ble_gattc_exchange_mtu(s_conn, mtu_cb, NULL)) fail("mtu", -1);
        return 0;
    case BLE_GAP_EVENT_DISCONNECT: {
        ESP_LOGW(TAG, "disconnected reason=0x%x", ev->disconnect.reason);
        s_conn = BLE_HS_CONN_HANDLE_NONE;
        pev_t pe = {.kind = PEV_DISCONNECTED};
        bridge_post_event(&pe);
        schedule_rescan();
        return 0;
    }
    case BLE_GAP_EVENT_MTU:
        s_mtu = ev->mtu.value;
        return 0;
    case BLE_GAP_EVENT_NOTIFY_RX: {
        pev_t pe;
        uint16_t h = ev->notify_rx.attr_handle;
        if (h == s_stor_val) pe.kind = PEV_STORAGE;
        else if (h == s_audio_val) pe.kind = PEV_AUDIO;
        else return 0;
        uint16_t n = OS_MBUF_PKTLEN(ev->notify_rx.om);
        if (n > sizeof pe.data) n = sizeof pe.data;
        ble_hs_mbuf_to_flat(ev->notify_rx.om, pe.data, n, &pe.len);
        bridge_post_event(&pe);
        return 0;
    }
    default:
        return 0;
    }
}

static void scan_start(void)
{
    struct ble_gap_disc_params p = {.passive = 0, .filter_duplicates = 1, .itvl = 0x50, .window = 0x30};
    int rc = ble_gap_disc(s_own_addr_type, 30000, &p, gap_cb, NULL);
    if (rc && rc != BLE_HS_EALREADY) ESP_LOGE(TAG, "scan rc=%d", rc);
}

static void on_sync(void)
{
    ble_hs_util_ensure_addr(0);
    ble_hs_id_infer_auto(0, &s_own_addr_type);
    scan_start();
}

static void host_task(void *p)
{
    (void)p;
    nimble_port_run();
    nimble_port_freertos_deinit();
}

void pendant_start(void)
{
    ESP_ERROR_CHECK(nimble_port_init());
    ble_hs_cfg.sync_cb = on_sync;
    ble_svc_gap_device_name_set(g_cfg.bridge_id);
    ble_att_set_preferred_mtu(517);
    nimble_port_freertos_init(host_task);
}

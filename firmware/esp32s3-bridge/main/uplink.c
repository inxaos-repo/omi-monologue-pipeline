/* WiFi STA with reconnect/backoff, SNTP, and the receiver POST. */
#include <stdio.h>
#include <string.h>
#include <sys/time.h>

#include "bridge.h"
#include "esp_event.h"
#include "esp_timer.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_random.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "omi_proto.h"
#include "sdkconfig.h"

static const char *TAG = "uplink";
static EventGroupHandle_t s_ev;
#define ONLINE_BIT BIT0
static uint32_t s_attempt;
static esp_timer_handle_t s_retry;

static void retry_cb(void *arg) { (void)arg; esp_wifi_connect(); }

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg; (void)data;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_ev, ONLINE_BIT);
        uint32_t ms = omi_backoff_ms(s_attempt++, CONFIG_OMI_BACKOFF_BASE_MS, CONFIG_OMI_BACKOFF_MAX_MS, 20,
                                     (double)esp_random() / 4294967296.0);
        ESP_LOGW(TAG, "wifi down, retry in %u ms", (unsigned)ms);
        esp_timer_stop(s_retry);
        esp_timer_start_once(s_retry, (uint64_t)ms * 1000);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        s_attempt = 0;
        xEventGroupSetBits(s_ev, ONLINE_BIT);
        ESP_LOGI(TAG, "online");
    }
}

void uplink_start(void)
{
    s_ev = xEventGroupCreate();
    const esp_timer_create_args_t ta = {.callback = retry_cb, .name = "wifi_retry"};
    ESP_ERROR_CHECK(esp_timer_create(&ta, &s_retry));
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t ic = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&ic));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));
    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, g_cfg.wifi_ssid, sizeof wc.sta.ssid);
    strlcpy((char *)wc.sta.password, g_cfg.wifi_pass, sizeof wc.sta.password);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    /* BLE + WiFi coexist: modem sleep is required with coexistence */
    esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_sntp_config_t sc = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    esp_netif_sntp_init(&sc);
}

bool uplink_wait_online(uint32_t timeout_ms)
{
    return xEventGroupWaitBits(s_ev, ONLINE_BIT, pdFALSE, pdTRUE, pdMS_TO_TICKS(timeout_ms)) & ONLINE_BIT;
}

bool uplink_time_valid(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return tv.tv_sec > 1700000000;
}

int uplink_post_frames(const char *format, double start_epoch, const uint8_t *body, size_t len)
{
    if (!uplink_wait_online(0)) return -1;
    char url[320], auth[150];
    snprintf(url, sizeof url, "%s/v1/frames?device=%s&start=%.3f&format=%s&codec=21", g_cfg.ingest_url,
             g_cfg.device, start_epoch, format);
    snprintf(auth, sizeof auth, "Bearer %s", g_cfg.token);
    esp_http_client_config_t c = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = CONFIG_OMI_HTTP_TIMEOUT_MS,
    };
    esp_http_client_handle_t h = esp_http_client_init(&c);
    if (!h) return -1;
    esp_http_client_set_header(h, "Authorization", auth);
    esp_http_client_set_header(h, "Content-Type", "application/octet-stream");
    esp_http_client_set_header(h, "X-Omi-Bridge", g_cfg.bridge_id);
    esp_http_client_set_post_field(h, (const char *)body, (int)len);
    esp_err_t err = esp_http_client_perform(h);
    int st = err == ESP_OK ? esp_http_client_get_status_code(h) : -1;
    esp_http_client_cleanup(h);
    if (!omi_http_ok(st)) ESP_LOGW(TAG, "POST %s %u B -> %d (%s)", format, (unsigned)len, st, esp_err_to_name(err));
    return st;
}

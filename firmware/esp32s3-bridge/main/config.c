#include <string.h>

#include "bridge.h"
#include "esp_log.h"
#include "nvs.h"
#include "sdkconfig.h"

static const char *TAG = "cfg";
bridge_cfg_t g_cfg;

static void get(nvs_handle_t h, const char *key, char *dst, size_t cap, const char *def)
{
    size_t n = cap;
    if (h && nvs_get_str(h, key, dst, &n) == ESP_OK) return;
    strlcpy(dst, def, cap);
}

void cfg_load(void)
{
    nvs_handle_t h = 0;
    if (nvs_open("omi", NVS_READONLY, &h) != ESP_OK) h = 0;
    get(h, "wifi_ssid", g_cfg.wifi_ssid, sizeof g_cfg.wifi_ssid, CONFIG_OMI_WIFI_SSID);
    get(h, "wifi_pass", g_cfg.wifi_pass, sizeof g_cfg.wifi_pass, CONFIG_OMI_WIFI_PASS);
    get(h, "ingest_url", g_cfg.ingest_url, sizeof g_cfg.ingest_url, CONFIG_OMI_INGEST_URL);
    get(h, "token", g_cfg.token, sizeof g_cfg.token, CONFIG_OMI_INGEST_TOKEN);
    get(h, "device", g_cfg.device, sizeof g_cfg.device, CONFIG_OMI_DEVICE_NAME);
    get(h, "pendant_name", g_cfg.pendant_name, sizeof g_cfg.pendant_name, CONFIG_OMI_PENDANT_NAME);
    get(h, "pendant_addr", g_cfg.pendant_addr, sizeof g_cfg.pendant_addr, CONFIG_OMI_PENDANT_ADDR);
    get(h, "bridge_id", g_cfg.bridge_id, sizeof g_cfg.bridge_id, CONFIG_OMI_BRIDGE_ID);
    if (h) nvs_close(h);
    ESP_LOGI(TAG, "bridge=%s url=%s device=%s pendant=%s/%s token=%s", g_cfg.bridge_id, g_cfg.ingest_url,
             g_cfg.device, g_cfg.pendant_name, g_cfg.pendant_addr[0] ? g_cfg.pendant_addr : "*",
             g_cfg.token[0] ? "set" : "MISSING");
}

#include "bridge.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    ESP_LOGI("main", "board %s", CONFIG_OMI_BOARD_NAME);
    cfg_load();
    uplink_start();
    led_init(); /* polls uplink state: after uplink_start */
    bridge_start();
    pendant_start();
    ESP_LOGI("main", "omi s3 bridge started");
}

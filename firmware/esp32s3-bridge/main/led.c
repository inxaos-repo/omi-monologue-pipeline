/*
 * Status LED. WS2812 (SuperMini GPIO21): red = no WiFi, blue = scanning for pendant,
 * green = pendant connected + uploads OK, amber = upload retrying. Plain GPIO: off/blink/on.
 * CONFIG_OMI_STATUS_LED_GPIO < 0: no LED.
 */
#include "bridge.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

static volatile led_state_t s_state = LED_SCANNING;
void led_set(led_state_t s) { s_state = s; }

#if CONFIG_OMI_STATUS_LED_GPIO >= 0
#if CONFIG_OMI_STATUS_LED_WS2812
#include "led_strip.h"
static led_strip_handle_t s_strip;
static void show(int r, int g, int b)
{
    const int k = CONFIG_OMI_STATUS_LED_BRIGHTNESS;
    led_strip_set_pixel(s_strip, 0, r * k / 255, g * k / 255, b * k / 255);
    led_strip_refresh(s_strip);
}
#endif

static void led_task(void *arg)
{
    int last = -1;
    for (unsigned t = 0;; t++) {
        int s = uplink_wait_online(0) ? (int)s_state : -2;
#if CONFIG_OMI_STATUS_LED_WS2812
        if (s != last) {
            if (s == -2) show(255, 0, 0);
            else if (s == LED_SCANNING) show(0, 0, 255);
            else if (s == LED_CONNECTED) show(0, 255, 0);
            else show(255, 110, 0);
            last = s;
        }
#else
        (void)last;
        int on = s == LED_CONNECTED ? 1 : s == LED_SCANNING ? (t / 10) % 2 : s == -2 ? (t % 20) == 0 : (t / 2) % 2;
        gpio_set_level(CONFIG_OMI_STATUS_LED_GPIO, on);
#endif
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

void led_init(void)
{
#if CONFIG_OMI_STATUS_LED_WS2812
    led_strip_config_t cfg = {
        .strip_gpio_num = CONFIG_OMI_STATUS_LED_GPIO,
        .max_leds = 1,
        .led_pixel_format = LED_PIXEL_FORMAT_GRB,
        .led_model = LED_MODEL_WS2812,
    };
    led_strip_rmt_config_t rmt = { .clk_src = RMT_CLK_SRC_DEFAULT, .resolution_hz = 10 * 1000 * 1000 };
    if (led_strip_new_rmt_device(&cfg, &rmt, &s_strip) != ESP_OK) {
        ESP_LOGE("led", "WS2812 init failed on GPIO%d", CONFIG_OMI_STATUS_LED_GPIO);
        return;
    }
    led_strip_clear(s_strip);
#else
    gpio_reset_pin(CONFIG_OMI_STATUS_LED_GPIO);
    gpio_set_direction(CONFIG_OMI_STATUS_LED_GPIO, GPIO_MODE_OUTPUT);
#endif
    xTaskCreate(led_task, "led", 3072, NULL, 2, NULL);
}
#else
void led_init(void) {}
#endif

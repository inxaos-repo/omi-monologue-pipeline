idf_component_register(SRCS "main.c" "config.c" "uplink.c" "pendant.c" "bridge.c"
                       INCLUDE_DIRS "."
                       REQUIRES omi_proto bt nvs_flash esp_wifi esp_netif esp_event esp_http_client
                                esp_timer esp_psram esp-tls)

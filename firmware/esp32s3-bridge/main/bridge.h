#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* ---- config.c: Kconfig defaults overridden by NVS namespace "omi" ---- */
typedef struct {
    char wifi_ssid[33], wifi_pass[65];
    char ingest_url[160], token[129];
    char device[48], pendant_name[32], pendant_addr[18], bridge_id[48];
} bridge_cfg_t;
extern bridge_cfg_t g_cfg;
void cfg_load(void);

/* ---- uplink.c: WiFi + HTTP ---- */
void uplink_start(void);
bool uplink_wait_online(uint32_t timeout_ms);
/* POST body to <url>/v1/frames?device=&start=&format=&codec=21. Returns HTTP status or -1. */
int uplink_post_frames(const char *format, double start_epoch, const uint8_t *body, size_t len);
bool uplink_time_valid(void);

/* ---- pendant.c: NimBLE central ---- */
typedef enum { PEV_CONNECTED, PEV_DISCONNECTED, PEV_STORAGE, PEV_AUDIO } pev_kind_t;
typedef struct {
    pev_kind_t kind;
    uint16_t len;
    uint8_t data[512];
} pev_t;
void pendant_start(void);
/* write to storage chr 30295781 (with response). false on error / not connected */
bool pendant_storage_write(const uint8_t *d, size_t n);
void pendant_disconnect(void);
uint16_t pendant_mtu(void);

/* ---- bridge.c: custody state machine (consumes pendant events) ---- */
void bridge_start(void);
void bridge_post_event(const pev_t *ev); /* called from NimBLE host task */

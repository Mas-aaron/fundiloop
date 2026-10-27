# ESP32-CAM mDNS setup (CameraWebServer)

Goal: reach the overhead camera at `http://fundiloop.local` instead of a
DHCP address that changes every boot.

## Sketch changes

In your CameraWebServer sketch:

```cpp
#include <ESPmDNS.h>
```

After WiFi connects (after `WiFi.waitForConnectResult()` succeeds):

```cpp
if (!MDNS.begin("fundiloop")) {
  Serial.println("mDNS responder failed");
}
MDNS.addService("http", "tcp", 80);
```

No `MDNS.update()` call is needed in `loop()` on ESP32 (that is ESP8266 only).

## URLs the backend tries, in order

1. `http://fundiloop.local/capture` — still-image endpoint
2. `http://fundiloop.local/snapshot` — alternate still endpoint
3. `http://fundiloop.local:81/stream` — MJPEG stream (first JPEG frame is used)

## Windows note

Windows cannot resolve `.local` hostnames without Bonjour. Install
"Bonjour Print Services" (free from Apple) on the demo laptop, or use the
Settings section on the phone page to enter the camera's raw IP instead —
no server restart needed.

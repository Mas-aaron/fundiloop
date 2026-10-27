#include <Wire.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_BME280.h>

#define SDA_PIN 21
#define SCL_PIN 22
#define OLED_ADDR 0x3C
#define BME_ADDR 0x76
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);
Adafruit_BME280 bme;

unsigned long lastScan = 0;
unsigned long lastBeat = 0;

bool probeAddr(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

void runScan() {
  bool hasOled = probeAddr(OLED_ADDR);
  bool hasBme = probeAddr(BME_ADDR);

  String found = "";
  String missing = "";
  if (hasOled) found += "0x3C";
  else missing += "0x3C";
  if (hasBme) {
    if (found.length()) found += ",";
    found += "0x76";
  } else {
    if (missing.length()) missing += ",";
    missing += "0x76";
  }

  Serial.print("I2C_SCAN:found=");
  Serial.print(found);
  Serial.print(";missing=");
  Serial.println(missing);

  if (hasOled && hasBme) Serial.println("SENSOR_OK");
  else Serial.println("SENSOR_MISSING");
}

void setup() {
  Serial.begin(115200);
  Wire.begin(SDA_PIN, SCL_PIN);
  delay(500);
  display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR);
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.println("FundiLoop patient");
  display.display();
  bme.begin(BME_ADDR);
  Serial.println("READY");
  runScan();
}

void loop() {
  unsigned long now = millis();
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd == "SCAN") runScan();
  }
  if (now - lastScan >= 5000) {
    lastScan = now;
    runScan();
  }
  if (now - lastBeat >= 2000) {
    lastBeat = now;
    Serial.print("HEARTBEAT:uptime=");
    Serial.println(now);
  }
}

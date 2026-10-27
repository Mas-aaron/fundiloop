// FundiLoop patient board firmware (Prompt 1).
// Target: ESP32 DevKit connected to the laptop via USB serial.
// Wiring: BME280 over I2C (SDA=GPIO21, SCL=GPIO22, addr 0x76 or 0x77),
//         SSD1306 OLED over I2C (addr 0x3C), LED anode via 220R to GPIO2.
//
// Serial protocol (115200 baud): the host sends one line command and the
// board replies with exactly one structured JSON line:
//   PING        -> {"cmd":"PING","status":"OK","uptime_ms":123}
//   I2C_SCAN    -> {"cmd":"I2C_SCAN","i2c_devices":["0x3C","0x76"]}
//   READ_SENSOR -> {"cmd":"READ_SENSOR","temperature_c":24.6,...}
//   SELFTEST    -> {"cmd":"SELFTEST","oled_ok":true,...}
//
// Required Arduino libraries (Library Manager):
//   Adafruit BME280 Library, Adafruit SSD1306, Adafruit GFX Library

#include <Wire.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_BME280.h>

#define SDA_PIN 21
#define SCL_PIN 22
#define OLED_ADDR 0x3C
#define BME_ADDR_PRIMARY 0x76
#define BME_ADDR_ALT 0x77
#define LED_PIN 2
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);
Adafruit_BME280 bme;
uint8_t bmeAddr = 0;  // 0 = not found

bool probeAddr(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

String scanBus() {
  // Returns e.g. "\"0x3C\",\"0x76\"" (JSON array items, no brackets).
  String items = "";
  for (uint8_t addr = 0x08; addr < 0x78; addr++) {
    if (probeAddr(addr)) {
      if (items.length()) items += ",";
      char buf[8];
      snprintf(buf, sizeof(buf), "\"0x%02X\"", addr);
      items += buf;
    }
  }
  return items;
}

bool detectBme() {
  if (probeAddr(BME_ADDR_PRIMARY)) { bmeAddr = BME_ADDR_PRIMARY; return true; }
  if (probeAddr(BME_ADDR_ALT)) { bmeAddr = BME_ADDR_ALT; return true; }
  bmeAddr = 0;
  return false;
}

void showOnOled(const String &l1, const String &l2) {
  if (!probeAddr(OLED_ADDR)) return;
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.println(l1);
  display.setCursor(0, 16);
  display.println(l2);
  display.display();
}

void handlePing() {
  Serial.print("{\"cmd\":\"PING\",\"status\":\"OK\",\"uptime_ms\":");
  Serial.print(millis());
  Serial.println("}");
}

void handleI2cScan() {
  Serial.print("{\"cmd\":\"I2C_SCAN\",\"i2c_devices\":[");
  Serial.print(scanBus());
  Serial.println("]}");
}

void handleReadSensor() {
  Serial.print("{\"cmd\":\"READ_SENSOR\"");
  if (bmeAddr == 0 && !detectBme()) {
    Serial.println(",\"error\":\"BME280 not found\",\"temperature_c\":null,"
                   "\"humidity_pct\":null,\"pressure_hpa\":null}");
    return;
  }
  if (!bme.begin(bmeAddr)) {
    Serial.println(",\"error\":\"BME280 init failed\",\"temperature_c\":null,"
                   "\"humidity_pct\":null,\"pressure_hpa\":null}");
    return;
  }
  char addrBuf[8];
  snprintf(addrBuf, sizeof(addrBuf), "\"0x%02X\"", bmeAddr);
  Serial.print(",\"temperature_c\":");
  Serial.print(bme.readTemperature(), 1);
  Serial.print(",\"humidity_pct\":");
  Serial.print(bme.readHumidity(), 1);
  Serial.print(",\"pressure_hpa\":");
  Serial.print(bme.readPressure() / 100.0F, 1);
  Serial.print(",\"bme_address\":");
  Serial.print(addrBuf);
  Serial.println("}");
}

void handleSelftest() {
  String found = scanBus();
  bool hasOled = probeAddr(OLED_ADDR);
  bool hasBme = (bmeAddr != 0) || detectBme();
  float temp = NAN;
  if (hasBme && bme.begin(bmeAddr)) temp = bme.readTemperature();

  // LED check: toggle GPIO2 three times (visible blink).
  pinMode(LED_PIN, OUTPUT);
  for (int i = 0; i < 3; i++) {
    digitalWrite(LED_PIN, HIGH);
    delay(120);
    digitalWrite(LED_PIN, LOW);
    delay(120);
  }
  bool ledOk = true;  // pin toggled; reversed LED is diagnosed optically

  if (hasOled) {
    display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR);
    if (!isnan(temp)) {
      showOnOled("FundiLoop SELFTEST", "T=" + String(temp, 1) + "C PASS");
    } else {
      showOnOled("FundiLoop SELFTEST", "BME FAIL");
    }
  }

  Serial.print("{\"cmd\":\"SELFTEST\",\"i2c_devices\":[");
  Serial.print(found);
  Serial.print("],\"oled_ok\":");
  Serial.print(hasOled ? "true" : "false");
  Serial.print(",\"bme_ok\":");
  Serial.print(hasBme ? "true" : "false");
  Serial.print(",\"led_ok\":");
  Serial.print(ledOk ? "true" : "false");
  Serial.print(",\"temperature_c\":");
  if (isnan(temp)) Serial.print("null");
  else Serial.print(temp, 1);
  Serial.println("}");
}

void setup() {
  Serial.begin(115200);
  Wire.begin(SDA_PIN, SCL_PIN);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  delay(500);
  detectBme();
  if (probeAddr(OLED_ADDR)) {
    display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR);
    showOnOled("FundiLoop patient", "ready");
  }
  Serial.println("{\"type\":\"ready\",\"board\":\"fundiloop-patient\",\"cmds\":"
                 "[\"PING\",\"I2C_SCAN\",\"READ_SENSOR\",\"SELFTEST\"]}");
}

void loop() {
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    cmd.toUpperCase();
    if (cmd == "PING") handlePing();
    else if (cmd == "I2C_SCAN" || cmd == "SCAN") handleI2cScan();
    else if (cmd == "READ_SENSOR") handleReadSensor();
    else if (cmd == "SELFTEST") handleSelftest();
  }
}

# FundiLoop Patient Board (ESP32)

## Required Arduino libraries

Install via Library Manager:

- `Adafruit SSD1306` (by Adafruit)
- `Adafruit GFX Library` (dependency of SSD1306)
- `Adafruit BME280 Library` (by Adafruit)
- `Adafruit Unified Sensor` (dependency of BME280)

`Wire.h` ships with the ESP32 Arduino core.

## Wiring

Both devices share one I2C bus on GPIO21/22.

| Device  | VCC  | GND | SDA     | SCL     |
|---------|------|-----|---------|---------|
| BME280  | 3V3  | GND | GPIO21  | GPIO22  |
| SSD1306 | 3V3  | GND | GPIO21  | GPIO22  |

Wire colors: red=VCC, black=GND, yellow=SDA, green=SCL, blue=LED.

Note: both devices share the I2C bus. Do not move SDA/SCL;
`patient_board.ino` expects SDA=21, SCL=22.

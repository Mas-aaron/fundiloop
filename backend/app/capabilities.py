"""Capability registry for generative discovery (no project catalog).

AVAILABLE_TESTS describes the verification tests the backend can actually
run, so generated plans can be validated step by step. KNOWN_PARTS is the
shared vocabulary the model uses to name parts. SAM_PROMPT_PARTS is the
smaller subset worth a dedicated grounding call (one SAM request each);
everything else Muse names straight from the frame. SENSOR_INTERFACES
tells the planner how each sensor wires and which test can verify it.
"""
from __future__ import annotations

AVAILABLE_TESTS: dict[str, dict] = {
    "RUN_I2C_SCAN": {
        "description": "Scan the I2C bus on the probe station and return found device addresses.",
        "expects": "list of hex addresses like ['0x3C', '0x76']",
        "can_verify": ["device present on bus", "address matches"],
    },
    "READ_SENSOR": {
        "description": "Read one or more sensor values from the I2C sensor.",
        "expects": "at least one non-null reading",
        "can_verify": ["sensor responds", "reading is plausible"],
    },
    "CHECK_OLED": {
        "description": "Send a draw command to the OLED and confirm it acknowledges.",
        "expects": "ack true",
        "can_verify": ["display is wired and responsive"],
    },
    "CHECK_GPIO": {
        "description": "Drive a GPIO pin and read it back via the probe.",
        "expects": "pin state matches",
        "can_verify": ["LED or wire is connected to the correct pin"],
    },
    "CHECK_SERIAL": {
        "description": "Read recent serial output from the patient board.",
        "expects": "non-empty recent lines",
        "can_verify": ["board is running and code is executing"],
    },
}

KNOWN_PARTS: list[str] = [
    "esp32",
    "arduino_uno",
    "arduino_nano",
    "esp8266",
    "pico",
    "bme280",
    "dht22",
    "dht11",
    "ds18b20",
    "lm35",
    "hc_sr04",
    "pir",
    "mq2",
    "ldr",
    "soil_moisture",
    "ssd1306",
    "lcd1602",
    "led",
    "rgb_led",
    "resistor",
    "jumper_wire",
    "button",
    "potentiometer",
    "servo",
    "buzzer",
    "relay",
    "wire",
    "breadboard",
    "multimeter",
]

# Visually groundable subset: one SAM request per entry, esp32 first.
SAM_PROMPT_PARTS: list[str] = [
    "esp32",
    "arduino_uno",
    "bme280",
    "dht22",
    "ssd1306",
    "lcd1602",
    "led",
    "rgb_led",
    "resistor",
    "jumper_wire",
    "button",
    "breadboard",
    "servo",
    "buzzer",
    "hc_sr04",
    "relay",
]

# How each sensor wires and which AVAILABLE_TESTS key can verify it.
SENSOR_INTERFACES: dict[str, dict] = {
    "bme280": {"interface": "i2c", "addresses": ["0x76", "0x77"],
               "verify": "RUN_I2C_SCAN then READ_SENSOR"},
    "ssd1306": {"interface": "i2c", "addresses": ["0x3C"],
                "verify": "CHECK_OLED"},
    "lcd1602": {"interface": "i2c backpack", "addresses": ["0x27"],
                "verify": "RUN_I2C_SCAN then CHECK_SERIAL"},
    "dht22": {"interface": "digital single-wire", "pins": "1 digital pin",
              "verify": "CHECK_SERIAL"},
    "dht11": {"interface": "digital single-wire", "pins": "1 digital pin",
              "verify": "CHECK_SERIAL"},
    "ds18b20": {"interface": "onewire", "pins": "1 digital pin",
                "verify": "CHECK_SERIAL"},
    "hc_sr04": {"interface": "digital trig+echo", "pins": "2 digital pins",
                "verify": "CHECK_GPIO then CHECK_SERIAL"},
    "pir": {"interface": "digital", "pins": "1 digital pin",
            "verify": "CHECK_GPIO then CHECK_SERIAL"},
    "mq2": {"interface": "analog", "pins": "1 analog pin",
            "verify": "CHECK_SERIAL"},
    "ldr": {"interface": "analog", "pins": "1 analog pin",
            "verify": "CHECK_SERIAL"},
    "lm35": {"interface": "analog", "pins": "1 analog pin",
             "verify": "CHECK_SERIAL"},
    "soil_moisture": {"interface": "analog", "pins": "1 analog pin",
                      "verify": "CHECK_SERIAL"},
    "potentiometer": {"interface": "analog", "pins": "1 analog pin",
                      "verify": "CHECK_SERIAL"},
    "button": {"interface": "digital", "pins": "1 digital pin",
               "verify": "CHECK_GPIO then CHECK_SERIAL"},
    "led": {"interface": "digital", "pins": "1 digital pin",
            "verify": "CHECK_GPIO"},
    "rgb_led": {"interface": "3x digital (or PWM)", "pins": "3 digital pins",
                "verify": "CHECK_GPIO"},
    "buzzer": {"interface": "digital", "pins": "1 digital pin",
               "verify": "CHECK_GPIO then CHECK_SERIAL"},
    "servo": {"interface": "PWM digital", "pins": "1 PWM pin",
              "verify": "CHECK_GPIO then CHECK_SERIAL"},
    "relay": {"interface": "digital", "pins": "1 digital pin",
              "verify": "CHECK_GPIO then CHECK_SERIAL"},
}

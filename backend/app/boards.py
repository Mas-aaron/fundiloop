"""Board profiles: pin maps and capabilities per patient board.

Learning mode is board-general: the plan prompt, the verification pin
map, and the generated firmware all build from the session's profile.
The fixed guided/diagnostic flows only fit the ESP32 kit (GUIDED_BOARDS);
other boards use Learning mode.
"""
from __future__ import annotations

BOARD_PROFILES: dict[str, dict] = {
    "esp32_devkit": {
        "label": "ESP32 DevKit",
        "mcu": "ESP32",
        "voltage": "3V3",
        "i2c": {"sda": 21, "scl": 22},
        "led_builtin": 2,
        "gpio": [2, 4, 5, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 23,
                 25, 26, 27, 32, 33],
        "adc": ["VP", "VN", "34", "35", "32", "33", "25", "26", "27",
                "14", "12"],
        "serial_baud": 115200,
        "notes": "3V3 logic. Most breakout sensors work directly, no shifting.",
    },
    "arduino_uno": {
        "label": "Arduino Uno",
        "mcu": "ATmega328P",
        "voltage": "5V",
        "i2c": {"sda": "A4", "scl": "A5"},
        "led_builtin": 13,
        "gpio": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13],
        "adc": ["A0", "A1", "A2", "A3", "A4", "A5"],
        "serial_baud": 9600,
        "notes": ("5V logic; 3V3-only sensors need level shifting. "
                  "Builtin LED on digital pin 13. PWM on 3, 5, 6, 9, 10, 11."),
    },
}

DEFAULT_BOARD = "esp32_devkit"

# Fixed guided/diagnostic flows only fit this kit; every board learns.
GUIDED_BOARDS = ("esp32_devkit",)


def get_board(name: str | None) -> dict:
    """Return the profile for `name` (default ESP32). Raises ValueError."""
    key = (name or "").strip().lower() or DEFAULT_BOARD
    if key not in BOARD_PROFILES:
        raise ValueError("unknown board %r (known: %s)"
                         % (name, ", ".join(sorted(BOARD_PROFILES))))
    return BOARD_PROFILES[key]


def board_key(name: str | None) -> str:
    """Validated board key (default ESP32). Raises ValueError."""
    get_board(name)
    return (name or "").strip().lower() or DEFAULT_BOARD


def pin_map_for(board: str | None) -> dict:
    """Verification pin map for a board: led/sda/scl + identity."""
    key = board_key(board)
    prof = BOARD_PROFILES[key]
    i2c = prof.get("i2c", {})
    return {"board": key, "label": prof.get("label", key),
            "voltage": prof.get("voltage", ""),
            "led_gpio": prof.get("led_builtin", 2),
            "sda": i2c.get("sda"), "scl": i2c.get("scl")}


def describe_board(board: str | None) -> str:
    """One-paragraph profile brief for model prompts."""
    key = board_key(board)
    prof = BOARD_PROFILES[key]
    i2c = prof.get("i2c", {})
    gpio = prof.get("gpio", [])
    return (
        "%s (%s, %s logic): I2C on SDA=%s SCL=%s, builtin LED on %s, "
        "digital pins %s, analog %s, serial %s baud. %s"
        % (prof.get("label", key), prof.get("mcu", "?"),
           prof.get("voltage", "?"), i2c.get("sda"), i2c.get("scl"),
           prof.get("led_builtin"), ", ".join(str(g) for g in gpio),
           ", ".join(str(a) for a in prof.get("adc", [])),
           prof.get("serial_baud"), prof.get("notes", ""))
    )

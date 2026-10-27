"""Circuit graph: ordered assembly steps + fault definitions from YAML."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import AssemblyStep, FaultDefinition

DEFAULT_CIRCUIT_PATH = Path(__file__).resolve().parent.parent / "data" / "circuit_esp32_bme280_oled.yaml"


@dataclass
class CircuitGraph:
    name: str
    steps: list[AssemblyStep] = field(default_factory=list)
    led_gpio: int = 2
    i2c_sda_gpio: int = 21
    i2c_scl_gpio: int = 22
    i2c_expected: dict = field(default_factory=dict)
    faults: dict[str, FaultDefinition] = field(default_factory=dict)

    def get_step(self, index: int) -> AssemblyStep | None:
        return self.steps[index] if 0 <= index < len(self.steps) else None

    @property
    def total_steps(self) -> int:
        return len(self.steps)


def load_circuit_graph(path: str | Path | None = None) -> CircuitGraph:
    path = Path(path) if path else DEFAULT_CIRCUIT_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = [AssemblyStep(**s) for s in data.get("steps", [])]
    faults = {
        fid: FaultDefinition(fault_id=fid, **fdef)
        for fid, fdef in (data.get("faults", {}) or {}).items()
    }
    i2c = data.get("i2c", {}) or {}
    return CircuitGraph(
        name=data.get("name", "circuit"),
        steps=steps,
        led_gpio=int(((data.get("led") or {}).get("gpio", 2))),
        i2c_sda_gpio=int(i2c.get("sda_gpio", 21)),
        i2c_scl_gpio=int(i2c.get("scl_gpio", 22)),
        i2c_expected=i2c.get("expected", {}),
        faults=faults,
    )

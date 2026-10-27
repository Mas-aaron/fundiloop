"""Core data models for FundiLoop.

FundiLoop is a verifiable assembly and diagnostic agent for embedded
hardware: it guides a technician, watches the circuit, and only declares
success when a real electrical verification test passes.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Mode(str, Enum):
    GUIDED_ASSEMBLY = "GUIDED_ASSEMBLY"
    DIAGNOSTIC = "DIAGNOSTIC"


class AgentState(str, Enum):
    INSTRUCTING = "INSTRUCTING"
    VERIFYING = "VERIFYING"
    FAULT_FOUND = "FAULT_FOUND"
    VERIFIED_FIXED = "VERIFIED_FIXED"
    COMPLETE = "COMPLETE"


class VerificationTest(str, Enum):
    RUN_I2C_SCAN = "RUN_I2C_SCAN"
    CHECK_OLED = "CHECK_OLED"
    BLINK_LED = "BLINK_LED"
    READ_SENSOR = "READ_SENSOR"
    SELFTEST = "SELFTEST"
    PING = "PING"
    NONE = "NONE"


class AssemblyStep(BaseModel):
    """One ordered step of the circuit build, loaded from YAML."""

    step_id: str
    instruction_student: str
    instruction_pro: str
    expected_component: str
    verification_test: VerificationTest
    success_criteria: str
    # Deterministic pass/fail inputs for the verifier (subset of success_criteria).
    expect_i2c: list[str] = []  # every address must be present, e.g. ["0x3C"]
    expect_i2c_any: list[list[str]] = []  # at least one per group, e.g. [["0x76","0x77"]]
    expect_oled_lit: bool = False
    expect_led: bool = False


class FaultDefinition(BaseModel):
    fault_id: str
    description: str
    observable_evidence: list[str]


class AgentDecision(BaseModel):
    """JSON schema returned by the Muse adapter on every turn.

    Advisory only: the orchestrator runs verification_test itself and the
    sensor result decides pass/fail. Muse never declares success alone.
    """

    mode: Mode
    state: AgentState
    fault_hypothesis: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[str] = []
    next_instruction: str
    speak_to_tech: str
    verification_test: VerificationTest


class ProbeReading(BaseModel):
    """Parsed electrical measurements from the patient board."""

    connected: bool = False
    i2c_devices: list[str] = []
    temperature_c: float | None = None
    humidity_pct: float | None = None
    pressure_hpa: float | None = None
    oled_ok: bool | None = None
    led_ok: bool | None = None
    mock: bool = False
    raw: str = ""


class StepCheck(BaseModel):
    step_id: str
    status: str = "pending"  # pending | pass | fail
    measured: str = ""
    updated_at: str = ""


class StartSessionRequest(BaseModel):
    mode: Mode = Mode.GUIDED_ASSEMBLY


class CheckNowRequest(BaseModel):
    code_snippet: str = ""
    simulate_fault: str | None = None
    transcript: str = ""  # technician utterance, e.g. "done" via push-to-talk


class Passport(BaseModel):
    """Repair/Assembly Passport issued when the loop completes."""

    mode: str
    steps_completed: int
    steps_total: int
    faults_found: list[str] = []
    measurements_before: dict = {}
    measurements_after: dict = {}
    started_at: str = ""
    completed_at: str = ""
    total_attempts: int = 0
    failed_attempts: int = 0

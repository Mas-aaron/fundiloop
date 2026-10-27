"""Shared Pydantic schemas for FundiLoop."""
from enum import Enum
from pydantic import BaseModel, Field


class DiagnosticState(str, Enum):
    DIAGNOSING = "DIAGNOSING"
    INSTRUCTING = "INSTRUCTING"
    VERIFYING = "VERIFYING"
    DONE = "DONE"


class VerificationTest(str, Enum):
    RUN_I2C_SCAN = "RUN_I2C_SCAN"
    CHECK_SERIAL = "CHECK_SERIAL"
    CHECK_OLED = "CHECK_OLED"
    NONE = "NONE"


class DiagnosticResponse(BaseModel):
    state: DiagnosticState
    fault_hypothesis: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = []
    next_instruction: str
    verification_test: VerificationTest
    speak_to_tech: str


class ProbeResult(BaseModel):
    found: list[str] = []
    missing: list[str] = []
    raw: str = ""
    timestamp: str = ""


class DiagnoseRequest(BaseModel):
    frame_path: str = ""
    probe_result: ProbeResult
    conversation_history: list[dict] = []

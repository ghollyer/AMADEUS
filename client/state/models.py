"""Pydantic models for AMADEUS client state."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Microscope / instrument state
# ---------------------------------------------------------------------------

class StagePosition(BaseModel):
    x_m: float = 0.0
    y_m: float = 0.0
    z_m: float = 0.0
    alpha_deg: float = 0.0
    beta_deg: float = 0.0


class BeamShift(BaseModel):
    x: float = 0.0
    y: float = 0.0


class Stigmation(BaseModel):
    obj_x: float = 0.0
    obj_y: float = 0.0
    cond_x: float = 0.0
    cond_y: float = 0.0


class MicroscopeState(BaseModel):
    high_tension_kV: float | None = None
    magnification: float | None = None
    mag_index: int | None = None
    focus_nm: float | None = None
    beam_blanked: bool = False
    column_valves_open: bool | None = None
    beam_shift: BeamShift = Field(default_factory=BeamShift)
    beam_tilt: BeamShift = Field(default_factory=BeamShift)
    stigmation: Stigmation = Field(default_factory=Stigmation)
    stage: StagePosition = Field(default_factory=StagePosition)
    microscope_name: str | None = None
    last_updated: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Sample metadata
# ---------------------------------------------------------------------------

class SampleMetadata(BaseModel):
    name: str = ""
    elements: list[str] = Field(default_factory=list)
    phases: list[str] = Field(default_factory=list)
    grid_type: str = ""          # e.g. "lacey carbon", "holey SiN"
    notes: str = ""


# ---------------------------------------------------------------------------
# Experimental plan
# ---------------------------------------------------------------------------

class ScanPreset(BaseModel):
    width: int = 512
    height: int = 512
    dwell_us: float = 2.0
    signal_index: int = 0


class OperationalPreferences(BaseModel):
    search_preset: ScanPreset = Field(default_factory=ScanPreset)
    preview_preset: ScanPreset = Field(default_factory=lambda: ScanPreset(width=1024, height=1024))
    capture_preset: ScanPreset = Field(default_factory=lambda: ScanPreset(width=2048, height=2048, dwell_us=4.0))
    blank_between_acquisitions: bool = True
    post_move_pause_s: float = 2.0
    max_beam_shift_nm: float = 500.0


class ExperimentalPlan(BaseModel):
    objective: str = ""
    techniques: list[str] = Field(default_factory=list)  # e.g. ["HAADF", "EELS"]
    target_magnifications: list[float] = Field(default_factory=list)
    target_particle_count: int | None = None
    notes: str = ""


# ---------------------------------------------------------------------------
# Map objects
# ---------------------------------------------------------------------------

class ImageCalibration(BaseModel):
    x_scale: float = 1.0   # nm/px
    y_scale: float = 1.0
    units: str = "nm"


class ImageSidecar(BaseModel):
    image_id: str
    name: str
    shape: list[int]
    dtype: str
    calibration: ImageCalibration = Field(default_factory=ImageCalibration)
    stage_position: StagePosition | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    tags: dict[str, Any] = Field(default_factory=dict)


class MapObject(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    label: str = ""
    position: StagePosition
    examined: bool = False
    image_ids: list[str] = Field(default_factory=list)
    notes: str = ""
    tags: dict[str, Any] = Field(default_factory=dict)

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
    """Per-stigmator x/y in device units, as get_stigmation's "stigmators"."""
    objective: BeamShift = Field(default_factory=BeamShift)
    condenser: BeamShift = Field(default_factory=BeamShift)
    intermediate: BeamShift | None = None


class MicroscopeState(BaseModel):
    high_tension_kV: float | None = None
    magnification: float | None = None
    mag_index: int | None = None
    focus_nm: float | None = None       # None when the instrument is uncalibrated
    focus_dac: float | None = None      # device value of the focus lens
    beam_blanked: bool = False
    column_valves_open: bool | None = None
    # Deflector/stigmator values are the column driver's device units (raw
    # DAC 0-65535 on JEOL, 32768 = neutral; Gatan's own on the DM fallback).
    beam_shift: BeamShift = Field(default_factory=BeamShift)
    beam_tilt: BeamShift = Field(default_factory=BeamShift)
    stigmation: Stigmation = Field(default_factory=Stigmation)
    stage: StagePosition = Field(default_factory=StagePosition)
    operation_mode: str | None = None   # "IMAGING" (TEM) / "SCANNING" (STEM) / "DIFFRACTION"
    tem_stem_mode: str | None = None    # "TEM" / "STEM"
    function_mode: str | None = None    # e.g. "MAG", "LowMAG", "SAMAG", "DIFF"
    spot_size: int | None = None
    alpha: int | None = None
    aperture_kind: str | None = None    # e.g. "CLA", "OLA"
    aperture_size: int | None = None    # hole number, 0 = open
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
    # Enforced by client/policy.py at the ZMQ chokepoint. Off by default:
    # blanking changes what the operator sees between acquisitions, so turn
    # it on deliberately (set_operational_preferences).
    blank_between_acquisitions: bool = False
    # Wait after a settled stage move before the next command (drift).
    post_move_pause_s: float = 2.0
    # Limit on beam-shift excursions from neutral, in the column driver's
    # device units (get_beam_shift reports unit, range and neutral). Enforced
    # by client/policy.py: beyond it needs the operator's confirmation.
    max_beam_shift: float = 2000.0


class ExperimentalPlan(BaseModel):
    objective: str = ""
    techniques: list[str] = Field(default_factory=list)  # e.g. ["HAADF", "EELS"]
    target_magnifications: list[float] = Field(default_factory=list)
    target_particle_count: int | None = None
    notes: str = ""


class TaskStep(BaseModel):
    label: str
    status: str = "pending"


class SessionTask(BaseModel):
    objective: str = ""
    status: str = "idle"
    current_step: int | None = None
    steps: list[TaskStep] = Field(default_factory=list)
    status_summary: str = ""
    blocking_condition: str = ""
    updated: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Map objects
# ---------------------------------------------------------------------------

class ImageCalibration(BaseModel):
    x_scale: float = 1.0   # per px, in `units` as DM reported (often µm) — use client.analysis.units.nm_per_px
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

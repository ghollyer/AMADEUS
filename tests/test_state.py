"""Tests for state models, manager, and image store."""

import tempfile
from pathlib import Path

import numpy as np
import pytest

from client.state.models import (
    ExperimentalPlan,
    ImageCalibration,
    MapObject,
    MicroscopeState,
    OperationalPreferences,
    SampleMetadata,
    StagePosition,
)
from client.state.manager import StateManager
from client.state.image_store import ImageStore


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ---------------------------------------------------------------------------
# Model round-trips
# ---------------------------------------------------------------------------

def test_stage_position_defaults():
    pos = StagePosition()
    assert pos.x_m == 0.0
    assert pos.alpha_deg == 0.0


def test_microscope_state_defaults():
    ms = MicroscopeState()
    assert ms.beam_blanked is False
    assert ms.focus_nm is None


def test_sample_metadata_round_trip():
    s = SampleMetadata(name="test", elements=["Fe", "O"])
    s2 = SampleMetadata.model_validate(s.model_dump())
    assert s2.elements == ["Fe", "O"]


def test_operational_preferences_defaults():
    prefs = OperationalPreferences()
    assert prefs.blank_between_acquisitions is False     # opt-in since it is enforced
    assert prefs.capture_preset.width == 2048


# ---------------------------------------------------------------------------
# ImageStore
# ---------------------------------------------------------------------------

def test_image_store_save_load(tmp_dir):
    store = ImageStore(tmp_dir / "images")
    arr = np.random.rand(64, 64).astype(np.float32)
    cal = ImageCalibration(x_scale=0.5, y_scale=0.5, units="nm")
    sidecar = store.save(arr, name="test_img", calibration=cal)

    loaded_arr, loaded_sidecar = store.load(sidecar.image_id)
    np.testing.assert_array_almost_equal(arr, loaded_arr)
    assert loaded_sidecar.name == "test_img"
    assert loaded_sidecar.calibration.x_scale == 0.5


def test_image_store_list_and_delete(tmp_dir):
    store = ImageStore(tmp_dir / "images")
    arr = np.zeros((32, 32), dtype=np.float32)
    s1 = store.save(arr, name="img1")
    s2 = store.save(arr, name="img2")
    ids = store.list_ids()
    assert s1.image_id in ids
    assert s2.image_id in ids
    store.delete(s1.image_id)
    assert s1.image_id not in store.list_ids()


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------

def test_state_manager_persistence(tmp_dir):
    sm = StateManager(tmp_dir)
    sm.set_sample(name="myslide", elements=["Au"])
    sm.set_plan(objective="Find nanoparticles")

    # Reload from disk
    sm2 = StateManager(tmp_dir)
    assert sm2.sample.name == "myslide"
    assert sm2.sample.elements == ["Au"]
    assert sm2.plan.objective == "Find nanoparticles"


def test_state_manager_saved_locations(tmp_dir):
    sm = StateManager(tmp_dir)
    pos = StagePosition(x_m=1e-6, y_m=2e-6)
    sm.save_location("grain_1", pos)
    sm2 = StateManager(tmp_dir)
    retrieved = sm2.get_location("grain_1")
    assert retrieved is not None
    assert retrieved.x_m == pytest.approx(1e-6)


def test_state_manager_map_objects(tmp_dir):
    sm = StateManager(tmp_dir)
    obj = MapObject(
        label="particle_A",
        position=StagePosition(x_m=5e-6, y_m=3e-6),
    )
    sm.add_map_object(obj)
    sm.update_map_object(obj.id, examined=True)

    sm2 = StateManager(tmp_dir)
    retrieved = sm2.get_map_object(obj.id)
    assert retrieved is not None
    assert retrieved.examined is True
    assert retrieved.label == "particle_A"


def test_state_save_is_atomic_and_keeps_a_backup(tmp_dir):
    sm = StateManager(tmp_dir)
    sm.set_sample(name="first")
    sm.set_sample(name="second")
    assert (tmp_dir / "amadeus_state.json.bak").exists()
    assert not (tmp_dir / "amadeus_state.json.tmp").exists()


def test_state_falls_back_to_backup_when_main_file_is_corrupt(tmp_dir):
    # Old code wrote the file in place; a torn write (or any damage) made
    # StateManager() raise and the whole session state was lost.
    sm = StateManager(tmp_dir)
    sm.set_sample(name="good")
    sm.set_sample(name="newer")
    (tmp_dir / "amadeus_state.json").write_text('{"sample": {"na', encoding="utf-8")
    sm2 = StateManager(tmp_dir)
    assert sm2.sample.name == "good"
    assert sm2.loaded_from.name == "amadeus_state.json.bak"

"""StateManager — central in-memory state with JSON persistence.

All state is kept in memory and flushed to disk on every mutation.
On startup the manager reloads from disk if a state file exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from client.state.models import (
    ExperimentalPlan,
    MapObject,
    MicroscopeState,
    OperationalPreferences,
    SampleMetadata,
    StagePosition,
)
from client.state.image_store import ImageStore


class StateManager:
    STATE_FILE = "amadeus_state.json"

    def __init__(self, data_dir: Path | str = "amadeus_data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self.data_dir / self.STATE_FILE

        self.images = ImageStore(self.data_dir / "images")

        # In-memory state
        self.microscope = MicroscopeState()
        self.sample = SampleMetadata()
        self.plan = ExperimentalPlan()
        self.preferences = OperationalPreferences()
        self.saved_locations: dict[str, StagePosition] = {}
        self.map_objects: dict[str, MapObject] = {}

        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        if "sample" in raw:
            self.sample = SampleMetadata.model_validate(raw["sample"])
        if "plan" in raw:
            self.plan = ExperimentalPlan.model_validate(raw["plan"])
        if "preferences" in raw:
            self.preferences = OperationalPreferences.model_validate(raw["preferences"])
        if "saved_locations" in raw:
            self.saved_locations = {
                k: StagePosition.model_validate(v)
                for k, v in raw["saved_locations"].items()
            }
        if "map_objects" in raw:
            self.map_objects = {
                k: MapObject.model_validate(v)
                for k, v in raw["map_objects"].items()
            }

    def save(self) -> None:
        payload = {
            "sample": self.sample.model_dump(),
            "plan": self.plan.model_dump(),
            "preferences": self.preferences.model_dump(),
            "saved_locations": {
                k: v.model_dump() for k, v in self.saved_locations.items()
            },
            "map_objects": {
                k: v.model_dump() for k, v in self.map_objects.items()
            },
        }
        self._state_path.write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # Sample
    # ------------------------------------------------------------------

    def set_sample(self, **kwargs: Any) -> SampleMetadata:
        self.sample = SampleMetadata(**{**self.sample.model_dump(), **kwargs})
        self.save()
        return self.sample

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------

    def set_plan(self, **kwargs: Any) -> ExperimentalPlan:
        self.plan = ExperimentalPlan(**{**self.plan.model_dump(), **kwargs})
        self.save()
        return self.plan

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def set_preferences(self, **kwargs: Any) -> OperationalPreferences:
        self.preferences = OperationalPreferences(
            **{**self.preferences.model_dump(), **kwargs}
        )
        self.save()
        return self.preferences

    # ------------------------------------------------------------------
    # Stage locations
    # ------------------------------------------------------------------

    def save_location(self, name: str, position: StagePosition) -> None:
        self.saved_locations[name] = position
        self.save()

    def get_location(self, name: str) -> StagePosition | None:
        return self.saved_locations.get(name)

    # ------------------------------------------------------------------
    # Map objects
    # ------------------------------------------------------------------

    def add_map_object(self, obj: MapObject) -> MapObject:
        self.map_objects[obj.id] = obj
        self.save()
        return obj

    def get_map_object(self, obj_id: str) -> MapObject | None:
        return self.map_objects.get(obj_id)

    def update_map_object(self, obj_id: str, **kwargs: Any) -> MapObject:
        obj = self.map_objects[obj_id]
        updated = MapObject(**{**obj.model_dump(), **kwargs})
        self.map_objects[obj_id] = updated
        self.save()
        return updated

    def list_map_objects(self) -> list[MapObject]:
        return list(self.map_objects.values())

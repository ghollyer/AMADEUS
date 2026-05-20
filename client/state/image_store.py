"""Image store — saves .npy arrays with .json sidecar files."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np

from client.state.models import ImageCalibration, ImageSidecar, StagePosition


class ImageStore:
    """Saves images as .npy files with JSON sidecar metadata.

    Directory layout::

        <root>/
            <image_id>.npy
            <image_id>.json
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        array: np.ndarray,
        *,
        name: str = "",
        calibration: ImageCalibration | None = None,
        stage_position: StagePosition | None = None,
        tags: dict | None = None,
        image_id: str | None = None,
    ) -> ImageSidecar:
        """Save *array* and return the sidecar metadata object."""
        iid = image_id or str(uuid.uuid4())
        sidecar = ImageSidecar(
            image_id=iid,
            name=name or iid,
            shape=list(array.shape),
            dtype=str(array.dtype),
            calibration=calibration or ImageCalibration(),
            stage_position=stage_position,
            timestamp=datetime.utcnow(),
            tags=tags or {},
        )
        np.save(self.root / f"{iid}.npy", array)
        (self.root / f"{iid}.json").write_text(
            sidecar.model_dump_json(indent=2), encoding="utf-8"
        )
        return sidecar

    def load(self, image_id: str) -> tuple[np.ndarray, ImageSidecar]:
        """Load array and sidecar for *image_id*."""
        arr = np.load(self.root / f"{image_id}.npy")
        sidecar = ImageSidecar.model_validate_json(
            (self.root / f"{image_id}.json").read_text(encoding="utf-8")
        )
        return arr, sidecar

    def load_sidecar(self, image_id: str) -> ImageSidecar:
        return ImageSidecar.model_validate_json(
            (self.root / f"{image_id}.json").read_text(encoding="utf-8")
        )

    def list_ids(self) -> list[str]:
        return [p.stem for p in sorted(self.root.glob("*.npy"))]

    def delete(self, image_id: str) -> None:
        for ext in (".npy", ".json"):
            p = self.root / f"{image_id}{ext}"
            if p.exists():
                p.unlink()

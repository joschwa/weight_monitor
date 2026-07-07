from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Calibration:
    offset: float          # raw counts at 0g
    scale: float            # raw counts per gram
    reference_weights_g: list[float]
    calibrated_at: str

    def to_grams(self, raw_avg: float) -> float:
        return (raw_avg - self.offset) / self.scale

    @classmethod
    def create(cls, offset: float, scale: float, reference_weights_g: list[float]) -> "Calibration":
        return cls(
            offset=offset,
            scale=scale,
            reference_weights_g=reference_weights_g,
            calibrated_at=datetime.now(timezone.utc).isoformat(),
        )

    @classmethod
    def load(cls, path: str | Path) -> "Calibration":
        with open(path) as f:
            data = json.load(f)
        return cls(**data)

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)

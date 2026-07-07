from __future__ import annotations

import statistics
import time
from typing import Callable, Protocol

from weight_monitor.calibration import Calibration


class SensorReadError(Exception):
    """Raised when a filtered reading could not be obtained after retries."""


class RawReader(Protocol):
    def read_raw(self) -> int:
        """Return a single raw ADC count. May raise on transient failure."""


class HX711RawReader:
    """Wraps a physical HX711 over GPIO.

    The exact library is decided during hardware bring-up (see the plan's
    Stage 1) -- this class only assumes a `.get_raw_data()`-style call and
    is a thin adapter so the rest of the codebase never imports GPIO
    libraries directly (which don't exist on non-Pi dev machines).
    """

    def __init__(self, dout_pin: int, pd_sck_pin: int):
        from hx711 import HX711  # imported lazily: only present with [hardware] extra

        self._hx = HX711(dout_pin=dout_pin, pd_sck_pin=pd_sck_pin)
        self._hx.reset()

    def read_raw(self) -> int:
        values = self._hx.get_raw_data(times=1)
        if not values:
            raise SensorReadError("HX711 returned no data")
        return values[0]


class FakeRawReader:
    """Test/dev double. `values` is consumed in order; a callable is called each time."""

    def __init__(self, values: Callable[[], int] | list[int]):
        self._values = values
        self._index = 0

    def read_raw(self) -> int:
        if callable(self._values):
            return self._values()
        value = self._values[self._index % len(self._values)]
        self._index += 1
        return value


class Sensor:
    def __init__(
        self,
        reader: RawReader,
        calibration: Calibration,
        samples_per_reading: int = 12,
        trim_outliers: int = 2,
        sample_interval_seconds: float = 0.1,
        read_retries: int = 3,
        read_retry_backoff_seconds: float = 0.5,
    ):
        self._reader = reader
        self._calibration = calibration
        self._samples_per_reading = samples_per_reading
        self._trim_outliers = trim_outliers
        self._sample_interval_seconds = sample_interval_seconds
        self._read_retries = read_retries
        self._read_retry_backoff_seconds = read_retry_backoff_seconds

    def _sample_raw_avg(self) -> float:
        samples = []
        for i in range(self._samples_per_reading):
            samples.append(self._reader.read_raw())
            if i < self._samples_per_reading - 1:
                time.sleep(self._sample_interval_seconds)

        samples.sort()
        trim = self._trim_outliers
        trimmed = samples[trim:-trim] if trim and len(samples) > 2 * trim else samples
        return statistics.fmean(trimmed)

    def read_grams(self) -> float:
        """Take a filtered, calibrated weight reading, retrying on failure."""
        last_error: Exception | None = None
        for attempt in range(self._read_retries):
            try:
                raw_avg = self._sample_raw_avg()
                return self._calibration.to_grams(raw_avg)
            except Exception as exc:  # noqa: BLE001 - any sensor failure should retry/surface
                last_error = exc
                if attempt < self._read_retries - 1:
                    time.sleep(self._read_retry_backoff_seconds)
        raise SensorReadError(f"failed after {self._read_retries} attempts: {last_error}")

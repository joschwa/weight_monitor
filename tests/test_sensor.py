from weight_monitor.calibration import Calibration
from weight_monitor.sensor import FakeRawReader, Sensor, SensorReadError

import pytest


def make_sensor(reader, **overrides):
    calibration = Calibration.create(offset=1000, scale=2.0, reference_weights_g=[500])
    kwargs = dict(samples_per_reading=6, trim_outliers=1, sample_interval_seconds=0, read_retries=2,
                  read_retry_backoff_seconds=0)
    kwargs.update(overrides)
    return Sensor(reader, calibration, **kwargs)


def test_read_grams_averages_and_converts():
    reader = FakeRawReader([1100, 1100, 1100, 1100, 1100, 1100])
    sensor = make_sensor(reader)
    assert sensor.read_grams() == pytest.approx(50.0)


def test_read_grams_trims_outliers():
    # one wild low and one wild high outlier should be trimmed before averaging
    reader = FakeRawReader([-100000, 1100, 1100, 1100, 1100, 100000])
    sensor = make_sensor(reader)
    assert sensor.read_grams() == pytest.approx(50.0)


def test_read_grams_retries_then_raises():
    def always_fail():
        raise RuntimeError("no data")

    reader = FakeRawReader(always_fail)
    sensor = make_sensor(reader)
    with pytest.raises(SensorReadError):
        sensor.read_grams()


def test_read_grams_succeeds_after_transient_failure():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 3:
            raise RuntimeError("transient")
        return 1100

    reader = FakeRawReader(flaky)
    sensor = make_sensor(reader, read_retries=5)
    assert sensor.read_grams() == pytest.approx(50.0)

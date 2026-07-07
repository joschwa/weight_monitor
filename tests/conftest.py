import pytest

from weight_monitor import db
from weight_monitor.calibration import Calibration
from weight_monitor.config import StaticConfig
from weight_monitor.sensor import FakeRawReader, Sensor


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def config():
    return StaticConfig(
        data={
            "gpio": {"dout_pin": 5, "pd_sck_pin": 6},
            "sensor": {
                "samples_per_reading": 4,
                "trim_outliers": 0,
                "sample_interval_seconds": 0,
                "read_retries": 1,
                "read_retry_backoff_seconds": 0,
                "hopper_max_capacity_g": 2000,
            },
            "paths": {
                "database": "data/test.db",
                "calibration": "data/test_calibration.json",
                "log_file": "data/test.log",
            },
            "smtp": {
                "host": "smtp.example.com",
                "port": 587,
                "from_address": "from@example.com",
                "to_address": "to@example.com",
                "retry_scan_interval_minutes": 5,
            },
            "log_level": "INFO",
        },
        secrets={"smtp_password": "unused-in-tests"},
    )


def make_sensor(values):
    calibration = Calibration.create(offset=0, scale=1.0, reference_weights_g=[100])
    reader = FakeRawReader(values)
    return Sensor(reader, calibration, samples_per_reading=1, trim_outliers=0,
                  sample_interval_seconds=0, read_retries=1, read_retry_backoff_seconds=0)

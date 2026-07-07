from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml

from weight_monitor.models import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent


class StaticConfig:
    """Read-only config loaded once at startup from config.yaml + secrets.yaml."""

    def __init__(self, data: dict, secrets: dict):
        self._data = data
        self._secrets = secrets

    @classmethod
    def load(
        cls,
        config_path: str | Path = REPO_ROOT / "config" / "config.yaml",
        secrets_path: str | Path = REPO_ROOT / "config" / "secrets.yaml",
    ) -> "StaticConfig":
        with open(config_path) as f:
            data = yaml.safe_load(f)
        with open(secrets_path) as f:
            secrets = yaml.safe_load(f)
        return cls(data, secrets)

    @property
    def gpio_dout_pin(self) -> int:
        return self._data["gpio"]["dout_pin"]

    @property
    def gpio_pd_sck_pin(self) -> int:
        return self._data["gpio"]["pd_sck_pin"]

    @property
    def samples_per_reading(self) -> int:
        return self._data["sensor"]["samples_per_reading"]

    @property
    def trim_outliers(self) -> int:
        return self._data["sensor"]["trim_outliers"]

    @property
    def sample_interval_seconds(self) -> float:
        return self._data["sensor"]["sample_interval_seconds"]

    @property
    def read_retries(self) -> int:
        return self._data["sensor"]["read_retries"]

    @property
    def read_retry_backoff_seconds(self) -> float:
        return self._data["sensor"]["read_retry_backoff_seconds"]

    @property
    def hopper_max_capacity_g(self) -> float:
        return self._data["sensor"]["hopper_max_capacity_g"]

    @property
    def database_path(self) -> Path:
        return REPO_ROOT / self._data["paths"]["database"]

    @property
    def calibration_path(self) -> Path:
        return REPO_ROOT / self._data["paths"]["calibration"]

    @property
    def log_file(self) -> Path:
        return REPO_ROOT / self._data["paths"]["log_file"]

    @property
    def smtp_host(self) -> str:
        return self._data["smtp"]["host"]

    @property
    def smtp_port(self) -> int:
        return self._data["smtp"]["port"]

    @property
    def smtp_from_address(self) -> str:
        return self._data["smtp"]["from_address"]

    @property
    def smtp_to_address(self) -> str:
        return self._data["smtp"]["to_address"]

    @property
    def smtp_retry_scan_interval_minutes(self) -> int:
        return self._data["smtp"]["retry_scan_interval_minutes"]

    @property
    def smtp_password(self) -> str:
        return self._secrets["smtp_password"]

    @property
    def log_level(self) -> str:
        return self._data.get("log_level", "INFO")


def get_settings(conn: sqlite3.Connection) -> Settings:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    values = {row["key"]: json.loads(row["value"]) for row in rows}
    return Settings(
        feed_times=values["feed_times"],
        control_time=values["control_time"],
        delay_minutes=values["delay_minutes"],
        threshold_g=values["threshold_g"],
        calibration_mode=values["calibration_mode"],
    )


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()

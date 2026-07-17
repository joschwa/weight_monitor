#!/usr/bin/env python3
"""One-time (or post-hardware-change) HX711 calibration.

Run directly on the Pi with the hardware wired up:

    python scripts/calibrate.py

Walks through a tare step and one or two known reference weights, then
writes offset/scale to the path configured in config.yaml (paths.calibration).
"""
from __future__ import annotations

import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weight_monitor import db
from weight_monitor.calibration import Calibration
from weight_monitor.config import StaticConfig, get_settings, set_setting
from weight_monitor.sensor import HX711RawReader

SAMPLES = 20
SAMPLE_INTERVAL_SECONDS = 0.1


def filtered_raw_read(reader) -> float:
    samples = [reader.read_raw() for _ in range(SAMPLES)]
    for _ in range(SAMPLES - 1):
        time.sleep(SAMPLE_INTERVAL_SECONDS)
    samples.sort()
    trimmed = samples[2:-2] if len(samples) > 4 else samples
    return statistics.fmean(trimmed)


def main() -> None:
    config = StaticConfig.load()
    reader = HX711RawReader(config.gpio_dout_pin, config.gpio_pd_sck_pin)

    input("Clear the platform completely, then press Enter to tare...")
    offset = filtered_raw_read(reader)
    print(f"  tare raw reading: {offset:.1f}")

    known_grams = float(input("Place a known reference weight, enter its mass in grams: "))
    input("Press Enter once it's settled on the platform...")
    raw_with_weight = filtered_raw_read(reader)
    scale = (raw_with_weight - offset) / known_grams
    print(f"  raw reading with weight: {raw_with_weight:.1f} -> scale = {scale:.4f} counts/gram")

    reference_weights = [known_grams]

    verify = input("Verify with a second known weight? [y/N]: ").strip().lower()
    if verify == "y":
        second_grams = float(input("Enter its mass in grams: "))
        input("Press Enter once it's settled on the platform...")
        raw_second = filtered_raw_read(reader)
        measured = (raw_second - offset) / scale
        error_pct = abs(measured - second_grams) / second_grams * 100
        print(f"  measured {measured:.1f}g vs actual {second_grams:.1f}g ({error_pct:.1f}% error)")
        if error_pct > 5:
            print("  WARNING: error exceeds 5% -- consider re-running calibration.")
        reference_weights.append(second_grams)

    calibration = Calibration.create(offset=offset, scale=scale, reference_weights_g=reference_weights)
    calibration.save(config.calibration_path)
    print(f"Saved calibration to {config.calibration_path}")

    _maybe_configure_feed_countdown(reader, calibration, config)


def _weigh_empty_feeder(reader, calibration: Calibration) -> float:
    input("Place the empty feeder (no food) on the platform, then press Enter...")
    raw = filtered_raw_read(reader)
    grams = calibration.to_grams(raw)
    print(f"  empty feeder weight: {grams:.1f}g")
    return grams


def _maybe_configure_feed_countdown(reader, calibration: Calibration, config: StaticConfig) -> None:
    conn = db.connect(config.database_path)
    db.init_db(conn)
    settings = get_settings(conn)

    if settings.feeder_empty_weight_g is not None and settings.refill_countdown_enabled:
        print(f"\nExisting empty-feeder weight on file: {settings.feeder_empty_weight_g:.1f}g")
        choice = input("Continue using it, or replace? [continue/replace]: ").strip().lower()
        if choice == "replace":
            weight = _weigh_empty_feeder(reader, calibration)
            set_setting(conn, "feeder_empty_weight_g", weight)
            set_setting(conn, "feeder_empty_weight_set_at", datetime.now(timezone.utc).isoformat())
            set_setting(conn, "feeds_left_equal_alerted", False)
            set_setting(conn, "feeds_left_below_alerted", False)
            print("Feed countdown tracking updated.")
        else:
            print(
                "Keeping existing empty-feeder weight. Feed countdown tracking stays enabled "
                "-- reminder thresholds re-arm automatically once feeds left recovers above them."
            )
    else:
        answer = input("\nEnable feed countdown tracking by weighing the empty feeder now? [y/N]: ").strip().lower()
        if answer == "y":
            weight = _weigh_empty_feeder(reader, calibration)
            set_setting(conn, "feeder_empty_weight_g", weight)
            set_setting(conn, "feeder_empty_weight_set_at", datetime.now(timezone.utc).isoformat())
            set_setting(conn, "feeds_left_equal_alerted", False)
            set_setting(conn, "feeds_left_below_alerted", False)
            set_setting(conn, "refill_countdown_enabled", True)
            print("Feed countdown tracking enabled.")
        else:
            set_setting(conn, "refill_countdown_enabled", False)
            print("Feed countdown tracking left disabled.")


if __name__ == "__main__":
    main()

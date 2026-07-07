from __future__ import annotations

import logging
import signal
import time

from weight_monitor import db
from weight_monitor.calibration import Calibration
from weight_monitor.config import StaticConfig
from weight_monitor.events import recover_missed_events
from weight_monitor.notifier import scan_and_retry
from weight_monitor.scheduler import start_scheduler
from weight_monitor.sensor import HX711RawReader, Sensor

logger = logging.getLogger(__name__)

_shutdown = False


def _handle_signal(signum, frame) -> None:
    global _shutdown
    logger.info("received signal %s, shutting down", signum)
    _shutdown = True


def main() -> None:
    config = StaticConfig.load()
    logging.basicConfig(
        level=config.log_level,
        filename=config.log_file,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    conn = db.connect(config.database_path)
    db.init_db(conn)

    calibration = Calibration.load(config.calibration_path)
    reader = HX711RawReader(config.gpio_dout_pin, config.gpio_pd_sck_pin)
    sensor = Sensor(
        reader,
        calibration,
        samples_per_reading=config.samples_per_reading,
        trim_outliers=config.trim_outliers,
        sample_interval_seconds=config.sample_interval_seconds,
        read_retries=config.read_retries,
        read_retry_backoff_seconds=config.read_retry_backoff_seconds,
    )

    recovered = recover_missed_events(conn, sensor, config)
    if recovered:
        logger.info("recovered %d event(s) on startup", len(recovered))
    scan_and_retry(config, conn)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    scheduler = start_scheduler(conn, sensor, config)
    logger.info("weight_monitor daemon started")
    try:
        while not _shutdown:
            time.sleep(1)
    finally:
        scheduler.shutdown()
        conn.close()


if __name__ == "__main__":
    main()

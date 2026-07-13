from __future__ import annotations

import sqlite3

from flask import Flask

from weight_monitor.config import StaticConfig


def create_app(config: StaticConfig, conn: sqlite3.Connection) -> Flask:
    """App factory. `conn` is injected (not opened here) so tests can pass
    the same in-memory connection used elsewhere in the test suite, and so
    the process holds exactly one sqlite3.Connection, matching the rest of
    the codebase (daemon.py, cli.py)."""
    app = Flask(__name__)
    app.config["WM_CONN"] = conn
    app.config["WM_STATIC_CONFIG"] = config

    from weight_monitor.webui.routes import bp

    app.register_blueprint(bp)
    return app


def main() -> None:
    from weight_monitor import db

    config = StaticConfig.load()
    conn = db.connect(config.database_path)
    db.init_db(conn)
    app = create_app(config, conn)
    # threaded=False: a single shared sqlite3.Connection is only safe from
    # one thread at a time.
    app.run(host="0.0.0.0", port=5000, threaded=False)


if __name__ == "__main__":
    main()

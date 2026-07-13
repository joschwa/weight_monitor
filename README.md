# weight_monitor (for automatic pet feeders)

Automatic pet feeders can jam and fail in a range of ways. This code is used to process load cell data and email notifications when the difference between pre- and post- feeding is below a threshold. I added an additional weigh in around midnight for callibration.



## Deployment (Raspberry Pi)

In my set up, a load cell sits under
the feeder and is read via an HX711 on a Raspberry Pi Zero W running Raspberry Pi OS Lite with Python 11. Code was tested with this configuration. Some adaption likely required for other set ups.

The weight checks around each scheduled feed time gives the amount dispensed or amount eaten (depending on whether bowl is weighed). Email alerts fire when that amount falls below a threshold. 


### 1. Create a venv and install dependencies

```bash
cd ~/weight_monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[hardware]"
```

### 2. Configure

```bash
cp config/config.example.yaml config/config.yaml
cp config/secrets.example.yaml config/secrets.yaml
```

Edit `config/config.yaml` (GPIO pins, SMTP `from_address`/`to_address`) and
`config/secrets.yaml` (Gmail app password from
https://myaccount.google.com/apppasswords — requires 2-Step Verification).

### 3. Calibrate the load cell

```bash
python scripts/calibrate.py
```

Follow the prompts: tare with the platform empty, then place a known reference weight. This writes `data/calibration.json`.

### 4. Run tests (if required)

Runs a before/after weight check and sends an email — confirms the
sensor, database, and SMTP all work together:

```bash
wm-cli trigger-feed-now --delay-seconds 30
wm-cli trigger-control-now --delay-seconds 30
```

Check your inbox for both notification emails. Review recent events if required:

```bash
wm-cli status
```

### 5. Install the systemd service

`systemd/weight-monitor.service.template` has no hardcoded user or path —
`scripts/install_service.sh` fills in `%USER%`/`%WORKDIR%` from the account that runs it and for wherever the repo actually is, then installs and starts it:

```bash
chmod +x scripts/install_service.sh
bash scripts/install_service.sh
```

Verify it's running and check the logs:

```bash
systemctl status weight-monitor
journalctl -u weight-monitor -f
```

### 6. Adjust schedule settings

Feed times, control time, and the before/after check offsets are all
settings-table keys, changed via the CLI:

```bash
wm-cli set-setting feed_times '["08:00", "18:00"]'   # list of "HH:MM" strings
wm-cli set-setting control_time "00:00"
wm-cli set-setting baseline_minutes 10   # before-check offset, taken this many minutes *before* each label time
wm-cli set-setting delay_minutes 25      # after-check offset, taken this many minutes *after* each label time
```

E.g. with the defaults above, the 08:00 feed check takes a "before" weight
at 07:50 and an "after" weight at 08:20.

### 7. Calibration period

The monitor starts in calibration mode (`calibration_mode=true` by
default), which emails you the measured delta on **every** feed and control
event, no threshold applied. This is so can gather enough data to pick a sensible threshold.

Check current settings and recent events any time with:

```bash
wm-cli status
```

### 8. Set a threshold and switch to alert mode

Once you've seen enough real deltas (normal feeds vs. the midnight control
noise floor), pick a `threshold_g` safely above the noise floor and below a
normal feed's delta, then switch modes:

```bash
wm-cli set-setting threshold_g 100
wm-cli set-setting calibration_mode false
```

Changes take effect within ~60 seconds without restarting the service.
After this, feed events only email you when the delta drops below
threshold, and control events stop emailing entirely (their job was
calibration data). Both still run on schedule and get logged either way —
sensor errors and anomalies still email regardless of mode or event type,
and logged control data remains available for the future web UI's history.

### Web UI (optional)

A basic Flask app to edit settings and view event history from a browser,
instead of SSH + `wm-cli`. Install its dependency and service the same way
as the daemon:

```bash
pip install -e ".[webui]"
chmod +x scripts/install_web_service.sh
bash scripts/install_web_service.sh
```

Then visit `http://<pi-ip>:5000/` from any device on your local network.
The page has a settings form (same keys as `wm-cli set-setting`) and a
history section: a line chart of deltas over time (one line per feed/control
label, with a dashed threshold line), an editable date range, and checkboxes
to include/exclude individual labels from the chart and table below it.

**No authentication** — anyone on your local network who can reach the Pi's
IP can view data and change settings. Fine for a trusted home LAN; don't
port-forward this to the internet.

### Useful commands reference

| Command | Purpose |
|---|---|
| `wm-cli status` | Show current settings and recent events |
| `wm-cli trigger-feed-now [--delay-seconds N]` | Run a feed check immediately |
| `wm-cli trigger-control-now [--delay-seconds N]` | Run a control check immediately |
| `wm-cli set-setting <key> <value>` | Update a setting (JSON value) |
| `wm-cli notify-retry` | Manually flush any queued/failed notifications |
| `systemctl status weight-monitor` | Check daemon status |
| `journalctl -u weight-monitor -f` | Tail daemon logs |
| `systemctl status weight-monitor-web` | Check web UI status |
| `journalctl -u weight-monitor-web -f` | Tail web UI logs |


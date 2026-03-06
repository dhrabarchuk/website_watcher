# Website Watcher (`watch.py`)

`watch.py` continuously loads a target URL with Selenium/Chrome, detects host-style failures, and records metrics/artifacts for troubleshooting.

## What It Does

- Repeatedly opens a URL on a fixed interval.
- Marks each attempt as:
  - `ok`
  - `host_error`
  - `timeout`
  - `hard_timeout`
  - `webdriver_error`
- Tracks:
  - all-time success/failure totals
  - rolling-window stats (last 50 attempts)
  - hourly failure percentages (last 24 hours)
- Saves artifacts for failed attempts (HTML/screenshot).
- Saves extra diagnostics JSON for timeout-style failures.
- Optionally writes JSONL events and a machine-readable health summary file.
- Can run a startup `--self-test` and exit.

## Output Files and Folders

- `host_error/host_error_log.txt`
  - Text event log.
- `host_error/host_error_stats.csv`
  - Per-attempt CSV stats.
- `host_error/health.json` (default path; configurable)
  - Latest heartbeat/status snapshot for external monitors.
- `host_error/`
  - Host-error artifacts (`*.png`, `*.html`)
  - Timeout diagnostics JSON (`*_timeout_diagnostics.json`)
- Optional JSONL event stream (`--jsonl /path/events.jsonl`)
  - One event object per attempt.
- `failures/`
  - Non-host-error failure artifacts (`timeout`, `webdriver_error`, etc.).

## Dashboard Behavior (TTY mode)

When run in a terminal:

- Shows a live curses dashboard with:
  - hourly failure bars
  - all-time + rolling summaries
  - recent events
- During active Selenium load:
  - spinner animation (`Testing with Selenium...`)
- During pause between attempts:
  - shrinking text countdown bar (`Pause: X.Xs [###---]`)
- Press `q` to stop.

When not in a TTY (redirected output, cron, etc.), it prints plain log lines instead.

## URL and Error Detection

- If URL is passed without scheme, it defaults to `https://`.
- Validates URL scheme is `http` or `https`.
- Host-error detection uses page title/source keywords plus Cloudflare-style checks.

## Timeout and Recovery Notes

- Selenium page-load timeout is configurable (`--timeout`, default `60` seconds).
- A hard watchdog timeout (`timeout + 15s`) is used to detect wedged loads.
- On webdriver/hard-timeout failures, the script recreates the browser session.
- Timeout diagnostics JSON includes useful context such as:
  - `current_url`, `title`, `document.readyState`
  - browser console logs
  - performance logs and parsed `Network.loadingFailed` events
  - request/domain timing breakdown (slowest and failed requests)
  - browser/chromedriver resource stats (CPU/RAM, best effort)

## Observability Features

- JSONL event output (`--jsonl`)
  - Includes status, attempt number, load time, counts, failure percentages, and resource stats.
- Health file output (`--health-file`)
  - Updated each attempt with heartbeat, current status, summaries, consecutive failures, and resource stats.
- Resource tracking
  - Uses process-tree inspection to estimate Chrome/chromedriver CPU and RSS.

## Requirements

- Python 3
- Selenium (`pip install selenium`)
- Chrome/Chromium binary available:
  - auto-discovered from PATH/common locations, or
  - set with `BROWSER_BINARY=/path/to/chrome`
- Matching ChromeDriver available to Selenium.

## Usage

```bash
python3 watch.py --url https://example.com
```

Options:

- `-u, --url`: target URL (required unless `--self-test` is used)
- `--pause` (default `5`): seconds between attempts
- `--timeout` (default `60`): Selenium page-load timeout in seconds
- `--jsonl`: optional JSONL event output path
- `--health-file` (default `host_error/health.json`): health summary JSON output path
- `--self-test`: run startup checks (driver + write paths) and exit

Example:

```bash
python3 watch.py --url lockify360.com/en-US --pause 3 --timeout 60
```

With JSONL + health output:

```bash
python3 watch.py \
  --url https://example.com \
  --jsonl host_error/events.jsonl \
  --health-file host_error/health.json
```

Self-test only:

```bash
python3 watch.py --self-test
```

## Typical Workflow

1. Start watcher against target URL.
2. Let it run until failures/timeouts occur.
3. Inspect:
   - `host_error/host_error_log.txt`
   - `host_error/host_error_stats.csv`
   - timeout diagnostics JSON files in `host_error/`
   - `host_error/health.json` (or your configured health file)
   - JSONL events file (if `--jsonl` is enabled)
   - screenshots/HTML artifacts in `host_error/` or `failures/`

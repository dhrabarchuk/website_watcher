import csv
import argparse
import curses
import os
import re
import socket
import tempfile
import threading
import time
from datetime import datetime, timedelta
from collections import deque
from shutil import which
from urllib.parse import urlparse
import sys

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, WebDriverException

# =========================
# CONFIG
# =========================
DEFAULT_PAUSE_SECONDS = 5
DEFAULT_PAGE_LOAD_TIMEOUT = 60
HARD_PAGE_LOAD_TIMEOUT_GRACE = 15

TEXT_LOG_FILE = "host_error_log.txt"
CSV_LOG_FILE = "host_error_stats.csv"
FAILURE_DIR = "failures"
HOST_ERROR_DIR = "host_error"

# If you want to watch the browser, set HEADLESS = False
HEADLESS = True

# Rolling window size for "last N attempts" stats
ROLLING_WINDOW = 50
HOURLY_HISTORY_HOURS = 24
BAR_WIDTH = 40
SPINNER_FRAMES = "|/-\\"

# =========================
# IDENTIFY THIS PROBE
# =========================
SERVER_NAME = socket.gethostname()


# =========================
# BROWSER DISCOVERY
# =========================
def find_browser_binary():
    """
    Find a usable Chrome/Chromium binary across hosts.
    Priority:
      1) $BROWSER_BINARY environment variable (if set)
      2) PATH lookup (like running `which`)
      3) common absolute locations (including snap)
    Returns a full path string, or None if not found.
    """
    env_path = os.environ.get("BROWSER_BINARY")
    if env_path and os.path.exists(env_path):
        return env_path

    candidates = [
        # Google Chrome
        "google-chrome",
        "google-chrome-stable",
        # Chromium (Debian/Ubuntu variants)
        "chromium",
        "chromium-browser",
        # Some distros use this wrapper name
        "chrome",
    ]

    for name in candidates:
        p = which(name)
        if p:
            return p

    # Fall back to common absolute locations
    absolute_candidates = [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ]
    for p in absolute_candidates:
        if os.path.exists(p):
            return p

    return None


# =========================
# HELPERS
# =========================
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def normalize_and_validate_url(raw_url):
    """
    Accepts URLs with or without scheme.
    Returns a normalized URL string or raises ValueError.
    """
    candidate = (raw_url or "").strip()
    if not candidate:
        raise ValueError("URL cannot be empty.")

    # Selenium requires an absolute URL. Default to HTTPS if omitted.
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            "Invalid URL. Use a full HTTP/HTTPS URL, e.g. "
            "'https://lockify360.com/en-US' or 'http://example.com'."
        )

    return candidate


def looks_like_host_error(title, page_source):
    text = (title + "\n" + page_source).lower()
    generic_error_checks = [
        "host error",
        "error 502",
        "error 503",
        "bad gateway",
        "web server is down",
        "connection timed out",
        "error code 5xx",
        "origin is unreachable",
        "gateway time-out",
    ]

    # "cloudflare" alone appears on many healthy pages; only treat it as an
    # error signal when paired with Cloudflare-specific error wording.
    cloudflare_error_checks = ["cloudflare ray id"]

    if any(item in text for item in generic_error_checks):
        return True

    # Match Cloudflare-style codes precisely to avoid accidental matches
    # like "error code 1000" when checking for "10".
    has_cf_error_code = re.search(r"\berror code\s*(?:10\d{2}|52\d|53\d)\b", text) is not None
    if "cloudflare" in text and (
        any(item in text for item in cloudflare_error_checks) or has_cf_error_code
    ):
        return True

    return False


def log_line(message):
    with open(TEXT_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(message + "\n")


def ensure_csv_header():
    if not os.path.exists(CSV_LOG_FILE):
        with open(CSV_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp",
                "server_name",
                "attempt",
                "status",
                "load_time_seconds",
                "title",
                "success_count",
                "failure_count",
                "consecutive_failures",
                "failure_percent_all_time",
                "avg_success_load_time_all_time",
                "min_success_load_time_all_time",
                "max_success_load_time_all_time",
                f"failure_percent_last_{ROLLING_WINDOW}",
                f"avg_success_load_time_last_{ROLLING_WINDOW}",
                f"min_success_load_time_last_{ROLLING_WINDOW}",
                f"max_success_load_time_last_{ROLLING_WINDOW}",
                "browser_binary",
                "url",
            ])


def append_csv_row(row):
    with open(CSV_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(row)


def save_failure_artifacts(driver, attempt, reason):
    target_dir = HOST_ERROR_DIR if reason == "host_error" else FAILURE_DIR
    os.makedirs(target_dir, exist_ok=True)
    stamp = safe_ts()
    base = f"{SERVER_NAME}_attempt_{attempt}_{stamp}_{reason}"

    screenshot_path = os.path.join(target_dir, base + ".png")
    html_path = os.path.join(target_dir, base + ".html")

    try:
        driver.save_screenshot(screenshot_path)
    except Exception:
        pass

    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
    except Exception:
        pass


def all_time_stats(success_count, failure_count, total_success_load, min_success, max_success):
    attempts = success_count + failure_count

    if success_count > 0:
        avg = total_success_load / float(success_count)
        avg_str = f"{avg:.2f}s"
        min_str = f"{min_success:.2f}s"
        max_str = f"{max_success:.2f}s"
    else:
        avg = None
        avg_str = "n/a"
        min_str = "n/a"
        max_str = "n/a"

    fail_pct = (100.0 * failure_count / float(attempts)) if attempts else None
    fail_pct_str = "n/a" if fail_pct is None else f"{fail_pct:.1f}%"

    summary = (
        f"all_time: success={success_count} failed={failure_count} "
        f"fail_pct={fail_pct_str} avg={avg_str} min={min_str} max={max_str}"
    )

    return avg, fail_pct, summary


def rolling_stats(window_attempts):
    """
    window_attempts: deque of dicts like:
      {"ok": bool, "load_time": float or None}
    Returns: (fail_pct, avg, min, max, summary_str)
    """
    n = len(window_attempts)
    if n == 0:
        return None, None, None, None, f"last_{ROLLING_WINDOW}: n=0"

    failures = 0
    success_times = []

    for a in window_attempts:
        if not a.get("ok", False):
            failures += 1
        else:
            lt = a.get("load_time")
            if lt is not None:
                success_times.append(lt)

    fail_pct = 100.0 * failures / float(n)

    if success_times:
        avg = sum(success_times) / float(len(success_times))
        mn = min(success_times)
        mx = max(success_times)
        avg_str = f"{avg:.2f}s"
        mn_str = f"{mn:.2f}s"
        mx_str = f"{mx:.2f}s"
    else:
        avg = mn = mx = None
        avg_str = mn_str = mx_str = "n/a"

    summary = (
        f"last_{ROLLING_WINDOW}: n={n} fail_pct={fail_pct:.1f}% "
        f"avg={avg_str} min={mn_str} max={mx_str}"
    )
    return fail_pct, avg, mn, mx, summary


def update_hourly_stats(hourly_stats, when_dt, is_failure):
    bucket = when_dt.replace(minute=0, second=0, microsecond=0)
    entry = hourly_stats.setdefault(bucket, {"attempts": 0, "failures": 0})
    entry["attempts"] += 1
    if is_failure:
        entry["failures"] += 1

    oldest_bucket = bucket - timedelta(hours=HOURLY_HISTORY_HOURS - 1)
    stale_keys = [k for k in hourly_stats if k < oldest_bucket]
    for k in stale_keys:
        del hourly_stats[k]


def safe_addstr(stdscr, y, x, text, attr=0):
    max_y, max_x = stdscr.getmaxyx()
    if y < 0 or y >= max_y or x >= max_x:
        return
    if x < 0:
        text = text[-x:]
        x = 0
    if not text:
        return
    if len(text) > (max_x - x):
        text = text[: max_x - x]
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


def init_color_pairs():
    colors = {
        "ok": 0,
        "failed": 0,
        "info": 0,
        "testing": 0,
    }
    if not curses.has_colors():
        return colors
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_CYAN, -1)
    curses.init_pair(4, curses.COLOR_YELLOW, -1)
    colors["ok"] = curses.color_pair(1)
    colors["failed"] = curses.color_pair(2)
    colors["info"] = curses.color_pair(3)
    colors["testing"] = curses.color_pair(4)
    return colors


def render_dashboard(stdscr, state):
    stdscr.erase()
    max_y, _ = stdscr.getmaxyx()
    y = 0
    colors = state.get("colors", {})
    ok_attr = colors.get("ok", 0)
    failed_attr = colors.get("failed", 0)
    info_attr = colors.get("info", 0)
    testing_attr = colors.get("testing", 0)

    safe_addstr(stdscr, y, 0, "Host Error Watch (Ctrl+C to exit)", info_attr)
    y += 1
    safe_addstr(
        stdscr,
        y,
        0,
        (
            f"target={state['url']} pause={state['pause_seconds']:.2f}s "
            f"timeout={state['page_load_timeout']:.2f}s server={SERVER_NAME}"
        ),
    )
    y += 1
    safe_addstr(stdscr, y, 0, f"last_update={state['last_update']}")
    y += 2

    safe_addstr(stdscr, y, 0, f"Failure Percent by Hour (last {HOURLY_HISTORY_HOURS}h)", info_attr)
    y += 1

    now_dt = datetime.now()
    current_bucket = now_dt.replace(minute=0, second=0, microsecond=0)
    rows = []
    for i in range(HOURLY_HISTORY_HOURS - 1, -1, -1):
        bucket = current_bucket - timedelta(hours=i)
        row = state["hourly_stats"].get(bucket)
        if not row or row.get("attempts", 0) <= 0:
            continue
        rows.append((bucket, row["attempts"], row["failures"]))

    if not rows and y < max_y:
        safe_addstr(stdscr, y, 0, "(no attempts in the last 24 hours)")
        y += 1
    else:
        for bucket, attempts, failures in rows:
            if y >= max_y:
                break
            fail_pct = 100.0 * failures / float(attempts)
            filled = int(round(BAR_WIDTH * fail_pct / 100.0))
            if filled < 0:
                filled = 0
            if filled > BAR_WIDTH:
                filled = BAR_WIDTH
            bar = "#" * filled + "-" * (BAR_WIDTH - filled)
            pct_display = f"{fail_pct:5.1f}%"
            label = bucket.strftime("%m-%d %H:00")
            safe_addstr(stdscr, y, 0, f"{label} [{bar}] {pct_display} ({failures}/{attempts})")
            y += 1

    if y < max_y:
        y += 1
        summary_all_line = state.get("summary_all", "")
        prefix = "attempts="
        totals_line = summary_all_line
        all_time_line = summary_all_line
        if summary_all_line.startswith(prefix) and " | " in summary_all_line:
            totals_line, all_time_line = summary_all_line.split(" | ", 1)
        safe_addstr(stdscr, y, 0, f"Totals: {totals_line}")
        y += 1
        if y < max_y:
            safe_addstr(stdscr, y, 0, f"All-time: {all_time_line}")
            y += 1
        if y < max_y:
            safe_addstr(stdscr, y, 0, f"Window: {state['summary_w']}")
            y += 1

    if y < max_y and state.get("is_testing"):
        safe_addstr(
            stdscr,
            y,
            0,
            f"Testing with Selenium... {state.get('spinner_frame', SPINNER_FRAMES[0])}",
            testing_attr,
        )
        y += 1

    if y < max_y:
        event_text = f"Last event: {state['last_event']}"
        status = state.get("last_event_status", "info")
        event_attr = info_attr
        if status == "ok":
            event_attr = ok_attr
        elif status == "failed":
            event_attr = failed_attr
        safe_addstr(stdscr, y, 0, event_text, event_attr)
        y += 1

    if y < max_y:
        safe_addstr(stdscr, y, 0, "Recent attempts:", info_attr)
        y += 1

    remaining = max_y - y
    if remaining > 0:
        recent_events = state.get("recent_events", [])
        tail = recent_events[-remaining:]
        for item in tail:
            if isinstance(item, dict):
                text = item.get("text", "")
                status = item.get("status", "info")
            else:
                text = str(item)
                status = "info"
            attr = info_attr
            if status == "ok":
                attr = ok_attr
            elif status == "failed":
                attr = failed_attr
            safe_addstr(stdscr, y, 0, text, attr)
            y += 1

    stdscr.refresh()


def cleanup_dir_tree(path):
    try:
        for root, dirs, files in os.walk(path, topdown=False):
            for name in files:
                try:
                    os.remove(os.path.join(root, name))
                except Exception:
                    pass
            for name in dirs:
                try:
                    os.rmdir(os.path.join(root, name))
                except Exception:
                    pass
        os.rmdir(path)
    except Exception:
        pass


def create_driver(browser_binary, page_load_timeout):
    options = Options()
    if HEADLESS:
        options.add_argument("--headless=new")

    # Stability flags for servers/containers/root sessions
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,1024")

    user_data_dir = tempfile.mkdtemp(prefix="selenium_chrome_")
    options.add_argument(f"--user-data-dir={user_data_dir}")
    options.binary_location = browser_binary

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(page_load_timeout)
    return driver, user_data_dir


def force_kill_driver_service(driver):
    try:
        service_process = getattr(getattr(driver, "service", None), "process", None)
        if service_process is None:
            return
        if service_process.poll() is None:
            service_process.kill()
    except Exception:
        pass


def run_probe(stdscr, url, pause_seconds, page_load_timeout):
    os.makedirs(HOST_ERROR_DIR, exist_ok=True)

    hourly_stats = {}
    summary_all = "all_time: success=0 failed=0 fail_pct=n/a avg=n/a min=n/a max=n/a"
    summary_w = f"last_{ROLLING_WINDOW}: n=0"
    last_event = "Initializing..."

    recent_events = deque(maxlen=500)
    recent_events.append({"text": f"[{now()}] Initializing...", "status": "info"})
    colors = {"ok": 0, "failed": 0, "info": 0, "testing": 0}

    if stdscr is not None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        colors = init_color_pairs()
        stdscr.nodelay(True)
        stdscr.timeout(0)
        render_dashboard(
            stdscr,
            {
                "url": url,
                "pause_seconds": pause_seconds,
                "page_load_timeout": page_load_timeout,
                "hourly_stats": hourly_stats,
                "summary_all": summary_all,
                "summary_w": summary_w,
                "last_event": last_event,
                "last_event_status": "info",
                "recent_events": list(recent_events),
                "is_testing": False,
                "spinner_frame": SPINNER_FRAMES[0],
                "colors": colors,
                "last_update": now(),
            },
        )

    ensure_csv_header()

    browser_binary = find_browser_binary()
    if not browser_binary:
        raise RuntimeError(
            "Could not find a Chrome/Chromium binary. Install chromium/chrome or set "
            "BROWSER_BINARY=/path/to/binary environment variable."
        )

    hard_page_load_timeout = page_load_timeout + HARD_PAGE_LOAD_TIMEOUT_GRACE
    temp_dirs = []
    active_worker = None
    stop_requested = False

    driver = None
    try:
        driver, user_data_dir = create_driver(browser_binary, page_load_timeout)
        temp_dirs.append(user_data_dir)

        attempt_count = 0
        success_count = 0
        failure_count = 0
        consecutive_failures = 0

        total_success_load = 0.0
        min_success = None
        max_success = None

        window_attempts = deque(maxlen=ROLLING_WINDOW)
        last_event = "Ready. Waiting for first attempt..."

        while True:
            if stdscr is not None:
                key = stdscr.getch()
                if key in (ord("q"), ord("Q")):
                    raise KeyboardInterrupt

            attempt_count += 1
            start_ts = time.perf_counter()
            event_time = datetime.now()

            status = "unknown"
            title = ""
            load_time = 0.0
            ok_for_window = False
            load_time_for_window = None
            is_failure = False

            if stdscr is None:
                print(f"\n[{now()}] [{SERVER_NAME}] Attempt {attempt_count}: loading {url}", flush=True)
            else:
                last_event = f"[{now()}] Attempt {attempt_count}: loading {url}"

            try:
                result = {}
                worker_exc = {}

                def _load_page():
                    try:
                        driver.get(url)
                        result["title"] = driver.title or ""
                        result["source"] = driver.page_source or ""
                    except Exception as ex:
                        worker_exc["error"] = ex

                worker = threading.Thread(target=_load_page, daemon=True)
                active_worker = worker
                worker.start()
                spinner_idx = 0
                while worker.is_alive():
                    if stdscr is not None:
                        render_dashboard(
                            stdscr,
                            {
                                "url": url,
                                "pause_seconds": pause_seconds,
                                "page_load_timeout": page_load_timeout,
                                "hourly_stats": hourly_stats,
                                "summary_all": (
                                    f"attempts={attempt_count} consecutive_failures={consecutive_failures} | {summary_all}"
                                ),
                                "summary_w": summary_w,
                                "last_event": last_event,
                                "last_event_status": "info",
                                "recent_events": list(recent_events),
                                "is_testing": True,
                                "spinner_frame": SPINNER_FRAMES[spinner_idx % len(SPINNER_FRAMES)],
                                "colors": colors,
                                "last_update": now(),
                            },
                        )
                        key = stdscr.getch()
                        if key in (ord("q"), ord("Q")):
                            stop_requested = True
                            last_event = f"[{now()}] Stop requested. Finishing current attempt..."
                    spinner_idx += 1
                    elapsed = time.perf_counter() - start_ts
                    if elapsed >= hard_page_load_timeout:
                        force_kill_driver_service(driver)
                        worker.join(timeout=1.0)
                        active_worker = None if not worker.is_alive() else worker
                        if worker.is_alive():
                            stop_requested = True
                            last_event = f"[{now()}] Stop: load worker stuck after hard timeout."
                            raise KeyboardInterrupt
                        raise WebDriverException(
                            f"hard timeout waiting for load thread after {elapsed:.2f}s"
                        )
                    time.sleep(0.1)
                worker.join()
                active_worker = None
                if "error" in worker_exc:
                    raise worker_exc["error"]

                load_time = time.perf_counter() - start_ts
                title = result.get("title", "")
                source = result.get("source", "")

                if looks_like_host_error(title, source):
                    status = "host_error"
                    is_failure = True
                    failure_count += 1
                    consecutive_failures += 1

                    event_body = f"FAILED host error - load_time={load_time:.2f}s - title={title}"
                    last_event = f"[{now()}] {event_body}"
                    recent_events.append({"text": last_event, "status": "failed"})
                    if stdscr is None:
                        print(f"[{now()}] [{SERVER_NAME}] {event_body}", flush=True)
                    log_line(f"[{now()}] [{SERVER_NAME}] {event_body}")
                    save_failure_artifacts(driver, attempt_count, "host_error")
                else:
                    status = "ok"
                    ok_for_window = True
                    load_time_for_window = load_time

                    success_count += 1
                    consecutive_failures = 0
                    total_success_load += load_time
                    if min_success is None or load_time < min_success:
                        min_success = load_time
                    if max_success is None or load_time > max_success:
                        max_success = load_time

                    last_event = f"[{now()}] OK - load_time={load_time:.2f}s - title={title}"
                    recent_events.append({"text": last_event, "status": "ok"})
                    if stdscr is None:
                        print(f"[{now()}] [{SERVER_NAME}] OK - load_time={load_time:.2f}s - title={title}", flush=True)

            except TimeoutException:
                status = "timeout"
                is_failure = True
                failure_count += 1
                consecutive_failures += 1
                load_time = time.perf_counter() - start_ts
                event_body = f"FAILED timeout after {page_load_timeout:.2f}s - elapsed={load_time:.2f}s"
                last_event = f"[{now()}] {event_body}"
                recent_events.append({"text": last_event, "status": "failed"})
                if stdscr is None:
                    print(
                        f"[{now()}] [{SERVER_NAME}] FAILED - timeout after {page_load_timeout:.2f}s "
                        f"- elapsed={load_time:.2f}s",
                        flush=True,
                    )
                log_line(f"[{now()}] [{SERVER_NAME}] {event_body}")
                save_failure_artifacts(driver, attempt_count, "timeout")

            except WebDriverException as e:
                status = "webdriver_error"
                is_failure = True
                failure_count += 1
                consecutive_failures += 1
                load_time = time.perf_counter() - start_ts
                error_line = str(e).splitlines()[0] if str(e) else "unknown webdriver error"
                event_body = f"FAILED webdriver error - elapsed={load_time:.2f}s - error={error_line}"
                last_event = f"[{now()}] {event_body}"
                recent_events.append({"text": last_event, "status": "failed"})
                if stdscr is None:
                    print(
                        f"[{now()}] [{SERVER_NAME}] FAILED - webdriver error "
                        f"- elapsed={load_time:.2f}s - error={e}",
                        flush=True,
                    )
                log_line(f"[{now()}] [{SERVER_NAME}] {event_body}")
                save_failure_artifacts(driver, attempt_count, "webdriver_error")
                # Recreate the driver/session after webdriver-level failures.
                try:
                    if driver is not None:
                        if not (active_worker is not None and active_worker.is_alive()):
                            driver.quit()
                except Exception:
                    pass
                driver, user_data_dir = create_driver(browser_binary, page_load_timeout)
                temp_dirs.append(user_data_dir)

            update_hourly_stats(hourly_stats, event_time, is_failure)
            window_attempts.append({"ok": ok_for_window, "load_time": load_time_for_window})
            avg_all, failpct_all, summary_all = all_time_stats(
                success_count, failure_count, total_success_load, min_success, max_success
            )
            failpct_w, avg_w, min_w, max_w, summary_w = rolling_stats(window_attempts)

            if stdscr is None:
                print(
                    f"[{now()}] [{SERVER_NAME}] Totals: attempts={attempt_count} "
                    f"consecutive_failures={consecutive_failures} | {summary_all} | {summary_w}",
                    flush=True,
                )
            else:
                render_dashboard(
                    stdscr,
                    {
                        "url": url,
                        "pause_seconds": pause_seconds,
                        "page_load_timeout": page_load_timeout,
                        "hourly_stats": hourly_stats,
                        "summary_all": (
                            f"attempts={attempt_count} consecutive_failures={consecutive_failures} | {summary_all}"
                        ),
                        "summary_w": summary_w,
                        "last_event": last_event,
                        "last_event_status": ("failed" if is_failure else "ok"),
                        "recent_events": list(recent_events),
                        "is_testing": False,
                        "spinner_frame": SPINNER_FRAMES[0],
                        "colors": colors,
                        "last_update": now(),
                    },
                )

            append_csv_row([
                now(),
                SERVER_NAME,
                attempt_count,
                status,
                f"{load_time:.2f}",
                title,
                success_count,
                failure_count,
                consecutive_failures,
                "" if failpct_all is None else f"{failpct_all:.1f}",
                "" if avg_all is None else f"{avg_all:.2f}",
                "" if min_success is None else f"{min_success:.2f}",
                "" if max_success is None else f"{max_success:.2f}",
                "" if failpct_w is None else f"{failpct_w:.1f}",
                "" if avg_w is None else f"{avg_w:.2f}",
                "" if min_w is None else f"{min_w:.2f}",
                "" if max_w is None else f"{max_w:.2f}",
                browser_binary,
                url,
            ])

            slept = 0.0
            while slept < pause_seconds:
                step = min(0.2, pause_seconds - slept)
                time.sleep(step)
                slept += step
                if stdscr is not None:
                    key = stdscr.getch()
                    if key in (ord("q"), ord("Q")):
                        stop_requested = True
                        break
            if stop_requested:
                raise KeyboardInterrupt

    except KeyboardInterrupt:
        if stdscr is None:
            print(f"\n[{now()}] [{SERVER_NAME}] Stopped by user.", flush=True)
    finally:
        if driver is not None:
            try:
                if not (active_worker is not None and active_worker.is_alive()):
                    driver.quit()
            except Exception:
                pass
        for d in temp_dirs:
            cleanup_dir_tree(d)


def main():
    parser = argparse.ArgumentParser(
        description="Continuously probe a URL for host errors and log results."
    )
    parser.add_argument(
        "-u",
        "--url",
        required=True,
        help="Target URL to test (accepts with or without scheme)",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=DEFAULT_PAUSE_SECONDS,
        help=f"Seconds to wait between attempts (default: {DEFAULT_PAUSE_SECONDS})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_PAGE_LOAD_TIMEOUT,
        help=f"Page-load timeout in seconds (default: {DEFAULT_PAGE_LOAD_TIMEOUT})",
    )
    args = parser.parse_args()
    try:
        url = normalize_and_validate_url(args.url)
    except ValueError as e:
        parser.error(str(e))
    if args.pause <= 0:
        parser.error("--pause must be greater than 0.")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0.")

    pause_seconds = args.pause
    page_load_timeout = args.timeout

    if sys.stdout.isatty():
        curses.wrapper(lambda stdscr: run_probe(stdscr, url, pause_seconds, page_load_timeout))
    else:
        run_probe(None, url, pause_seconds, page_load_timeout)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""DuolingoAutoStreak – macht jeden Tag zu einer zufälligen Uhrzeit eine kurze Lesson.

Unterbefehle:
  serve     Dauerbetrieb (systemd): Zeit würfeln, warten, Lesson, Wiederholungen
  run-now   sofort einen Lauf machen (auch wenn heute schon erledigt: --force)
  status    Zustand anzeigen
  resume    Pause aufheben
  plan      Zeitplan für N Tage würfeln (Trockenlauf, ändert nichts)
  explore   Erkundung: Seite, Lesson-Start und Netzwerk mitschneiden
"""
import argparse
import datetime as dt
import fcntl
import json
import logging
import logging.handlers
import os
import random
import re
import sys
import time
import tomllib
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LOGS = HERE / "logs"
DEBUG = HERE / "debug"
SESSION = DATA / "session.json"
STATE = DATA / "state.json"
CONFIG = DATA / "config.toml"
TZ = ZoneInfo("Europe/Berlin")

WINDOW_START = dt.time(3, 0)
WINDOW_END = dt.time(22, 0)
LAST_CHANCE = dt.time(23, 0)
MAX_RETRIES = 3
RETRY_GAP_MIN = (15, 75)
MAX_SCREENSHOTS = 20
VIEWPORTS = [(1366, 768), (1440, 900), (1536, 864), (1600, 900), (1920, 1080)]

log = logging.getLogger("duo")


class LoginRequired(Exception):
    """Session abgelaufen – Hofa muss sich neu einloggen."""


class SecurityCheck(Exception):
    """Captcha oder Sicherheitsabfrage – sofort aufhören."""


# ---------------------------------------------------------------- Infrastruktur

def setup_logging():
    LOGS.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    file = logging.handlers.RotatingFileHandler(LOGS / "bot.log", maxBytes=1_000_000, backupCount=5)
    file.setFormatter(fmt)
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.addHandler(file)
    log.addHandler(out)
    log.setLevel(logging.INFO)


def now():
    return dt.datetime.now(TZ)


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, default=str)
    os.replace(tmp, path)


def load_config():
    try:
        return tomllib.loads(CONFIG.read_text())
    except FileNotFoundError:
        return {}


# ---------------------------------------------------------------- Benachrichtigung

def notify(title, message, priority="default", tags="", image=None):
    topic = load_config().get("ntfy", {}).get("topic")
    if not topic:
        log.warning("Kein ntfy-Topic konfiguriert, Meldung nur im Log: %s – %s", title, message)
        return
    server = load_config()["ntfy"].get("server", "https://ntfy.sh")
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": tags}
    if image and Path(image).exists():
        headers["Filename"] = Path(image).name
        headers["Message"] = message.encode("utf-8")
        body, method = Path(image).read_bytes(), "PUT"
    else:
        body, method = message.encode("utf-8"), "POST"
    req = urllib.request.Request(f"{server}/{topic}", data=body, method=method, headers=headers)
    try:
        urllib.request.urlopen(req, timeout=30).read()
        log.info("Benachrichtigt: %s", title)
    except OSError as e:
        log.error("ntfy fehlgeschlagen: %s", e)


# ---------------------------------------------------------------- Zeitplan

def pick_time(day, avoid_minute=None, rng=random):
    """Gleichverteilte Zeit zwischen 03:00 und 22:00, auf die Sekunde, nie dieselbe Minute wie gestern."""
    start = dt.datetime.combine(day, WINDOW_START, TZ)
    span = int((dt.datetime.combine(day, WINDOW_END, TZ) - start).total_seconds())
    while True:
        t = start + dt.timedelta(seconds=rng.randrange(span))
        if t.strftime("%H:%M") != avoid_minute:
            return t


def retry_time(after, attempts_left, rng=random):
    """Nächster Versuch nach 15–75 min, gestaucht, damit alle Versuche bis 23:00 passen."""
    last = dt.datetime.combine(after.date(), LAST_CHANCE, TZ)
    room = (last - after).total_seconds() / 60
    if room < 1:
        return None
    gap = rng.uniform(*RETRY_GAP_MIN)
    gap = min(gap, room / max(attempts_left, 1))
    return after + dt.timedelta(minutes=max(gap, min(2, room)))


def plan_day(state, day):
    """Legt den Tagesplan an, falls für `day` noch keiner existiert."""
    if state.get("day") == str(day):
        return
    t = pick_time(day, state.get("planned_minute"))
    state["planned_minute"] = t.strftime("%H:%M")
    if now() > t:  # Dienst lief zur geplanten Zeit nicht (Neustart) – bald nachholen
        t = now() + dt.timedelta(seconds=random.randint(120, 900))
    state.update(day=str(day), next_run=t.isoformat(), attempts=0, done=False, gave_up=False)
    log.info("Plan für %s: %s", day, t.strftime("%H:%M:%S"))


def cmd_plan(args):
    rng = random.Random(args.seed)
    prev = None
    day = now().date()
    for _ in range(args.days):
        t = pick_time(day, prev, rng)
        retries = []
        cur = t
        for left in range(MAX_RETRIES, 0, -1):
            cur = retry_time(cur, left, rng)
            if cur is None:
                break
            retries.append(cur.strftime("%H:%M"))
        print(day, t.strftime("%H:%M:%S"), "Retries:", " ".join(retries))
        prev = t.strftime("%H:%M")
        day += dt.timedelta(days=1)


# ---------------------------------------------------------------- Browser

def human_pause(lo=0.8, hi=3.5):
    time.sleep(random.uniform(lo, hi))


def keep_screenshots():
    shots = sorted(DEBUG.glob("*.png"), key=lambda p: p.stat().st_mtime)
    for old in shots[:-MAX_SCREENSHOTS]:
        old.unlink()


def screenshot(page, label):
    DEBUG.mkdir(exist_ok=True)
    path = DEBUG / f"{now():%Y%m%d-%H%M%S}-{label}.png"
    try:
        page.screenshot(path=path)
        keep_screenshots()
        return path
    except Exception as e:  # Seite schon zu – Screenshot ist nur Beiwerk
        log.warning("Screenshot fehlgeschlagen: %s", e)
        return None


class Browser:
    """Headless-Chromium mit der gespeicherten Session; speichert sie beim Schließen zurück."""

    def __init__(self, state, headless=True):
        from playwright.sync_api import sync_playwright
        if not SESSION.exists():
            raise LoginRequired("keine Session-Datei vorhanden")
        session = load_json(SESSION, {})
        width, height = state.setdefault("viewport", list(random.choice(VIEWPORTS)))
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            headless=headless, channel="chromium",
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        self.context = self.browser.new_context(
            storage_state={"cookies": session["cookies"], "origins": session["origins"]},
            user_agent=session.get("device", {}).get("user_agent"),
            viewport={"width": width, "height": height - random.randint(70, 110)},
            locale="de-DE", timezone_id="Europe/Berlin")
        self.context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.page = self.context.new_page()
        self.session = session

    def close(self, save=True):
        try:
            if save:
                fresh = self.context.storage_state()
                if any(c["name"] == "jwt_token" for c in fresh["cookies"]):
                    fresh["device"] = self.session.get("device", {})
                    save_json(SESSION, fresh)
        finally:
            self.browser.close()
            self._pw.stop()


def move_and_click(page, locator):
    """Maus in Teilschritten mit leichtem Zittern auf einen zufälligen Punkt im Element bewegen, dann klicken."""
    locator.scroll_into_view_if_needed(timeout=5000)
    box = locator.bounding_box()
    if not box:
        locator.click(timeout=5000)
        return
    x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
    page.mouse.move(x + random.uniform(-40, 40), y + random.uniform(-30, 30), steps=random.randint(4, 9))
    page.mouse.move(x, y, steps=random.randint(6, 14))
    time.sleep(random.uniform(0.05, 0.25))
    page.mouse.click(x, y, delay=random.randint(40, 120))


def human_type(page, text):
    for ch in text:
        page.keyboard.type(ch)
        time.sleep(random.uniform(0.04, 0.18))


# ---------------------------------------------------------------- Erkennung

LOGIN_URL = re.compile(r"duolingo\.com/(log-in|login|register|\?isLoggingIn)")
SECURITY_TEXT = re.compile(r"captcha|recaptcha|hcaptcha|arkose|funcaptcha|verify you are human|"
                           r"bist du ein mensch|sicherheitsüberprüfung|unusual activity|ungewöhnliche aktivität",
                           re.I)
SECURITY_FRAMES = re.compile(r"recaptcha|hcaptcha|arkoselabs|funcaptcha|challenges\.cloudflare", re.I)


def check_blockers(page):
    if LOGIN_URL.search(page.url):
        raise LoginRequired(f"Login-Seite erschienen ({page.url})")
    if any(SECURITY_FRAMES.search(f.url or "") for f in page.frames):
        raise SecurityCheck("Captcha-Frame auf der Seite")
    try:
        body = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return
    if SECURITY_TEXT.search(body):
        raise SecurityCheck("Sicherheitsabfrage im Seitentext")


# ---------------------------------------------------------------- Erkundung

def cmd_explore(args):
    """Schneidet Seite, data-test-Attribute und JSON-Antworten mit – Grundlage für die Lesson-Logik."""
    out = DEBUG / "explore"
    out.mkdir(parents=True, exist_ok=True)
    state = load_json(STATE, {})
    b = Browser(state, headless=not args.headed)
    page = b.page
    responses = []

    def on_response(resp):
        if "duolingo.com" in resp.url and "json" in (resp.headers.get("content-type") or ""):
            entry = {"url": resp.url, "method": resp.request.method, "status": resp.status}
            try:
                body = resp.json()
                entry["keys"] = list(body)[:40] if isinstance(body, dict) else type(body).__name__
                if "/sessions" in resp.url:
                    (out / f"session-{len(responses)}.json").write_text(json.dumps(body, indent=1))
            except Exception:
                pass
            responses.append(entry)

    page.on("response", on_response)

    def dump(name):
        page.screenshot(path=out / f"{name}.png")
        tests = page.eval_on_selector_all(
            "[data-test]", "els => els.map(e => [e.getAttribute('data-test'), e.tagName, (e.innerText||'').slice(0,60)])")
        (out / f"{name}-datatest.json").write_text(json.dumps(tests, indent=0, ensure_ascii=False))
        print(f"== {name}: {page.url}  ({len(tests)} data-test)")

    try:
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        dump("01-start")
        for step in args.click:
            loc = page.locator(step).first
            loc.click(timeout=8000)
            page.wait_for_timeout(4000)
            dump(f"02-after-{re.sub(r'[^a-z0-9]+', '-', step.lower())[:40]}")
    finally:
        (out / "responses.json").write_text(json.dumps(responses, indent=1))
        b.close(save=True)
        save_json(STATE, state)


# ---------------------------------------------------------------- Lesson

def do_lesson(page):
    """Wird nach dem Erkundungslauf implementiert."""
    raise NotImplementedError


def read_streak(page):
    """Wird nach dem Erkundungslauf implementiert: (Streak-Zahl, heute erledigt?)."""
    raise NotImplementedError


def run_once(state, force=False):
    """Ein kompletter Versuch. Gibt die Streak-Zahl zurück oder wirft eine Ausnahme."""
    b = Browser(state)
    page = b.page
    shot = None
    try:
        page.goto("https://www.duolingo.com/learn", wait_until="domcontentloaded")
        page.wait_for_timeout(random.randint(4000, 7000))
        check_blockers(page)
        streak, done_today = read_streak(page)
        if done_today and not force:
            log.info("Streak heute schon erledigt (%s Tage) – überspringe", streak)
            return streak, True
        do_lesson(page)
        page.goto("https://www.duolingo.com/learn", wait_until="domcontentloaded")
        page.wait_for_timeout(random.randint(4000, 7000))
        streak, done_today = read_streak(page)
        if not done_today:
            raise RuntimeError(f"Lesson durchgelaufen, aber Streak zeigt heute nicht erledigt ({streak})")
        return streak, False
    except Exception as e:
        shot = screenshot(page, type(e).__name__)
        e.screenshot = shot
        raise
    finally:
        b.close()


# ---------------------------------------------------------------- Dienst

def attempt(state):
    """Ein Versuch samt Zustandsführung und Benachrichtigung."""
    state["attempts"] += 1
    n = state["attempts"]
    log.info("Versuch %d/%d", n, MAX_RETRIES + 1)
    try:
        streak, skipped = run_once(state)
    except LoginRequired as e:
        log.error("Login nötig: %s", e)
        state.update(gave_up=True, login_required=True, session_mtime=SESSION.stat().st_mtime if SESSION.exists() else 0)
        notify("Duolingo: neu einloggen", "Die Session ist abgelaufen. Am PC login.py ausführen (siehe README).",
               "high", "key", getattr(e, "screenshot", None))
        return
    except SecurityCheck as e:
        log.error("Sicherheitsabfrage: %s", e)
        state.update(gave_up=True, paused=True, pause_reason=str(e))
        notify("Duolingo: Captcha – Bot pausiert", f"{e}. Kein weiterer Versuch. Fortsetzen: bot.py resume",
               "urgent", "warning", getattr(e, "screenshot", None))
        return
    except Exception as e:
        log.exception("Versuch %d fehlgeschlagen", n)
        nxt = retry_time(now(), MAX_RETRIES + 1 - n) if n <= MAX_RETRIES else None
        if nxt:
            state["next_run"] = nxt.isoformat()
            notify(f"Duolingo: Versuch {n} fehlgeschlagen", f"{type(e).__name__}: {e}\nNächster Versuch {nxt:%H:%M}.",
                   "default", "x", getattr(e, "screenshot", None))
            return
        state["gave_up"] = True
        state["failed_days"] = state.get("failed_days", 0) + 1
        msg = f"Alle Versuche heute gescheitert. Letzter Fehler: {type(e).__name__}: {e}"
        if state["failed_days"] >= 2:
            state.update(paused=True, pause_reason="zwei Tage in Folge gescheitert")
            msg += "\nZwei Tage in Folge – Bot pausiert. Fortsetzen: bot.py resume"
        else:
            msg += "\nNeuer Anlauf morgen."
        notify("Duolingo: Streak in Gefahr", msg, "urgent", "rotating_light", getattr(e, "screenshot", None))
        return
    state.update(done=True, failed_days=0)
    if not skipped:
        notify("Duolingo: Streak gesichert", f"Lesson erledigt. Streak: {streak} Tage.", "low", "fire")


def cmd_serve(args):
    log.info("Dienst gestartet")
    while True:
        with open(DATA / "bot.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = load_json(STATE, {})
            if state.get("login_required") and SESSION.exists() and SESSION.stat().st_mtime != state.get("session_mtime"):
                log.info("Neue Session erkannt – Login-Sperre aufgehoben")
                state.update(login_required=False, day=None)
            plan_day(state, now().date())
            due = dt.datetime.fromisoformat(state["next_run"])
            if not (state["done"] or state["gave_up"] or state.get("paused") or state.get("login_required")) and now() >= due:
                attempt(state)
            save_json(STATE, state)
        time.sleep(min(300, max(5, (due - now()).total_seconds())) if now() < due else 300)


def cmd_run_now(args):
    with open(DATA / "bot.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_json(STATE, {})
        streak, skipped = run_once(state, force=args.force)
        print(f"Streak: {streak}" + (" (heute schon erledigt, übersprungen)" if skipped else " – Lesson erledigt"))
        if not skipped:
            state["done"] = state.get("day") == str(now().date()) or state.get("done", False)
        save_json(STATE, state)


def cmd_status(args):
    print(json.dumps(load_json(STATE, {}), indent=1, ensure_ascii=False))


def cmd_resume(args):
    state = load_json(STATE, {})
    state.update(paused=False, pause_reason=None, failed_days=0, login_required=False, day=None)
    save_json(STATE, state)
    print("Pause aufgehoben, Tagesplan wird neu gewürfelt.")


def main():
    p = argparse.ArgumentParser(description="DuolingoAutoStreak")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    r = sub.add_parser("run-now")
    r.add_argument("--force", action="store_true", help="auch wenn heute schon erledigt")
    r.set_defaults(fn=cmd_run_now)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)
    pl = sub.add_parser("plan")
    pl.add_argument("--days", type=int, default=30)
    pl.add_argument("--seed", type=int)
    pl.set_defaults(fn=cmd_plan)
    e = sub.add_parser("explore")
    e.add_argument("--url", default="https://www.duolingo.com/learn")
    e.add_argument("--click", action="append", default=[], help="Selektor nacheinander anklicken")
    e.add_argument("--headed", action="store_true")
    e.set_defaults(fn=cmd_explore)
    args = p.parse_args()
    DATA.mkdir(mode=0o700, exist_ok=True)
    setup_logging()
    args.fn(args)


if __name__ == "__main__":
    main()

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
import base64
import datetime as dt
import fcntl
import json
import logging
import logging.handlers
import os
import random
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LOGS = HERE / "logs"
DEBUG = HERE / "debug"
SESSION = DATA / "session.json"
STATE = DATA / "state.json"
SETTINGS = DATA / "settings.json"
CONTROL = DATA / "control.json"
TZ = ZoneInfo("Europe/Berlin")

# Über die Weboberfläche änderbar, gespeichert in data/settings.json
DEFAULTS = {"window_start": "03:00", "window_end": "22:00", "last_chance": "23:00", "max_retries": 3,
            "notify_success": True, "ntfy_server": "https://ntfy.sh", "ntfy_topic": "",
            "web_host": "0.0.0.0", "web_port": 8095}
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


def load_settings():
    return {**DEFAULTS, **load_json(SETTINGS, {})}


def send_control(**cmd):
    """Befehl an die laufende Hauptschleife (Weboberfläche, Kommandozeile)."""
    ctl = load_json(CONTROL, {})
    ctl.update(cmd)
    save_json(CONTROL, ctl)


# ---------------------------------------------------------------- Benachrichtigung

def notify(title, message, priority="default", tags="", image=None):
    cfg = load_settings()
    topic, server = cfg["ntfy_topic"], cfg["ntfy_server"].rstrip("/")
    if not topic:
        log.warning("Kein ntfy-Topic konfiguriert, Meldung nur im Log: %s – %s", title, message)
        return
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

def pick_time(day, cfg, avoid_minute=None, rng=random):
    """Gleichverteilte Zeit im Fenster (Standard 03:00–22:00), auf die Sekunde, nie dieselbe Minute wie gestern."""
    start = dt.datetime.combine(day, dt.time.fromisoformat(cfg["window_start"]), TZ)
    span = int((dt.datetime.combine(day, dt.time.fromisoformat(cfg["window_end"]), TZ) - start).total_seconds())
    while True:
        t = start + dt.timedelta(seconds=rng.randrange(span))
        if t.strftime("%H:%M") != avoid_minute:
            return t


def retry_time(after, attempts_left, cfg, rng=random):
    """Nächster Versuch nach 15–75 min, gestaucht, damit alle Versuche bis zur letzten Chance (23:00) passen."""
    last = dt.datetime.combine(after.date(), dt.time.fromisoformat(cfg["last_chance"]), TZ)
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
    t = pick_time(day, load_settings(), state.get("planned_minute"))
    state["planned_minute"] = t.strftime("%H:%M")
    if now() > t:  # Dienst lief zur geplanten Zeit nicht (Neustart) – bald nachholen
        t = now() + dt.timedelta(seconds=random.randint(120, 900))
    state.update(day=str(day), next_run=t.isoformat(), attempts=0, done=False, gave_up=False)
    log.info("Plan für %s: %s", day, t.strftime("%H:%M:%S"))


def cmd_plan(args):
    rng = random.Random(args.seed)
    cfg = load_settings()
    prev = None
    day = now().date()
    for _ in range(args.days):
        t = pick_time(day, cfg, prev, rng)
        retries = []
        cur = t
        for left in range(cfg["max_retries"], 0, -1):
            cur = retry_time(cur, left, cfg, rng)
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
SECURITY_TEXT = re.compile(r"verify you are human|are you a robot|press and hold|bist du ein mensch|"
                           r"sicherheitsüberprüfung|unusual activity|ungewöhnliche aktivität", re.I)
SECURITY_FRAMES = re.compile(r"recaptcha|hcaptcha|arkoselabs|funcaptcha|challenges\.cloudflare", re.I)


def check_blockers(page):
    if LOGIN_URL.search(page.url):
        raise LoginRequired(f"Login-Seite erschienen ({page.url})")
    # Duolingo lädt auf jeder Seite ein unsichtbares reCAPTCHA – nur ein sichtbares ist eine Abfrage
    frames = page.locator("iframe")
    for i in range(frames.count()):
        el = frames.nth(i)
        try:
            ident = f"{el.get_attribute('src') or ''} {el.get_attribute('title') or ''}"
            if SECURITY_FRAMES.search(ident) and el.is_visible():
                raise SecurityCheck(f"sichtbares Captcha ({ident.split('?')[0].strip()})")
        except SecurityCheck:
            raise
        except Exception:
            continue
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

LESSON_URL = "https://www.duolingo.com/lesson"
MAX_STEPS = 45
MAX_LESSON_S = 420
SESSION_API = re.compile(r"duolingo\.com/\d{4}-\d{2}-\d{2}/sessions$")
SESSION_DONE = re.compile(r"duolingo\.com/\d{4}-\d{2}-\d{2}/sessions/[A-Za-z0-9_-]+$")
CANT_DO = re.compile(r"can.?t (listen|speak|hear) now|kann gerade nicht|jetzt nicht (sprechen|zuhören|hören)", re.I)
DISMISS = re.compile(r"^\s*(no thanks|not now|maybe later|later|got it|nein,? danke|nicht jetzt|vielleicht später|"
                     r"später|verstanden)\s*$", re.I)
FINISH = re.compile(r"^\s*(continue|claim xp|weiter|xp einsammeln|ok)\s*$", re.I)
BANK = '[data-test="word-bank"] [data-test$="challenge-tap-token"]'
TEXT_OF = """e => (e.querySelector('[data-test="challenge-tap-token-text"],[data-test="challenge-judge-text"]') || e)
                 .innerText.trim().split('\\n').pop().trim()"""
TOKEN = '[data-test$="challenge-tap-token"]'

READ_CHALLENGE = """() => {
  const ch = document.querySelector('[data-test~="challenge"]');
  if (!ch) return null;
  const type = (ch.getAttribute('data-test').split(' ').find(t => t.startsWith('challenge-')) || '').slice(10);
  const txt = e => (e.querySelector('[data-test="challenge-tap-token-text"],[data-test="challenge-judge-text"]') || e)
                     .innerText.trim().split('\\n').pop();
  // Die Aufgabe samt Lösung hängt als Prop am React-Baum der Aufgabe
  const fk = Object.keys(ch).find(k => k.startsWith('__reactFiber$'));
  const solved = v => v && typeof v === 'object' && !Array.isArray(v) && v.type &&
    ['correctSolutions', 'correctIndex', 'correctIndices', 'correctTokens', 'pairs'].some(k => k in v);
  let data = null;
  for (let f = fk && ch[fk], d = 0; f && d < 40 && !data; d++, f = f.return) {
    const hit = Object.values(f.memoizedProps || {}).find(solved);
    if (hit) {
      data = {};
      for (const k of ['type', 'prompt', 'choices', 'correctIndex', 'correctIndices', 'correctTokens',
                       'correctSolutions', 'pairs']) if (k in hit) data[k] = hit[k];
      data = JSON.parse(JSON.stringify(data));
    }
  }
  return {type, data, text: ch.innerText,
          bank: [...document.querySelectorAll('[data-test="word-bank"] [data-test$="challenge-tap-token"]')].map(txt),
          tokens: [...document.querySelectorAll('[data-test$="challenge-tap-token"]')].map(txt),
          choices: [...document.querySelectorAll('[data-test="challenge-choice"]')].map(txt),
          input: !!ch.querySelector('textarea, input[type="text"]')};
}"""


def option_texts(ch):
    out = []
    for c in ch.get("choices") or []:
        out.append(c if isinstance(c, str) else c.get("text", ""))
    return out


class ChallengeBank:
    """Sammelt die Aufgaben samt Lösungen aus den Session-Antworten, die die Web-App selbst lädt."""

    def __init__(self, page):
        self.items = []
        self.submitted = False
        page.on("response", self._on_response)
        page.on("request", self._on_request)

    def _on_request(self, req):
        # Abschluss einer Lesson: die App schickt die Session an /<version>/sessions/<id>
        if req.method in ("PUT", "POST") and SESSION_DONE.search(req.url.split("?")[0]):
            self.submitted = True
            log.info("Lesson an Duolingo übermittelt (%s)", req.method)

    def _on_response(self, resp):
        if resp.request.method != "POST" or not SESSION_API.search(resp.url.split("?")[0]):
            return
        try:
            self._collect(resp.json())
        except Exception:
            pass

    def _collect(self, node):
        if isinstance(node, dict):
            if "type" in node and "id" in node and any(k in node for k in ("prompt", "choices", "pairs")):
                self.items.append(node)
            for v in node.values():
                self._collect(v)
        elif isinstance(node, list):
            for v in node:
                self._collect(v)

    def find(self, dom):
        best, best_score = None, 0
        for ch in reversed(self.items):
            if ch.get("type") != dom["type"]:
                continue
            score, opts = 0, option_texts(ch)
            if isinstance(ch.get("prompt"), str) and ch["prompt"] and ch["prompt"] in dom["text"]:
                score += 3
            if opts and dom["bank"] and sorted(opts) == sorted(dom["bank"]):
                score += 3
            if opts and dom["choices"] and sorted(opts) == sorted(dom["choices"]):
                score += 3
            words = {w for p in ch.get("pairs") or [] for w in p.values() if isinstance(w, str)}
            if words and dom["tokens"] and set(dom["tokens"]) <= words:
                score += 3
            if score > best_score:
                best, best_score = ch, score
        return best


def visible(locator):
    try:
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:
        return False


def click_text(page, selector, text):
    """Klickt das erste freie Element mit genau diesem Text (ohne Tastenkürzel-Nummer davor)."""
    loc = page.locator(selector)
    for i in range(loc.count()):
        el = loc.nth(i)
        if el.evaluate(TEXT_OF) != text:
            continue
        if el.is_visible() and el.is_enabled() and el.get_attribute("aria-disabled") != "true":
            move_and_click(page, el)
            time.sleep(random.uniform(0.4, 1.2))
            return True
    raise RuntimeError(f"Element mit Text {text!r} nicht gefunden")


def click_next(page, timeout=6000):
    btn = page.locator('[data-test="player-next"]')
    try:
        btn.wait_for(state="visible", timeout=timeout)
        deadline = time.time() + timeout / 1000
        while not btn.is_enabled() and time.time() < deadline:
            time.sleep(0.3)
        if btn.is_enabled():
            move_and_click(page, btn)
            return True
    except Exception:
        pass
    return False


def dismiss_popups(page):
    pop = page.locator('[data-test="intro-popover"]')
    if visible(pop):
        page.keyboard.press("Escape")
        time.sleep(0.5)
    buttons = page.get_by_role("button", name=DISMISS)
    if visible(buttons):
        log.info("Pop-up weggeklickt: %s", buttons.first.inner_text().strip())
        move_and_click(page, buttons.first)
        human_pause(0.6, 1.5)


def answer(page, dom, ch):
    """Löst die aktuelle Aufgabe. Gibt eine kurze Beschreibung zurück."""
    t = dom["type"]
    cant = page.get_by_role("button", name=CANT_DO)
    if visible(cant):
        move_and_click(page, cant.first)
        return "abgeschaltet (kann gerade nicht)"
    if ch is None:
        move_and_click(page, page.locator('[data-test="player-skip"]'))
        return "übersprungen (keine Lösung gefunden)"
    if ch.get("pairs"):
        for p in ch["pairs"]:
            a = p.get("learningToken") or p.get("learningWord")
            b = p.get("fromToken") or p.get("translation")
            click_text(page, TOKEN, a)
            click_text(page, TOKEN, b)
        return "Paare zugeordnet"
    if dom["bank"]:
        opts = option_texts(ch)
        order = [opts[i] for i in ch["correctIndices"]] if ch.get("correctIndices") else ch.get("correctTokens", [])
        for word in order:
            human_pause(0.3, 1.2)
            click_text(page, BANK, word)
        return "Wortbank: " + " ".join(order)
    if dom["choices"] and ch.get("correctIndex") is not None:
        target = option_texts(ch)[ch["correctIndex"]]
        click_text(page, '[data-test="challenge-choice"]', target)
        return f"Auswahl: {target}"
    if dom["input"]:
        text = (ch.get("correctSolutions") or [ch.get("prompt", "")])[0]
        move_and_click(page, page.locator('[data-test~="challenge"] textarea, [data-test~="challenge"] input[type="text"]').first)
        human_type(page, text)
        return f"getippt: {text}"
    move_and_click(page, page.locator('[data-test="player-skip"]'))
    return "übersprungen (unbekannte Form)"


def do_lesson(page, bank, done_before=False):
    """Startet die nächste Lesson und löst sie mit den Lösungen aus der Seite bzw. den Session-Daten."""
    page.goto(LESSON_URL, wait_until="domcontentloaded")
    started = time.time()
    last_id = None
    unknown = answered = 0
    for step in range(MAX_STEPS):
        if time.time() - started > MAX_LESSON_S:
            raise RuntimeError(f"Lesson nach {MAX_LESSON_S} s nicht fertig")
        page.wait_for_timeout(random.randint(1500, 2800))
        check_blockers(page)
        if not re.search(r"/(lesson|practice)", page.url):
            log.info("Lesson beendet nach %d Schritten (%s)", step, page.url)
            return
        dismiss_popups(page)
        dom = page.evaluate(READ_CHALLENGE)
        if dom and dom["type"]:
            ch = dom["data"] if dom["data"] and dom["data"].get("type") == dom["type"] else bank.find(dom)
            key = (dom["type"], dom["text"][:80])
            if key == last_id and ch is not None:
                log.warning("Aufgabe wiederholt sich – überspringe")
                ch = None
            last_id = key
            if ch is None and not visible(page.get_by_role("button", name=CANT_DO)):
                unknown += 1
                if unknown >= 3:
                    raise RuntimeError(f"Dreimal in Folge keine Lösung gefunden (zuletzt {dom['type']})")
            else:
                unknown = 0
            human_pause()
            try:
                what = answer(page, dom, ch)
            except RuntimeError as e:  # Element nicht gefunden: diese Aufgabe überspringen
                unknown += 1
                if unknown >= 3:
                    raise
                move_and_click(page, page.locator('[data-test="player-skip"]'))
                what = f"übersprungen ({e})"
            human_pause(0.5, 1.5)
            click_next(page, 3000)  # Prüfen (bei Paaren evtl. nicht nötig)
            page.wait_for_timeout(random.randint(900, 1600))
            verdict = "richtig" if visible(page.locator('[data-test*="blame-correct"]')) else \
                      "falsch" if visible(page.locator('[data-test*="blame-incorrect"]')) else "?"
            log.info("Schritt %d %s: %s → %s", step, dom["type"], what, verdict)
            answered += 1
            click_next(page, 4000)  # Weiter
            continue
        # Kein Aufgaben-Element: Zwischen- oder Abschlussbildschirm. Ist der Tag gebucht, sind wir fertig –
        # danach folgen nur noch Werbe- und Auswahlbildschirme, die wir gar nicht erst anklicken.
        if answered and (bank.submitted or (not done_before and read_streak(page)[1])):
            log.info("Lesson fertig nach %d Aufgaben", answered)
            return
        if not click_next(page, 2500):
            finish = page.get_by_role("button", name=FINISH)
            if visible(finish):
                move_and_click(page, finish.first)
            else:
                log.info("Schritt %d: nichts anklickbar (%s)", step, page.url)
    raise RuntimeError(f"Lesson nach {MAX_STEPS} Schritten nicht fertig")


STREAK_JS = """async ([id, jwt]) => {
  try {
    const r = await fetch(`/2023-05-23/users/${id}?fields=streak,streakData`,
                          {headers: {Authorization: `Bearer ${jwt}`}, credentials: 'include',
                           signal: AbortSignal.timeout(15000)});
    return [r.status, await r.text()];
  } catch (e) {
    return [0, String(e)];
  }
}"""


def read_streak(page):
    """(Streak-Zahl, heute erledigt?) über dieselbe API, die die Web-App nutzt."""
    jwt = next((c["value"] for c in page.context.cookies() if c["name"] == "jwt_token"), None)
    if not jwt:
        raise LoginRequired("kein jwt_token-Cookie")
    uid = json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))["sub"]
    status, text = page.evaluate(STREAK_JS, [uid, jwt])
    if status in (401, 403):
        raise LoginRequired(f"API antwortet {status}")
    if status == 0 and visible(page.locator('[data-test="loading-screen"]')):
        # Ungültige Session: die Web-App bleibt bei "LOADING..." und die API antwortet nie
        raise LoginRequired("Seite hängt beim Laden und die API antwortet nicht – Session ungültig")
    if status != 200:
        raise RuntimeError(f"Streak-Abfrage fehlgeschlagen ({status}: {text[:80]})")
    data = json.loads(text)
    current = (data.get("streakData") or {}).get("currentStreak") or {}
    return data.get("streak", 0), current.get("endDate") == str(now().date())


def run_once(state, force=False):
    """Ein kompletter Versuch. Gibt die Streak-Zahl zurück oder wirft eine Ausnahme."""
    b = Browser(state)
    page = b.page
    bank = ChallengeBank(page)  # vor dem ersten Aufruf, denn /learn lädt die Lessons vorab
    shot = None
    try:
        page.goto("https://www.duolingo.com/learn", wait_until="domcontentloaded")
        page.wait_for_timeout(random.randint(4000, 7000))
        check_blockers(page)
        streak, done_today = read_streak(page)
        if done_today and not force:
            log.info("Streak heute schon erledigt (%s Tage) – überspringe", streak)
            return streak, True
        do_lesson(page, bank, done_today)
        for _ in range(3):  # der Server bucht die Streak manchmal mit kurzer Verzögerung
            page.goto("https://www.duolingo.com/learn", wait_until="domcontentloaded")
            page.wait_for_timeout(random.randint(4000, 7000))
            streak, done_today = read_streak(page)
            if done_today:
                break
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

def record(state, result, detail="", streak=None):
    """Verlauf für die Weboberfläche (letzte 60 Einträge)."""
    if streak is not None:
        state["last_streak"] = streak
    state.setdefault("history", []).append(
        {"ts": now().isoformat(timespec="seconds"), "result": result, "detail": detail, "streak": streak})
    state["history"] = state["history"][-60:]


def attempt(state, force=False):
    """Ein Versuch samt Zustandsführung, Verlauf und Benachrichtigung."""
    cfg = load_settings()
    retries = int(cfg["max_retries"])
    state["attempts"] = state.get("attempts", 0) + 1
    n = state["attempts"]
    log.info("Versuch %d/%d", n, retries + 1)
    state["running"] = now().isoformat(timespec="seconds")
    save_json(STATE, state)
    try:
        streak, skipped = run_once(state, force)
    except LoginRequired as e:
        log.error("Login nötig: %s", e)
        record(state, "login", str(e))
        state.update(gave_up=True, login_required=True, session_mtime=SESSION.stat().st_mtime if SESSION.exists() else 0)
        notify("Duolingo: neu einloggen", "Die Session ist abgelaufen. Am PC login.py ausführen (siehe README).",
               "high", "key", getattr(e, "screenshot", None))
        return
    except SecurityCheck as e:
        log.error("Sicherheitsabfrage: %s", e)
        record(state, "captcha", str(e))
        state.update(gave_up=True, paused=True, pause_reason=str(e))
        notify("Duolingo: Captcha – Bot pausiert", f"{e}. Kein weiterer Versuch. Fortsetzen über die Weboberfläche.",
               "urgent", "warning", getattr(e, "screenshot", None))
        return
    except Exception as e:
        log.exception("Versuch %d fehlgeschlagen", n)
        record(state, "fehler", f"{type(e).__name__}: {e}")
        nxt = retry_time(now(), retries + 1 - n, cfg) if n <= retries else None
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
            msg += "\nZwei Tage in Folge – Bot pausiert. Fortsetzen über die Weboberfläche."
        else:
            msg += "\nNeuer Anlauf morgen."
        notify("Duolingo: Streak in Gefahr", msg, "urgent", "rotating_light", getattr(e, "screenshot", None))
        return
    finally:
        state["running"] = None
    state.update(done=True, failed_days=0)
    record(state, "übersprungen" if skipped else "ok", "heute schon erledigt" if skipped else "Lesson erledigt", streak)
    if not skipped and cfg["notify_success"]:
        notify("Duolingo: Streak gesichert", f"Lesson erledigt. Streak: {streak} Tage.", "low", "fire")


def apply_control(state):
    """Übernimmt Befehle aus data/control.json. Gibt True zurück, wenn ein Lauf angefordert ist."""
    ctl = load_json(CONTROL, None)
    if not ctl:
        return False
    CONTROL.unlink(missing_ok=True)
    if ctl.get("pause") is True:
        state.update(paused=True, pause_reason="manuell pausiert")
        log.info("Pausiert (manuell)")
    elif ctl.get("pause") is False:
        state.update(paused=False, pause_reason=None, failed_days=0, login_required=False)
        ctl["replan"] = True
        log.info("Fortgesetzt")
    if ctl.get("replan") and not state.get("done"):
        state["day"] = None  # Tagesplan mit den aktuellen Einstellungen neu würfeln
    return bool(ctl.get("run"))


def cmd_serve(args):
    import web
    wake = threading.Event()
    web.start(sys.modules[__name__], wake)
    log.info("Dienst gestartet")
    while True:
        with open(DATA / "bot.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = load_json(STATE, {})
            state["running"] = None
            run_now = apply_control(state)
            if state.get("login_required") and SESSION.exists() and SESSION.stat().st_mtime != state.get("session_mtime"):
                log.info("Neue Session erkannt – Login-Sperre aufgehoben")
                state.update(login_required=False, day=None)
            plan_day(state, now().date())
            due = dt.datetime.fromisoformat(state["next_run"])
            if run_now and not state.get("login_required"):
                log.info("Manueller Lauf angefordert")
                attempt(state, force=True)
            elif not (state["done"] or state["gave_up"] or state.get("paused") or state.get("login_required")) \
                    and now() >= due:
                attempt(state)
            save_json(STATE, state)
        wait = (due - now()).total_seconds() if now() < due else 300
        wake.wait(min(300, max(5, wait)))
        wake.clear()


def cmd_run_now(args):
    with open(DATA / "bot.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_json(STATE, {})
        streak, skipped = run_once(state, force=args.force)
        print(f"Streak: {streak}" + (" (heute schon erledigt, übersprungen)" if skipped else " – Lesson erledigt"))
        record(state, "übersprungen" if skipped else "ok", "manuell über die Kommandozeile", streak)
        if state.get("day") == str(now().date()):
            state["done"] = True
        save_json(STATE, state)


def cmd_status(args):
    cfg = load_settings()
    cfg["ntfy_topic"] = "***" if cfg["ntfy_topic"] else ""
    state = load_json(STATE, {})
    state.pop("history", None)
    print(json.dumps({"einstellungen": cfg, "zustand": state}, indent=1, ensure_ascii=False))


def cmd_resume(args):
    send_control(pause=False)
    print("Fortsetzen angefordert – der Dienst übernimmt es innerhalb von 5 Minuten (sofort nach Neustart).")


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

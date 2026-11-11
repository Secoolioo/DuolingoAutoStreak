#!/usr/bin/env python3
"""DuolingoAutoStreak – does one short lesson every day at a random time to keep the streak alive.

Commands:
  serve     long-running service (systemd): pick a time, wait, do the lesson, retry on failure
  run-now   do one run right away (use --force to run even if today is already done)
  status    show settings and state
  resume    lift a pause
  plan      dry run: print the schedule for N days (changes nothing)
  explore   record the page, data-test attributes and network traffic (for debugging the lesson logic)
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
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LOGS = HERE / "logs"
DEBUG = HERE / "debug"
ACCOUNTS = DATA / "accounts"  # one session file per logged-in account: <user id>.json
STATE = DATA / "state.json"
SETTINGS = DATA / "settings.json"
CONTROL = DATA / "control.json"

# Editable in the web UI, stored in data/settings.json
DEFAULTS = {"window_start": "03:00", "window_end": "22:00", "last_chance": "23:00", "max_retries": 3,
            "account": "", "notify_success": True, "api_fallback": True, "ntfy_server": "https://ntfy.sh", "ntfy_topic": "",
            "web_host": "0.0.0.0", "web_port": 8095}
RETRY_GAP_MIN = (15, 75)
MAX_SCREENSHOTS = 20
VIEWPORTS = [(1366, 768), (1440, 900), (1536, 864), (1600, 900), (1920, 1080)]

log = logging.getLogger("duo")


class LoginRequired(Exception):
    """Session expired – log in again with login.py."""


class SecurityCheck(Exception):
    """Captcha or security check – stop immediately."""


# ---------------------------------------------------------------- Infrastructure

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


def system_timezone():
    try:
        return str(Path("/etc/localtime").resolve()).split("zoneinfo/", 1)[1]
    except (IndexError, OSError):
        return "UTC"


# The server's time zone unless settings.json sets "timezone" (takes effect after a restart)
TZ = ZoneInfo(load_json(SETTINGS, {}).get("timezone") or system_timezone())


def now():
    return dt.datetime.now(TZ)


def load_settings():
    return {**DEFAULTS, **load_json(SETTINGS, {})}


def accounts():
    """All logged-in accounts, sorted by user name."""
    out = []
    for f in ACCOUNTS.glob("*.json"):
        meta = load_json(f, {}).get("account", {})
        out.append({"id": f.stem, "username": meta.get("username") or f.stem, "name": meta.get("name", ""), "file": f})
    return sorted(out, key=lambda a: a["username"].lower())


def active_account():
    """The account selected in the settings, or the only/first one if none is selected."""
    known = accounts()
    chosen = load_settings()["account"]
    return next((a for a in known if a["id"] == chosen), known[0] if known else None)


def send_control(**cmd):
    """Command for the running main loop (web UI, command line)."""
    ctl = load_json(CONTROL, {})
    ctl.update(cmd)
    save_json(CONTROL, ctl)


# ---------------------------------------------------------------- Notifications

def notify(title, message, priority="default", tags="", image=None):
    cfg = load_settings()
    topic, server = cfg["ntfy_topic"], cfg["ntfy_server"].rstrip("/")
    if not topic:
        log.warning("No ntfy topic configured, message only logged: %s – %s", title, message)
        return
    # Title and message go into the query string: HTTP headers must not contain line breaks
    query = urllib.parse.urlencode({"title": title, "message": message, "priority": priority, "tags": tags})
    if image and Path(image).exists():
        body, method, headers = Path(image).read_bytes(), "PUT", {"Filename": Path(image).name}
    else:
        body, method, headers = b"", "POST", {}
    req = urllib.request.Request(f"{server}/{topic}?{query}", data=body, method=method, headers=headers)
    try:
        urllib.request.urlopen(req, timeout=30).read()
        log.info("Notified: %s", title)
    except Exception as e:  # a failed notification must never stop the service
        log.error("ntfy failed: %s", e)


# ---------------------------------------------------------------- Schedule

def pick_time(day, cfg, avoid_minute=None, rng=random):
    """Uniformly random time in the window (default 03:00–22:00), to the second, never yesterday's minute."""
    start = dt.datetime.combine(day, dt.time.fromisoformat(cfg["window_start"]), TZ)
    span = int((dt.datetime.combine(day, dt.time.fromisoformat(cfg["window_end"]), TZ) - start).total_seconds())
    while True:
        t = start + dt.timedelta(seconds=rng.randrange(span))
        if t.strftime("%H:%M") != avoid_minute:
            return t


def retry_time(after, attempts_left, cfg, rng=random):
    """Next attempt after 15–75 min, squeezed so all attempts fit before the last chance (23:00)."""
    last = dt.datetime.combine(after.date(), dt.time.fromisoformat(cfg["last_chance"]), TZ)
    room = (last - after).total_seconds() / 60
    if room < 1:
        return None
    gap = rng.uniform(*RETRY_GAP_MIN)
    gap = min(gap, room / max(attempts_left, 1))
    return after + dt.timedelta(minutes=max(gap, min(2, room)))


def plan_day(state, day):
    """Creates the plan for `day` unless one exists."""
    if state.get("day") == str(day):
        return
    t = pick_time(day, load_settings(), state.get("planned_minute"))
    state["planned_minute"] = t.strftime("%H:%M")
    if now() > t:  # the service wasn't running at the planned time (reboot) – catch up soon
        t = now() + dt.timedelta(seconds=random.randint(120, 900))
    state.update(day=str(day), next_run=t.isoformat(), attempts=0, done=False, gave_up=False)
    log.info("Plan for %s: %s", day, t.strftime("%H:%M:%S"))


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
    except Exception as e:  # page already gone – the screenshot is only a bonus
        log.warning("Screenshot failed: %s", e)
        return None


class Browser:
    """Headless Chromium with the saved session; saves the refreshed session on close."""

    def __init__(self, state, headless=True):
        from playwright.sync_api import sync_playwright
        self.account = active_account()
        if not self.account:
            raise LoginRequired("no account logged in")
        session = load_json(self.account["file"], {})
        device = session.get("device", {})
        width, height = state.setdefault("viewport", list(random.choice(VIEWPORTS)))
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            headless=headless, channel="chromium",
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        self.context = self.browser.new_context(
            storage_state={"cookies": session["cookies"], "origins": session["origins"]},
            user_agent=device.get("user_agent"),
            viewport={"width": width, "height": height - random.randint(70, 110)},
            locale=device.get("locale", "en-US"), timezone_id=TZ.key)
        self.context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.page = self.context.new_page()
        self.session = session

    def close(self, save=True):
        try:
            if save:
                fresh = self.context.storage_state()
                if any(c["name"] == "jwt_token" for c in fresh["cookies"]):
                    fresh["device"] = self.session.get("device", {})
                    fresh["account"] = self.session.get("account", {})
                    save_json(self.account["file"], fresh)
        finally:
            self.browser.close()
            self._pw.stop()


def move_and_click(page, locator):
    """Moves the mouse in small, slightly jittery steps to a random point inside the element, then clicks."""
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


# ---------------------------------------------------------------- Detection

LOGIN_URL = re.compile(r"duolingo\.com/(log-in|login|register|\?isLoggingIn)")
SECURITY_TEXT = re.compile(r"verify you are human|are you a robot|press and hold|bist du ein mensch|"
                           r"sicherheitsüberprüfung|unusual activity|ungewöhnliche aktivität", re.I)
SECURITY_FRAMES = re.compile(r"recaptcha|hcaptcha|arkoselabs|funcaptcha|challenges\.cloudflare", re.I)


def check_blockers(page):
    if LOGIN_URL.search(page.url):
        raise LoginRequired(f"login page appeared ({page.url})")
    # Duolingo loads an invisible reCAPTCHA on every page – only a visible one is an actual challenge
    frames = page.locator("iframe")
    for i in range(frames.count()):
        el = frames.nth(i)
        try:
            ident = f"{el.get_attribute('src') or ''} {el.get_attribute('title') or ''}"
            if SECURITY_FRAMES.search(ident) and el.is_visible():
                raise SecurityCheck(f"visible captcha ({ident.split('?')[0].strip()})")
        except SecurityCheck:
            raise
        except Exception:
            continue
    try:
        body = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return
    if SECURITY_TEXT.search(body):
        raise SecurityCheck("security check in page text")


# ---------------------------------------------------------------- Exploration

def cmd_explore(args):
    """Records the page, data-test attributes and JSON responses – the basis for the lesson logic."""
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
TOKEN = '[data-test$="challenge-tap-token"]'
CHOICE = '[data-test="challenge-choice"]'
# Visible text of an answer button, read two ways: multi-word buttons render one line per word, and choices
# add a keyboard-shortcut digit that may sit on its own line or in the same line as the answer
OPTION_TEXT = """e => {
  const root = e.querySelector('[data-test="challenge-tap-token-text"],[data-test="challenge-judge-text"]') || e;
  const byLine = (root.innerText || '').split('\\n').map(s => s.trim()).filter(s => s && !/^\\d+$/.test(s)).join(' ');
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT), parts = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const t = n.textContent.trim();
    if (t && !/^\\d+$/.test(t)) parts.push(t);
  }
  return [byLine, parts.join(' ')];
}"""
FEEDBACK = '[data-test*="blame"]'
TRACE = None  # folder for a screenshot per step (run-now --trace)

READ_CHALLENGE = """() => {
  const ch = document.querySelector('[data-test~="challenge"]');
  if (!ch) return null;
  const type = (ch.getAttribute('data-test').split(' ').find(t => t.startsWith('challenge-')) || '').slice(10);
  const txt = e => ((e.querySelector('[data-test="challenge-tap-token-text"],[data-test="challenge-judge-text"]') || e)
                     .innerText || '').split('\\n').map(s => s.trim()).filter(s => s && !/^\\d+$/.test(s)).join(' ');
  // The challenge, including its solution, is a prop on the challenge's React tree
  const fk = Object.keys(ch).find(k => k.startsWith('__reactFiber$'));
  const solved = v => v && typeof v === 'object' && !Array.isArray(v) && v.type &&
    ['correctSolutions', 'correctIndex', 'correctIndices', 'correctTokens', 'pairs', 'displayTokens'].some(k => k in v);
  let data = null;
  for (let f = fk && ch[fk], d = 0; f && d < 40 && !data; d++, f = f.return) {
    const hit = Object.values(f.memoizedProps || {}).find(solved);
    if (hit) {
      data = {};
      for (const k of ['id', 'type', 'prompt', 'choices', 'correctIndex', 'correctIndices', 'correctTokens',
                       'correctSolutions', 'pairs', 'displayTokens']) if (k in hit) data[k] = hit[k];
      data = JSON.parse(JSON.stringify(data));
    }
  }
  return {type, data, text: ch.innerText,
          bank: [...document.querySelectorAll('[data-test="word-bank"] [data-test$="challenge-tap-token"]')].map(txt),
          tokens: [...document.querySelectorAll('[data-test$="challenge-tap-token"]')].map(txt),
          choices: [...document.querySelectorAll('[data-test="challenge-choice"]')].map(txt),
          input: !!ch.querySelector('textarea, input[type="text"]'),
          // every button of the challenge, for the log when a challenge cannot be solved
          buttons: [...ch.querySelectorAll('button, [role="button"]')].map(e =>
            (e.getAttribute('data-test') || e.tagName.toLowerCase()) + ': ' + txt(e)).slice(0, 20)};
}"""


def option_texts(ch):
    """Answer texts of the choices: plain strings, or "text" (word banks) / "phrase" (picture cards)."""
    out = []
    for c in ch.get("choices") or []:
        out.append(c if isinstance(c, str) else c.get("text") or c.get("phrase") or c.get("word") or "")
    return out


def norm(text):
    return re.sub(r"\s+", " ", text or "").strip().casefold().rstrip(".!?,;:")


def tap_order(ch):
    """Words to tap, in order: from the solution indices/tokens, the blanks of a sentence, or a single choice."""
    opts = option_texts(ch)
    if ch.get("correctIndices"):
        return [opts[i] for i in ch["correctIndices"]]
    if ch.get("correctTokens"):
        return ch["correctTokens"]
    blanks = [t["text"] for t in ch.get("displayTokens") or [] if t.get("isBlank")]
    if blanks:
        return blanks
    if ch.get("correctIndex") is not None and opts:
        return [opts[ch["correctIndex"]]]
    return []


class ChallengeBank:
    """Collects challenges and solutions from the session responses the web app loads itself."""

    def __init__(self, page):
        self.items = []
        self.sessions = []    # raw session objects from POST /sessions (for the API fallback)
        self.put_body = None  # what the app itself sends when it finishes a lesson
        self.submitted = False
        page.on("response", self._on_response)
        page.on("request", self._on_request)

    def _on_request(self, req):
        # End of a lesson: the app sends the session to /<version>/sessions/<id>
        if req.method in ("PUT", "POST") and SESSION_DONE.search(req.url.split("?")[0]):
            self.submitted = True
            self.put_body = req.post_data
            log.info("Lesson submitted to Duolingo (%s)", req.method)

    def _on_response(self, resp):
        if resp.request.method != "POST" or not SESSION_API.search(resp.url.split("?")[0]):
            return
        try:
            body = resp.json()
        except Exception:
            return
        if isinstance(body, dict) and body.get("id") and body.get("challenges"):
            self.sessions.append(body)
        self._collect(body)

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
            opts = [norm(o) for o in opts]
            if opts and dom["bank"] and sorted(opts) == sorted(map(norm, dom["bank"])):
                score += 3
            if opts and dom["choices"] and sorted(opts) == sorted(map(norm, dom["choices"])):
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


def clickable(el):
    return el.is_visible() and el.is_enabled() and el.get_attribute("aria-disabled") != "true"


def find_option(page, text, index=None, count=None):
    """The free answer button for `text`: by its data-test name, by its visible text, or by position."""
    # Prefer the word bank: tokens already placed in the answer row look the same, and clicking one removes it
    buttons = page.locator(BANK) if page.locator(BANK).count() else page.locator(f"{TOKEN}, {CHOICE}")
    n = buttons.count()
    # 1. Tap tokens carry their full text in data-test, also for several words ("gerade jetzt-challenge-tap-token")
    for i in range(n):
        el = buttons.nth(i)
        if el.get_attribute("data-test") == f"{text}-challenge-tap-token" and clickable(el):
            return el
    # 2. Normalised visible text
    want = norm(text)
    for i in range(n):
        el = buttons.nth(i)
        if want in {norm(t) for t in el.evaluate(OPTION_TEXT)} and clickable(el):
            return el
    # 2b. Any other button of the challenge with that text (some answers are not regular tap tokens)
    others = page.locator('[data-test~="challenge"] button, [data-test~="challenge"] [role="button"]')
    for i in range(others.count()):
        el = others.nth(i)
        if (el.get_attribute("data-test") or "").startswith(("player-", "quit-")):
            continue
        if want in {norm(t) for t in el.evaluate(OPTION_TEXT)} and clickable(el):
            log.info("Answer %r found among other buttons (%s)", text, el.get_attribute("data-test"))
            return el
    # 3. Position – only for single-answer challenges and only if the number of buttons matches the data
    if index is not None:
        for group in (page.locator(CHOICE), buttons):
            if group.count() == count and clickable(group.nth(index)):
                log.info("Answer %r found by position %d", text, index)
                return group.nth(index)
    raise RuntimeError(f"no button for {text!r}")


def click_option(page, text, index=None, count=None):
    move_and_click(page, find_option(page, text, index, count))
    time.sleep(random.uniform(0.4, 1.2))


def blank_groups(ch):
    """Missing words of a sentence, one entry per run of consecutive blanks."""
    groups, cur = [], []
    for t in ch.get("displayTokens") or []:
        if t.get("isBlank"):
            cur.append(t["text"])
        elif cur and t.get("text", "").strip():
            groups.append(" ".join(cur))
            cur = []
    if cur:
        groups.append(" ".join(cur))
    return groups


def type_answer(page, ch):
    """Typing challenges: one field per gap (fill in the blanks) or one field for the whole sentence."""
    fields = page.locator('[data-test~="challenge"] textarea, [data-test~="challenge"] input[type="text"]')
    gaps = blank_groups(ch)
    if gaps and fields.count() == len(gaps):
        texts = gaps
    elif gaps and fields.count() == 1:
        texts = [" ".join(gaps)]
    else:
        texts = [(ch.get("correctSolutions") or [ch.get("prompt", "")])[0]]
    for i, text in enumerate(texts):
        move_and_click(page, fields.nth(i))
        human_type(page, text)
    return "typed: " + " | ".join(texts)


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
    for i in range(buttons.count()):
        btn = buttons.nth(i)
        # Never the lesson's own controls: in German the skip button is labelled "SPÄTER" (later)
        if (btn.get_attribute("data-test") or "").startswith(("player-", "quit-")) or not btn.is_visible():
            continue
        log.info("Dismissed pop-up: %s", btn.inner_text().strip())
        move_and_click(page, btn)
        human_pause(0.6, 1.5)
        return


def answer(page, dom, ch):
    """Solves the current challenge. Returns a short description."""
    cant = page.get_by_role("button", name=CANT_DO)
    if visible(cant):
        move_and_click(page, cant.first)
        return "turned off (can't listen/speak now)"
    if ch is None:
        move_and_click(page, page.locator('[data-test="player-skip"]'))
        return "skipped (no solution found)"
    if ch.get("pairs"):
        for p in ch["pairs"]:
            click_option(page, p.get("learningToken") or p.get("learningWord"))
            click_option(page, p.get("fromToken") or p.get("translation"))
        return "matched pairs"
    has_buttons = bool(dom["bank"] or dom["tokens"] or dom["choices"])
    if dom["input"] and not has_buttons:
        return type_answer(page, ch)
    order = tap_order(ch)
    if order and has_buttons:
        single = len(order) == 1 and ch.get("correctIndex") is not None
        for word in order:
            human_pause(0.3, 1.2)
            click_option(page, word, ch["correctIndex"] if single else None, len(option_texts(ch)))
        return "tapped: " + " | ".join(order)
    if dom["input"]:
        return type_answer(page, ch)
    raise RuntimeError(f"no way to answer {dom['type']}")


def describe(ch, dom):
    """Short, URL-free summary of a challenge for the log."""
    info = {k: (ch or {}).get(k) for k in ("prompt", "correctIndex", "correctIndices", "correctTokens", "correctSolutions")
            if (ch or {}).get(k) is not None}
    info["choices"] = option_texts(ch) if ch else None
    info["blanks"] = blank_groups(ch) if ch else None
    info["buttons"] = dom.get("buttons") or dom["bank"] or dom["choices"] or dom["tokens"]
    info["input"] = dom["input"]
    return json.dumps(info, ensure_ascii=False)[:700]


def do_lesson(page, bank, done_before=False):
    """Starts the next lesson and solves it with the solutions from the page or the session data."""
    page.goto(LESSON_URL, wait_until="domcontentloaded")
    started = time.time()
    last_id = None
    unknown = answered = 0
    failures = {}    # challenge -> how often it was skipped or answered wrong
    reported = set()  # challenge types already documented with a screenshot in this run
    for step in range(MAX_STEPS):
        if time.time() - started > MAX_LESSON_S:
            raise RuntimeError(f"lesson not finished after {MAX_LESSON_S} s")
        page.wait_for_timeout(random.randint(1500, 2800))
        check_blockers(page)
        if not re.search(r"/(lesson|practice)", page.url):
            log.info("Lesson left after %d steps (%s)", step, page.url)
            return
        dismiss_popups(page)
        if visible(page.locator(FEEDBACK)):  # result of the last challenge still showing – just continue
            click_next(page, 3000)
            continue
        dom = page.evaluate(READ_CHALLENGE)
        if TRACE:
            page.screenshot(path=TRACE / f"step-{step:02d}.png")
            log.info("Trace %d: %s", step, json.dumps({k: (bool(v) if k == "data" else v) for k, v in (dom or {}).items()
                                                      if k != "text"}, ensure_ascii=False)[:300])
        if dom and dom["type"]:
            ch = dom["data"] if dom["data"] and dom["data"].get("type") == dom["type"] else bank.find(dom)
            # Repeated mistakes come back with a "previous mistake" banner, so identify by the data, not the text
            ident = (ch or {}).get("id") or (dom["type"], (ch or {}).get("prompt") or dom["text"][-80:])
            key = (dom["type"], dom["text"][:80])
            if key == last_id and ch is not None:
                log.warning("Challenge repeats itself – skipping")
                ch = None
            last_id = key
            if ch is None and not visible(page.get_by_role("button", name=CANT_DO)):
                unknown += 1
                if unknown >= 3:
                    raise RuntimeError(f"no solution found three times in a row (last: {dom['type']})")
            else:
                unknown = 0
            human_pause()
            try:
                what = answer(page, dom, ch)
            except Exception as e:  # e.g. element not found: skip just this challenge
                if dom["type"] not in reported:
                    reported.add(dom["type"])
                    screenshot(page, f"unsolved-{dom['type']}")
                    log.warning("Unsolved %s: %s", dom["type"], describe(ch, dom))
                unknown += 1
                if unknown >= 3:
                    raise
                skip = page.locator('[data-test="player-skip"]')
                if visible(skip):
                    move_and_click(page, skip)
                what = f"skipped ({str(e).splitlines()[0][:120]})"
            human_pause(0.5, 1.5)
            click_next(page, 3000)  # check (match challenges may not need it)
            page.wait_for_timeout(random.randint(900, 1600))
            verdict = "correct" if visible(page.locator('[data-test*="blame-correct"]')) else \
                      "wrong" if visible(page.locator('[data-test*="blame-incorrect"]')) else "?"
            log.info("Step %d %s: %s → %s", step, dom["type"], what, verdict)
            if verdict != "correct" and not what.startswith("turned off"):
                failures[str(ident)] = failures.get(str(ident), 0) + 1
                if failures[str(ident)] >= 2:  # Duolingo would ask it again and again – give up early
                    raise RuntimeError(f"{dom['type']} failed twice ({what})")
            answered += 1
            click_next(page, 4000)  # continue
            continue
        # No challenge: an in-between or final screen. Once the lesson is submitted we are done – what
        # follows are only promo and goal-picker screens, which we don't click at all.
        if answered and (bank.submitted or (not done_before and read_streak(page)[1])):
            log.info("Lesson finished after %d challenges", answered)
            if TRACE and bank.put_body:
                (TRACE / "put.json").write_text(bank.put_body)
                log.info("Trace: lesson PUT body saved (%d bytes)", len(bank.put_body))
            return
        if not click_next(page, 2500):
            finish = page.get_by_role("button", name=FINISH)
            if visible(finish):
                move_and_click(page, finish.first)
            else:
                log.info("Step %d: nothing to click (%s)", step, page.url)
    raise RuntimeError(f"lesson not finished after {MAX_STEPS} steps")


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
    """(streak length, done today?) via the same API the web app uses."""
    jwt = next((c["value"] for c in page.context.cookies() if c["name"] == "jwt_token"), None)
    if not jwt:
        raise LoginRequired("no jwt_token cookie")
    uid = json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))["sub"]
    status, text = page.evaluate(STREAK_JS, [uid, jwt])
    if status in (401, 403):
        raise LoginRequired(f"API answered {status}")
    if status == 0 and visible(page.locator('[data-test="loading-screen"]')):
        # Invalid session: the web app stays at "LOADING..." and the API never answers
        raise LoginRequired("page stuck loading and the API does not answer – session invalid")
    if status != 200:
        raise RuntimeError(f"streak request failed ({status}: {text[:80]})")
    data = json.loads(text)
    current = (data.get("streakData") or {}).get("currentStreak") or {}
    return data.get("streak", 0), current.get("endDate") == str(now().date())


PUT_JS = """async ([id, jwt, body]) => {
  try {
    const r = await fetch(`/2023-05-23/sessions/${id}`,
                          {method: 'PUT', credentials: 'include', body: JSON.stringify(body),
                           headers: {Authorization: `Bearer ${jwt}`, 'Content-Type': 'application/json'},
                           signal: AbortSignal.timeout(30000)});
    return [r.status, (await r.text()).slice(0, 300)];
  } catch (e) {
    return [0, String(e)];
  }
}"""
# Not part of what the app sends back when it finishes a lesson
NOT_SUBMITTED = ("adaptiveChallenges", "mistakesReplacementChallenges", "adaptiveInterleavedChallenges")


def build_completion(session):
    """A finished lesson in the shape the web app itself sends (measured from its own PUT request)."""
    done = {k: v for k, v in session.items() if k not in NOT_SUBMITTED}
    challenges = []
    for ch in session.get("challenges") or []:
        ch = dict(ch, correct=True, timeTaken=random.randint(3500, 14000))
        guess = " ".join(tap_order(ch)) or (ch.get("correctSolutions") or [None])[0]
        if guess:
            ch["guess"] = guess
        challenges.append(ch)
    end = int(time.time())
    done.update(
        challenges=challenges, heartsLeft=0, startTime=end - random.randint(180, 360), endTime=end,
        enableBonusPoints=True, failed=False, maxInLessonStreak=len(challenges), shouldLearnThings=True,
        courseId=f"DUOLINGO_{session['learningLanguage'].upper()}_{session['fromLanguage'].upper()}",
        dailyRefreshInfo=None,
        pathLevelSpecifics={"skillId": session.get("skillId"), "crownLevelIndex": session.get("levelIndex", 0),
                            "treeId": (session.get("metadata") or {}).get("skill_tree_id"), "nodeState": "active",
                            "lessonNumber": None})
    return done


def complete_via_api(page, bank):
    """Fallback: finish one of the lessons the app preloaded, through the same endpoint the app uses."""
    sessions = [x for x in bank.sessions if x.get("type") == "LESSON"] or bank.sessions
    if not sessions:
        raise RuntimeError("API fallback: no preloaded lesson")
    session = sessions[0]
    jwt = next((c["value"] for c in page.context.cookies() if c["name"] == "jwt_token"), None)
    status, text = page.evaluate(PUT_JS, [session["id"], jwt, build_completion(session)])
    if status != 200:
        raise RuntimeError(f"API fallback: server answered {status} ({text[:120]})")
    log.info("API fallback: lesson %s submitted (%d challenges)", session["id"], len(session.get("challenges") or []))


def run_once(state, force=False, fallback=False, api_only=False):
    """One complete attempt. Returns (streak, skipped, method) or raises."""
    b = Browser(state)
    page = b.page
    bank = ChallengeBank(page)  # before the first page load, because /learn preloads the lessons
    shot = None
    try:
        page.goto("https://www.duolingo.com/learn", wait_until="domcontentloaded")
        page.wait_for_timeout(random.randint(4000, 7000))
        check_blockers(page)
        streak, done_today = read_streak(page)
        if done_today and not force:
            log.info("Streak already extended today (%s days) – skipping", streak)
            return streak, True, "none"
        method = "api" if api_only else "ui"
        if not api_only:
            try:
                do_lesson(page, bank, done_today)
            except (LoginRequired, SecurityCheck):
                raise
            except Exception as e:
                if not fallback:
                    raise
                log.warning("UI lesson failed (%s) – using the API fallback", e)
                screenshot(page, "ui-failed")
                method = "api"
        if method == "api":
            page.goto("https://www.duolingo.com/learn", wait_until="domcontentloaded")
            page.wait_for_timeout(random.randint(4000, 7000))
            check_blockers(page)
            complete_via_api(page, bank)
        for _ in range(3):  # the server sometimes books the streak with a short delay
            page.goto("https://www.duolingo.com/learn", wait_until="domcontentloaded")
            page.wait_for_timeout(random.randint(4000, 7000))
            streak, done_today = read_streak(page)
            if done_today:
                break
        if not done_today:
            raise RuntimeError(f"lesson completed, but the streak is not marked as done today ({streak})")
        return streak, False, method
    except Exception as e:
        shot = screenshot(page, type(e).__name__)
        e.screenshot = shot
        raise
    finally:
        b.close()


# ---------------------------------------------------------------- Service

def record(state, result, detail="", streak=None):
    """History for the web UI (last 60 entries)."""
    if streak is not None:
        state["last_streak"] = streak
    acc = active_account()
    state.setdefault("history", []).append(
        {"ts": now().isoformat(timespec="seconds"), "result": result, "detail": detail, "streak": streak,
         "account": acc["username"] if acc else ""})
    state["history"] = state["history"][-60:]


def attempt(state, manual=False):
    """One attempt including state, history and notifications. Manual runs ("Run now") always do a lesson,
    may use the API fallback, and never count towards the day's attempts, retries or failed days."""
    cfg = load_settings()
    retries = int(cfg["max_retries"])
    if manual:
        n, last = 0, True
        log.info("Manual run")
    else:
        state["attempts"] = state.get("attempts", 0) + 1
        n = state["attempts"]
        last = n > retries
        log.info("Attempt %d/%d", n, retries + 1)
    state["running"] = now().isoformat(timespec="seconds")
    save_json(STATE, state)
    try:
        streak, skipped, method = run_once(state, force=manual, fallback=bool(cfg["api_fallback"]) and last)
    except LoginRequired as e:
        log.error("Login required: %s", e)
        record(state, "login", str(e))
        state.update(gave_up=True, login_required=True, session_mtime=session_mtime())
        notify("Duolingo: log in again", "The session has expired. Run login.py on your PC (see README).",
               "high", "key", getattr(e, "screenshot", None))
        return
    except SecurityCheck as e:
        log.error("Security check: %s", e)
        record(state, "captcha", str(e))
        state.update(gave_up=True, paused=True, pause_reason=str(e))
        notify("Duolingo: captcha – bot paused", f"{e}. No further attempts. Resume in the web UI.",
               "urgent", "warning", getattr(e, "screenshot", None))
        return
    except Exception as e:
        log.exception("Manual run failed" if manual else f"Attempt {n} failed")
        record(state, "error", ("manual run: " if manual else "") + f"{type(e).__name__}: {e}")
        if manual:
            notify("Duolingo: manual run failed", f"{type(e).__name__}: {e}", "default", "x",
                   getattr(e, "screenshot", None))
            return
        nxt = retry_time(now(), retries + 1 - n, cfg) if n <= retries else None
        if nxt:
            state["next_run"] = nxt.isoformat()
            notify(f"Duolingo: attempt {n} failed", f"{type(e).__name__}: {e}\nNext attempt at {nxt:%H:%M}.",
                   "default", "x", getattr(e, "screenshot", None))
            return
        state["gave_up"] = True
        state["failed_days"] = state.get("failed_days", 0) + 1
        msg = f"All attempts failed today. Last error: {type(e).__name__}: {e}"
        if state["failed_days"] >= 2:
            state.update(paused=True, pause_reason="failed two days in a row")
            msg += "\nTwo days in a row – bot paused. Resume in the web UI."
        else:
            msg += "\nTrying again tomorrow."
        notify("Duolingo: streak at risk", msg, "urgent", "rotating_light", getattr(e, "screenshot", None))
        return
    finally:
        state["running"] = None
    state.update(done=True, failed_days=0)
    if skipped:
        record(state, "skipped", "already done today", streak)
        return
    via = " via the API fallback" if method == "api" else ""
    record(state, "api" if method == "api" else "ok", ("manual run: " if manual else "") + "lesson done" + via, streak)
    if cfg["notify_success"] or method == "api":
        notify("Duolingo: streak saved", f"Lesson done{via}. Streak: {streak} days.", "low", "fire")


def session_mtime():
    acc = active_account()
    return acc["file"].stat().st_mtime if acc else 0


def apply_control(state):
    """Applies commands from data/control.json. Returns True if a run was requested."""
    ctl = load_json(CONTROL, None)
    if not ctl:
        return False
    CONTROL.unlink(missing_ok=True)
    if ctl.get("pause") is True:
        state.update(paused=True, pause_reason="paused manually")
        log.info("Paused (manually)")
    elif ctl.get("pause") is False:
        state.update(paused=False, pause_reason=None, failed_days=0, login_required=False)
        ctl["replan"] = True
        log.info("Resumed")
    if ctl.get("switch"):  # another account selected: today starts over for it
        state.update(day=None, done=False, gave_up=False, login_required=False, last_streak=None)
        acc = active_account()
        log.info("Account switched to %s", acc["username"] if acc else "-")
    elif ctl.get("replan") and not state.get("done"):
        state["day"] = None  # re-roll today's plan with the current settings
    return bool(ctl.get("run"))


def cmd_serve(args):
    import web
    wake = threading.Event()
    web.start(sys.modules[__name__], wake)
    log.info("Service started (time zone %s)", TZ.key)
    while True:
        with open(DATA / "bot.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = load_json(STATE, {})
            state["running"] = None
            run_now = apply_control(state)
            if state.get("login_required") and session_mtime() not in (0, state.get("session_mtime")):
                log.info("New session detected – login block lifted")
                state.update(login_required=False, day=None)
            plan_day(state, now().date())
            due = dt.datetime.fromisoformat(state["next_run"])
            if run_now and not state.get("login_required"):
                attempt(state, manual=True)
            elif not (state["done"] or state["gave_up"] or state.get("paused") or state.get("login_required")) \
                    and now() >= due:
                attempt(state)
            save_json(STATE, state)
        wait = (due - now()).total_seconds() if now() < due else 300
        wake.wait(min(300, max(5, wait)))
        wake.clear()


def cmd_run_now(args):
    global TRACE
    if args.trace:
        TRACE = DEBUG / "trace"
        TRACE.mkdir(parents=True, exist_ok=True)
    with open(DATA / "bot.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_json(STATE, {})
        streak, skipped, method = run_once(state, force=args.force, api_only=args.api_only)
        print(f"Streak: {streak}" + (" (already done today, skipped)" if skipped else f" – lesson done ({method})"))
        record(state, "skipped" if skipped else ("api" if method == "api" else "ok"),
               "manual run from the command line", streak)
        if state.get("day") == str(now().date()):
            state["done"] = True
        save_json(STATE, state)


def cmd_status(args):
    cfg = load_settings()
    cfg["ntfy_topic"] = "***" if cfg["ntfy_topic"] else ""
    state = load_json(STATE, {})
    state.pop("history", None)
    print(json.dumps({"settings": cfg, "timezone": TZ.key, "state": state}, indent=1, ensure_ascii=False))


def cmd_resume(args):
    send_control(pause=False)
    print("Resume requested – the service applies it within 5 minutes (immediately after a restart).")


def main():
    p = argparse.ArgumentParser(description="DuolingoAutoStreak")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    r = sub.add_parser("run-now")
    r.add_argument("--force", action="store_true", help="run even if today is already done")
    r.add_argument("--trace", action="store_true", help="save a screenshot per step to debug/trace/")
    r.add_argument("--api-only", action="store_true", help="skip the UI and finish a lesson via the API fallback")
    r.set_defaults(fn=cmd_run_now)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)
    pl = sub.add_parser("plan")
    pl.add_argument("--days", type=int, default=30)
    pl.add_argument("--seed", type=int)
    pl.set_defaults(fn=cmd_plan)
    e = sub.add_parser("explore")
    e.add_argument("--url", default="https://www.duolingo.com/learn")
    e.add_argument("--click", action="append", default=[], help="click these selectors one after another")
    e.add_argument("--headed", action="store_true")
    e.set_defaults(fn=cmd_explore)
    args = p.parse_args()
    DATA.mkdir(mode=0o700, exist_ok=True)
    setup_logging()
    args.fn(args)


if __name__ == "__main__":
    main()

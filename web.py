"""Minimalistische Weboberfläche für DuolingoAutoStreak: Status, Verlauf, Einstellungen.

Läuft als Thread im Dienst (bot.py serve) und ist fürs Heimnetz gedacht, ohne Login. Änderungen landen in
data/settings.json bzw. als Befehl in data/control.json; ausgeführt werden sie von der Hauptschleife.
"""
import datetime as dt
import html
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e3e3e0;--acc:#58a700;--warn:#d97706;--bad:#dc2626}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--card:#1e1e1e;--fg:#ededed;--mut:#9a9a9a;--line:#2e2e2e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:720px;margin:0 auto;padding:24px 16px 48px}h1{font-size:20px;margin:0 0 16px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 10px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
.big{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}.num{font-size:44px;font-weight:700;line-height:1}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;background:var(--mut)}
.ok{background:var(--acc)}.wait{background:var(--warn)}.bad{background:var(--bad)}
.mut{color:var(--mut)}.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}form{margin:0}
button{font:inherit;border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:8px;padding:7px 14px;cursor:pointer}
button.pri{background:var(--acc);border-color:var(--acc);color:#fff}
table{width:100%;border-collapse:collapse;font-size:14px}td{padding:6px 4px;border-top:1px solid var(--line);vertical-align:top}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
label{display:block;font-size:13px;color:var(--mut)}input{font:inherit;width:100%;padding:6px 8px;border:1px solid var(--line);
border-radius:8px;background:var(--bg);color:var(--fg)}input[type=checkbox]{width:auto}
pre{font-size:12px;overflow-x:auto;white-space:pre-wrap;margin:0}.msg{padding:8px 12px;border-radius:8px;background:var(--line);margin-bottom:14px}
"""

RESULT = {"ok": ("ok", "Lesson erledigt"), "übersprungen": ("ok", "Schon erledigt"), "fehler": ("bad", "Fehler"),
          "login": ("bad", "Login nötig"), "captcha": ("bad", "Captcha")}
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
TOPIC = re.compile(r"^[A-Za-z0-9_-]{0,64}$")


def esc(value):
    return html.escape(str(value if value is not None else ""))


def fmt(ts):
    try:
        return dt.datetime.fromisoformat(ts).strftime("%d.%m. %H:%M")
    except (TypeError, ValueError):
        return "–"


class Handler(BaseHTTPRequestHandler):
    bot = None   # bot-Modul, gesetzt in start()
    wake = None  # threading.Event der Hauptschleife

    def log_message(self, *args):
        pass

    # ------------------------------------------------------------ Ausgabe

    def send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, msg=""):
        self.send_response(303)
        self.send_header("Location", "/" + (f"?msg={quote(msg)}" if msg else ""))
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            msg = parse_qs(url.query).get("msg", [""])[0]
            self.send(200, self.page(msg))
        elif url.path == "/screenshot":
            shots = sorted(self.bot.DEBUG.glob("*.png"), key=lambda p: p.stat().st_mtime)
            if shots:
                self.send(200, shots[-1].read_bytes(), "image/png")
            else:
                self.send(404, "Kein Screenshot vorhanden")
        else:
            self.send(404, "Nicht gefunden")

    # ------------------------------------------------------------ Aktionen

    def do_POST(self):
        # Schutz gegen fremde Webseiten, die Formulare an diese Adresse schicken
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self.send(403, "Fremde Herkunft abgelehnt")
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        action = urlparse(self.path).path.strip("/")
        b = self.bot
        if action == "run":
            b.send_control(run=True)
            msg = "Lauf angefordert – dauert ein paar Minuten"
        elif action == "pause":
            b.send_control(pause=True)
            msg = "Pausiert"
        elif action == "resume":
            b.send_control(pause=False)
            msg = "Fortgesetzt"
        elif action == "test":
            b.notify("Duolingo-Bot: Test", "Benachrichtigungen kommen an.", "default", "bell")
            msg = "Test-Benachrichtigung gesendet" if b.load_settings()["ntfy_topic"] else "Kein ntfy-Topic eingetragen"
        elif action == "settings":
            msg = self.save_settings(form)
        else:
            self.send(404, "Unbekannte Aktion")
            return
        self.wake.set()
        self.redirect(msg)

    def save_settings(self, form):
        b = self.bot
        cfg = b.load_json(b.SETTINGS, {})
        times = {k: form.get(k, "").strip() for k in ("window_start", "window_end", "last_chance")}
        if not all(HHMM.match(v) for v in times.values()):
            return "Uhrzeiten bitte als HH:MM"
        if not times["window_start"] < times["window_end"] <= times["last_chance"]:
            return "Es muss gelten: Fensterbeginn < Fensterende ≤ letzte Chance"
        try:
            retries = int(form.get("max_retries", "3"))
        except ValueError:
            retries = -1
        if not 0 <= retries <= 5:
            return "Wiederholungen: 0 bis 5"
        topic = form.get("ntfy_topic", "").strip()
        if topic != "***" and not TOPIC.match(topic):
            return "ntfy-Topic: nur Buchstaben, Ziffern, - und _"
        cfg.update(times, max_retries=retries, notify_success="notify_success" in form)
        if topic != "***":
            cfg["ntfy_topic"] = topic
        b.save_json(b.SETTINGS, cfg)
        b.send_control(replan=True)
        b.log.info("Einstellungen geändert über die Weboberfläche")
        return "Gespeichert – der heutige Plan wird neu gewürfelt, falls noch nicht erledigt"

    # ------------------------------------------------------------ Seite

    def status(self, st):
        if st.get("running"):
            return "wait", "Läuft gerade …"
        if st.get("login_required"):
            return "bad", "Neu einloggen nötig (login.py am PC)"
        if st.get("paused"):
            return "bad", f"Pausiert: {st.get('pause_reason') or ''}"
        if st.get("done"):
            return "ok", "Heute erledigt"
        if st.get("gave_up"):
            return "bad", "Heute gescheitert – nächster Anlauf morgen"
        return "wait", f"Geplant für {fmt(st.get('next_run'))[-5:]} Uhr"

    def page(self, msg):
        b = self.bot
        st, cfg = b.load_json(b.STATE, {}), b.load_settings()
        cls, text = self.status(st)
        tries = f"{st.get('attempts', 0)} von {int(cfg['max_retries']) + 1} Versuchen heute"
        pause_btn = ('<form method=post action=/resume><button>Fortsetzen</button></form>' if st.get("paused")
                     else '<form method=post action=/pause><button>Pausieren</button></form>')
        rows = "".join(
            f"<tr><td class=mut>{fmt(h['ts'])}</td><td><span class='dot {RESULT.get(h['result'], ('', ''))[0]}'></span>"
            f"{esc(RESULT.get(h['result'], ('', h['result']))[1])}</td><td>{esc(h.get('streak') if h.get('streak') is not None else '')}</td>"
            f"<td class=mut>{esc(h.get('detail'))}</td></tr>"
            for h in reversed(st.get("history", [])[-15:])) or "<tr><td class=mut>Noch keine Läufe</td></tr>"
        try:
            log_tail = "\n".join((b.LOGS / "bot.log").read_text(errors="replace").splitlines()[-30:])
        except FileNotFoundError:
            log_tail = ""
        shot = '<a href=/screenshot target=_blank>letzter Fehler-Screenshot</a>' if any(b.DEBUG.glob("*.png")) else ""
        topic = "***" if cfg["ntfy_topic"] else ""
        checked = "checked" if cfg["notify_success"] else ""
        return f"""<!doctype html><html lang=de><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Duolingo-Streak-Bot</title><style>{CSS}</style></head>
<body><main><h1>Duolingo-Streak-Bot</h1>{f'<div class=msg>{esc(msg)}</div>' if msg else ''}
<section><h2>Status</h2><div class=big><span class=num>{esc(st.get('last_streak', '–'))}</span>
<span class=mut>Tage Streak</span></div>
<p><span class='dot {cls}'></span>{esc(text)}<br><span class=mut>{esc(tries)} · {shot}</span></p>
<div class=row><form method=post action=/run><button class=pri>Jetzt ausführen</button></form>{pause_btn}
<form method=post action=/test><button>Test-Benachrichtigung</button></form>
<a href=/><button type=button>Aktualisieren</button></a></div></section>
<section><h2>Verlauf</h2><table>{rows}</table></section>
<section><h2>Einstellungen</h2><form method=post action=/settings><div class=grid>
<div><label>Fenster ab</label><input name=window_start value="{esc(cfg['window_start'])}"></div>
<div><label>Fenster bis</label><input name=window_end value="{esc(cfg['window_end'])}"></div>
<div><label>Letzte Chance</label><input name=last_chance value="{esc(cfg['last_chance'])}"></div>
<div><label>Wiederholungen</label><input name=max_retries type=number min=0 max=5 value="{esc(cfg['max_retries'])}"></div>
<div><label>ntfy-Topic</label><input name=ntfy_topic value="{topic}" autocomplete=off></div>
<div><label>Erfolg melden</label><input type=checkbox name=notify_success {checked}></div></div>
<div class=row><button class=pri>Speichern</button></div></form></section>
<section><details><summary class=mut>Log (letzte 30 Zeilen)</summary><pre>{esc(log_tail)}</pre></details></section>
</main></body></html>"""


def start(bot, wake):
    """Startet die Weboberfläche als Hintergrund-Thread."""
    cfg = bot.load_settings()
    handler = type("BoundHandler", (Handler,), {"bot": bot, "wake": wake})
    server = ThreadingHTTPServer((cfg["web_host"], int(cfg["web_port"])), handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    bot.log.info("Weboberfläche auf Port %s", cfg["web_port"])

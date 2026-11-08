"""Minimal web UI for DuolingoAutoStreak: status, account, history, settings.

Runs as a thread inside the service (bot.py serve) and is meant for a home network – there is no login.
Changes go to data/settings.json or, as a command, to data/control.json; the main loop carries them out.
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
.mut{color:var(--mut)}a{color:var(--acc)}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px}form{margin:0}
button{font:inherit;border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:8px;padding:7px 14px;cursor:pointer}
button.pri{background:var(--acc);border-color:var(--acc);color:#fff}
table{width:100%;border-collapse:collapse;font-size:14px}td{padding:6px 4px;border-top:1px solid var(--line);vertical-align:middle}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
label{display:block;font-size:13px;color:var(--mut)}input{font:inherit;width:100%;padding:6px 8px;border:1px solid var(--line);
border-radius:8px;background:var(--bg);color:var(--fg)}input[type=checkbox]{width:auto}
.tag{font-size:12px;border-radius:6px;padding:2px 8px;background:var(--acc);color:#fff}.right{text-align:right;white-space:nowrap}
pre{font-size:12px;overflow-x:auto;white-space:pre-wrap;margin:0}.msg{padding:8px 12px;border-radius:8px;background:var(--line);margin-bottom:14px}
"""

RESULT = {"ok": ("ok", "Lesson done"), "skipped": ("ok", "Already done"), "error": ("bad", "Error"),
          "login": ("bad", "Login needed"), "captcha": ("bad", "Captcha")}
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
    bot = None   # the bot module, set in start()
    wake = None  # threading.Event of the main loop

    def log_message(self, *args):
        pass

    # ------------------------------------------------------------ Output

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
        elif url.path == "/ntfy":
            cfg = self.bot.load_settings()
            topic, server = cfg["ntfy_topic"], cfg["ntfy_server"].rstrip("/")
            body = (f"<p>1. Install the “ntfy” app from the App Store or Play Store.</p>"
                    f"<p>2. Tap + in the app and subscribe to this topic:</p><p><b>{esc(topic)}</b></p>"
                    f"<p>Or open it directly: <a href='{esc(server)}/{esc(topic)}'>{esc(server)}/{esc(topic)}</a></p>"
                    if topic else "<p>No topic set yet.</p>")
            self.send(200, f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                           f"<title>Subscribe to ntfy</title><style>{CSS}</style><main><h1>Notifications on your phone</h1>"
                           f"<section>{body}</section><a href=/>back</a></main>")
        elif url.path == "/screenshot":
            shots = sorted(self.bot.DEBUG.glob("*.png"), key=lambda p: p.stat().st_mtime)
            if shots:
                self.send(200, shots[-1].read_bytes(), "image/png")
            else:
                self.send(404, "No screenshot yet")
        else:
            self.send(404, "Not found")

    # ------------------------------------------------------------ Actions

    def do_POST(self):
        # Blocks foreign websites from submitting forms to this address
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self.send(403, "Foreign origin rejected")
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        action = urlparse(self.path).path.strip("/")
        b = self.bot
        if action == "run":
            b.send_control(run=True)
            msg = "Run requested – takes a few minutes"
        elif action == "pause":
            b.send_control(pause=True)
            msg = "Paused"
        elif action == "resume":
            b.send_control(pause=False)
            msg = "Resumed"
        elif action == "test":
            b.notify("Duolingo bot: test", "Notifications are working.", "default", "bell")
            msg = "Test notification sent" if b.load_settings()["ntfy_topic"] else "No ntfy topic set"
        elif action == "account":
            msg = self.select_account(form.get("id", ""))
        elif action == "account/remove":
            msg = self.remove_account(form.get("id", ""))
        elif action == "settings":
            msg = self.save_settings(form)
        else:
            self.send(404, "Unknown action")
            return
        self.wake.set()
        self.redirect(msg)

    def select_account(self, account_id):
        b = self.bot
        acc = next((a for a in b.accounts() if a["id"] == account_id), None)
        if not acc:
            return "Unknown account"
        cfg = b.load_json(b.SETTINGS, {})
        cfg["account"] = acc["id"]
        b.save_json(b.SETTINGS, cfg)
        b.send_control(switch=True)
        return f"Now using {acc['username']} – today's plan is rolled again for this account"

    def remove_account(self, account_id):
        b = self.bot
        acc = next((a for a in b.accounts() if a["id"] == account_id), None)
        if not acc:
            return "Unknown account"
        was_active = b.active_account()["id"] == acc["id"]
        acc["file"].unlink()
        if was_active:
            b.send_control(switch=True)
        b.log.info("Account %s removed in the web UI", acc["username"])
        return f"Removed {acc['username']}"

    def save_settings(self, form):
        b = self.bot
        cfg = b.load_json(b.SETTINGS, {})
        times = {k: form.get(k, "").strip() for k in ("window_start", "window_end", "last_chance")}
        if not all(HHMM.match(v) for v in times.values()):
            return "Times must be HH:MM"
        if not times["window_start"] < times["window_end"] <= times["last_chance"]:
            return "Window start < window end ≤ last chance"
        try:
            retries = int(form.get("max_retries", "3"))
        except ValueError:
            retries = -1
        if not 0 <= retries <= 5:
            return "Retries: 0 to 5"
        topic = form.get("ntfy_topic", "").strip()
        if topic != "***" and not TOPIC.match(topic):
            return "ntfy topic: letters, digits, - and _ only"
        cfg.update(times, max_retries=retries, notify_success="notify_success" in form)
        if topic != "***":
            cfg["ntfy_topic"] = topic
        b.save_json(b.SETTINGS, cfg)
        b.send_control(replan=True)
        b.log.info("Settings changed in the web UI")
        return "Saved – today's plan is rolled again unless it is already done"

    # ------------------------------------------------------------ Page

    def status(self, st):
        if st.get("running"):
            return "wait", "Running …"
        if st.get("login_required"):
            return "bad", "Log in again (login.py on your PC)"
        if st.get("paused"):
            return "bad", f"Paused: {st.get('pause_reason') or ''}"
        if st.get("done"):
            return "ok", "Done today"
        if st.get("gave_up"):
            return "bad", "Failed today – trying again tomorrow"
        return "wait", f"Planned for {fmt(st.get('next_run'))[-5:]}"

    def accounts_html(self, active):
        rows = []
        for a in self.bot.accounts():
            is_active = active and a["id"] == active["id"]
            use = ("<span class=tag>active</span>" if is_active else
                   f"<form method=post action=/account><input type=hidden name=id value='{esc(a['id'])}'>"
                   f"<button class=pri>Use</button></form>")
            remove = (f"<form method=post action=/account/remove onsubmit=\"return confirm('Remove {esc(a['username'])}?')\">"
                      f"<input type=hidden name=id value='{esc(a['id'])}'><button>Remove</button></form>")
            rows.append(f"<tr><td><b>{esc(a['username'])}</b> <span class=mut>{esc(a['name'])}</span></td>"
                        f"<td class=right><div class=row style='margin:0;justify-content:flex-end'>{use}{remove}</div></td></tr>")
        table = f"<table>{''.join(rows)}</table>" if rows else "<p class=mut>No account logged in yet.</p>"
        return table + "<p class=mut>Add an account: run <code>login.py</code> on your PC and log in (see README).</p>"

    def page(self, msg):
        b = self.bot
        st, cfg, active = b.load_json(b.STATE, {}), b.load_settings(), b.active_account()
        cls, text = self.status(st)
        tries = f"{st.get('attempts', 0)} of {int(cfg['max_retries']) + 1} attempts today"
        pause_btn = ('<form method=post action=/resume><button>Resume</button></form>' if st.get("paused")
                     else '<form method=post action=/pause><button>Pause</button></form>')
        rows = "".join(
            f"<tr><td class=mut>{fmt(h['ts'])}</td><td><span class='dot {RESULT.get(h['result'], ('', ''))[0]}'></span>"
            f"{esc(RESULT.get(h['result'], ('', h['result']))[1])}</td><td>{esc(h.get('streak') if h.get('streak') is not None else '')}</td>"
            f"<td class=mut>{esc(h.get('account', ''))} {esc(h.get('detail'))}</td></tr>"
            for h in reversed(st.get("history", [])[-15:])) or "<tr><td class=mut>No runs yet</td></tr>"
        try:
            log_tail = "\n".join((b.LOGS / "bot.log").read_text(errors="replace").splitlines()[-30:])
        except FileNotFoundError:
            log_tail = ""
        shot = '<a href=/screenshot target=_blank>last error screenshot</a>' if any(b.DEBUG.glob("*.png")) else ""
        topic = "***" if cfg["ntfy_topic"] else ""
        checked = "checked" if cfg["notify_success"] else ""
        who = esc(active["username"]) if active else "no account"
        streak = st.get("last_streak")
        return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Duolingo Streak Bot</title><style>{CSS}</style></head>
<body><main><h1>Duolingo Streak Bot</h1>{f'<div class=msg>{esc(msg)}</div>' if msg else ''}
<section><h2>Status · {who}</h2><div class=big><span class=num>{esc(streak if streak is not None else '–')}</span>
<span class=mut>day streak</span></div>
<p><span class='dot {cls}'></span>{esc(text)}<br><span class=mut>{esc(tries)} · {shot}</span></p>
<div class=row><form method=post action=/run><button class=pri>Run now</button></form>{pause_btn}
<form method=post action=/test><button>Test notification</button></form>
<a href=/><button type=button>Refresh</button></a></div></section>
<section><h2>Account</h2>{self.accounts_html(active)}</section>
<section><h2>History</h2><table>{rows}</table></section>
<section><h2>Settings</h2><form method=post action=/settings><div class=grid>
<div><label>Window from</label><input name=window_start value="{esc(cfg['window_start'])}"></div>
<div><label>Window until</label><input name=window_end value="{esc(cfg['window_end'])}"></div>
<div><label>Last chance</label><input name=last_chance value="{esc(cfg['last_chance'])}"></div>
<div><label>Retries</label><input name=max_retries type=number min=0 max=5 value="{esc(cfg['max_retries'])}"></div>
<div><label>ntfy topic</label><input name=ntfy_topic value="{topic}" autocomplete=off></div>
<div><label>Notify on success</label><input type=checkbox name=notify_success {checked}></div></div>
<div class=row><button class=pri>Save</button><a href=/ntfy><button type=button>Subscribe on phone</button></a></div></form></section>
<section><details><summary class=mut>Log (last 30 lines)</summary><pre>{esc(log_tail)}</pre></details></section>
</main></body></html>"""


def start(bot, wake):
    """Starts the web UI as a background thread."""
    cfg = bot.load_settings()
    handler = type("BoundHandler", (Handler,), {"bot": bot, "wake": wake})
    server = ThreadingHTTPServer((cfg["web_host"], int(cfg["web_port"])), handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    bot.log.info("Web UI on port %s", cfg["web_port"])

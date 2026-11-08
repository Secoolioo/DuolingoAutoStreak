#!/usr/bin/env python3
"""Einmaliger Duolingo-Login auf dem Desktop-PC.

Startet ein normales Chromium-Fenster ohne Automations-Flags (damit auch
"Mit Google anmelden" funktioniert), wartet, bis du eingeloggt bist, liest
erst dann die Session (Cookies + Local Storage) per CDP aus und kopiert sie
per scp auf den Server. Aufruf:  .venv/bin/python login.py
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
LOGGED_IN_URL = re.compile(r"^https://www\.duolingo\.com/(learn|home|lesson|practice-hub|leaderboard|quests|profile|shop)")
TIMEOUT_S = 15 * 60


def load_env(path):
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = os.path.expanduser(value.strip().strip('"'))
    return env


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def page_urls(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
            return [t.get("url", "") for t in json.load(r) if t.get("type") == "page"]
    except OSError:
        return []


def wait_for_login(proc, port):
    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            sys.exit("Fenster wurde geschlossen, bevor der Login fertig war.")
        if any(LOGGED_IN_URL.match(u) for u in page_urls(port)):
            time.sleep(4)  # Weiterleitungen und Cookies fertig schreiben lassen
            return
        time.sleep(2)
    sys.exit("Kein Login innerhalb von 15 Minuten erkannt.")


def export_session(port):
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        context = browser.contexts[0]
        state = context.storage_state()
        if not any(c["name"] == "jwt_token" for c in state["cookies"]):
            sys.exit("Eingeloggt-Seite erkannt, aber kein jwt_token-Cookie gefunden.")
        page = next(pg for pg in context.pages if "duolingo.com" in pg.url)
        local = page.evaluate("() => Object.entries(localStorage).map(([name, value]) => ({name, value}))")
        state["origins"] = [o for o in state["origins"] if o["origin"] != "https://www.duolingo.com"]
        state["origins"].append({"origin": "https://www.duolingo.com", "localStorage": local})
        state["device"] = {"user_agent": page.evaluate("() => navigator.userAgent")}
        browser.close()
    return state


def upload(state_file, env):
    host = env.get("DEPLOY_HOST")
    if not host:
        print(f"Kein DEPLOY_HOST in deploy.env – Session nur lokal unter {state_file}.")
        return
    user, target = env.get("DEPLOY_USER", "root"), env["DEPLOY_DIR"]
    key = ["-i", env["SSH_KEY"]] if env.get("SSH_KEY") else []
    remote = f"{target}/data/session.json"
    subprocess.run(["scp", "-q", *key, str(state_file), f"{user}@{host}:{remote}.new"], check=True)
    subprocess.run(["ssh", *key, f"{user}@{host}",
                    f"chown duobot:duobot {remote}.new && chmod 600 {remote}.new && mv {remote}.new {remote}"],
                   check=True)
    print(f"Session nach {host}:{remote} übertragen.")


def main():
    env = load_env(HERE / "deploy.env")
    with sync_playwright() as p:
        chromium = p.chromium.executable_path
    profile = tempfile.mkdtemp(prefix="duo-login-")
    port = free_port()
    proc = subprocess.Popen(
        [chromium, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
         "--no-first-run", "--no-default-browser-check", "--password-store=basic",
         "https://www.duolingo.com/log-in"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        print("Bitte im geöffneten Fenster bei Duolingo anmelden …")
        wait_for_login(proc, port)
        state = export_session(port)
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        shutil.rmtree(profile, ignore_errors=True)

    data = HERE / "data"
    data.mkdir(mode=0o700, exist_ok=True)
    state_file = data / "session.json"
    fd = os.open(state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    print(f"Login erkannt, {len(state['cookies'])} Cookies gesichert.")
    upload(state_file, env)


if __name__ == "__main__":
    main()

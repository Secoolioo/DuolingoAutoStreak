#!/usr/bin/env python3
"""One-time Duolingo login on your desktop PC – adds an account to the bot.

Opens a normal Chromium window without automation flags (so "Sign in with Google" works too), waits until
you are logged in, only then reads the session (cookies + local storage) via CDP and copies it to the server
as data/accounts/<user id>.json. Select the account afterwards in the web UI. Run:  .venv/bin/python login.py
"""
import base64
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
ACCOUNT_JS = """async ([id, jwt]) => {
  const r = await fetch(`/2023-05-23/users/${id}?fields=username,name`, {headers: {Authorization: `Bearer ${jwt}`}});
  return r.ok ? await r.json() : {};
}"""


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
            sys.exit("The window was closed before the login finished.")
        if any(LOGGED_IN_URL.match(u) for u in page_urls(port)):
            time.sleep(4)  # let redirects and cookies settle
            return
        time.sleep(2)
    sys.exit("No login detected within 15 minutes.")


def export_session(port):
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        context = browser.contexts[0]
        state = context.storage_state()
        jwt = next((c["value"] for c in state["cookies"] if c["name"] == "jwt_token"), None)
        if not jwt:
            sys.exit("Logged-in page detected, but no jwt_token cookie found.")
        page = next(pg for pg in context.pages if "duolingo.com" in pg.url)
        local = page.evaluate("() => Object.entries(localStorage).map(([name, value]) => ({name, value}))")
        state["origins"] = [o for o in state["origins"] if o["origin"] != "https://www.duolingo.com"]
        state["origins"].append({"origin": "https://www.duolingo.com", "localStorage": local})
        state["device"] = page.evaluate("() => ({user_agent: navigator.userAgent, locale: navigator.language})")
        uid = str(json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))["sub"])
        info = page.evaluate(ACCOUNT_JS, [uid, jwt])
        state["account"] = {"id": uid, "username": info.get("username", ""), "name": info.get("name", "")}
        browser.close()
    return state


def upload(state_file, account_id, env):
    user, target = env.get("DEPLOY_USER", "root"), env["DEPLOY_DIR"]
    key = ["-i", env["SSH_KEY"]] if env.get("SSH_KEY") else []
    accounts = f"{target}/data/accounts"
    remote = f"{accounts}/{account_id}.json"
    host = f"{user}@{env['DEPLOY_HOST']}"
    subprocess.run(["ssh", *key, host, f"install -d -m 700 -o duobot -g duobot {accounts}"], check=True)
    subprocess.run(["scp", "-q", *key, str(state_file), f"{host}:{remote}.new"], check=True)
    subprocess.run(["ssh", *key, host,
                    f"chown duobot:duobot {remote}.new && chmod 600 {remote}.new && mv {remote}.new {remote}"],
                   check=True)
    print(f"Session copied to {env['DEPLOY_HOST']}:{remote}.")


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
        print("Please log in to Duolingo in the window that just opened …", flush=True)
        wait_for_login(proc, port)
        state = export_session(port)
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        shutil.rmtree(profile, ignore_errors=True)

    acc = state["account"]
    print(f"Logged in as {acc['username']} ({acc['name']}), {len(state['cookies'])} cookies saved.", flush=True)
    if env.get("DEPLOY_HOST"):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / f"{acc['id']}.json"
            fd = os.open(state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(state, f)
            upload(state_file, acc["id"], env)
        print("Now select this account in the web UI (port 8095 on the server).")
    else:
        accounts = HERE / "data" / "accounts"
        accounts.mkdir(parents=True, mode=0o700, exist_ok=True)
        state_file = accounts / f"{acc['id']}.json"
        fd = os.open(state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        print(f"No DEPLOY_HOST in deploy.env – session saved locally to {state_file}.")


if __name__ == "__main__":
    main()

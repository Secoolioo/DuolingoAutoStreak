# DuolingoAutoStreak

Hält eine Duolingo-Streak automatisch: Ein Python/Playwright-Dienst auf einem Linux-Server macht jeden Tag zu einer zufälligen Uhrzeit eine kurze Lesson im gerade aktiven Kurs. Das Ergebnis kommt per [ntfy](https://ntfy.sh) aufs Handy, eine kleine Weboberfläche zeigt Status und Verlauf.

> Duolingos Nutzungsbedingungen verbieten Automatisierung. Nutzung auf eigenes Risiko.

## So funktioniert es

- **Zeitplan:** Jeden Tag wird eine neue, gleichverteilte Uhrzeit zwischen 03:00 und 22:00 gewürfelt (Europe/Berlin), auf die Sekunde genau und nie zweimal hintereinander in derselben Minute. Scheitert ein Lauf, folgen bis zu 3 Wiederholungen im Abstand von 15–75 Minuten, die letzte spätestens um 23:00.
- **Lesson:** Headless-Chromium öffnet `duolingo.com/lesson` mit der gespeicherten Session. Die Lösungen liest der Bot aus den Aufgabendaten, die die Web-App selbst lädt, und beantwortet jede Aufgabe richtig. So gibt es keine Wiederholungsschleifen und keinen Herzverlust. Hör- und Sprechaufgaben schaltet er über „Kann gerade nicht zuhören/sprechen“ ab. Zwischen den Klicks macht er zufällige Pausen von 0,8–3,5 s und bewegt die Maus in leicht variierten Bahnen.
- **Erfolgskontrolle:** Vor und nach dem Lauf fragt der Bot den Streak-Stand ab, wie die Web-App es auch tut. Ist der Tag schon erledigt, überspringt er den Lauf.
- **Schutz:** Bei einem sichtbaren Captcha bricht er sofort ab und pausiert ohne Wiederholung. Erscheint die Login-Seite, meldet er „neu einloggen“. Scheitern alle Versuche an zwei Tagen in Folge, pausiert er und meldet sich. Pro Lesson gelten höchstens 45 Schritte und 7 Minuten. Findet er dreimal in Folge keine Lösung, bricht er ab.

## Dateien

| Datei | Zweck |
| --- | --- |
| `bot.py` | Dienst und Befehle: `serve`, `run-now [--force]`, `status`, `resume`, `plan`, `explore` |
| `web.py` | Weboberfläche (Standardbibliothek, Port 8095) |
| `login.py` | Einmaliger Login am Desktop-PC, überträgt die Session auf den Server |
| `install-vm.sh` | Einmalige Einrichtung auf dem Server (Service-User `duobot`, venv, Chromium, systemd) |
| `deploy.sh` | Code per rsync auf den Server spielen und den Dienst neu starten |
| `ctl.sh` | Befehle auf dem Server als Service-User ausführen |

Laufzeitdaten liegen auf dem Server in `data/` (Session, Zustand, Einstellungen; nur für `duobot` lesbar), `logs/` (rotierend, 5 × 1 MB) und `debug/` (die letzten 20 Fehler-Screenshots). Nichts davon gehört ins Repo.

## Einrichtung

1. `deploy.env.example` nach `deploy.env` kopieren und Server, User, Zielordner und SSH-Schlüssel eintragen.
2. Code übertragen und einmalig installieren:
   ```bash
   ./deploy.sh
   ssh root@SERVER /opt/DuolingoAutoStreak/install-vm.sh
   ```
3. Am Desktop-PC einloggen (öffnet ein normales Chromium-Fenster, schließt sich nach dem Login selbst):
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium
   .venv/bin/python login.py
   ```
4. Dienst starten: `systemctl enable --now duolingo-streak-bot`
5. In der Weboberfläche `http://SERVER:8095` ein langes, zufälliges ntfy-Topic eintragen. Auf dem Handy die App „ntfy“ installieren und dasselbe Topic abonnieren.

## Bedienung

- **Weboberfläche:** `http://SERVER:8095` – Streak, heutiger Plan, Verlauf, Log, Knöpfe für „Jetzt ausführen“, Pause und Test-Benachrichtigung, Einstellungen für Zeitfenster, Wiederholungen und ntfy. Sie hat keinen Login und gehört deshalb nur ins Heimnetz.
- **Logs:** `journalctl -u duolingo-streak-bot -f` oder `/opt/DuolingoAutoStreak/logs/bot.log`
- **Neustart:** `systemctl restart duolingo-streak-bot`
- **Sofort ein Lauf:** `/opt/DuolingoAutoStreak/ctl.sh run-now` (mit `--force` auch an einem schon erledigten Tag)
- **Session abgelaufen:** am PC wieder `.venv/bin/python login.py` ausführen. Die neue Session hebt die Sperre automatisch auf.

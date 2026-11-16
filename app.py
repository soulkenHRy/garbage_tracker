"""Smart Bin: which Toronto bin does this item go in?

The camera board (an ESP32-S3 with a camera and a hand sensor) stays plugged into this laptop by USB.
When a hand comes close, the board takes a photo and sends it over the cable. This app asks Google
Gemini which bin the item belongs in, shows the answer on a web page and saves every scan in scans.db.

Run it:  press F5 in VS Code  (or: venv/bin/python app.py). The page opens by itself.
The Gemini API key is read from the GEMINI_API_KEY environment variable; a .env file works too.
The board runs the sketch in smart_bin_board/ (upload it once with the Arduino IDE).
"""
import base64
import json
import logging
import os
import queue
import sqlite3
import threading
import time
import webbrowser
from contextlib import closing
from datetime import datetime

import requests
import serial
from flask import Flask, Response, jsonify, request
from serial.tools import list_ports
from werkzeug.exceptions import HTTPException

# ---- Config -----------------------------------------------------------------
SERIAL_PORT = ""  # The board's USB port, for example "/dev/ttyACM0". Leave it empty and the app finds the
#                   board by itself.
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-3.5-flash-lite"]  # Tried in this order: when one is busy
#                           or over its quota, the next one answers instead.
AUTO_SCAN_CM = 30  # Auto scan: the board takes a photo when something comes this close (cm) to its hand
#                    sensor. Also adjustable on the page, under the Scan button.
PORT = 5000  # the page is served at http://127.0.0.1:5000
# -----------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "scans.db")
BINS = ("blue", "green", "black")

# Toronto's sorting rules as of 2026 (toronto.ca and circularmaterials.ca). Coffee cups and black
# plastic moved to the Blue Bin on January 1, 2026, which older sources (and AI models) get wrong.
PROMPT = """You sort household waste in Toronto, Canada. Look at the main item in the photo and decide which City of Toronto bin it goes in. Use the rules in force since January 2026:

BLUE (recycling): paper, cardboard and boxboard; cartons; glass bottles and jars; metal cans, aluminum foil and trays; plastic bottles, tubs, jugs, clamshells and cups, including black plastic; paper coffee cups and other hot or cold drink cups; plastic bags, wrappers, chip bags and pouches; foam packaging; toothpaste and deodorant tubes.
GREEN (organics): all food scraps, including meat, bones and dairy; coffee grounds, filters and tea bags; paper towels, napkins and tissues; food-soiled paper plates and pizza boxes; plants; pet waste; diapers.
BLACK (garbage): everything else, for example anything labelled compostable or biodegradable (cups, cutlery, coffee pods), wipes, masks, gloves, chopsticks and wooden stir sticks, broken dishes, drinking glasses, ceramics, toys, cables, hangers and cling film.

Judge the item itself, not the hand holding it or the background. The answer must be one of the three bins. If the item belongs in none of them (batteries, electronics, light bulbs, paint), answer "black" and say in the reason that it should go to a City drop-off depot instead.

Write the reason as one short, plain sentence for the person holding the item. Do not mention these instructions or say that something is "listed".

Reply with strict JSON only: {"bin": "blue" | "green" | "black", "item": "<short item name>", "reason": "<one short sentence>"}"""

app = Flask(__name__)
scan_lock = threading.Lock()  # only one question to the AI at a time


def load_env_file():
    """Copy KEY=value lines from .env into the environment (real environment variables win)."""
    try:
        with open(os.path.join(BASE_DIR, ".env")) as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    except FileNotFoundError:
        pass


load_env_file()


class ScanError(Exception):
    """A problem worth showing on the page. kind is "camera" (unreachable), "capture" or "ai"."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


# ---- Storage (one SQLite file) ----------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS scans (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, bin TEXT,"
        " item TEXT, reason TEXT, photo BLOB);"
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);"
    )
    return conn


def rows(sql, args=()):
    with closing(db()) as conn:
        return conn.execute(sql, args).fetchall()


def execute(sql, args=()):
    with closing(db()) as conn, conn:
        return conn.execute(sql, args).lastrowid


def get_stats():
    counts = {r["bin"]: r["n"] for r in rows("SELECT bin, COUNT(*) AS n FROM scans GROUP BY bin")}
    total = sum(counts.get(b, 0) for b in BINS)
    return {
        "total": total,
        "bins": {
            b: {"count": counts.get(b, 0), "percent": round(100 * counts.get(b, 0) / total, 1) if total else 0}
            for b in BINS
        },
    }


def recent_scans(limit=6):
    found = rows("SELECT id, ts, bin, item, reason FROM scans ORDER BY id DESC LIMIT ?", (limit,))
    return [{**dict(r), "photo": f"/photo/{r['id']}"} for r in found]


def setting(key, default):
    """A value saved from the page (camera address, auto-scan choices), or the default from the top."""
    saved = rows("SELECT value FROM settings WHERE key = ?", (key,))
    return saved[0]["value"] if saved else default


def save_setting(key, value):
    execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))


def auto_scan():
    return {"on": setting("auto_on", "1") == "1", "cm": int(setting("auto_cm", AUTO_SCAN_CM))}


# ---- Camera board (USB) -----------------------------------------------------
def data_url(jpeg):
    """The photo in the form a web page can show directly."""
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


class Board:
    """The camera board on the USB cable. A background thread reads everything the board sends:
    distance lines ("D 57.2 30") and photos (a line "PHOTO hand 11873 210", then the JPEG bytes).
    The sketch in smart_bin_board/ describes the lines in full."""

    def __init__(self):
        self.port = None  # the open USB serial port, or None while there is no board
        self.name = ""  # for example "/dev/ttyACM0"
        self.problem = "Looking for the camera board…"  # what the page shows while the board is offline
        self.cm = None  # the last distance in cm; None means nothing in range
        self.heard = 0.0  # when the last distance line arrived
        self.hand = (0, 0.0, b"")  # number, time and JPEG of the last photo the hand sensor set off
        self.answers = queue.Queue()  # answers to "scan": a JPEG, b"" (arrived damaged) or None (camera failed)
        self.ask_lock = threading.Lock()  # one Scan-button photo at a time
        self.send_lock = threading.Lock()

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def online(self):
        return self.port is not None and time.time() - self.heard < 2

    def run(self):
        """Find the board, read from it until it goes away, then look again. Never gives up."""
        while True:
            try:
                name = self.find()
                with serial.Serial(name, 115200, timeout=0.5, write_timeout=1, exclusive=True) as port:
                    self.listen(port, name)
            except LookupError as e:  # no board in sight; find() says what to do
                self.problem = str(e)
            except (serial.SerialException, OSError) as e:
                if getattr(e, "errno", None) == 13:
                    self.problem = ("This user is not allowed to open the board's USB port. Run "
                                    "'sudo usermod -aG dialout $USER', then log out and in again.")
                elif getattr(e, "errno", None) == 16 or "lock" in str(e).lower():
                    self.problem = ("Another program is using the board's USB port. Close the Arduino IDE's "
                                    "Serial Monitor, or wait for the upload to finish.")
                else:
                    self.problem = "The USB connection to the board dropped. Check the cable; the app reconnects by itself."
            except Exception as e:  # never let a surprise stop the reading thread
                self.problem = f"Unexpected problem while reading from the board: {e}"
            self.port = None
            time.sleep(1)

    def find(self):
        """The name of the board's USB port. Raises LookupError with advice when it is not there."""
        if SERIAL_PORT:
            return SERIAL_PORT
        ports = list(list_ports.comports())
        for p in ports:
            if p.vid == 0x303A:  # Espressif: the ESP32-S3's own USB socket, which the sketch talks on
                return p.device
        if any(p.vid == 0x1A86 for p in ports):  # the board's second socket (a USB-to-serial chip)
            raise LookupError("The board is plugged in by its other USB socket. Move the cable to the "
                              "socket you used for the sensor test.")
        raise LookupError("No camera board found on USB. Plug the board into this laptop.")

    def listen(self, port, name):
        self.name = name
        self.problem = (f"The board on {name} is connected but sends no readings. Upload the smart_bin_board "
                        "sketch to it with the Arduino IDE, or press its RST button.")
        port.reset_input_buffer()
        settled = time.time() + 0.5  # throw away whatever the board had queued before the app started
        while time.time() < settled:
            port.read(max(1, port.in_waiting))
        self.port = port
        while True:
            words = port.readline().decode("ascii", "replace").split()
            if len(words) == 3 and words[0] == "D":
                self.reading(words[1], words[2])
            elif len(words) == 4 and words[0] == "PHOTO" and words[2].isdigit() and int(words[2]) < 1_000_000:
                self.photo_arrived(port, words[1], int(words[2]), words[3])
            elif words == ["ERR", "scan"]:
                self.answers.put(None)

    def reading(self, cm, near):
        """A distance line: remember it, and make sure the board uses the page's auto-scan distance."""
        try:
            cm, near = (None if cm == "-" else float(cm)), int(near)
        except ValueError:
            return
        self.cm, self.heard = cm, time.time()
        auto = auto_scan()
        wanted = auto["cm"] if auto["on"] else 0  # 0 tells the board to take no photos by itself
        if near != wanted:
            self.send(f"near {wanted}")

    def photo_arrived(self, port, why, size, took):
        """The JPEG bytes follow the PHOTO line directly. why is "hand" (sensor) or "scan" (our request)."""
        started, jpeg = time.time(), b""
        while len(jpeg) < size and time.time() - started < 1.5:
            jpeg += port.read(size - len(jpeg))
        self.heard = time.time()  # the board is clearly there, even though no distance line came meanwhile
        good = len(jpeg) == size and jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")
        source = "hand sensor" if why == "hand" else "Scan button"
        if good:
            print(f"Photo from the board ({source}): {size / 1024:.1f} KB, taken in {took} ms, "
                  f"received in {(time.time() - started) * 1000:.0f} ms", flush=True)
        else:
            print(f"A photo from the board ({source}) arrived damaged and was dropped: {len(jpeg)} of {size} bytes",
                  flush=True)
        if why == "scan":
            self.answers.put(jpeg if good else b"")
        elif why == "hand" and good:
            self.hand = (self.hand[0] + 1, time.time(), jpeg)

    def send(self, command):
        port = self.port
        if port is not None:
            with self.send_lock:
                try:
                    port.write(command.encode() + b"\n")
                except (serial.SerialException, OSError):
                    pass  # the reading thread notices a lost board and reconnects

    def photo(self):
        """A photo taken right now, as JPEG bytes. Used by the Scan button."""
        with self.ask_lock:
            for _ in range(2):  # a photo that arrives damaged is asked for once more
                if not self.online():
                    raise ScanError("camera", self.problem)
                while not self.answers.empty():
                    self.answers.get_nowait()  # a late answer to an earlier request
                self.send("scan")
                try:
                    jpeg = self.answers.get(timeout=5)
                except queue.Empty:
                    raise ScanError("camera", "The board did not send a photo. Press its RST button, then scan again.")
                if jpeg is None:
                    raise ScanError("capture", "The board's camera could not take a photo. Check that the camera's "
                                               "ribbon cable is pushed in fully, then press the board's RST button.")
                if jpeg:
                    return jpeg
            raise ScanError("capture", "The photo arrived damaged twice in a row. Scan again.")


board = Board()


# ---- AI ---------------------------------------------------------------------
def classify(jpeg):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ScanError("ai", "No Gemini API key found. Put GEMINI_API_KEY=your-key in the .env file next "
                              "to app.py, then restart the app.")
    body = {
        "contents": [{"parts": [
            {"text": PROMPT},
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}},
        ]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "bin": {"type": "STRING", "enum": list(BINS)},
                    "item": {"type": "STRING"},
                    "reason": {"type": "STRING"},
                },
                "required": ["bin", "item", "reason"],
            },
        },
    }
    # Gemini is sometimes busy for a few seconds ("high demand"), so go through the models twice
    # before giving up, but never keep the page waiting much longer than 20 seconds.
    started, reply, problem = time.time(), None, ""
    for model in GEMINI_MODELS * 2:
        if time.time() - started > 20:
            break
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key},
                json=body,
                timeout=(5, 15),
            )
        except requests.RequestException:
            continue
        if r.status_code == 200:
            reply = r
            break
        try:
            detail = r.json()["error"]["message"]
        except Exception:
            detail = r.text
        problem = f"HTTP {r.status_code}: {detail.strip()[:200]}"
        if r.status_code in (400, 401, 403):  # a wrong key will not fix itself, so stop here
            raise ScanError("ai", f"Gemini refused the request ({problem})")
        time.sleep(0.5)
    if reply is None and problem:
        raise ScanError("ai", f"Gemini could not answer right now. Wait a few seconds and scan again. ({problem})")
    if reply is None:
        raise ScanError("ai", "No answer from Google Gemini. Check this laptop's internet connection, then "
                              "scan again.")
    try:
        parts = reply.json()["candidates"][0]["content"]["parts"]
        answer = json.loads("".join(p.get("text", "") for p in parts if not p.get("thought")))
        result = {k: str(answer[k]).strip() for k in ("bin", "item", "reason")}
        result["bin"] = result["bin"].lower()
        result["item"] = result["item"][:1].upper() + result["item"][1:]
    except Exception:
        raise ScanError("ai", "Gemini's answer was not in the expected format. Try scanning again.")
    if result["bin"] not in BINS or not result["item"]:
        raise ScanError("ai", "Gemini did not name one of the three bins. Try scanning again.")
    return result


# ---- Web --------------------------------------------------------------------
@app.get("/")
def index():
    return PAGE


@app.get("/api/state")
def api_state():
    return jsonify(stats=get_stats(), history=recent_scans(), auto=auto_scan())


@app.post("/api/snap")
def api_snap():
    """Scan, step 1 (Scan button only): ask the board for a photo, so the page can show it straight away."""
    try:
        return jsonify(photo=data_url(board.photo()))
    except ScanError as e:
        return jsonify(error=str(e), kind=e.kind), 502


@app.post("/api/scan")
def api_scan():
    """Scan, step 2: ask the AI about a photo and save the answer. The photo comes from /api/snap
    (Scan button) or from /api/distance (the board took it when a hand came close)."""
    try:
        sent = (request.get_json(silent=True) or {}).get("photo") or ""
        jpeg = base64.b64decode(sent.partition(",")[2], validate=True)
    except (ValueError, AttributeError):
        jpeg = b""
    if not jpeg.startswith(b"\xff\xd8"):
        return jsonify(error="The page sent no photo to look at. Scan again."), 400
    if not scan_lock.acquire(blocking=False):  # e.g. the same photo arriving from two browser tabs
        return jsonify(error="A scan is already running.", kind="busy"), 409
    try:
        result = classify(jpeg)
        ts = datetime.now().isoformat(timespec="seconds")
        scan_id = execute(
            "INSERT INTO scans (ts, bin, item, reason, photo) VALUES (?, ?, ?, ?, ?)",
            (ts, result["bin"], result["item"], result["reason"], jpeg),
        )
        scan = {"id": scan_id, "ts": ts, **result, "photo": f"/photo/{scan_id}"}
        return jsonify(scan=scan, stats=get_stats(), history=recent_scans())
    except ScanError as e:
        return jsonify(error=str(e), kind=e.kind), 502
    finally:
        scan_lock.release()


@app.get("/api/distance")
def api_distance():
    """The hand sensor's reading, for the page, which asks several times a second. Nothing is sent to the
    board here: this reports what the board last said. state is "ok" or "offline".
    The page says which hand photo it already has (?have=number); a newer one comes back as "photo"."""
    if not board.online():
        return jsonify(state="offline", cm=None, port=board.name, problem=board.problem)
    number, when, jpeg = board.hand
    reply = {"state": "ok", "cm": board.cm, "port": board.name, "snap": number,
             "age": int((time.time() - when) * 1000) if number else 0}
    have = request.args.get("have", "")
    if number and have.isdigit() and int(have) != number:
        reply["photo"] = data_url(jpeg)
    return jsonify(reply)


@app.post("/api/auto")
def api_auto():
    """Save the auto-scan switch and its trigger distance."""
    data = request.get_json(silent=True) or {}
    try:
        cm = int(data.get("cm"))
    except (TypeError, ValueError):
        cm = 0
    if not 5 <= cm <= 200:
        return jsonify(error="Pick a distance between 5 and 200 cm."), 400
    save_setting("auto_on", "1" if data.get("on") else "0")
    save_setting("auto_cm", cm)
    return jsonify(auto=auto_scan())


@app.get("/photo/<int:scan_id>")
def photo(scan_id):
    found = rows("SELECT photo FROM scans WHERE id = ?", (scan_id,))
    if not found or not found[0]["photo"]:
        return jsonify(error="No such photo."), 404
    return Response(found[0]["photo"], mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/reset")
def api_reset():
    execute("DELETE FROM scans")
    return jsonify(stats=get_stats(), history=[])


@app.errorhandler(Exception)
def on_error(e):
    """Never crash the demo: every failure reaches the page as a readable message."""
    if isinstance(e, HTTPException):
        return jsonify(error=e.description), e.code
    app.logger.exception("Unexpected error")
    return jsonify(error=f"The app hit an unexpected error: {e}"), 500


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Smart Bin</title>
<style>
  :root {
    color-scheme: light;
    --page: #f4f3ef; --surface: #ffffff; --wash: #f6f5f1;
    --ink: #141413; --ink-2: #52514e; --line: #e1e0d9; --axis: #c3c2b7;
    /* Bin colours. Checked for colour-blind separation (worst pair dE 22.5) and 3:1 contrast on white.
       Black is deliberately outside the usual chart-colour range: the bin really is black, and every
       mark that uses it also carries a text label. */
    --blue: #1f63b8; --green: #1a7f37; --black: #262624;
    --accent: #ffc21a; --accent-hover: #ffcf4a;
    --good: #0ca30c; --bad: #b42318;
  }
  *, *::before, *::after { box-sizing: border-box; }
  [hidden] { display: none !important; }
  body { margin: 0; background: var(--page); color: var(--ink);
         font: 16px/1.45 system-ui, -apple-system, "Segoe UI", Ubuntu, sans-serif; }
  button, input { font: inherit; color: inherit; }
  button { cursor: pointer; }
  :focus-visible { outline: 3px solid var(--ink); outline-offset: 2px; }
  h1, h2, p { margin: 0; }
  .wrap { max-width: 1120px; margin: 0 auto; padding: 20px 20px 48px; }
  .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }

  /* Header */
  .top { display: flex; align-items: center; justify-content: space-between; gap: 12px 16px; flex-wrap: wrap; margin-bottom: 20px; }
  .brand { display: flex; align-items: center; gap: 12px; }
  .logo { display: flex; align-items: flex-end; gap: 3px; }
  .logo i { width: 9px; height: 24px; border-radius: 3px 3px 2px 2px; }
  h1 { font-size: 1.35rem; font-weight: 800; letter-spacing: -0.02em; }
  .tag { color: var(--ink-2); }
  .cam { display: inline-flex; align-items: center; gap: 10px; padding: 8px 14px; border-radius: 999px;
         border: 1px solid var(--line); background: var(--surface); font-weight: 600; font-size: .95rem; }
  .cam:hover { border-color: var(--axis); }
  .cam small { font-weight: 400; font-size: .9rem; color: var(--ink-2); }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--axis); flex: none; }
  [data-state="on"] > .dot { background: var(--good); }
  [data-state="off"] > .dot { background: none; box-shadow: inset 0 0 0 2.5px var(--bad); }

  /* Layout */
  .card { background: var(--surface); border: 1px solid var(--line); border-radius: 16px; padding: 20px; }
  .stage { display: grid; gap: 20px; grid-template-columns: minmax(0, 400px) minmax(0, 1fr); }
  .lower { display: grid; gap: 20px; grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: start; margin-top: 20px; }
  @media (max-width: 860px) { .stage, .lower { grid-template-columns: minmax(0, 1fr); } }

  /* Photo and scan button */
  .photo { position: relative; aspect-ratio: 4 / 3; border-radius: 12px; overflow: hidden; background: #1b1b1a;
           display: grid; place-items: center; }
  .photo img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; }
  .photo-empty { color: #b5b4ad; text-align: center; font-size: .95rem; }
  .photo-empty svg { display: block; margin: 0 auto 8px; }
  .photo.stale img { opacity: .45; }   /* waiting for a new photo: the one on show is the old one */
  .photo::before { content: ""; position: absolute; inset: 0; z-index: 1; background: #fff; opacity: 0; pointer-events: none; }
  .photo.flash::before { animation: flash .5s ease-out; }   /* camera flash: a new photo has just arrived */
  @keyframes flash { from { opacity: .9; } }
  .photo.busy::after { content: ""; position: absolute; left: 0; right: 0; top: 4%; height: 3px; background: var(--accent);
                       box-shadow: 0 0 14px 2px rgba(255, 194, 26, .7); animation: sweep 1.1s ease-in-out infinite alternate; }
  @keyframes sweep { to { top: 96%; } }
  .scan { width: 100%; min-height: 64px; margin-top: 16px; border: 0; border-radius: 12px; background: var(--accent);
          font-size: 1.35rem; font-weight: 800; letter-spacing: -0.01em;
          display: flex; align-items: center; justify-content: center; gap: 12px; }
  .scan:hover { background: var(--accent-hover); }
  .scan:active { transform: translateY(1px); }
  .scan:disabled { background: #eceae4; color: var(--ink-2); cursor: progress; transform: none; }
  kbd { font: 700 .72rem/1 system-ui, sans-serif; letter-spacing: .04em; text-transform: uppercase; padding: 6px 8px;
        border-radius: 6px; background: rgba(20, 20, 19, .12); }
  .scan:disabled kbd { display: none; }
  @media (max-width: 860px) { kbd { display: none; } }
  .auto { display: flex; align-items: center; justify-content: center; flex-wrap: wrap; gap: 6px 8px; margin-top: 14px; font-size: .95rem; }
  .auto label { display: flex; align-items: center; gap: 8px; cursor: pointer; font-weight: 600; }
  .auto input[type="checkbox"] { width: 18px; height: 18px; margin: 0; accent-color: var(--ink); }
  .auto input[type="number"] { width: 4.2rem; min-height: 34px; padding: 0 8px; border: 1px solid var(--axis); border-radius: 8px; }
  .sensor { display: flex; align-items: center; justify-content: center; gap: 8px; margin-top: 10px; color: var(--ink-2); font-size: .9rem; text-align: center; }
  [data-state="near"] > .dot { background: var(--accent); box-shadow: 0 0 0 3px rgba(255, 194, 26, .4); }

  /* Verdict: the answer, big enough to read from across the table */
  .verdict { display: flex; flex-direction: column; justify-content: center; min-height: 300px;
             padding: 28px clamp(20px, 4vw, 44px); transition: background-color .2s; }
  .verdict[data-bin] { color: #fff; border-color: transparent; }
  .verdict[data-bin="blue"] { background: var(--blue); }
  .verdict[data-bin="green"] { background: var(--green); }
  .verdict[data-bin="black"] { background: var(--black); }
  .verdict > * { animation: rise .25s ease-out both; }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } }
  .eyebrow { font-size: .82rem; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; color: var(--ink-2); }
  .verdict[data-bin] .eyebrow { color: #fff; }
  .eyebrow.bad { color: var(--bad); display: flex; align-items: center; gap: 8px; }
  .eyebrow.bad::before { content: "!"; display: grid; place-items: center; width: 20px; height: 20px; border-radius: 50%;
                         background: var(--bad); color: #fff; letter-spacing: 0; font-size: .85rem; }
  .headline { margin-top: 8px; font-size: clamp(1.9rem, 4.2vw, 3rem); line-height: 1.06; font-weight: 800; letter-spacing: -0.03em; }
  .bin-name { margin-top: 6px; font-size: clamp(3.2rem, 8.5vw, 6rem); line-height: .95; font-weight: 800; letter-spacing: -0.04em; }
  .bin-kind { margin-top: 10px; font-size: clamp(1.3rem, 2.6vw, 1.8rem); font-weight: 600; }
  .verdict hr { width: 100%; margin: 22px 0 16px; border: 0; border-top: 1px solid rgba(255, 255, 255, .4); }
  .item { font-size: 1.35rem; font-weight: 700; }
  .reason { margin-top: 4px; font-size: 1.1rem; max-width: 48ch; }
  .when { margin-top: 14px; font-size: .88rem; }
  .bins { list-style: none; margin: 22px 0 0; padding: 0; display: grid; gap: 10px; font-size: 1.05rem; }
  .bins li { display: flex; align-items: center; gap: 10px; }
  .bins i, .chip i { width: 12px; height: 16px; border-radius: 3px; flex: none; }
  .bins span { color: var(--ink-2); }
  .err-msg { margin-top: 12px; font-size: 1.1rem; max-width: 52ch; }
  .actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 20px; }
  .btn { min-height: 44px; padding: 0 18px; border-radius: 10px; border: 1px solid var(--ink); background: var(--ink);
         color: #fff; font-weight: 700; }
  .btn.ghost { background: var(--surface); color: var(--ink); border-color: var(--axis); }
  .btn:disabled { opacity: .55; cursor: progress; }

  /* Chart and history */
  .card-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
  h2 { font-size: 1.05rem; font-weight: 700; }
  .muted { color: var(--ink-2); font-size: .92rem; }
  .link { background: none; border: 0; padding: 4px 0; color: var(--ink-2); font-size: .92rem; text-decoration: underline; }
  .link:hover { color: var(--ink); }
  .chart { width: 100%; border-collapse: collapse; }
  .chart th { width: 1%; padding: 10px 16px 10px 0; text-align: left; font-weight: 600; white-space: nowrap; }
  .chart th small { display: block; font-weight: 400; font-size: .85rem; color: var(--ink-2); }
  .chart .plot { padding: 10px 0; border-left: 1px solid var(--axis); }
  .chart .track { display: flex; align-items: center; gap: 8px; }
  .chart .bar { flex: none; height: 20px; width: calc((100% - 3.5rem) * var(--p, 0)); border-radius: 0 4px 4px 0;
                transition: width .45s cubic-bezier(.2, .7, .2, 1); }
  .chart .val { font-weight: 700; font-variant-numeric: tabular-nums; }
  .chart .count { width: 1%; padding-left: 16px; text-align: right; white-space: nowrap; color: var(--ink-2);
                  font-variant-numeric: tabular-nums; }
  .chart tr:hover .bar, .chart tr:focus-visible .bar { filter: brightness(1.2); }
  .chart tr:focus-visible { outline-offset: -2px; }
  .tip { position: fixed; z-index: 20; pointer-events: none; max-width: 260px; padding: 8px 12px; border-radius: 8px;
         background: var(--ink); color: #fff; font-size: .88rem; box-shadow: 0 6px 20px rgba(0, 0, 0, .2); }
  .tip b { display: block; font-size: 1rem; }
  .history { list-style: none; margin: 0; padding: 0; }
  .history li + li { border-top: 1px solid var(--line); }
  .history .empty { padding: 18px 0; color: var(--ink-2); }
  .hist { display: flex; align-items: center; gap: 12px; width: 100%; padding: 8px 6px; border: 0; border-radius: 8px;
          background: none; text-align: left; }
  .hist:hover { background: var(--wash); }
  .hist img { width: 56px; height: 42px; flex: none; object-fit: cover; border-radius: 6px; background: #1b1b1a; }
  .hist-main { flex: 1; min-width: 0; }
  .hist-item { display: block; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hist-time { color: var(--ink-2); font-size: .85rem; }
  .chip { display: inline-flex; align-items: center; gap: 8px; flex: none; font-size: .92rem; font-weight: 600; }

  /* Camera dialog */
  dialog { width: min(460px, calc(100vw - 32px)); padding: 24px; border: 0; border-radius: 16px; color: var(--ink);
           box-shadow: 0 20px 60px rgba(0, 0, 0, .3); }
  dialog::backdrop { background: rgba(20, 20, 19, .5); }
  .dlg-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
  .dlg-head h2 { font-size: 1.3rem; }
  .x { width: 36px; height: 36px; border: 0; border-radius: 8px; background: none; font-size: 1.5rem; line-height: 1; }
  .x:hover { background: var(--wash); }
  .cam-state { display: flex; align-items: center; gap: 10px; font-weight: 600; margin-bottom: 18px; }
  .dlg-msg { min-height: 1.45em; margin-top: 14px; font-weight: 600; }
  .help { margin-top: 10px; padding-top: 14px; border-top: 1px solid var(--line); color: var(--ink-2); font-size: .92rem; }

  @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; } }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="brand">
      <span class="logo" aria-hidden="true"><i style="background:var(--blue)"></i><i style="background:var(--green)"></i><i style="background:var(--black)"></i></span>
      <h1>Smart Bin</h1>
      <span class="tag">Toronto waste sorter</span>
    </div>
    <button id="camBtn" class="cam" type="button" aria-haspopup="dialog" title="Camera board">
      <span class="dot" aria-hidden="true"></span><span id="camText">Checking camera…</span><small id="camAddrLabel"></small>
    </button>
  </header>

  <main>
    <div class="stage">
      <section class="card" aria-label="Camera">
        <div class="photo" id="photoBox">
          <img id="photo" alt="" hidden>
          <div class="photo-empty" id="photoEmpty">
            <svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 8h3l1.6-2.4h6.8L17 8h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13" r="3.6"/></svg>
            No photo yet
          </div>
        </div>
        <button id="scanBtn" class="scan" type="button"><span id="scanText">Scan item</span><kbd>Space</kbd></button>
        <div class="auto">
          <label><input type="checkbox" id="autoOn"> Auto scan when closer than</label>
          <span><input id="autoCm" type="number" min="5" max="200" step="1" inputmode="numeric" aria-label="Auto-scan distance in centimetres"> cm</span>
        </div>
        <p class="sensor" id="sensor" data-state="wait"><span class="dot" aria-hidden="true"></span><span id="sensorText">Checking the hand sensor…</span></p>
      </section>
      <section class="card verdict" id="verdict" aria-live="polite" aria-label="Result"></section>
    </div>

    <div class="lower">
      <section class="card">
        <div class="card-head"><h2>Scans per bin</h2><span class="muted" id="total"></span></div>
        <table class="chart">
          <thead class="sr-only"><tr><th>Bin</th><th>Share of scans</th><th>Number of scans</th></tr></thead>
          <tbody>
            <tr id="row-blue" tabindex="0"><th scope="row">Blue bin<small>Recycling</small></th><td class="plot"><div class="track"><span class="bar" style="background:var(--blue)"></span><span class="val">0%</span></div></td><td class="count">0 scans</td></tr>
            <tr id="row-green" tabindex="0"><th scope="row">Green bin<small>Organics</small></th><td class="plot"><div class="track"><span class="bar" style="background:var(--green)"></span><span class="val">0%</span></div></td><td class="count">0 scans</td></tr>
            <tr id="row-black" tabindex="0"><th scope="row">Black bin<small>Garbage</small></th><td class="plot"><div class="track"><span class="bar" style="background:var(--black)"></span><span class="val">0%</span></div></td><td class="count">0 scans</td></tr>
          </tbody>
        </table>
      </section>
      <section class="card">
        <div class="card-head"><h2>Recent scans</h2><button id="clearBtn" class="link" type="button" hidden>Clear all</button></div>
        <ol class="history" id="history"></ol>
      </section>
    </div>
  </main>
</div>

<dialog id="camDialog" aria-labelledby="camTitle">
  <form method="dialog" class="dlg-head"><h2 id="camTitle">Camera board</h2><button class="x" aria-label="Close">&times;</button></form>
  <p class="cam-state" id="camState"><span class="dot" aria-hidden="true"></span><span id="camStateText"></span></p>
  <p class="dlg-msg" id="camMsg" role="status"></p>
  <p class="help">The board talks to this laptop over its USB cable and needs no WiFi. Keep it plugged into the USB socket you used for the sensor test. If it stays offline, press the board's RST button.</p>
</dialog>
<div class="tip" id="tip" hidden></div>

<script>
const BIN = {
  blue:  { name: "Blue bin",  kind: "Recycling", short: "Blue" },
  green: { name: "Green bin", kind: "Organics",  short: "Green" },
  black: { name: "Black bin", kind: "Garbage",   short: "Black" },
};
const $ = id => document.getElementById(id);
let busy = false, camAddress = "", errorKind = null;

// Build elements with textContent only: item names and reasons come from the AI, so never use innerHTML.
function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  node.append(...kids);
  return node;
}

async function api(path, body) {
  let res;
  try {
    res = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  } catch {
    throw { down: true, message: "The Smart Bin app is not running. Start it again in VS Code (press F5)." };
  }
  const data = await res.json().catch(() => ({ error: "The app sent an answer this page could not read." }));
  if (!res.ok || data.error) throw { ...data, message: data.error || "Something went wrong." };
  return data;
}

function clock(ts) {
  const d = new Date(ts), time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? time : d.toLocaleDateString([], { month: "short", day: "numeric" }) + ", " + time;
}
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// ---- Result panel ----
function setVerdict(bin, ...nodes) {
  const v = $("verdict");
  if (bin) v.dataset.bin = bin; else delete v.dataset.bin;
  v.replaceChildren(...nodes);
  errorKind = null;
}
function showIdle() {
  setVerdict(null,
    el("p", { class: "eyebrow", text: "Ready" }),
    el("p", { class: "headline", text: "Which bin does it go in?" }),
    el("ul", { class: "bins" }, ...Object.entries(BIN).map(([key, b]) =>
      el("li", {}, el("i", { style: `background:var(--${key})` }), el("b", { text: b.name }), el("span", { text: b.kind })))));
}
function showPhoto(src, alt) {
  const img = $("photo");
  if (img.src !== src) img.src = src;
  img.alt = alt; img.hidden = false;
  $("photoEmpty").hidden = true;
}
function showScan(s) {
  showPhoto(s.photo, "Photo of the scanned item: " + s.item);
  setVerdict(s.bin,
    el("p", { class: "eyebrow", text: "Put it in the" }),
    el("p", { class: "bin-name", text: BIN[s.bin].name }),
    el("p", { class: "bin-kind", text: BIN[s.bin].kind }),
    el("hr"),
    el("p", { class: "item", text: s.item }),
    el("p", { class: "reason", text: s.reason }),
    el("p", { class: "when", text: "Scanned " + clock(s.ts) }));
}
function showError(e, photo) {
  const title = { camera: "Can't reach the camera", capture: "The camera sent no photo", ai: "The AI couldn't answer", busy: "Already scanning" }[e.kind] || "Something went wrong";
  // When only the AI failed, the photo is still good: ask again about that photo instead of taking a new one.
  const again = e.kind === "ai" && photo ? ["Ask again", () => scan(photo)] : ["Try again", () => scan()];
  const actions = el("div", { class: "actions" }, el("button", { class: "btn", type: "button", text: again[0], onclick: again[1] }));
  setVerdict(null, el("p", { class: "eyebrow bad", text: "Problem" }), el("p", { class: "headline", text: title }), el("p", { class: "err-msg", text: e.message }), actions);
  errorKind = e.kind || "app";
}

// ---- Scan ----
// Two steps: get the photo and show it at once (with a flash), then ask the AI about it.
// The Scan button asks the camera for a photo. The hand sensor passes in the photo that the camera
// board took by itself when the hand arrived.
async function scan(photo) {
  if (busy) return;
  busy = true;
  const box = $("photoBox"), headline = el("p", { class: "headline" });
  const say = (eyebrow, text) => { headline.textContent = text; setVerdict(null, el("p", { class: "eyebrow", text: eyebrow }), headline); };
  $("scanBtn").disabled = true; $("scanText").textContent = "Scanning…"; box.classList.add("busy");
  let slow;
  try {
    if (typeof photo !== "string") {   // started by the button or the keyboard, so there is no photo yet
      photo = null;
      box.classList.add("stale"); say("Scanning", "Taking the photo…");
      photo = (await api("/api/snap", {})).photo;
    }
    box.classList.remove("stale", "flash"); void box.offsetWidth; box.classList.add("flash");
    showPhoto(photo, "Photo of the item"); setCam(true);
    say("Photo taken", "Working out the bin…");
    slow = setTimeout(() => { headline.textContent = "Still working on it…"; }, 7000);
    const d = await api("/api/scan", { photo });
    showScan({ ...d.scan, photo }); renderStats(d.stats); renderHistory(d.history);
  } catch (e) {
    if (e.kind === "camera") setCam(false, undefined, e.message);
    showError(e, photo);
  } finally {
    busy = false; clearTimeout(slow); lastScanEnd = Date.now();
    $("scanBtn").disabled = false; $("scanText").textContent = "Scan item"; box.classList.remove("busy", "stale");
  }
}

// ---- Chart and history ----
function renderStats(stats) {
  $("total").textContent = stats.total ? plural(stats.total, "scan") : "No scans yet";
  for (const key of Object.keys(BIN)) {
    const row = $("row-" + key), d = stats.bins[key];
    row.querySelector(".bar").style.setProperty("--p", d.percent / 100);
    row.querySelector(".val").textContent = Math.round(d.percent) + "%";
    row.querySelector(".count").textContent = plural(d.count, "scan");
    row.dataset.value = `${d.percent}% · ${d.count} of ${plural(stats.total, "scan")}`;
  }
}
function renderHistory(list) {
  $("clearBtn").hidden = !list.length;
  $("history").replaceChildren(...(list.length ? list.map(s =>
    el("li", {}, el("button", { class: "hist", type: "button", title: "Show this scan", onclick: () => showScan(s) },
      el("img", { src: s.photo, alt: "" }),
      el("span", { class: "hist-main" }, el("span", { class: "hist-item", text: s.item }), el("span", { class: "hist-time", text: clock(s.ts) })),
      el("span", { class: "chip" }, el("i", { style: `background:var(--${s.bin})` }), BIN[s.bin].short))))
    : [el("li", { class: "empty", text: "Your scans will show up here." })]));
}
for (const key of Object.keys(BIN)) {   // hover and keyboard focus show the exact figures
  const row = $("row-" + key), tip = $("tip");
  const show = (x, y) => {
    tip.replaceChildren(el("b", { text: row.dataset.value || "" }), `${BIN[key].name} · ${BIN[key].kind}`);
    tip.hidden = false;
    tip.style.left = Math.max(8, Math.min(x + 14, innerWidth - tip.offsetWidth - 8)) + "px";
    tip.style.top = Math.max(8, y - tip.offsetHeight - 12) + "px";
  };
  row.addEventListener("pointermove", e => show(e.clientX, e.clientY));
  row.addEventListener("pointerleave", () => { tip.hidden = true; });
  row.addEventListener("focus", () => { const r = row.querySelector(".val").getBoundingClientRect(); show(r.right, r.top); });
  row.addEventListener("blur", () => { tip.hidden = true; });
}
$("clearBtn").onclick = async () => {
  if (!confirm("Delete every saved scan? This cannot be undone.")) return;
  try {
    const d = await api("/api/reset", {});
    renderStats(d.stats); renderHistory(d.history); showIdle();
    $("photo").hidden = true; $("photoEmpty").hidden = false;
  } catch (e) { showError(e); }
};

// ---- Camera board ----
// The board is on the USB cable. Every sensor reading (watchSensor, below) also says whether it is there.
function setCam(online, port, problem) {
  if (port !== undefined) camAddress = port;
  const state = online == null ? "wait" : online ? "on" : "off";
  $("camBtn").dataset.state = state; $("camState").dataset.state = state;
  $("camText").textContent = online == null ? "Checking camera…" : online ? "Camera online" : "Camera offline";
  $("camAddrLabel").textContent = camAddress ? "USB " + camAddress.replace("/dev/", "") : "USB";
  $("camStateText").textContent = online == null ? "Checking…" : online ? "Connected on " + camAddress : "Not connected";
  $("camMsg").textContent = online ? "" : problem || "";
  if (online && errorKind === "camera") showIdle();   // the board is back, so drop the old error
}
$("camBtn").onclick = () => { if (!$("camDialog").open) $("camDialog").showModal(); };

// ---- Hand sensor: scans by itself when something comes close ----
// The board takes the photo itself the moment a hand arrives and sends it over USB. This page asks the
// app for the latest reading several times a second and gets the new photo along with it.
const FRESH_MS = 10000;   // a waiting photo older than this is no longer worth scanning
let auto = { on: true, cm: 30 }, seenSnap = null, lastScanEnd = 0;
function showSensor(state, text) { $("sensor").dataset.state = state; $("sensorText").textContent = text; }
async function watchSensor() {
  let wait = 100;
  if (!busy && !document.hidden) {
    // Ready for a scan: auto scan is on, the camera dialog is closed and the last result has had 3 seconds on screen.
    const ready = auto.on && !$("camDialog").open && Date.now() - lastScanEnd > 3000;
    const ask = ready && seenSnap !== null;   // say which photo we already have, so a newer one comes back
    let d;
    try { d = await api("/api/distance" + (ask ? "?have=" + seenSnap : "")); } catch (e) { d = { state: "down" }; }
    if (d.state !== "ok") {
      wait = 1000;
      if (d.state === "down") { showSensor("off", "Hand sensor: the app is not running"); setCam(false); $("camText").textContent = "App not running"; }
      else { showSensor("off", "Hand sensor: waiting for the camera board"); setCam(false, d.port, d.problem); }
    } else if (!busy) {
      const close = d.cm !== null && d.cm <= auto.cm;
      const reading = d.cm === null ? "nothing in range" : Math.round(d.cm) + " cm";
      showSensor(close ? "near" : "on", "Hand sensor: " + reading + (close ? " · hand detected" : ""));
      if ($("camBtn").dataset.state !== "on" || camAddress !== d.port) setCam(true, d.port);
      if (d.photo) {
        seenSnap = d.snap;
        if (d.age <= FRESH_MS) scan(d.photo);
      } else if (ask || seenSnap === null) {
        seenSnap = d.snap;   // nothing new: remember where the photo counter stands
      }
    }
  }
  setTimeout(watchSensor, wait);
}
function showAuto() { $("autoOn").checked = auto.on; $("autoCm").value = auto.cm; }
async function saveAuto() {
  const cm = Math.min(200, Math.max(5, Math.round(Number($("autoCm").value)) || auto.cm));
  try { auto = (await api("/api/auto", { on: $("autoOn").checked, cm })).auto; } catch (e) { /* keep the old choice */ }
  showAuto();
}
$("autoOn").onchange = saveAuto;
$("autoCm").onchange = saveAuto;

// ---- Start ----
$("scanBtn").onclick = scan;
document.addEventListener("keydown", e => {   // Space scans, unless you are typing or already on a button
  const tag = document.activeElement ? document.activeElement.tagName : "";
  if (e.code !== "Space" || e.repeat || $("camDialog").open || ["INPUT", "BUTTON", "TEXTAREA", "SELECT"].includes(tag)) return;
  e.preventDefault(); scan();
});
showIdle(); setCam(null);
api("/api/state").then(d => {
  renderStats(d.stats); renderHistory(d.history); auto = d.auto;
}).catch(showError).finally(() => { showAuto(); watchSensor(); });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    # The page asks for the sensor reading all the time; keep those lines out of the terminal.
    logging.getLogger("werkzeug").addFilter(lambda record: "GET /api/distance" not in record.getMessage())
    board.start()
    if os.environ.get("OPEN_BROWSER", "1") != "0":  # set OPEN_BROWSER=0 to stop the page opening by itself
        threading.Timer(1.0, webbrowser.open, args=[f"http://127.0.0.1:{PORT}"]).start()
    app.run(host="127.0.0.1", port=PORT, load_dotenv=False)  # .env is already read by load_env_file()

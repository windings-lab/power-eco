#!/usr/bin/env python3
"""Live power graph: CPU (RAPL) + GPU (nvidia-smi) + estimated rest + monitors.

Serves http://localhost:<port>/ with a live chart fed by Server-Sent Events.
Stdlib only.
"""

import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text())

RAPL_DIR = "/sys/class/powercap/intel-rapl:0"


# ---------------------------------------------------------------- CPU (RAPL)

class Rapl:
    """Package power from the RAPL energy counter (needs read access to energy_uj)."""

    def __init__(self):
        self.path = os.path.join(RAPL_DIR, "energy_uj")
        try:
            self.wrap = int(Path(RAPL_DIR, "max_energy_range_uj").read_text())
        except OSError:
            self.wrap = 2**32
        self.last = None
        self.error = None

    def read(self):
        try:
            e = int(Path(self.path).read_text())
        except PermissionError:
            self.error = f"no read access to {self.path} (see README)"
            return None
        except OSError as ex:
            self.error = str(ex)
            return None
        self.error = None
        now = time.monotonic()
        prev, self.last = self.last, (e, now)
        if prev is None:
            return None
        de = e - prev[0]
        if de < 0:
            de += self.wrap
        dt = now - prev[1]
        return de / 1e6 / dt if dt > 0 else None


# ---------------------------------------------------------------- GPU (NVIDIA)

class NvidiaPower:
    """Keeps one nvidia-smi process streaming power.draw every second."""

    def __init__(self, interval_ms):
        self.value = None
        self.error = None
        self.interval_ms = interval_ms
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                p = subprocess.Popen(
                    ["nvidia-smi", "--query-gpu=power.draw",
                     "--format=csv,noheader,nounits", f"-lms={self.interval_ms}"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                for line in p.stdout:
                    try:
                        # Multi-GPU: one line per GPU per tick; this box has one.
                        self.value = float(line.strip())
                        self.error = None
                    except ValueError:
                        pass
                p.wait()
            except FileNotFoundError:
                self.error = "nvidia-smi not found"
                return
            self.value = None
            self.error = "nvidia-smi exited, restarting"
            time.sleep(5)


# ---------------------------------------------------------------- Monitors

def edid_name(raw: bytes):
    """Monitor name from the EDID display-descriptor (tag 0xFC)."""
    for off in (54, 72, 90, 108):
        d = raw[off:off + 18]
        if len(d) == 18 and d[0:3] == b"\0\0\0" and d[3] == 0xFC:
            return d[5:18].split(b"\n")[0].decode("ascii", "replace").strip()
    return None


class Ddc:
    """Polls DDC/CI power mode (VCP D6) and brightness (VCP 10) where supported.

    ddcutil is slow (~1 s per call), so this runs in its own thread and the
    sampler only reads the cached results.
    """

    def __init__(self, interval_s):
        self.interval_s = interval_s
        self.bus_for = {}     # "card1-DP-1" -> 2   (learned, kept even if monitor goes away)
        self.seen_ok = set()  # connectors that have answered DDC at least once
        self.state = {}       # connector -> {"power": "on"/"standby"/None, "brightness": 0..1 or None, "ok": bool}
        self.lock = threading.Lock()
        self.redetect = threading.Event()
        self.redetect.set()
        threading.Thread(target=self._run, daemon=True).start()

    def _detect(self):
        try:
            out = subprocess.run(["ddcutil", "detect"], capture_output=True,
                                 text=True, timeout=30).stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return
        # Keep "Invalid display" entries too: a monitor that is switched off
        # by its button looks exactly like one without DDC/CI support.
        for block in re.split(r"\n(?=\S)", out):
            bus = re.search(r"/dev/i2c-(\d+)", block)
            conn = re.search(r"DRM_connector:\s+(\S+)", block)
            if bus and conn:
                self.bus_for[conn.group(1)] = int(bus.group(1))

    def _query(self, bus):
        try:
            r = subprocess.run(["ddcutil", "--bus", str(bus), "getvcp", "10", "D6", "--brief"],
                               capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            return {"power": None, "brightness": None, "ok": False}
        res = {"power": None, "brightness": None, "ok": False}
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "VCP":
                code = parts[1].upper()
                if code == "10" and parts[2] == "C" and len(parts) >= 5:
                    cur, mx = int(parts[3]), int(parts[4])
                    res["brightness"] = cur / mx if mx else None
                    res["ok"] = True
                elif code == "D6" and parts[2] == "SNC":
                    # 01 = on, 02/03 = standby/suspend, 04 = off (button), 05 = off (hard)
                    res["power"] = "on" if int(parts[3].lstrip("x"), 16) == 1 else "standby"
                    res["ok"] = True
        return res

    def _run(self):
        while True:
            if self.redetect.is_set():
                self.redetect.clear()
                self._detect()
            for conn, bus in list(self.bus_for.items()):
                try:
                    res = self._query(bus)
                except ValueError:
                    res = {"power": None, "brightness": None, "ok": False}
                with self.lock:
                    self.state[conn] = res
                    if res["ok"]:
                        self.seen_ok.add(conn)
            self.redetect.wait(self.interval_s)

    def get(self, conn):
        with self.lock:
            return self.state.get(conn)


class Monitors:
    def __init__(self, ddc: Ddc):
        self.ddc = ddc
        self.names = {}       # connector -> EDID name (cached by edid bytes)
        self.prev_status = {}

    def _name(self, path, conn):
        try:
            raw = Path(path, "edid").read_bytes()
        except OSError:
            raw = b""
        if raw:
            self.names[conn] = edid_name(raw) or conn
        return self.names.get(conn)

    def read(self):
        out = []
        changed = False
        for path in sorted(glob.glob("/sys/class/drm/card*-*")):
            conn = os.path.basename(path)
            try:
                status = Path(path, "status").read_text().strip()
                enabled = Path(path, "enabled").read_text().strip() == "enabled"
                dpms = Path(path, "dpms").read_text().strip()
            except OSError:
                continue
            name = self._name(path, conn) if status == "connected" else self.names.get(conn)
            if status != self.prev_status.get(conn):
                changed = True
                self.prev_status[conn] = status
            if name is None:
                continue  # never seen a monitor on this port

            cfg = CFG["monitors"].get(name, {**CFG["default_monitor"], "label": name})
            # A monitor counts as DDC-capable if config says so or it has ever
            # answered; only then is DDC silence evidence that it is off.
            ddc_capable = cfg.get("ddc", False) or conn in self.ddc.seen_ok
            ddc = self.ddc.get(conn) if ddc_capable else None
            brightness = ddc["brightness"] if ddc else None

            if status != "connected":
                state, why = "off", "відключено від порту (вимкнено або від'єднано)"
            elif ddc and ddc["power"] == "standby":
                state, why = "off", "вимкнено кнопкою (DDC: power off)"
            elif ddc and not ddc["ok"]:
                state, why = "off", "вимкнено кнопкою (DDC мовчить)"
            elif not enabled:
                state, why = "standby", "вихід вимкнено в KDE"
            elif dpms != "On":
                state, why = "standby", "екран погашено (DPMS)"
            else:
                state, why = "on", "є сигнал" + (" + DDC on" if ddc else "")

            if state == "on":
                b = brightness if brightness is not None else 1.0
                watts = cfg["on_w_min"] + (cfg["on_w_max"] - cfg["on_w_min"]) * b
            elif state == "standby" or (state == "off" and status == "connected"):
                watts = cfg["standby_w"]  # soft-off still draws a little
            else:
                watts = 0.0

            out.append({
                "id": conn, "name": cfg.get("label", name), "state": state, "why": why,
                "watts": round(watts, 1), "brightness": brightness,
                "ddc": ddc_capable,
            })
        if changed:
            self.ddc.redetect.set()  # hotplug: re-learn DDC buses
        return out


# ---------------------------------------------------------------- Sampler

class Sampler:
    def __init__(self):
        self.rapl = Rapl()
        self.gpu = NvidiaPower(int(CFG["sample_interval_s"] * 1000))
        self.mon = Monitors(Ddc(CFG["ddc_interval_s"]))
        n = int(CFG["history_s"] / CFG["sample_interval_s"])
        self.history = deque(maxlen=n)
        self.events = deque(maxlen=200)
        self.energy_wh = 0.0
        self.started = time.time()
        self.listeners = []
        self.lock = threading.Lock()
        self._last_states = {}
        threading.Thread(target=self._run, daemon=True).start()

    def _sample(self):
        cpu = self.rapl.read()
        gpu = self.gpu.value
        mons = self.mon.read()
        eff = CFG["psu_efficiency"]
        dc = (cpu or 0) + (gpu or 0) + CFG["rest_of_system_w"]
        wall_pc = dc / eff
        # "rest" = fixed board estimate + PSU conversion loss, so the stack sums to wall power.
        rest = wall_pc - (cpu or 0) - (gpu or 0)
        mon_total = sum(m["watts"] for m in mons)
        total = wall_pc + mon_total

        now = time.time()
        for m in mons:
            prev = self._last_states.get(m["id"])
            if prev is not None and prev != m["state"]:
                self.events.append({"t": now, "id": m["id"], "name": m["name"],
                                    "from": prev, "to": m["state"], "why": m["why"]})
            self._last_states[m["id"]] = m["state"]

        self.energy_wh += total * CFG["sample_interval_s"] / 3600
        return {
            "t": now,
            "cpu": None if cpu is None else round(cpu, 1),
            "gpu": None if gpu is None else round(gpu, 1),
            "rest": round(rest, 1),
            "monitors": mons,
            "pc": round(wall_pc, 1),
            "total": round(total, 1),
            "energy_wh": round(self.energy_wh, 2),
            "since": self.started,
            "errors": [e for e in (self.rapl.error, self.gpu.error) if e],
        }

    def _run(self):
        interval = CFG["sample_interval_s"]
        nxt = time.monotonic()
        while True:
            s = self._sample()
            with self.lock:
                self.history.append(s)
                listeners = list(self.listeners)
            msg = {"sample": s, "events": list(self.events)[-20:]}
            for q in listeners:
                q.put(msg)
            nxt += interval
            time.sleep(max(0, nxt - time.monotonic()))

    def subscribe(self):
        import queue
        q = queue.Queue(maxsize=100)
        with self.lock:
            self.listeners.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.listeners.remove(q)


# ---------------------------------------------------------------- HTTP

SAMPLER = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/history":
            with SAMPLER.lock:
                data = {"history": list(SAMPLER.history), "events": list(SAMPLER.events),
                        "config": {"price_per_kwh": CFG.get("price_per_kwh"),
                                   "currency": CFG.get("currency", "")}}
            self._send(200, json.dumps(data).encode(), "application/json")
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = SAMPLER.subscribe()
            try:
                while True:
                    msg = q.get()
                    self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                SAMPLER.unsubscribe(q)
        else:
            self._send(404, b"not found", "text/plain")


def main():
    global SAMPLER
    SAMPLER = Sampler()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else CFG["port"]
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print(f"Power graph: http://localhost:{port}/")
    srv.serve_forever()


if __name__ == "__main__":
    main()

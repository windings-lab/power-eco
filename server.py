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


# ---------------------------------------------------------------- Windows mode
# Used when running under WSL (or native Windows): /sys has no RAPL/DRM/DDC,
# so CPU and monitors are read from the Windows host through powershell.exe.

def is_wsl():
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


PS_CPU = r"""
$ErrorActionPreference = 'SilentlyContinue'
$em = (Get-Counter -ListSet 'Energy Meter').PathsWithInstances | Where-Object { $_ -like '*RAPL_Package0_PKG*\Power' } | Select-Object -First 1
$util = '\Processor Information(_Total)\% Processor Utility'
$lhmUrl = '__LHM__'
function Find-Pkg($n, $parent) {
  if ($n.Text -eq 'CPU Package' -and $parent -eq 'Powers') { return $n.Value }
  foreach ($c in $n.Children) { $r = Find-Pkg $c $n.Text; if ($r) { return $r } }
}
while ($true) {
  $l = $null
  if ($lhmUrl) { try { $l = Find-Pkg (Invoke-RestMethod -Uri $lhmUrl -TimeoutSec 1) '' } catch {} }
  $paths = @($util); if ($em) { $paths += $em }
  $s = (Get-Counter -Counter $paths).CounterSamples
  $u = ($s | Where-Object { $_.Path -like '*processor utility' }).CookedValue
  $p = $null; if ($em) { $p = ($s | Where-Object { $_.Path -like '*\power' }).CookedValue }
  [Console]::Out.WriteLine((@{ util = $u; mw = $p; lhm = $l } | ConvertTo-Json -Compress))
  [Console]::Out.Flush()
}
"""

PS_MON = r"""
$ErrorActionPreference = 'SilentlyContinue'
$br = @{}
Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness | ForEach-Object { $br[$_.InstanceName] = $_.CurrentBrightness }
$list = @(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorID | ForEach-Object {
  $n = -join ($_.UserFriendlyName | Where-Object { $_ -ne 0 } | ForEach-Object { [char]$_ })
  @{ id = $_.InstanceName; name = $n; brightness = $br[$_.InstanceName]; active = [bool]$_.Active }
})
ConvertTo-Json -Compress -InputObject $list
"""


PS_DDC = r"""
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices; using System.Text; using System.Collections.Generic;
public class Ddc {
  delegate bool EnumProc(IntPtr h, IntPtr hdc, IntPtr r, IntPtr d);
  [StructLayout(LayoutKind.Sequential)] struct RECT { public int l, t, r, b; }
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] struct MI { public int cb; public RECT rc; public RECT rw; public uint fl; [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dev; }
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] struct DD { public int cb; [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string name; [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string str; public int flags; [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string id; [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string key; }
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode, Pack = 1)] struct PM { public IntPtr h; [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string desc; }
  [DllImport("user32.dll")] static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr clip, EnumProc cb, IntPtr d);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern bool GetMonitorInfo(IntPtr h, ref MI mi);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern bool EnumDisplayDevices(string dev, uint i, ref DD dd, uint fl);
  [DllImport("dxva2.dll")] static extern bool GetNumberOfPhysicalMonitorsFromHMONITOR(IntPtr h, out uint n);
  [DllImport("dxva2.dll")] static extern bool GetPhysicalMonitorsFromHMONITOR(IntPtr h, uint n, [Out] PM[] a);
  [DllImport("dxva2.dll")] static extern bool GetVCPFeatureAndVCPFeatureReply(IntPtr h, byte code, out uint type, out uint cur, out uint max);
  [DllImport("dxva2.dll")] static extern bool DestroyPhysicalMonitors(uint n, PM[] a);
  public static string Query() {
    List<string> items = new List<string>();
    EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, delegate (IntPtr hm, IntPtr hdc, IntPtr r, IntPtr d) {
      MI mi = new MI(); mi.cb = Marshal.SizeOf(typeof(MI));
      if (!GetMonitorInfo(hm, ref mi)) return true;
      DD dd = new DD(); dd.cb = Marshal.SizeOf(typeof(DD));
      string code = "";
      for (uint q = 0; q < 8; q++) {
        DD x = new DD(); x.cb = Marshal.SizeOf(typeof(DD));
        if (!EnumDisplayDevices(mi.dev, q, ref x, 1)) break;
        if ((x.flags & 1) == 0) {  // attached but not part of the desktop (e.g. TV switched off)
          string[] pi = x.id.Split('#');
          if (pi.Length > 2) items.Add("{\"code\":\"" + (pi[1] + "\\" + pi[2]).Replace("\\", "\\\\") + "\",\"inactive\":true}");
          continue;
        }
        string[] p = x.id.Split('#'); if (p.Length > 2) code = p[1] + "\\" + p[2];
        break;
      }
      uint n; if (!GetNumberOfPhysicalMonitorsFromHMONITOR(hm, out n) || n == 0) return true;
      PM[] a = new PM[n]; if (!GetPhysicalMonitorsFromHMONITOR(hm, n, a)) return true;
      for (int i = 0; i < n; i++) {
        uint t, cur, max; string pw = "null", br = "null", ok = "false";
        if (GetVCPFeatureAndVCPFeatureReply(a[i].h, 0x10, out t, out cur, out max) && max > 0) { br = string.Format(System.Globalization.CultureInfo.InvariantCulture, "{0:0.####}", (double)cur / max); ok = "true"; }
        if (GetVCPFeatureAndVCPFeatureReply(a[i].h, 0xD6, out t, out cur, out max)) { pw = (cur & 0xFF).ToString(); ok = "true"; }
        items.Add("{\"code\":\"" + code.Replace("\\", "\\\\") + "\",\"power\":" + pw + ",\"brightness\":" + br + ",\"ok\":" + ok + "}");
      }
      DestroyPhysicalMonitors(n, a);
      return true;
    }, IntPtr.Zero);
    return "[" + string.Join(",", items.ToArray()) + "]";
  }
}
'@
while ($true) {
  [Console]::Out.WriteLine([Ddc]::Query())
  [Console]::Out.Flush()
  Start-Sleep -Seconds __INT__
}
"""


def powershell(script, **kw):
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, **kw)


class WinCpu:
    """CPU power: Energy Meter (RAPL) counter if Windows exposes it, else estimated from utilisation."""

    def __init__(self):
        w = CFG.get("windows", {})
        self.idle_w = w.get("cpu_idle_w", 8)
        self.max_w = w.get("cpu_max_w", 65)
        self.alpha = w.get("cpu_smooth", 0.25)  # EMA factor for the utilisation estimate (1 = no smoothing)
        self._ema = None
        self.value = None
        self.source = None
        self.error = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                p = subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                     PS_CPU.replace("__LHM__", CFG.get("windows", {}).get("lhm_url", ""))],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                for line in p.stdout:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    lhm = None
                    if d.get("lhm"):
                        m = re.search(r"[\d.,]+", str(d["lhm"]))
                        if m:
                            try:
                                lhm = float(m.group().replace(",", "."))
                            except ValueError:
                                pass
                    if lhm is not None:
                        self.value, self.source = lhm, "librehardwaremonitor"
                    elif d.get("mw") is not None:
                        self.value, self.source = d["mw"] / 1000.0, "energy-meter"
                    elif d.get("util") is not None:
                        u = min(max(d["util"], 0), 100) / 100
                        raw = self.idle_w + (self.max_w - self.idle_w) * u
                        self._ema = raw if self._ema is None else self._ema + self.alpha * (raw - self._ema)
                        self.value = self._ema
                        self.source = "estimate"
                    self.error = None
                p.wait()
            except FileNotFoundError:
                self.error = "powershell.exe not found (windows mode)"
                return
            self.value = None
            self.error = "powershell exited, restarting"
            time.sleep(5)

    def read(self):
        return self.value


class WinMonitors:
    """Monitors on Windows: names/active from WMI, power state + brightness from DDC/CI
    (dxva2 Monitor Configuration API), same VCP D6/10 logic as the Linux Ddc class."""

    def __init__(self):
        self.items = []       # WMI: id, name, active, brightness
        self.ddc = {}         # PnP code (e.g. "DELA021") -> {"power","brightness","ok"}
        self.seen_ok = set()
        self.inactive = set()  # attached to a port but not in the desktop: switched off
        self.lock = threading.Lock()
        self.error = None
        threading.Thread(target=self._wmi, daemon=True).start()
        threading.Thread(target=self._ddc, daemon=True).start()

    def _wmi(self):
        while True:
            try:
                out = powershell(PS_MON, timeout=60).stdout.strip()
                data = json.loads(out) if out else []
                if isinstance(data, dict):
                    data = [data]
                with self.lock:
                    self.items = data
                self.error = None
            except (FileNotFoundError, subprocess.TimeoutExpired, ValueError) as ex:
                self.error = f"monitor query failed: {ex}"
            time.sleep(max(CFG["ddc_interval_s"], 30))

    def _ddc(self):
        script = PS_DDC.replace("__INT__", str(int(CFG["ddc_interval_s"])))
        while True:
            try:
                p = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                for line in p.stdout:
                    try:
                        arr = json.loads(line)
                    except ValueError:
                        continue
                    cur, inactive = {}, set()
                    for e in arr:
                        if e.get("inactive"):
                            inactive.add(e["code"])
                            continue
                        cur[e["code"]] = {"power": None if e["power"] is None else ("on" if e["power"] == 1 else "standby"),
                                          "brightness": e["brightness"], "ok": e["ok"]}
                    with self.lock:
                        self.ddc = cur
                        self.inactive = inactive - set(cur)
                        self.seen_ok |= {c for c, v in cur.items() if v["ok"]}
                p.wait()
            except FileNotFoundError:
                return
            time.sleep(5)

    def read(self):
        with self.lock:
            items, ddc, seen = list(self.items), dict(self.ddc), set(self.seen_ok)
            inactive = set(self.inactive)
        out = []
        for m in items:
            name = m.get("name") or m["id"]
            parts = m["id"].split("\\")
            code = "\\".join(parts[1:3]).rsplit("_", 1)[0] if len(parts) >= 3 else m["id"]
            cfg = CFG["monitors"].get(name, {**CFG["default_monitor"], "label": name})
            capable = cfg.get("ddc", False) or code in seen
            d = ddc.get(code) if capable else None
            b = d["brightness"] if d and d["brightness"] is not None else (
                m["brightness"] / 100 if m.get("brightness") is not None else None)

            if not m.get("active", True):
                state, why = "off", "Windows: вимкнено/від'єднано"
            elif code in inactive and not d:
                state, why = "off", "Windows: підключено, але не активний (вимкнено)"
            elif capable and d and d["power"] == "standby":
                state, why = "off", "вимкнено кнопкою (DDC: power off)"
            elif capable and d is None:
                state, why = "off", "вимкнено кнопкою (DDC мовчить)"
            elif capable and d and not d["ok"]:
                state, why = "off", "вимкнено кнопкою (DDC мовчить)"
            else:
                state, why = "on", "є сигнал" + (" + DDC on" if d else " (стан кнопки невідомий)")

            if state == "on":
                watts = cfg["on_w_min"] + (cfg["on_w_max"] - cfg["on_w_min"]) * (b if b is not None else 1.0)
            elif m.get("active", True) or code in inactive:
                watts = cfg["standby_w"]
            else:
                watts = 0.0
            out.append({"id": m["id"], "name": cfg.get("label", name), "state": state, "why": why,
                        "watts": round(watts, 1), "brightness": b, "ddc": capable})
        return out


# ---------------------------------------------------------------- Sampler

class Sampler:
    def __init__(self, mode="linux"):
        self.mode = mode
        self.gpu = NvidiaPower(int(CFG["sample_interval_s"] * 1000))
        if mode == "windows":
            self.rapl = WinCpu()
            self.mon = WinMonitors()
        else:
            self.rapl = Rapl()
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
            "errors": [e for e in (self.rapl.error, self.gpu.error, getattr(self.mon, "error", None)) if e],
            "mode": self.mode,
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
    args = sys.argv[1:]
    mode = CFG.get("mode", "auto")
    for flag in ("--windows", "--linux"):
        if flag in args:
            args.remove(flag)
            mode = flag[2:]
    if mode == "auto":
        mode = "windows" if is_wsl() else "linux"
    SAMPLER = Sampler(mode)
    port = int(args[0]) if args else CFG["port"]
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print(f"Power graph ({mode} mode): http://localhost:{port}/")
    srv.serve_forever()


if __name__ == "__main__":
    main()

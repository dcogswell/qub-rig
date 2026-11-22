#!/usr/bin/env python3
"""clorqub rig agent — runs inside a Clore rental (cloreai/hiveos image).

Supervises one Qubic miner at a time (jetski or qli): downloads and
sha256-verifies the pool's HiveOS custom-miner package, renders
appsettings.json, runs the miner, mirrors stats.*.lock files, and writes
/clorqub/status.json every 5s. `agent.py switch <pool>` queues a pool
switch via /clorqub/control.json. Stdlib only.
"""

import argparse
import fcntl
import hashlib
import json
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import tempfile
import termios
import threading
import time
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("CLORQUB_BASE", "/clorqub")
CONTROL_PATH = os.path.join(BASE, "control.json")
STATE_PATH = os.path.join(BASE, "state.json")
STATUS_PATH = os.path.join(BASE, "status.json")
CREDS_PATH = os.path.join(BASE, "credentials.json")
LOGS_DIR = os.path.join(BASE, "logs")
PKGS_DIR = os.path.join(BASE, "pkgs")
STATS_DIR = os.path.join(BASE, "stats")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qubic_parse as qp  # noqa: E402

PKG_URL_QLI = (
    "https://github.com/qubic-li/hiveos/releases/download/latest/"
    "qubminer-latest.tar.gz")
PKG_URL_JETSKI_PPLNS = (
    "https://github.com/jtskxx/Jetski-Qubic-Pool/releases/download/latest/"
    "qubjetski.PPLNS-latest.tar.gz")
PKG_URL_JETSKI_SOLO = (
    "https://github.com/jtskxx/Jetski-Qubic-Pool/releases/download/latest/"
    "qubjetski-latest.tar.gz")

JETSKI_URLS = {
    "pplns": "wss://pplns.jtskxpool.ai/ws",
    "solo": "wss://solo.jtskxpool.ai/ws",
}
QLI_POOL_URL = "wss://wps.qubic.li/ws"
QLI_BINARY = "qli-Client"
JETSKI_BINARY = "qubjetski-Client"
JETSKI_API = "http://127.0.0.1:17899/summary"

LOOP_SECONDS = 5
STATUS_MIN_INTERVAL = 1.0


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log(msg):
    print(f"{utcnow()} [agent] {msg}", flush=True)


def agent_version():
    try:
        with open(__file__, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Config rendering
# ---------------------------------------------------------------------------

def render_jetski_config(mode, wallet, alias, threads, gpu):
    return {
        "pool": {
            "url": JETSKI_URLS[mode],
            "wallet": wallet,
            "alias": alias,
            "pps": mode == "pplns",
        },
        "miner": {
            "cpu": {"enabled": True, "version": "auto", "threads": threads},
            "gpu": {"enabled": gpu, "version": "CUDA", "cards": "all"},
        },
        "api": {"bind": "127.0.0.1:17899"},
    }


def render_qli_config(access_token, alias, threads, gpu):
    return {
        "ClientSettings": {
            "poolAddress": QLI_POOL_URL,
            "alias": alias,
            "accessToken": access_token,
            "qubicAddress": None,
            "displayDetailedHashrates": True,
            "trainer": {"cpu": True, "gpu": gpu, "cpuThreads": threads},
        }
    }


# ---------------------------------------------------------------------------
# Package download / verification
# ---------------------------------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_text(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "clorqub-agent"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_expected_hash(pkg_url):
    """The .hash sibling of a package URL holds the bare sha256 hex."""
    text = fetch_text(pkg_url.replace(".tar.gz", ".hash"))
    m = re.search(r"[0-9a-fA-F]{64}", text)
    if not m:
        raise ValueError(f"no sha256 in {pkg_url}.hash: {text[:80]!r}")
    return m.group(0).lower()


def download(url, dest, timeout=300):
    req = urllib.request.Request(url, headers={"User-Agent": "clorqub-agent"})
    with urllib.request.urlopen(req, timeout=timeout) as r, \
            open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 16)
    return os.path.getsize(dest)


def read_manifest_version(pkg_dir):
    manifest = os.path.join(pkg_dir, "h-manifest.conf")
    try:
        with open(manifest) as f:
            for line in f:
                m = re.match(r"\s*CUSTOM_VERSION\s*=\s*(\S+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def _top_dir(pkg_dir):
    entries = [e for e in os.listdir(pkg_dir)
               if os.path.isdir(os.path.join(pkg_dir, e))]
    return entries[0] if entries else None


def fetch_package(url, pool_root):
    """Download + verify + extract a miner package under pool_root/<sha>/.

    Returns {"url", "sha256", "version", "size", "seconds", "dir"}.
    Reuses an existing extracted dir for the same sha256."""
    os.makedirs(pool_root, exist_ok=True)
    expected = fetch_expected_hash(url)
    final_dir = os.path.join(pool_root, expected)
    inner = _top_dir(final_dir) if os.path.isdir(final_dir) else None
    if inner:
        return {"url": url, "sha256": expected,
                "version": read_manifest_version(
                    os.path.join(final_dir, inner)),
                "size": None, "seconds": 0.0,
                "dir": os.path.join(final_dir, inner), "cached": True}
    t0 = time.time()
    fd, tmp = tempfile.mkstemp(suffix=".tar.gz", dir=pool_root)
    os.close(fd)
    try:
        size = download(url, tmp)
        got = sha256_file(tmp)
        if got != expected:
            raise ValueError(
                f"sha256 mismatch: expected {expected}, got {got}")
        os.makedirs(final_dir, exist_ok=True)
        with tarfile.open(tmp) as tf:
            tf.extractall(final_dir, filter="data")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    inner = _top_dir(final_dir)
    if not inner:
        raise ValueError(f"no package dir in {final_dir}")
    pkg_dir = os.path.join(final_dir, inner)
    return {"url": url, "sha256": expected,
            "version": read_manifest_version(pkg_dir),
            "size": size, "seconds": round(time.time() - t0, 2),
            "dir": pkg_dir, "cached": False}


# ---------------------------------------------------------------------------
# Status writing — never leaks credential values
# ---------------------------------------------------------------------------

def sanitize_status(obj, secrets):
    """Recursively copy obj, replacing any string containing a secret with
    'REDACTED'. Defense-in-depth: secrets should never be added to the
    status dict in the first place."""
    hits = [s for s in secrets if s]
    if isinstance(obj, dict):
        return {k: sanitize_status(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_status(v, secrets) for v in obj]
    if isinstance(obj, str):
        for s in hits:
            if s in obj:
                return "REDACTED"
        return obj
    return obj


def write_status(status, secrets=()):
    status = sanitize_status(status, secrets)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATUS_PATH) or ".",
                               prefix=".status.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(status, f, indent=2)
        os.rename(tmp, STATUS_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Miner process supervision
# ---------------------------------------------------------------------------

def _ensure_viewer(raw_log_path):
    """Make stock `miner`/`miner log` work: a detached screen session
    named `miner` tails the raw log, and /run/hive/miner.1 links to it.
    Best-effort -- every failure is logged and ignored."""
    try:
        os.makedirs("/run/hive", exist_ok=True)
        os.makedirs("/run/screen", exist_ok=True)
        try:
            os.chmod("/run/screen", 0o777)
        except OSError:
            pass
        subprocess.run(["screen", "-S", "miner", "-X", "quit"],
                       capture_output=True, timeout=10)
        # tail lives in window 1 like stock HiveOS so `miner log`'s
        # screen-running check (-p 1) and bare `miner` attach both work
        subprocess.run(["screen", "-dmS", "miner"],
                       capture_output=True, timeout=10)
        subprocess.run(["screen", "-S", "miner", "-X", "screen", "1",
                        "tail", "-n", "+1", "-F", raw_log_path],
                       capture_output=True, timeout=10)
        subprocess.run(["screen", "-S", "miner", "-p", "0", "-X",
                        "stuff", "exit\n"],
                       capture_output=True, timeout=10)
        hive_log = "/run/hive/miner.1"
        if os.path.lexists(hive_log):
            os.remove(hive_log)
        os.symlink(raw_log_path, hive_log)
    except Exception as e:
        log(f"miner viewer setup failed: {e}")


def _viewer_alive():
    try:
        return subprocess.run(
            ["screen", "-S", "miner", "-Q", "select", "."],
            capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


class MinerProc:
    """Runs the miner in its own process group under a pty. The raw
    (colorized) stream is appended to logs/<pool>.raw.log for the `miner`
    screen viewer; the ANSI-stripped copy keeps going to logs/<pool>.log
    with a UTC timestamp prefix. Falls back to plain pipes if no pty."""

    def __init__(self, pool, pkg_dir, binary, args):
        self.pool = pool
        self.pkg_dir = pkg_dir
        self.cmd = [os.path.join(pkg_dir, binary)] + args
        self.log_path = os.path.join(LOGS_DIR, f"{pool}.log")
        self.raw_path = os.path.join(LOGS_DIR, f"{pool}.raw.log")
        self.proc = None
        self.started_at = None

    def start(self):
        os.makedirs(LOGS_DIR, exist_ok=True)
        logf = open(self.log_path, "ab", buffering=0)
        try:
            master, slave = pty.openpty()
        except OSError as e:
            log(f"pty unavailable ({e}); piping miner output")
            self.proc = subprocess.Popen(
                self.cmd, cwd=self.pkg_dir,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True)
            pump, args = self._pump_pipe, (self.proc, logf)
        else:
            try:
                fcntl.ioctl(slave, termios.TIOCSWINSZ,
                            struct.pack("HHHH", 60, 200, 0, 0))
            except OSError:
                pass
            rawf = open(self.raw_path, "ab", buffering=0)
            try:
                self.proc = subprocess.Popen(
                    self.cmd, cwd=self.pkg_dir,
                    stdin=slave, stdout=slave, stderr=slave,
                    start_new_session=True)
            except Exception:
                os.close(master)
                rawf.close()
                logf.close()
                raise
            finally:
                os.close(slave)
            pump, args = self._pump_pty, (master, logf, rawf)
        self.started_at = utcnow()
        threading.Thread(target=pump, args=args, daemon=True).start()
        _ensure_viewer(self.raw_path)
        log(f"started {self.pool} pid={self.proc.pid} "
            f"cmd={' '.join(self.cmd)}")

    @staticmethod
    def _stamp(logf, raw):
        line = qp.ANSI_RE.sub(
            "", raw.decode("utf-8", errors="replace").rstrip("\r"))
        logf.write(f"{utcnow()} {line}\n".encode())

    @staticmethod
    def _pump_pty(master, logf, rawf):
        buf = b""
        try:
            while True:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break  # EIO once the child exits (Linux)
                if not chunk:
                    break
                rawf.write(chunk)
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    MinerProc._stamp(logf, raw)
            if buf:
                MinerProc._stamp(logf, buf)
        finally:
            os.close(master)
            rawf.close()
            logf.close()

    @staticmethod
    def _pump_pipe(proc, logf):
        try:
            for raw in proc.stdout:
                MinerProc._stamp(logf, raw.rstrip(b"\n"))
        finally:
            logf.close()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self, grace=15):
        try:
            subprocess.run(["screen", "-S", "miner", "-X", "quit"],
                           capture_output=True, timeout=10)
        except Exception:
            pass
        if self.proc is None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.time() + grace
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return
            time.sleep(0.2)
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        self.proc.wait(timeout=10)


def hardware_info():
    info = {"nproc": os.cpu_count(), "cpu_model": None,
            "avx2": False, "avx512f": False, "gpus": None}
    try:
        with open("/proc/cpuinfo") as f:
            text = f.read()
        m = re.search(r"model name\s*:\s*(.+)", text)
        if m:
            info["cpu_model"] = m.group(1).strip()
        info["avx2"] = " avx2 " in text or "\tavx2" in text
        info["avx512f"] = "avx512f" in text
    except OSError:
        pass
    try:
        out = subprocess.run(["nvidia-smi", "-L"], capture_output=True,
                             text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            info["gpus"] = out.stdout.strip().splitlines()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return info


# Per-device hashrate tags, e.g. "[AVX2] 1 it/s | [CUDA] 84 it/s" (jetski,
# one line) or separate lines per device (qli).
DEVICE_RATE_RE = re.compile(
    r"\[([A-Za-z0-9_]+)\]\s*(\d+(?:\.\d+)?k?)\s*it/s")
AVG_RATE_RE = re.compile(r"(\d+(?:\.\d+)?k?)\s+avg it/s")
CPU_TAGS = {"AVX2", "AVX512", "GENERIC"}
GPU_TAGS = {"CUDA", "AMD"}


def parse_its(log_path, max_lines=300):
    """Latest per-device it/s from a miner log tail.

    Returns {"cpu": int|None, "gpu": int|None, "total": int|None,
             "avg": int|None, "epoch": int|None,
             "devices": {tag: latest_rate}}.
    Each device takes its rate from the most recent line mentioning it,
    which handles qli alternating CPU/GPU lines and jetski combined
    lines. Bare "GPU #0: N it/s" lines carry no [tag] and are ignored."""
    devices = {}
    avg = epoch = None
    for line in reversed(qp.tail_log_lines(log_path, max_lines=max_lines)):
        for m in DEVICE_RATE_RE.finditer(line):
            tag = m.group(1)
            if tag not in devices:
                devices[tag] = qp.rate_value(m.group(2))
        if avg is None:
            am = AVG_RATE_RE.search(line)
            if am:
                avg = qp.rate_value(am.group(1))
        if epoch is None:
            sm = qp.STATUS_RE.search(line) or qp.SHARES_ONLY_RE.search(line)
            if sm:
                epoch = int(sm.group("epoch"))
    cpu_rates = [v for t, v in devices.items() if t in CPU_TAGS]
    gpu_rates = [v for t, v in devices.items() if t in GPU_TAGS]
    cpu = sum(cpu_rates) if cpu_rates else None
    gpu = sum(gpu_rates) if gpu_rates else None
    total = ((cpu or 0) + (gpu or 0)) if (cpu is not None
                                         or gpu is not None) else None
    return {"cpu": cpu, "gpu": gpu, "total": total, "avg": avg,
            "epoch": epoch, "devices": devices}


def jetski_summary():
    try:
        return json.loads(fetch_text(JETSKI_API, timeout=3))
    except Exception:
        return None


def find_binary(pkg_dir, pool):
    name = JETSKI_BINARY if pool == "jetski" else QLI_BINARY
    path = os.path.join(pkg_dir, name)
    if os.path.isfile(path):
        return name
    # fall back to any *-Client in the package dir
    for e in os.listdir(pkg_dir):
        if e.endswith("-Client"):
            return e
    raise FileNotFoundError(f"no miner binary in {pkg_dir}")


def read_control():
    try:
        with open(CONTROL_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


POOLS = ("jetski", "qli")


def load_state():
    """Load persisted rig state; {} on missing/corrupt file."""
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    """Atomically persist rig state (tmp + rename)."""
    fd, tmp = tempfile.mkstemp(dir=BASE, prefix=".state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.rename(tmp, STATE_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def resolve_pool(state, env_pool):
    """Return (pool, source). A valid persisted pool wins over the env
    default so restarts (reboot, spot pause/resume) don't revert a switch."""
    if state.get("pool") in POOLS:
        return state["pool"], "state"
    if env_pool in POOLS:
        return env_pool, "env"
    return "jetski", "env"


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, "")))
    except ValueError:
        return default


def pkg_url_for(pool, jetski_mode):
    if pool == "qli":
        return os.environ.get("PKG_URL_QLI", PKG_URL_QLI)
    if jetski_mode == "solo":
        return os.environ.get("PKG_URL_JETSKI_SOLO", PKG_URL_JETSKI_SOLO)
    return os.environ.get("PKG_URL_JETSKI_PPLNS", PKG_URL_JETSKI_PPLNS)


def other_pool(pool):
    return "qli" if pool == "jetski" else "jetski"


def render_config(pool, jetski_mode, creds, alias, threads, gpu):
    if pool == "jetski":
        return render_jetski_config(
            jetski_mode, creds.get("jetski_wallet"), alias, threads, gpu)
    return render_qli_config(
        creds.get("qli_access_token"), alias, threads, gpu)


def secrets_of(creds):
    return [v for v in creds.values() if isinstance(v, str) and v]


def cmd_run(_args):
    os.makedirs(BASE, exist_ok=True)
    for d in (LOGS_DIR, PKGS_DIR, STATS_DIR):
        os.makedirs(d, exist_ok=True)

    timeline = {"agent_start": utcnow()}
    state = {
        "agent_version": agent_version(),
        "boot": timeline,
        "hardware": hardware_info(),
        "switches": [],
        "last_error": None,
        "jetski_mode": os.environ.get("JETSKI_MODE", "pplns"),
    }
    jetski_mode = state["jetski_mode"]
    if jetski_mode not in JETSKI_URLS:
        log(f"bad JETSKI_MODE={jetski_mode}, defaulting to pplns")
        jetski_mode = state["jetski_mode"] = "pplns"
    env_pool = os.environ.get("POOL", "jetski")
    rig_state = load_state()
    pool, pool_source = resolve_pool(rig_state, env_pool)
    if env_pool not in POOLS:
        log(f"bad POOL={env_pool}, defaulting to jetski")
    rig_state["boot_count"] = int(rig_state.get("boot_count") or 0) + 1
    rig_state["pool"] = pool
    rig_state["updated_at"] = utcnow()
    save_state(rig_state)
    state["boot_count"] = rig_state["boot_count"]
    state["pool_source"] = pool_source
    alias = os.environ.get("ALIAS")
    if not alias:
        _animals = ("badger", "bear", "cobra", "crow", "eagle",
                    "falcon", "fox", "heron", "koala", "lemur",
                    "lynx", "otter", "owl", "puma", "raven", "seal",
                    "sloth", "tiger", "viper", "wolf")
        _h = hashlib.sha256(os.uname().nodename.encode()).digest()
        alias = f"rig-{_animals[_h[0] % len(_animals)]}"
    threads = env_int("CPU_THREADS", max(1, (os.cpu_count() or 2) - 1))
    # never let an explicit count eat the whole visible CPU: the
    # stock amountOfThreads:0 semantics are nproc-1, so clamp to it
    if threads <= 0 or threads >= (os.cpu_count() or 2):
        threads = max(1, (os.cpu_count() or 2) - 1)
    gpu = os.environ.get("GPU", "off") == "on"
    packages = {}  # pool -> pkg info dict
    log(f"agent start pool={pool} (from {pool_source}) "
        f"boot_count={rig_state['boot_count']} jetski_mode={jetski_mode} "
        f"alias={alias} threads={threads} gpu={gpu}")

    # --- wait for credentials (never logged) ---
    creds = {}
    while not creds.get("jetski_wallet") or not creds.get("qli_access_token"):
        try:
            with open(CREDS_PATH) as f:
                creds = json.load(f)
        except (OSError, json.JSONDecodeError):
            creds = {}
        if creds.get("jetski_wallet") and creds.get("qli_access_token"):
            break
        time.sleep(2)
    timeline["cred_ready"] = utcnow()
    secrets = secrets_of(creds)
    log("credentials ready")

    miner = None
    backoff = 5
    bg_fetch = {}  # pool -> thread

    def start_bg_fetch(p):
        if p in packages or p in bg_fetch and bg_fetch[p].is_alive():
            return

        def work():
            try:
                packages[p] = fetch_package(
                    pkg_url_for(p, jetski_mode),
                    os.path.join(PKGS_DIR, p))
                log(f"background fetch {p}: {packages[p]['version']} "
                    f"cached={packages[p].get('cached')}")
            except Exception as e:
                log(f"background fetch {p} failed: {e}")
        t = threading.Thread(target=work, daemon=True)
        bg_fetch[p] = t
        t.start()

    def ensure_package(p):
        """Fetch p's package with retries; caches in `packages`."""
        if p in packages:
            return packages[p]
        key = f"pkg_download_start_{p}"
        timeline[key] = utcnow()
        attempt = 0
        while True:
            attempt += 1
            try:
                info = fetch_package(pkg_url_for(p, jetski_mode),
                                     os.path.join(PKGS_DIR, p))
                packages[p] = info
                timeline[f"pkg_download_end_{p}"] = utcnow()
                log(f"package {p}: version={info['version']} "
                    f"sha256={info['sha256'][:12]}... "
                    f"seconds={info['seconds']} cached={info.get('cached')}")
                return info
            except Exception as e:
                state["last_error"] = f"package fetch {p}: {e}"
                log(f"package fetch {p} attempt {attempt} failed: {e}")
                write_status(build_status(None), secrets)
                if attempt < 3:
                    time.sleep(min(5 * attempt, 30))
                else:
                    attempt = 0
                    time.sleep(60)

    def build_status(miner_obj, pkg_info=None):
        pkg_info = pkg_info or packages.get(pool)
        rate = qp.tail_hashrate(
            os.path.join(LOGS_DIR, f"{pool}.log"))
        shares = {"log_found": rate["found"],
                  "log_submitted": rate["submitted"],
                  "log_rejected": rate["rejected"]}
        stats_mirror = os.path.join(STATS_DIR, pool)
        files = qp.find_stats_files(stats_mirror, n=1)
        if files:
            parsed = qp.parse_stats_file(files[0][1])
            if parsed:
                shares.update({
                    "epoch": parsed["epoch"],
                    "found": parsed["found_shares"],
                    "submitted": parsed["submitted_shares"],
                    "invalid": parsed["invalid_shares"],
                    "best_score": parsed["best_score"],
                    "score_n": parsed["score_n"],
                    "score_med": parsed["score_med"],
                    "score_sum": parsed["score_sum"],
                })
        trainer = {}
        for line in reversed(qp.tail_log_lines(
                os.path.join(LOGS_DIR, f"{pool}.log"), max_lines=200)):
            m = qp.TRAINER_UPDATED_RE.search(line)
            if m:
                trainer = {"variant": m.group("variant"),
                           "version": m.group("ver")}
                break
            m = qp.TRAINER_START_RE.search(line)
            if m:
                trainer = {"flavor": m.group("flavor"),
                           "version": m.group("ver")}
                break
            m = qp.TRAINER_VARIANT_RE.search(line)
            if m:
                trainer = {"variant": m.group("variant")
                           or m.group("variant2")}
                break
        return {
            "pool": pool,
            "pool_source": state["pool_source"],
            "boot_count": state["boot_count"],
            "jetski_mode": jetski_mode,
            "alias": alias,
            "package": {k: pkg_info.get(k) for k in
                        ("url", "version", "sha256", "size", "seconds")}
            if pkg_info else None,
            "miner": {
                "pid": miner_obj.proc.pid if miner_obj and miner_obj.proc
                else None,
                "running": bool(miner_obj and miner_obj.alive()),
                "started_at": miner_obj.started_at if miner_obj else None,
            },
            "its": parse_its(os.path.join(LOGS_DIR, f"{pool}.log")),
            "shares": shares,
            "trainer": trainer or None,
            "jetski_summary": jetski_summary() if pool == "jetski" else None,
            "boot": timeline,
            "hardware": state["hardware"],
            "switches": state["switches"],
            "last_error": state["last_error"],
            "agent_version": state["agent_version"],
            "updated_at": utcnow(),
        }

    # TODO(PLAN.md "Updates"): check each pool's .hash URL on
    # package_check_interval (default 1h); on a new release, download +
    # verify and restart the miner onto it at a safe point. Skipped in v1.

    while True:
        pkg = ensure_package(pool)
        cfg = render_config(pool, jetski_mode, creds, alias, threads, gpu)
        cfg_path = os.path.join(pkg["dir"], "appsettings.json")
        fd = os.open(cfg_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
        binary = find_binary(pkg["dir"], pool)
        args = ["-start"] if pool == "jetski" else []

        if miner is None or not miner.alive():
            miner = MinerProc(pool, pkg["dir"], binary, args)
            miner.start()
            timeline.setdefault("miner_start", utcnow())
            start_bg_fetch(other_pool(pool))

        switched = False
        while True:
            time.sleep(LOOP_SECONDS)
            # mirror stats + status
            try:
                qp.mirror_stats_files(
                    pkg["dir"], os.path.join(STATS_DIR, pool))
            except Exception as e:
                log(f"stats mirror failed: {e}")
            rate = qp.tail_hashrate(miner.log_path)
            if (parse_its(miner.log_path)["total"] or 0) > 0 \
                    and "first_its" not in timeline:
                timeline["first_its"] = utcnow()
            if rate["found"] and "first_share" not in timeline:
                timeline["first_share"] = utcnow()
            try:
                write_status(build_status(miner, pkg), secrets)
            except Exception as e:
                log(f"status write failed: {e}")
            if not _viewer_alive():
                _ensure_viewer(miner.raw_path)

            ctrl = read_control()
            if ctrl and ctrl.get("switch_to") in ("jetski", "qli") \
                    and ctrl["switch_to"] != pool:
                new_pool = ctrl["switch_to"]
                log(f"switch requested: {pool} -> {new_pool}")
                miner.stop()
                state["switches"].append(
                    {"ts": utcnow(), "from": pool, "to": new_pool})
                pool = new_pool
                rig_state["pool"] = pool
                rig_state["updated_at"] = utcnow()
                save_state(rig_state)
                try:
                    os.unlink(CONTROL_PATH)
                except OSError:
                    pass
                if pool in bg_fetch and bg_fetch[pool].is_alive():
                    bg_fetch[pool].join(timeout=300)
                backoff = 5
                switched = True
                break
            elif ctrl and ctrl.get("switch_to") == pool:
                try:
                    os.unlink(CONTROL_PATH)
                except OSError:
                    pass

            if not miner.alive():
                state["last_error"] = (
                    f"{pool} exited rc={miner.proc.returncode}; "
                    f"restarting in {backoff}s")
                log(state["last_error"])
                write_status(build_status(miner, pkg), secrets)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                miner = MinerProc(pool, pkg["dir"], binary, args)
                miner.start()
            else:
                backoff = 5
        if not switched:
            break  # unreachable today; inner loop only exits on switch


def cmd_switch(args):
    if args.pool not in ("jetski", "qli"):
        print("pool must be jetski or qli", file=sys.stderr)
        return 2
    payload = {"switch_to": args.pool, "requested_at": utcnow()}
    fd, tmp = tempfile.mkstemp(dir=BASE, prefix=".control.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    os.rename(tmp, CONTROL_PATH)
    print(f"queued switch to {args.pool}")
    return 0


def cmd_status(_args):
    try:
        with open(STATUS_PATH) as f:
            print(f.read())
    except OSError as e:
        print(f"no status: {e}", file=sys.stderr)
        return 1
    return 0


def main():
    p = argparse.ArgumentParser(prog="clorqub-agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sp = sub.add_parser("switch")
    sp.add_argument("pool", choices=["jetski", "qli"])
    sub.add_parser("status")
    args = p.parse_args()
    return {"run": cmd_run, "switch": cmd_switch,
            "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

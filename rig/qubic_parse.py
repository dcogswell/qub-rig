"""Parsers copied verbatim from /home/daryl/qubjetski-Linux-v4.5/mining_optimizer.py
(the TUI app). That file is the source of truth; fixes there should be
ported here by hand. Stdlib only — this module also runs inside rentals.
"""

import json
import os
import re
import shutil
from datetime import datetime, timezone


def timedelta_safe(seconds):
    from datetime import timedelta
    return timedelta(seconds=seconds)


# Regex for the share/hashrate status line emitted by both miners.
# Example: 2026-10-24 15:57:28.118 [INFO]  E:236 | SHARES: 94/94 (R:17) | 100 it/s | 84 avg it/s
# Rates switch to k-suffix above ~1k ("[CUDA] 1.17k it/s") --
# rate_value() scales them back to plain ints.
STATUS_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)"
    r".*?E:(?P<epoch>\d+)"
    r"\s*\|\s*SHARES:\s*(?P<found>\d+)/(?P<submitted>\d+)\s*\(R:(?P<rejected>\d+)\)"
    r"\s*\|\s*(?:\[[^\]]*\]\s*)?(?P<it>\d+(?:\.\d+)?k?)\s*it/s"
    r"\s*\|\s*(?P<avg>\d+(?:\.\d+)?k?)\s*avg it/s"
)


def rate_value(s):
    """'1170' -> 1170, '1.17k' -> 1170. None-safe via callers."""
    return int(round(float(s[:-1]) * 1000)) \
        if s.endswith("k") else int(s)

# Fallback: some lines may not carry the it/s portion; still capture share counts.
SHARES_ONLY_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)"
    r".*?E:(?P<epoch>\d+)"
    r"\s*\|\s*SHARES:\s*(?P<found>\d+)/(?P<submitted>\d+)\s*\(R:(?P<rejected>\d+)\)"
)

# Trainer version lines. qubjetski: "Trainer Updated: AVX2    v236.17" and
# "[CPU] Worker started | AVX2 v236.17 | E:236". qli:
# "[CPU] Trainer: pplns cpu 236.3 is starting" /
# "[CPU] Trainer: tree cpu version 235.29 is starting" plus
# "[CPU] Trainer: Version: AVX2" (variant only, no version number).
TRAINER_UPDATED_RE = re.compile(
    r"(?:Trainer Updated:|Worker started \|)\s*(?P<variant>[A-Za-z0-9]+)"
    r"\s+v(?P<ver>\d+(?:\.\d+)+)")
TRAINER_START_RE = re.compile(
    r"Trainer:\s+(?P<flavor>[A-Za-z]+)\s+cpu\s+(?:version\s+)?"
    r"(?P<ver>\d+(?:\.\d+)+)\s+is starting")
TRAINER_VARIANT_RE = re.compile(
    r"Trainer:\s+Version:\s+(?P<variant>\S+)"
    r"|\bworker-(?P<variant2>[A-Za-z0-9]+)")


def find_stats_files(stats_dir, n=2):
    """Return up to n (epoch, path) pairs for stats.<epoch>.lock files,
    newest epoch first."""
    if not os.path.isdir(stats_dir):
        return []
    files = []
    for name in os.listdir(stats_dir):
        m = re.match(r"stats\.(\d+)\.lock$", name)
        if m:
            files.append((int(m.group(1)), os.path.join(stats_dir, name)))
    files.sort(key=lambda x: -x[0])
    return files[:n]


def find_stats_file(stats_dir, glob_pat):
    """Return the path to the most recent stats.<epoch>.lock file, or None."""
    files = find_stats_files(stats_dir, 1)
    return files[0][1] if files else None


def parse_stats_file(path):
    """Parse a stats lock file. Returns dict or None on failure.

    {
      "epoch": int,
      "found_shares": int,
      "submitted_shares": int,
      "invalid_shares": int,
      "solution_times": [datetime, ...]   # UTC, sorted ascending
      "best_score": int | None           # max ReportedScore, non-invalid sols
      "score_n": int                     # non-invalid solutions with a score
      "score_med": float | None          # median ReportedScore
      "score_sum": float                 # total ReportedScore
    }
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    sols = data.get("Solutions", []) or []
    times = []
    scores = []
    for s in sols:
        score = s.get("ReportedScore")
        if isinstance(score, (int, float)) and not s.get("Invalid"):
            scores.append(score)
        created = s.get("Created")
        if not created:
            continue
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        times.append(dt)
    times.sort()
    scores.sort()
    return {
        "epoch": data.get("Epoch"),
        "found_shares": data.get("FoundShares", 0),
        "submitted_shares": data.get("SubmittedShares", 0),
        "invalid_shares": data.get("InvalidShares", 0),
        "solution_times": times,
        "best_score": max(scores) if scores else None,
        "score_n": len(scores),
        "score_med": (scores[len(scores) // 2] if len(scores) % 2
                      else (scores[len(scores) // 2 - 1]
                            + scores[len(scores) // 2]) / 2)
        if scores else None,
        "score_sum": sum(scores),
    }


def count_shares_in_window(solution_times, now, seconds):
    """Count solution timestamps within the last `seconds` window (UTC)."""
    cutoff = now - timedelta_safe(seconds)
    return sum(1 for t in solution_times if t >= cutoff)


def last_share_time(solution_times):
    """Return the most recent solution timestamp, or None."""
    if not solution_times:
        return None
    return solution_times[-1]


def epoch_history(stats_dir, cache=None):
    """Per-epoch stats across every stats.<epoch>.lock file in stats_dir.

    Returns {epoch: {"best": int|None, "found": int, "submitted": int,
    "invalid": int}}. `cache` is an optional {path: (stat_key, parsed)}
    dict so repeated scans only re-parse files that changed."""
    hist = {}
    if not os.path.isdir(stats_dir):
        return hist
    seen = set()
    for name in os.listdir(stats_dir):
        m = re.match(r"stats\.(\d+)\.lock$", name)
        if not m:
            continue
        path = os.path.join(stats_dir, name)
        seen.add(path)
        try:
            st = os.stat(path)
        except OSError:
            continue
        key = (st.st_mtime, st.st_size)
        cached = cache.get(path) if cache is not None else None
        if cached is not None and cached[0] == key:
            stats = cached[1]
        else:
            stats = parse_stats_file(path)
            if stats is None:
                continue
            if cache is not None:
                cache[path] = (key, stats)
        epoch = stats["epoch"] if stats["epoch"] is not None \
            else int(m.group(1))
        hist[epoch] = {
            "best": stats["best_score"],
            "found": stats["found_shares"],
            "submitted": stats["submitted_shares"],
            "invalid": stats["invalid_shares"],
        }
    if cache is not None:
        prefix = stats_dir.rstrip(os.sep) + os.sep
        for path in [p for p in cache
                     if p.startswith(prefix) and p not in seen]:
            del cache[path]
    return hist


def mirror_stats_files(stats_dir, mirror_dir):
    """Copy stats.<epoch>.lock files from stats_dir into mirror_dir when
    the mirror copy is missing or stale (mtime+size differ); never deletes
    from the mirror. Returns mirror_dir for use as the effective stats
    dir, or stats_dir on failure. Needed for clients like qli whose
    updater deletes *.lock from its working dir."""
    if not os.path.isdir(stats_dir):
        return stats_dir
    try:
        os.makedirs(mirror_dir, exist_ok=True)
    except OSError:
        return stats_dir
    for name in os.listdir(stats_dir):
        if not re.match(r"stats\.\d+\.lock$", name):
            continue
        src = os.path.join(stats_dir, name)
        dst = os.path.join(mirror_dir, name)
        try:
            sst = os.stat(src)
            if sst.st_size == 0:
                continue  # mid-write; catch it next refresh
            try:
                dst_st = os.stat(dst)
            except OSError:
                dst_st = None
            if dst_st is None or (dst_st.st_mtime, dst_st.st_size) \
                    != (sst.st_mtime, sst.st_size):
                shutil.copy2(src, dst)
        except OSError:
            continue
    return mirror_dir


# ANSI escape sequences (CSI + charset selects) for stripping colored log output.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()#][0-9A-Za-z]")


def tail_log_lines(path, max_lines=500, max_bytes=262144):
    """Return the last max_lines lines of a log file (ANSI-stripped)."""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            back = min(size, max_bytes)
            f.seek(-back, os.SEEK_END)
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if back < size and lines:
        lines = lines[1:]  # drop possibly-partial first line
    lines = [ANSI_RE.sub("", ln) for ln in lines]
    return lines[-max_lines:]


def parse_log_ts(ts_str, tz="local"):
    """Parse a 'YYYY-MM-DD HH:MM:SS.fff' log timestamp to an aware datetime.

    Miners differ: qli logs in UTC, qubjetski logs in local time."""
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(ts_str, fmt)
        except ValueError:
            continue
        if tz == "utc":
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()
    return None


# Bytes of log read on the very first scan (covers ~24h at ~1 line/s).
LOG_BACKFILL_BYTES = 24 * 1024 * 1024


def read_new_log_lines(path, offset, backfill_bytes):
    """Incrementally read a growing log file.

    offset=None backfills the last backfill_bytes (first call); otherwise reads
    only bytes appended since offset. Handles truncation/rotation by restarting
    at 0. Returns (lines, new_offset); (None, offset) on error."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, offset
    if offset is None:
        offset = max(0, size - backfill_bytes)
    elif offset > size:
        offset = 0  # file rotated/truncated
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return None, offset
    lines = data.decode("utf-8", errors="replace").splitlines()
    if offset > 0 and lines and not data.startswith(b"\n"):
        lines = lines[1:]  # drop possibly-partial first line
    return lines, size


def tail_hashrate(log_file, max_lines=400):
    """Read the tail of a log file and return the latest hashrate info.

    Returns {"it": int|None, "avg": int|None, "epoch": int|None,
             "found": int|None, "submitted": int|None, "rejected": int|None,
             "last_line_ts": str|None}
    """
    result = {
        "it": None, "avg": None, "epoch": None,
        "found": None, "submitted": None, "rejected": None,
        "last_line_ts": None,
    }
    if not log_file or not os.path.isfile(log_file):
        return result
    try:
        with open(log_file, "rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                back = min(size, max_lines * 400)
                f.seek(-back, os.SEEK_END)
                chunk = f.read().decode("utf-8", errors="replace")
            except OSError:
                f.seek(0)
                chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return result

    lines = chunk.splitlines()
    for line in reversed(lines):
        m = STATUS_RE.search(line)
        if m:
            result["it"] = rate_value(m.group("it"))
            result["avg"] = rate_value(m.group("avg"))
            result["epoch"] = int(m.group("epoch"))
            result["found"] = int(m.group("found"))
            result["submitted"] = int(m.group("submitted"))
            result["rejected"] = int(m.group("rejected"))
            result["last_line_ts"] = m.group("ts")
            return result
        m2 = SHARES_ONLY_RE.search(line)
        if m2:
            result["epoch"] = int(m2.group("epoch"))
            result["found"] = int(m2.group("found"))
            result["submitted"] = int(m2.group("submitted"))
            result["rejected"] = int(m2.group("rejected"))
            result["last_line_ts"] = m2.group("ts")
    return result

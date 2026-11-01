#!/usr/bin/env python3
"""Reading and writing the on-disk snapshot artifacts, safely.

Two hazards this module exists to remove.

STALENESS.  canonical_positions.json is IBKR truth frozen at one instant, and
one_positions.json is ONE's book frozen at another.  The daemon refreshes both
every cycle, but nothing stopped a CLI tool from loading a snapshot captured
weeks earlier and reporting on it with complete confidence.  That is not a
cosmetic problem: reconcile.py would report a clean or catastrophic diff from
data nobody recognises, and flex_export.py turns the IBKR snapshot into a ONE
import file, so a stale one does not merely mislead -- it can push phantom
fills into the book.  load_snapshot() dates every snapshot it hands out and
refuses anything older than max_age_minutes unless the caller says otherwise.
It fails closed: a snapshot with no readable captured_at cannot be shown to be
fresh, so it is treated as stale.

TORN READS.  The daemon rewrites these files every 30 seconds while other
processes read them.  write_json_atomic() writes a sibling temp file and
renames it over the target, so a reader sees the whole old file or the whole
new one, never half of each.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

# The daemon cycles every 30s in market hours and every 5 min outside them, so
# anything older than this means the daemon is not running and the file is a
# leftover rather than a current picture.
DEFAULT_MAX_AGE_MINUTES = 15

STALE_FLAG = "--stale-ok"


class StaleSnapshotError(RuntimeError):
    """A snapshot too old to report on was loaded without an explicit override."""


def write_json_atomic(path: str, data) -> None:
    """Write JSON to ``path`` so a concurrent reader never sees a partial file."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        os.replace(tmp, path)          # atomic on Windows and POSIX alike
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _captured_at(snap: dict):
    raw = (snap or {}).get("captured_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def snapshot_age_minutes(snap: dict, now=None):
    """Minutes since the snapshot was captured, or None if it does not say."""
    dt = _captured_at(snap)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 60.0


def describe_age(minutes) -> str:
    if minutes is None:
        return "of unknown age"
    if minutes < 90:
        return f"{minutes:.0f} minutes old"
    if minutes < 60 * 48:
        return f"{minutes / 60:.1f} hours old"
    return f"{minutes / 1440:.1f} days old"


def load_snapshot(path: str, *, max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES,
                  allow_stale: bool = False, default=None, now=None) -> dict:
    """Load a snapshot, refusing one too old to report on.

    ``default`` is returned when the file is missing; without one, a missing
    file raises, because silently reporting on an empty book is its own kind of
    lie.  ``allow_stale`` skips the age check for a caller that has decided an
    old snapshot is what it wants.
    """
    name = os.path.basename(path)
    if not os.path.exists(path):
        if default is not None:
            return default
        raise StaleSnapshotError(
            f"{name} does not exist. Start the dashboard "
            f"(launch_dashboard.bat) to create it, or run canonical_engine.py.")

    with open(path, encoding="utf-8") as fh:
        snap = json.load(fh)

    if allow_stale:
        return snap

    age = snapshot_age_minutes(snap, now=now)
    if age is None or age > max_age_minutes:
        captured = (snap or {}).get("captured_at") or "an unrecorded time"
        raise StaleSnapshotError(
            f"{name} was captured {captured} -- {describe_age(age)}, past the "
            f"{max_age_minutes:g}-minute limit. Everything derived from it "
            f"would describe that moment, not now. Start the dashboard "
            f"(launch_dashboard.bat) to refresh it, or pass {STALE_FLAG} to "
            f"use it as-is.")
    return snap


def wants_stale(argv) -> bool:
    """True when the caller passed the override flag on the command line."""
    return STALE_FLAG in (argv or [])

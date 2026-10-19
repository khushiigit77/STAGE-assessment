"""Load and validate the supplied pack using its actual schemas.

Every input row is validated; invalid rows are counted and sampled in ``ValidationReport`` instead of being silently
accepted or dropped. Events are normalised with three distinct times:

* ``received_at`` - server time, trusted (README).
* ``occurred_at`` / ``sent_at`` - phone clock, possibly wrong by a constant offset.
* ``event_time`` - the best estimate of when the event really happened: ``received_at - (sent_at - occurred_at)``.
  Both phone timestamps share one clock, so their difference is reliable even when the clock itself is wrong.
  This is the same reconstruction the supplied simulator uses.

``buffer_lag_s = sent_at - occurred_at`` measures how long the phone held the event offline.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Pack, parse_ts

EVENT_KEYS = ("event_id", "user_id", "event", "occurred_at", "sent_at", "received_at", "session_id", "properties")
UNKNOWN_DIALECT_VALUES = {None, "", "NA", "null", "None"}


@dataclass
class ValidationReport:
    counts: dict = field(default_factory=lambda: defaultdict(int))
    samples: dict = field(default_factory=lambda: defaultdict(list))

    def add(self, kind: str, sample: object = None) -> None:
        self.counts[kind] += 1
        if sample is not None and len(self.samples[kind]) < 5:
            self.samples[kind].append(sample)

    def as_dict(self) -> dict:
        return {"counts": dict(sorted(self.counts.items())), "samples": {k: v for k, v in self.samples.items()}}


def normalise_event(raw: dict, report: ValidationReport) -> dict | None:
    """Validate one event and attach derived times. Returns None (and records why) for unusable rows."""
    missing = [k for k in EVENT_KEYS if k not in raw]
    if missing:
        report.add("event_missing_fields", {"event_id": raw.get("event_id"), "missing": missing})
        return None
    try:
        occ, sent, recv = parse_ts(raw["occurred_at"]), parse_ts(raw["sent_at"]), parse_ts(raw["received_at"])
    except (ValueError, TypeError) as exc:
        report.add("event_bad_timestamp", {"event_id": raw.get("event_id"), "error": str(exc)})
        return None
    if recv is None:
        report.add("event_missing_received_at", raw.get("event_id"))
        return None
    e = dict(raw)
    if occ is None or sent is None:
        lag = 0.0
        report.add("event_missing_phone_time", raw.get("event_id"))
    else:
        lag = (sent - occ).total_seconds()
        if lag < 0:  # phone clock moved backwards between capture and send; cannot trust the gap
            report.add("event_negative_buffer_lag", raw.get("event_id"))
            lag = 0.0
    e["_recv"] = recv
    e["_event_time"] = recv - timedelta(seconds=lag)
    e["_buffer_lag_s"] = lag
    e["_clock_skew_s"] = (recv - sent).total_seconds() if sent else None
    if "deliver_at" in raw:
        e["_deliver"] = parse_ts(raw["deliver_at"])
    d = raw.get("app_dialect")
    e["_dialect"] = None if d in UNKNOWN_DIALECT_VALUES else d
    if not isinstance(e.get("properties"), dict):
        report.add("event_bad_properties", raw.get("event_id"))
        e["properties"] = {}
    return e


def read_jsonl(path: str, report: ValidationReport, kind: str):
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                report.add(f"{kind}_bad_json", {"line": n, "error": str(exc)})


def load_events(path: str, report: ValidationReport, kind: str = "events") -> tuple[list[dict], int]:
    """Load and normalise events, dropping exact re-deliveries of the same ``event_id`` (first copy wins)."""
    seen: set[str] = set()
    out: list[dict] = []
    dups = 0
    for raw in read_jsonl(path, report, kind):
        e = normalise_event(raw, report)
        if e is None:
            continue
        if e["event_id"] in seen:
            dups += 1
            continue
        seen.add(e["event_id"])
        out.append(e)
    report.counts[f"{kind}_duplicate_event_ids_dropped"] += dups
    return out, dups


@dataclass
class ProfileRecord:
    """Latest profile row for a user plus any conflicting older rows from the same snapshot."""

    row: dict
    updated_at: datetime
    snapshot_at: datetime | None
    conflicts: list[dict]


def load_profiles(pack: Pack, report: ValidationReport) -> dict[str, ProfileRecord]:
    """One record per user. Duplicate rows: latest ``updated_at`` wins; differing fields are kept as conflicts."""
    rows: dict[str, list[dict]] = defaultdict(list)
    with open(pack.path("data", "profiles_snapshot.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                r["_updated"] = parse_ts(r["updated_at"])
                r["_snapshot"] = parse_ts(r.get("snapshot_at"))
                r["_signup"] = parse_ts(r.get("signup_at"))
            except (ValueError, TypeError) as exc:
                report.add("profile_bad_timestamp", {"user_id": r.get("user_id"), "error": str(exc)})
                continue
            if r.get("subscription_status") not in ("never", "trial_active", "active", "lapsed"):
                report.add("profile_bad_status", r.get("user_id"))
                continue
            rows[r["user_id"]].append(r)
    out = {}
    for uid, rs in rows.items():
        rs.sort(key=lambda r: r["_updated"], reverse=True)
        latest = rs[0]
        conflicts = []
        for older in rs[1:]:
            report.add("profile_duplicate_row", uid)
            diff = {k: {"kept": latest[k], "other": older[k]} for k in latest
                    if not k.startswith("_") and k != "updated_at" and latest[k] != older[k]}
            conflicts.append({"other_updated_at": older["updated_at"], "fields": diff})
        out[uid] = ProfileRecord(latest, latest["_updated"], latest["_snapshot"], conflicts)
    return out


def load_arms(path: str, report: ValidationReport, valid=("agent", "holdout")) -> dict[str, str]:
    arms = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("arm") not in valid:
                report.add("arm_invalid", r)
                continue
            if r["user_id"] in arms and arms[r["user_id"]] != r["arm"]:
                report.add("arm_conflict", r["user_id"])
            arms[r["user_id"]] = r["arm"]
    return arms


def load_controls(path: str, report: ValidationReport) -> list[dict]:
    out = []
    for c in read_jsonl(path, report, "control"):
        try:
            c["_at"] = parse_ts(c["at"])
            c["_until"] = parse_ts(c.get("until"))
        except (KeyError, ValueError) as exc:
            report.add("control_invalid", {"control": c, "error": str(exc)})
            continue
        out.append(c)
    return out


def load_faults(path: str, report: ValidationReport) -> dict[str, dict]:
    return {f["trigger_event_id"]: f for f in read_jsonl(path, report, "fault") if "trigger_event_id" in f}


def load_triggers(path: str, report: ValidationReport) -> list[dict]:
    """Triggers are kept with duplicates: re-delivery is a replay condition the agent must handle idempotently."""
    out = []
    for raw in read_jsonl(path, report, "trigger"):
        e = normalise_event(raw, report)
        if e is None:
            continue
        if e.get("_deliver") is None:
            report.add("trigger_missing_deliver_at", raw.get("event_id"))
            e["_deliver"] = e["_recv"]
        out.append(e)
    return out


def latest_time(*dts: datetime | None) -> datetime | None:
    vals = [d for d in dts if d is not None]
    return max(vals) if vals else None

"""Evidence with provenance.

Every fact the agent relies on is a ``Fact``: value, source type, source reference (event id or profile row),
observation time, receipt time, age at decision time and conflict notes. Precedence is field specific (see
``FIELD_POLICY``); there is no global "newest wins" rule.

Only events whose ``received_at`` is at or before the decision time are visible, so a decision can be reconstructed
from what was known when it was made.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from .catalogue import Catalogue
from .config import AgentConfig, IST, iso, ist_day
from .data_loader import ProfileRecord

FIELD_POLICY = {
    "signup_at": "Warehouse profile is authoritative; signup_completed event (clock-corrected) only when no profile row.",
    "subscription_status": "Profile value, overridden by a billing event (purchase/renewal/lapse) that happened or was "
                           "received after the profile row's updated_at (the export cannot reflect events it had not "
                           "received). Billing events are applied in clock-corrected event-time order.",
    "push_consent": "Profile notifications_opt_in AND no observed notification_opt_out(push). An observed opt-out is "
                    "binding: re-opt-in is not observable in the pack.",
    "whatsapp_consent": "Profile whatsapp_opt_in AND no observed notification_opt_out(whatsapp).",
    "page_dialect": "The trigger's app_dialect (app settings feed at the moment of the visit). The platform serves the "
                    "page for the app's current dialect, so profile preference cannot override it. Unknown or "
                    "uncatalogued dialect => no page.",
    "message_dialect": "Most recent observed app_dialect (trigger first, then events). Messages open in the app, so "
                       "this is the dialect the viewer will see. Profile preference and deeplink campaign dialects "
                       "are recorded as conflicts but never used.",
    "language": "Trigger app_language; used only to choose between language variants of the same content.",
}


@dataclass
class Fact:
    name: str
    value: object
    source_type: str                 # profile | event | trigger | derived | none
    source_ref: str | None = None
    observed_at: str | None = None
    received_at: str | None = None
    age_hours: float | None = None
    fresh: bool | None = None
    conflict: str | None = None
    note: str | None = None


class ViewerHistory:
    """Per-viewer event index, updated incrementally as live events arrive."""

    def __init__(self) -> None:
        self.events: dict[str, list[dict]] = defaultdict(list)
        self.history_start: datetime | None = None
        self.popularity: dict[str, Counter] = defaultdict(Counter)

    def add(self, e: dict) -> None:
        self.events[e["user_id"]].append(e)

    def finalise_history(self, events: list[dict], catalogue: Catalogue, replay_start: datetime,
                         config: AgentConfig) -> None:
        """Index the 30-day history and compute dialect popularity from plays before the replay day."""
        for e in events:
            self.add(e)
        self.history_start = min((e["_recv"] for e in events), default=None)
        cutoff = replay_start - timedelta(days=config.history_days_popularity)
        for e in events:
            if e["event"] == "play_started" and cutoff <= e["_event_time"] < replay_start:
                t = catalogue.titles.get(e["properties"].get("title_id"))
                if t:
                    self.popularity[t.dialect][t.slug] += 1

    def visible(self, user_id: str, now: datetime) -> list[dict]:
        return [e for e in self.events.get(user_id, ()) if e["_recv"] <= now]


@dataclass
class Snapshot:
    """Everything the agent may use for one decision, frozen at decision time."""

    user_id: str
    decided_at: datetime
    facts: dict[str, Fact]
    conflicts: list[str]
    assumptions: list[str]
    features: dict = field(default_factory=dict)

    def value(self, name: str):
        f = self.facts.get(name)
        return None if f is None else f.value

    def refs(self) -> list[dict]:
        return [{"fact": f.name, "value": f.value if not isinstance(f.value, (list, dict)) else "<structured>",
                 "source_type": f.source_type, "source_ref": f.source_ref, "observed_at": f.observed_at,
                 "age_hours": f.age_hours, "fresh": f.fresh, "conflict": f.conflict}
                for f in self.facts.values()]

    def as_dict(self) -> dict:
        return {"facts": {k: asdict(v) for k, v in self.facts.items()}, "conflicts": self.conflicts,
                "assumptions": self.assumptions}


def _age_h(now: datetime, t: datetime | None) -> float | None:
    return None if t is None else round((now - t).total_seconds() / 3600, 2)


def parse_deeplink(link: str | None) -> dict | None:
    """``ott://c/<campaign>/<dialect>/title/<title_id>`` -> parts. Malformed links return None."""
    if not link or not link.startswith("ott://c/"):
        return None
    parts = link[len("ott://c/"):].split("/")
    if len(parts) != 4 or parts[2] != "title":
        return None
    return {"campaign": parts[0], "dialect": parts[1], "title_id": parts[3]}


def build_snapshot(user_id: str, trigger: dict, now: datetime, profile: ProfileRecord | None,
                   history: ViewerHistory, catalogue: Catalogue, config: AgentConfig) -> Snapshot:
    facts: dict[str, Fact] = {}
    conflicts: list[str] = []
    assumptions: list[str] = []
    evs = history.visible(user_id, now)
    stale_cut = timedelta(days=config.profile_stale_days)

    # --- profile presence and freshness
    if profile is None:
        facts["profile"] = Fact("profile", None, "none", note="no profile row; no profile fact is invented")
        assumptions.append("No profile row: consent treated as absent, status inferred only from billing events.")
    else:
        fresh = (now - profile.updated_at) <= stale_cut
        facts["profile"] = Fact("profile", "present", "profile", f"profiles_snapshot:{user_id}",
                                observed_at=profile.row["updated_at"], age_hours=_age_h(now, profile.updated_at),
                                fresh=fresh)
        for c in profile.conflicts:
            conflicts.append(f"profile duplicate rows disagree on {sorted(c['fields'])} "
                             f"(kept row updated {profile.row['updated_at']}, other {c['other_updated_at']})")

    # --- signup
    signup_ev = next((e for e in evs if e["event"] == "signup_completed"), None)
    if profile and profile.row.get("_signup"):
        sv = profile.row["_signup"]
        conflict = None
        if signup_ev and abs((signup_ev["_event_time"] - sv).total_seconds()) > 86400:
            conflict = f"signup_completed event says {iso(signup_ev['_event_time'])}"
            conflicts.append(f"signup_at: profile {iso(sv)} vs event {iso(signup_ev['_event_time'])}")
        facts["signup_at"] = Fact("signup_at", iso(sv), "profile", f"profiles_snapshot:{user_id}",
                                  observed_at=iso(sv), age_hours=_age_h(now, sv), fresh=True, conflict=conflict)
    elif signup_ev:
        sv = signup_ev["_event_time"]
        facts["signup_at"] = Fact("signup_at", iso(sv), "event", signup_ev["event_id"], observed_at=iso(sv),
                                  received_at=iso(signup_ev["_recv"]), age_hours=_age_h(now, sv), fresh=True,
                                  note="clock-corrected event time" if signup_ev["_buffer_lag_s"] > 0 else None)
    else:
        facts["signup_at"] = Fact("signup_at", None, "none", note="unknown signup time")

    # --- subscription status (profile, then later billing events)
    status, src, ref, obs = None, "none", None, None
    base_time = None
    if profile:
        status, src, ref = profile.row["subscription_status"], "profile", f"profiles_snapshot:{user_id}"
        obs, base_time = profile.row["updated_at"], profile.updated_at
    lapsed_at = None
    profile_time = base_time
    billing = sorted((e for e in evs if e["event"] in ("purchase_completed", "subscription_renewed",
                                                       "subscription_lapsed")), key=lambda e: e["_event_time"])
    for e in billing:
        ev = e["event"]
        if ev == "subscription_lapsed":
            lapsed_at = e["_event_time"]
        # The export can only reflect events the warehouse had *received* by updated_at. An event that happened
        # earlier but arrived later (offline buffering) still overrides the profile.
        if profile_time is not None and e["_event_time"] <= profile_time and e["_recv"] <= profile_time:
            continue
        if ev == "subscription_lapsed":
            new = "lapsed"
        elif ev == "purchase_completed" and e["properties"].get("is_trial") is True:
            new = "trial_active"
        else:
            new = "active"
        if status is not None and src == "profile" and new != status:
            conflicts.append(f"subscription_status: profile '{status}' superseded by {ev} {e['event_id']}")
        status, src, ref, obs = new, "event", e["event_id"], iso(e["_event_time"])
        base_time = e["_event_time"]
    facts["subscription_status"] = Fact("subscription_status", status, src, ref, observed_at=obs,
                                        age_hours=_age_h(now, base_time),
                                        fresh=None if base_time is None else (now - base_time) <= stale_cut)
    facts["lapsed_at"] = Fact("lapsed_at", iso(lapsed_at), "event" if lapsed_at else "none",
                              observed_at=iso(lapsed_at))
    paid_ever = any(e["event"] in ("purchase_completed", "subscription_renewed")
                    and e["properties"].get("is_trial") is not True for e in evs)
    facts["paid_ever_observed"] = Fact("paid_ever_observed", paid_ever, "derived",
                                       note="from purchase/renewal events in the observable history")

    # --- consent
    for ch, col in (("push", "notifications_opt_in"), ("whatsapp", "whatsapp_opt_in")):
        opt_out = next((e for e in reversed(evs) if e["event"] == "notification_opt_out"
                        and e["properties"].get("channel") == ch), None)
        if profile is None:
            facts[f"{ch}_consent"] = Fact(f"{ch}_consent", False, "none", note="no profile row: consent unknown")
        elif opt_out is not None:
            conflict = None
            if profile.row[col] == "true":
                conflict = f"profile {col}=true but opt-out event {opt_out['event_id']}"
                conflicts.append(f"{ch}_consent: {conflict}; opt-out wins")
            facts[f"{ch}_consent"] = Fact(f"{ch}_consent", False, "event", opt_out["event_id"],
                                          observed_at=iso(opt_out["_event_time"]), conflict=conflict)
        else:
            facts[f"{ch}_consent"] = Fact(f"{ch}_consent", profile.row[col] == "true", "profile",
                                          f"profiles_snapshot:{user_id}", observed_at=profile.row["updated_at"],
                                          age_hours=_age_h(now, profile.updated_at),
                                          fresh=(now - profile.updated_at) <= stale_cut)

    # --- dialect
    trig_d = trigger.get("_dialect")
    last_ev_d = next(((e["_dialect"], e) for e in reversed(evs) if e.get("_dialect")), (None, None))
    pref = (profile.row.get("preferred_dialect") or None) if profile else None
    if trig_d and trig_d in catalogue.dialects:
        page_d = Fact("page_dialect", trig_d, "trigger", trigger["event_id"], observed_at=iso(trigger["_recv"]),
                      fresh=True)
    elif trig_d:
        page_d = Fact("page_dialect", None, "trigger", trigger["event_id"],
                      note=f"app dialect '{trig_d}' has no titles in the catalogue")
    else:
        page_d = Fact("page_dialect", None, "trigger", trigger["event_id"],
                      note=f"trigger app_dialect is missing/invalid ({trigger.get('app_dialect')!r})")
    facts["page_dialect"] = page_d
    msg_d_val, msg_src, msg_ref, msg_obs = None, "none", None, None
    if trig_d:
        msg_d_val, msg_src, msg_ref, msg_obs = trig_d, "trigger", trigger["event_id"], iso(trigger["_recv"])
    elif last_ev_d[0]:
        e = last_ev_d[1]
        msg_d_val, msg_src, msg_ref, msg_obs = last_ev_d[0], "event", e["event_id"], iso(e["_recv"])
    if msg_d_val and msg_d_val not in catalogue.dialects:
        msg_note = f"'{msg_d_val}' has no catalogue titles"
        msg_d_val = None
    else:
        msg_note = None
    facts["message_dialect"] = Fact("message_dialect", msg_d_val, msg_src, msg_ref, observed_at=msg_obs,
                                    note=msg_note)
    facts["preferred_dialect"] = Fact("preferred_dialect", pref, "profile" if profile else "none",
                                      f"profiles_snapshot:{user_id}" if profile else None,
                                      observed_at=profile.row["updated_at"] if profile else None)
    observed_app_d = trig_d or last_ev_d[0]
    if pref and observed_app_d and pref != observed_app_d:
        conflicts.append(f"dialect: profile preferred '{pref}' vs app setting '{observed_app_d}' "
                         f"(app setting used; profile is a daily export and may be stale)")
    dl = parse_deeplink((trigger.get("properties") or {}).get("deeplink"))
    if dl:
        t = catalogue.titles.get(dl["title_id"])
        note = f"deeplink campaign dialect '{dl['dialect']}'"
        if t and t.dialect != dl["dialect"]:
            note += f" disagrees with the linked title's own dialect '{t.dialect}'"
        conflicts.append(f"{note}; deeplink dialect is campaign metadata and not used as viewer evidence")
        facts["deeplink"] = Fact("deeplink", dl, "trigger", trigger["event_id"], note=note)
    facts["app_language"] = Fact("app_language", trigger.get("app_language"), "trigger", trigger["event_id"])

    # --- behavioural features (derived from visible events only)
    features = derive_features(evs, trigger, now, catalogue)
    if history.history_start is not None:
        features["history_start"] = iso(history.history_start)
    facts["history_depth"] = Fact("history_depth", len(evs), "derived",
                                  note=f"{len(evs)} visible events; first {features.get('first_seen')}")
    return Snapshot(user_id, now, facts, conflicts, assumptions, features)


def derive_features(evs: list[dict], trigger: dict, now: datetime, catalogue: Catalogue) -> dict:
    """Behavioural facts used by goal eligibility and planning. Times are clock-corrected event times."""
    plays = [e for e in evs if e["event"] == "play_started"]
    completes = [e for e in evs if e["event"] == "episode_completed"]
    series: dict[str, dict] = {}
    genres: Counter = Counter()
    for e in plays + completes:
        t = catalogue.titles.get(e["properties"].get("title_id"))
        if t is None:
            continue
        if e["event"] == "play_started":
            for g in t.genres:
                genres[g] += 1
        s = series.setdefault(t.slug, {"slug": t.slug, "type": t.content_type, "last_play": None, "max_idx": -1,
                                       "dialect": t.dialect, "title_id": t.title_id})
        idx = catalogue.episode_index(t.slug, e["properties"].get("episode_id"))
        if idx is not None:
            s["max_idx"] = max(s["max_idx"], idx)
        if e["event"] == "play_started" and (s["last_play"] is None or e["_event_time"] > s["last_play"]):
            s["last_play"] = e["_event_time"]
    unfinished = []
    for s in series.values():
        n = catalogue.episode_count(s["slug"])
        if s["type"] in ("show", "micro_drama") and s["last_play"] is not None and s["max_idx"] + 1 < n:
            idle_days = (now - s["last_play"]).total_seconds() / 86400
            unfinished.append({"slug": s["slug"], "title_id": s["title_id"], "dialect": s["dialect"],
                               "next_episode_number": s["max_idx"] + 2, "episodes": n,
                               "last_play": iso(s["last_play"]), "idle_days": round(idle_days, 2)})
    unfinished.sort(key=lambda x: x["last_play"], reverse=True)
    win14 = now - timedelta(days=14)
    trigger_id = trigger.get("event_id")
    opens_14 = [e for e in evs if e["event"] == "app_open" and e["event_id"] != trigger_id
                and win14 <= e["_event_time"] < trigger["_event_time"]]
    msg_days = Counter(ist_day(e["_event_time"]) for e in evs if e["event"] == "message_delivered"
                       and e["properties"].get("channel") in ("push", "whatsapp"))
    return {
        "first_seen": iso(min((e["_event_time"] for e in evs), default=None)),
        "ever_play_started": bool(plays),
        "ever_episode_completed": bool(completes),
        "play_days_last_14": sorted({ist_day(e["_event_time"]) for e in plays if e["_event_time"] >= win14}),
        "app_opens_prev_14d": len(opens_14),
        "unfinished_series": unfinished[:10],
        "watched_slugs": sorted(series),
        "top_genres": [g for g, _ in genres.most_common(3)],
        "message_days": dict(msg_days),
        "trigger_buffer_lag_s": trigger.get("_buffer_lag_s"),
        "trigger_event_time": iso(trigger.get("_event_time")),
        "trigger_ist_hour": trigger["_recv"].astimezone(IST).hour,
    }

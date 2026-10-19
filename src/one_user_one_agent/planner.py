"""Action planning: turns (goal, evidence) into *proposed* actions.

The planner never decides whether an action may be published; ``verification.py`` does that independently.
Content is chosen deterministically (popularity in the dialect over the 14 days before the replay, ties by id).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from .catalogue import Catalogue, Title
from .config import AgentConfig, IST, UTC, iso
from .evidence import Snapshot

PURCHASE_GOALS = ("G04", "G05", "G06")


def _row_order(goal_id: str | None) -> list[str]:
    return {
        "G07": ["next_episode", "because_you_watched", "popular_in_dialect", "new_releases", "genre"],
        "G08": ["new_releases", "because_you_watched", "popular_in_dialect", "genre"],
        "G02": ["popular_in_dialect", "because_you_watched", "new_releases", "genre"],
    }.get(goal_id or "", ["because_you_watched", "popular_in_dialect", "new_releases", "next_episode", "genre"])


def plan_home_page(snap: Snapshot, goal: dict | None, catalogue: Catalogue, popularity: dict[str, Counter],
                   publish_at: datetime, config: AgentConfig, interventions: dict) -> tuple[dict | None, list[str]]:
    """Return (proposal, notes). Proposal is None when no row can reach the minimum size."""
    notes: list[str] = []
    dialect = snap.value("page_dialect")
    if not dialect:
        return None, [snap.facts["page_dialect"].note or "no page dialect"]
    hp = interventions["home_page"]
    ttl_end = publish_at + timedelta(minutes=hp["ttl_minutes"])
    min_n, max_n, max_rows = hp["rows"]["min_titles_per_row"], hp["rows"]["max_titles_per_row"], hp["rows"]["max_rows"]
    lang = snap.value("app_language")
    gid = goal["goal_id"] if goal else None
    pop = popularity.get(dialect, Counter())
    today = publish_at.astimezone(IST).date()

    # one valid title per slug, preferring the app language; valid for the whole page TTL
    pool: dict[str, Title] = {}
    for t in catalogue.by_dialect.get(dialect, ()):
        if not catalogue.available_through(t, publish_at, ttl_end):
            continue
        cur = pool.get(t.slug)
        if cur is None or (cur.language != lang and t.language == lang):
            pool[t.slug] = t
    if not pool:
        return None, [f"no titles available in dialect {dialect}"]

    def rank(ts: list[Title]) -> list[Title]:
        prem_first = gid in PURCHASE_GOALS
        short_first = gid == "G02"
        return sorted(ts, key=lambda t: (-(t.premium if prem_first else 0),
                                         -(t.content_type == "micro_drama" if short_first else 0),
                                         -pop[t.slug], t.title_id))

    f = snap.features
    watched = set(f["watched_slugs"])
    unfinished_here = [s["slug"] for s in f["unfinished_series"] if s["slug"] in pool]
    top = f["top_genres"]
    candidates = {
        "next_episode": [pool[s] for s in unfinished_here],
        "because_you_watched": rank([t for s, t in pool.items() if s not in watched and top
                                     and set(t.genres) & set(top)]) if watched else [],
        "popular_in_dialect": rank([t for s, t in pool.items() if s not in watched]),
        "new_releases": rank([t for t in pool.values() if t.release_date and
                              today - timedelta(days=config.new_release_days) <= t.release_date <= today]),
        "genre": rank([t for s, t in pool.items() if s not in watched and top and top[0] in t.genres]),
    }

    used: set[str] = set()
    hero = None
    if gid == "G07" and goal["params"].get("target_slug") in pool:
        hero = pool[goal["params"]["target_slug"]]
    elif gid == "G08" and candidates["new_releases"]:
        hero = candidates["new_releases"][0]
    elif candidates["popular_in_dialect"]:
        hero = candidates["popular_in_dialect"][0]
    if hero:
        used.add(hero.slug)
    rows = []
    for rt in _row_order(gid):
        if len(rows) >= max_rows:
            break
        ids = []
        for t in candidates[rt]:
            if t.slug in used:
                continue
            ids.append(t)
            if len(ids) >= max_n:
                break
        if len(ids) < min_n:
            notes.append(f"row {rt} skipped: {len(ids)} valid titles < {min_n}")
            continue
        used.update(t.slug for t in ids)
        rows.append({"row_type": rt, "title_ids": [t.title_id for t in ids]})
    if not rows and hero is None:
        return None, notes + ["no row reached the minimum size"]
    page = {"type": "home_page", "dialect": dialect, "hero_title_id": hero.title_id if hero else None, "rows": rows}
    return page, notes


def next_send_time(now: datetime, hour_ist: int) -> datetime:
    """The next IST day at ``hour_ist``:00, so a message never lands in the same session or in quiet hours."""
    local = now.astimezone(IST)
    target = (local + timedelta(days=1)).replace(hour=hour_ist, minute=0, second=0, microsecond=0)
    return target.astimezone(UTC)


def plan_push(snap: Snapshot, goal: dict | None, catalogue: Catalogue, popularity: dict[str, Counter],
              now: datetime, config: AgentConfig) -> tuple[dict | None, str]:
    """Propose at most one next-day push for resume/return goals. Returns (proposal, reason-if-none)."""
    if goal is None or goal["goal_id"] not in config.supported_message_goals:
        return None, "goal does not use messages (proportionate default: home page only)"
    if not snap.value("push_consent"):
        return None, "no push consent"
    dialect = snap.value("message_dialect")
    if not dialect:
        return None, "dialect the viewer will see is unknown or uncatalogued"
    send_at = next_send_time(now, config.push_local_hour)
    lang = snap.value("app_language")
    if goal["goal_id"] == "G07":
        title = catalogue.pick_variant(goal["params"]["target_slug"], dialect, lang, send_at)
        if title is None:
            return None, "unfinished series is not available in the viewer's dialect"
        text = {"title": "Continue watching",
                "body": f"Episode {goal['params']['next_episode_number']} of {title.name} is waiting for you."}
    else:
        pop = popularity.get(dialect, Counter())
        watched = set(snap.features["watched_slugs"])
        opts = sorted((t for t in catalogue.by_dialect.get(dialect, ()) if t.slug not in watched
                       and catalogue.available(t, send_at)), key=lambda t: (-pop[t.slug], t.title_id))
        if not opts:
            return None, "no available title in the viewer's dialect"
        title = catalogue.pick_variant(opts[0].slug, dialect, lang, send_at) or opts[0]
        text = {"title": "Picked for you", "body": f"{title.name} is streaming now. Tap to watch."}
    return {"type": "push", "send_at": iso(send_at), "title_id": title.title_id, "text": text,
            "deeplink": f"ott://title/{title.title_id}", "dialect": dialect}, ""


def plan_in_app(snap: Snapshot, goal: dict | None, pricing: dict, config: AgentConfig) -> tuple[dict | None, str]:
    """Plan information for purchase goals, with prices copied from pricing.json only."""
    if goal is None or goal["goal_id"] not in config.in_app_goals:
        return None, "goal does not use in-app messages"
    plans = {p["plan_id"]: p for p in pricing["plans"]}
    m = plans.get("P_MONTH")
    if not m:
        return None, "no monthly plan in pricing"
    body = f"Keep watching with the monthly plan: Rs {m['price_inr']} for {m['days']} days."
    return {"type": "in_app_message", "text": {"title": "Your plan options", "body": body}}, ""

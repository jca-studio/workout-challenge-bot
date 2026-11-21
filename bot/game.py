"""Game logic that ties the database to the scoring rules. No Discord here, so it can be tested."""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from . import config, scoring
from .db import DB, MIX_UP, Challenge, theme_for
from .scoring import (
    GoalWeek, PlayerWeek, Standings, Week, cap_missions, challenge_weeks, scaled_target, week_for,
)


# Scoring weights come from settings so they can be tuned without code changes.
scoring.PERFECT_WEEK_BONUS = config.PERFECT_WEEK_BONUS
scoring.TEAM_BONUS_PER_PLAYER = config.TEAM_BONUS_PER_PLAYER


def now_local() -> datetime:
    return datetime.now(config.TZ)


def lock_time(week: Week) -> datetime:
    """A week locks at midnight at the end of its last day (Friday, or the challenge's last day), plus the
    late-night grace window (3 a.m. by default) so a Friday-night check-in after midnight still counts."""
    return datetime.combine(week.end + timedelta(days=1), time(config.LATE_NIGHT_GRACE_HOUR, 0), tzinfo=config.TZ)


def checkin_allowed(challenge: Challenge, day: date, now: datetime) -> tuple[bool, str]:
    if day < challenge.start or day > challenge.end:
        return False, "That day isn't part of the challenge."
    if day > now.date():
        return False, "You can't check in for a day that hasn't happened yet."
    week = week_for(day, challenge_weeks(challenge.start, challenge.end))
    if now >= lock_time(week):
        return False, ("That week is locked. Ask the commissioner if something was missed.")
    return True, ""


def player_week(db: DB, challenge: Challenge, user_id: int, week: Week) -> PlayerWeek:
    goals = db.goals(challenge.id, user_id, approved_only=True)
    out = []
    for g in goals:
        done = len(db.checkin_days(g.id, week.start, week.end))
        out.append(GoalWeek(g.id, g.name, scaled_target(g.target_days, week.days), done))
    return PlayerWeek(user_id, out)


def standings(db: DB, challenge: Challenge, through_week: int | None = None) -> Standings:
    weeks = challenge_weeks(challenge.start, challenge.end)
    if through_week is not None:
        weeks = weeks[:through_week]
    last_day = weeks[-1].end if weeks else challenge.start
    pw: dict[int, list[PlayerWeek]] = {}
    missions: dict[int, float] = {}
    for p in db.players(challenge.id):
        uid = p["user_id"]
        pw[uid] = [player_week(db, challenge, uid, w) for w in weeks]
        comps = [(d, pts) for d, pts in db.mission_completions(challenge.id, uid) if d <= last_day]
        missions[uid] = sum(cap_missions(comps, weeks, config.MISSION_WEEKLY_CAP))
    teams = db.teams(challenge.id) if challenge.mode == "teams" else {}
    return Standings(weeks, pw, missions, teams=teams, team_average=config.TEAM_SCORE_AVERAGE)


def choose_mode(player_count: int, override: str = "auto") -> str:
    if override in ("teams", "individual"):
        return override
    return "teams" if player_count >= config.TEAM_MODE_MIN_PLAYERS else "individual"


def assign_teams(user_ids: list[int], rng: random.Random | None = None) -> dict[int, str]:
    """Random split, as even as possible. Everyone has 3 goals, so equal headcount = equal potential points."""
    rng = rng or random.Random()
    ids = list(user_ids)
    rng.shuffle(ids)
    names = list(config.TEAM_NAMES)
    rng.shuffle(names)  # so the extra player (if any) doesn't always land on the same team
    return {uid: names[i % 2] for i, uid in enumerate(ids)}


def pick_mission(db: DB, challenge: Challenge, day: date, rng: random.Random | None = None):
    """Pick from the day's theme, swapping in a Random Mix-Up mission about once a week.
    Avoids repeats within a challenge until a theme runs out."""
    rng = rng or random.Random()
    used = db.used_mission_ids(challenge.id)
    theme = theme_for(day)
    mixups = [m for m in db.missions(challenge.guild_id, MIX_UP) if m["id"] not in used]
    if mixups and rng.random() < config.MIXUP_CHANCE:
        return rng.choice(mixups)
    pool = db.missions(challenge.guild_id, theme)
    fresh = [m for m in pool if m["id"] not in used]
    if fresh:
        return rng.choice(fresh)
    if pool:
        return rng.choice(pool)
    if mixups:
        return rng.choice(mixups)
    everything = db.missions(challenge.guild_id)
    return rng.choice(everything) if everything else None


def mission_open(posted_at: datetime, now: datetime) -> bool:
    return now < posted_at + timedelta(hours=config.MISSION_WINDOW_HOURS)


def open_mission(db: DB, challenge: Challenge, now: datetime):
    """The mission someone can still complete right now: today's, or yesterday's if still in its window."""
    for d in (now.date(), now.date() - timedelta(days=1)):
        m = db.daily_mission(challenge.id, d)
        if m and mission_open(datetime.fromisoformat(m["posted_at"]), now):
            return m
    return None


def mission_points_this_week(db: DB, challenge: Challenge, user_id: int, day: date) -> float:
    weeks = challenge_weeks(challenge.start, challenge.end)
    week = week_for(day, weeks)
    if week is None:
        return 0.0
    comps = [(d, p) for d, p in db.mission_completions(challenge.id, user_id) if week.contains(d)]
    return sum(cap_missions(comps, weeks, config.MISSION_WEEKLY_CAP))


def mission_points_left(db: DB, challenge: Challenge, user_id: int, day: date) -> float:
    """How many more mini mission points she can earn in the week that contains `day` (infinite with no cap)."""
    weeks = challenge_weeks(challenge.start, challenge.end)
    week = week_for(day, weeks)
    if week is None:
        return 0.0
    if config.MISSION_WEEKLY_CAP <= 0:
        return float("inf")
    comps = [(d, p) for d, p in db.mission_completions(challenge.id, user_id) if week.contains(d)]
    used = sum(cap_missions(comps, weeks, config.MISSION_WEEKLY_CAP))
    return max(0.0, config.MISSION_WEEKLY_CAP - used)


def open_goals_today(db: DB, challenge: Challenge, user_id: int, today: date):
    """Approved goals that still need check-ins this week and aren't already logged today."""
    week = week_for(today, challenge_weeks(challenge.start, challenge.end))
    if week is None:
        return []
    out = []
    for g in db.goals(challenge.id, user_id, approved_only=True):
        days = db.checkin_days(g.id, week.start, week.end)
        if today in days:
            continue
        if len(days) >= scaled_target(g.target_days, week.days):
            continue
        out.append(g)
    return out


def signup_closes(challenge: Challenge) -> datetime | None:
    """Sign-ups close at midnight at the end of the deadline day."""
    if not challenge.signup_deadline:
        return None
    return datetime.combine(challenge.signup_deadline + timedelta(days=1), time(0, 0), tzinfo=config.TZ)


def signups_open(challenge: Challenge, now: datetime) -> bool:
    if challenge.status != "signup":
        return False
    closes = signup_closes(challenge)
    return closes is None or now < closes


def should_lock(challenge: Challenge, now: datetime) -> bool:
    """Lock when the deadline passes, or on the start date as a fallback."""
    if challenge.status != "signup":
        return False
    closes = signup_closes(challenge)
    if closes and now >= closes:
        return True
    return now.date() >= challenge.start and now.hour >= config.MISSION_POST_HOUR


def default_deadline(start: date, today: date) -> date:
    """The day before the start, unless that's already passed."""
    return max(start - timedelta(days=1), today)

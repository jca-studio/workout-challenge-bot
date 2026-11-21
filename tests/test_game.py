import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from bot import config, game
from bot.db import DB, MIX_UP, THEMES

CSV = (Path(__file__).parent.parent / "data" / "missions.csv").read_text()


@pytest.fixture
def db(tmp_path):
    return DB(str(tmp_path / "t.db"))


def at(d: date, hour: int = 12) -> datetime:
    return datetime.combine(d, time(hour), tzinfo=config.TZ)


def test_mission_import(db):
    n = db.import_missions(1, CSV)
    assert n == 59
    counts = {t: len(db.missions(1, t)) for t in THEMES + [MIX_UP]}
    assert counts["Feel Good Friday"] == 7 and counts[MIX_UP] == 4 and counts["Munchie Monday"] == 12
    # re-import replaces rather than duplicates
    db.import_missions(1, CSV)
    assert len(db.missions(1)) == 59


def test_bad_theme_rejected(db):
    with pytest.raises(ValueError):
        db.import_missions(1, "Day,Mission,Points\nFunday,Thing,1\n")


def test_checkin_lock_rules():
    ch = type("C", (), {"start": date(2026, 12, 1), "end": date(2026, 12, 31)})()
    mon = date(2026, 12, 7)  # week Dec 5-11
    assert game.checkin_allowed(ch, mon, at(date(2026, 12, 9)))[0]          # Wednesday: fine
    assert game.checkin_allowed(ch, mon, at(date(2026, 12, 11), 23))[0]     # Friday 11 p.m.: fine
    assert game.checkin_allowed(ch, mon, at(date(2026, 12, 12), 1))[0]      # Saturday 1 a.m.: late-night window
    assert not game.checkin_allowed(ch, mon, at(date(2026, 12, 12), 3))[0]  # Saturday 3 a.m.: locked
    assert not game.checkin_allowed(ch, date(2026, 12, 10), at(date(2026, 12, 9)))[0]  # future
    assert not game.checkin_allowed(ch, date(2026, 11, 30), at(date(2026, 12, 2)))[0]  # before start


def test_mode_and_teams():
    assert game.choose_mode(7) == "individual" and game.choose_mode(8) == "teams"
    assert game.choose_mode(5, "teams") == "teams"
    teams = game.assign_teams(list(range(9)), random.Random(1))
    sizes = sorted([list(teams.values()).count(n) for n in config.TEAM_NAMES])
    assert sizes == [4, 5]


def test_full_month_simulation(db):
    db.import_missions(1, CSV)
    ch = db.create_challenge(1, "December", date(2026, 12, 1), date(2026, 12, 31), created_by=99)
    for uid in range(1, 10):
        db.join(ch.id, uid)
    mode = game.choose_mode(len(db.players(ch.id)))
    db.update_challenge(ch.id, mode=mode, status="active")
    for uid, team in game.assign_teams([p["user_id"] for p in db.players(ch.id)], random.Random(2)).items():
        db.set_team(ch.id, uid, team)
    for uid in range(1, 10):
        db.set_goals(ch.id, uid, [("Work out", 3), ("Brush teeth", 7), ("Veggies", 7)], photo_default=False)
    db.approve_goals(ch.id)
    ch = db.get_challenge(ch.id)

    rng = random.Random(3)
    d = ch.start
    used = []
    while d <= ch.end:
        m = game.pick_mission(db, ch, d, rng)
        db.set_daily_mission(ch.id, d, m["id"], at(d, 7))
        used.append(m["id"])
        for uid in range(1, 10):
            for g in game.open_goals_today(db, ch, uid, d):
                # player 1 is perfect; others skip ~20% of the time
                if uid == 1 or rng.random() > 0.2:
                    assert game.checkin_allowed(ch, d, at(d, 21))[0]
                    db.add_checkin(g.id, uid, d, None)
            if rng.random() > 0.3:
                db.complete_mission(ch.id, uid, d, m["points"], None)
        d += timedelta(days=1)

    assert len(used) == len(set(used)) or len(set(used)) >= 25  # few or no repeats
    s = game.standings(db, ch)
    assert len(s.weeks) == 5
    # Player 1: perfect every week = 3 goals + 1 bonus = 4 per week, short weeks included
    assert all(pw.perfect for pw in s.player_weeks[1])
    assert sum(pw.points for pw in s.player_weeks[1]) == pytest.approx(4 * 5)
    assert all(v <= 31 * 0.5 for v in s.mission_points.values())  # no cap: at most one 0.5 mission a day
    ranked = s.ranked_players()
    assert ranked[0][0] == 1
    assert len(s.ranked_teams()) == 2


def test_open_goals_stop_after_target(db):
    ch = db.create_challenge(1, "x", date(2026, 12, 5), date(2026, 12, 11), 1)
    db.join(ch.id, 5)
    db.set_goals(ch.id, 5, [("Work out", 3), ("Brush", 7), ("Veg", 7)], False)
    db.approve_goals(ch.id)
    g = db.goals(ch.id, 5)[0]
    for day in (5, 6, 7):
        db.add_checkin(g.id, 5, date(2026, 12, day), None)
    names = [x.name for x in game.open_goals_today(db, ch, 5, date(2026, 12, 8))]
    assert "Work out" not in names and "Brush" in names


def test_mission_window_and_cap(db, monkeypatch):
    monkeypatch.setattr(config, "MISSION_WEEKLY_CAP", 1.0)  # the cap is optional; test it switched on
    db.import_missions(1, CSV)
    ch = db.create_challenge(1, "x", date(2026, 12, 1), date(2026, 12, 31), 1)
    db.set_daily_mission(ch.id, date(2026, 12, 2), db.missions(1)[0]["id"], at(date(2026, 12, 2), 7))
    assert game.open_mission(db, ch, at(date(2026, 12, 3), 6)) is not None   # 23h later
    assert game.open_mission(db, ch, at(date(2026, 12, 3), 8)) is None       # 25h later
    # Dec 5-11 is one Saturday-Friday week: 0.5 + 0.5 hits the 1-point weekly cap
    assert game.mission_points_left(db, ch, 7, date(2026, 12, 6)) == 1
    db.complete_mission(ch.id, 7, date(2026, 12, 5), 0.5, None)
    db.complete_mission(ch.id, 7, date(2026, 12, 6), 0.5, None)
    assert game.mission_points_left(db, ch, 7, date(2026, 12, 7)) == 0
    assert game.mission_points_left(db, ch, 7, date(2026, 12, 12)) == 1  # new week, fresh cap

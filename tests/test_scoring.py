from datetime import date

import pytest

from bot.scoring import (
    GoalWeek, PlayerWeek, Standings, TeamWeek, cap_missions, challenge_weeks,
    fmt, goal_points, scaled_target, week_start_for,
)


def test_weeks_december_2026():
    # Dec 1 2026 is a Tuesday: 4-day first week, 6-day last week.
    weeks = challenge_weeks(date(2026, 12, 1), date(2026, 12, 31))
    assert [(w.start, w.end, w.days) for w in weeks] == [
        (date(2026, 12, 1), date(2026, 12, 4), 4),
        (date(2026, 12, 5), date(2026, 12, 11), 7),
        (date(2026, 12, 12), date(2026, 12, 18), 7),
        (date(2026, 12, 19), date(2026, 12, 25), 7),
        (date(2026, 12, 26), date(2026, 12, 31), 6),
    ]


def test_weeks_november_2025_starts_saturday():
    weeks = challenge_weeks(date(2025, 11, 1), date(2025, 11, 30))
    assert weeks[0].days == 7 and weeks[-1].start == date(2025, 11, 29) and weeks[-1].days == 2


def test_week_start_is_saturday():
    assert week_start_for(date(2026, 11, 20)) == date(2026, 11, 14)  # Friday -> previous Saturday
    assert week_start_for(date(2026, 11, 21)) == date(2026, 11, 21)  # Saturday -> itself


@pytest.mark.parametrize("target,days,expected", [
    (7, 4, 4), (5, 4, 3), (3, 4, 2), (7, 6, 6), (5, 6, 5), (3, 6, 3), (1, 2, 1), (3, 7, 3), (7, 1, 1),
])
def test_scaled_targets_match_rules_doc(target, days, expected):
    assert scaled_target(target, days) == expected


def test_partial_credit_table():
    assert goal_points(2, 3) == pytest.approx(6.667, abs=1e-3)
    assert goal_points(6, 7) == pytest.approx(8.571, abs=1e-3)
    assert goal_points(4, 3) == 10  # going past the target earns nothing extra
    assert goal_points(0, 5) == 0
    assert fmt(goal_points(6, 7)) == "8.6" and fmt(goal_points(2, 3)) == "6.7" and fmt(10.0) == "10"


def _player(uid, *goals):
    return PlayerWeek(uid, [GoalWeek(i, f"g{i}", t, d) for i, (t, d) in enumerate(goals)])


def test_example_week_from_rules_doc():
    p = _player(1, (3, 3), (7, 6), (7, 7))
    assert not p.perfect
    assert p.points == pytest.approx(28.571, abs=1e-3)
    perfect = _player(1, (3, 3), (7, 7), (7, 7))
    assert perfect.points == 45


def test_team_bonus_only_when_everyone_perfect():
    a = _player(1, (3, 3), (7, 7), (7, 7))
    b = _player(2, (3, 3), (7, 7), (5, 5))
    assert TeamWeek("x", [a, b]).goal_points == pytest.approx(90 * 1.25)
    c = _player(3, (3, 2), (7, 7), (5, 5))
    assert TeamWeek("x", [a, b, c]).goal_points == pytest.approx(45 + 45 + c.points)


def test_mission_cap_in_date_order():
    comps = [(date(2026, 12, d), 2.0) for d in range(1, 12)]  # 11 x 2 = 22
    counted = cap_missions(comps)
    assert sum(counted) == 20 and counted[-1] == 0


def test_standings_team_average():
    weeks = challenge_weeks(date(2026, 12, 5), date(2026, 12, 11))
    pw = {
        1: [_player(1, (3, 3), (7, 7), (7, 7))],  # 45
        2: [_player(2, (3, 3), (7, 7), (7, 7))],  # 45 -> team A perfect, +25%
        3: [_player(3, (3, 3), (7, 7), (7, 6))],  # 28.57
    }
    s = Standings(weeks, pw, {1: 1.0, 3: 2.0}, teams={1: "A", 2: "A", 3: "B"})
    assert s.team_total("A") == pytest.approx((90 * 1.25 + 1.0) / 2)
    assert s.team_total("B") == pytest.approx(28.571 + 2.0, abs=1e-3)
    assert s.ranked_teams()[0][0] == "A"
    assert s.ranked_players()[0] == (1, 46.0)

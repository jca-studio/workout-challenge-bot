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
    # Each goal is worth up to 1 point: the share of the target hit, capped at 1.
    assert goal_points(2, 3) == pytest.approx(0.667, abs=1e-3)
    assert goal_points(6, 7) == pytest.approx(0.857, abs=1e-3)
    assert goal_points(5, 3) == 1  # going past the target earns nothing extra
    assert goal_points(7, 7) == 1  # brushing teeth every day is 1 point a week, not 7
    assert goal_points(0, 5) == 0
    assert fmt(goal_points(6, 7)) == "0.86" and fmt(goal_points(2, 3)) == "0.67" and fmt(1.0) == "1"
    assert fmt(2.857) == "2.86" and fmt(0.25) == "0.25" and fmt(0.5) == "0.5" and fmt(0) == "0"


def _player(uid, *goals):
    return PlayerWeek(uid, [GoalWeek(i, f"g{i}", t, d) for i, (t, d) in enumerate(goals)])


def test_example_week_from_rules_doc():
    # 3 workouts (did 4), veggies 7 (did 7), sleep 7 (did 6) -> 1 + 1 + 0.86, no bonus
    p = _player(1, (3, 4), (7, 7), (7, 6))
    assert not p.perfect
    assert p.points == pytest.approx(2.857, abs=1e-3)
    perfect = _player(1, (3, 3), (7, 7), (7, 7))
    assert perfect.perfect and perfect.points == 4  # 3 goals + 1 perfect week bonus


def test_team_bonus_only_when_everyone_perfect():
    a = _player(1, (3, 3), (7, 7), (7, 7))
    b = _player(2, (3, 3), (7, 7), (5, 5))
    assert TeamWeek("x", [a, b]).goal_points == 4 + 4 + 2  # +1 per player
    c = _player(3, (3, 2), (7, 7), (5, 5))
    assert TeamWeek("x", [a, b, c]).goal_points == pytest.approx(4 + 4 + c.points)


def test_mission_cap_is_per_week():
    weeks = challenge_weeks(date(2026, 12, 5), date(2026, 12, 18))  # two full weeks
    comps = [(date(2026, 12, d), 0.5) for d in range(5, 12)]  # 7 x 0.5 in week 1
    comps += [(date(2026, 12, 12), 0.25), (date(2026, 12, 13), 0.25)]  # week 2
    comps += [(date(2026, 12, 25), 0.5)]  # after the challenge: doesn't count
    counted = cap_missions(comps, weeks)
    assert sum(counted[:7]) == 1 and counted[2:7] == [0, 0, 0, 0, 0]
    assert counted[7:9] == [0.25, 0.25] and counted[9] == 0


def test_standings_team_average():
    weeks = challenge_weeks(date(2026, 12, 5), date(2026, 12, 11))
    pw = {
        1: [_player(1, (3, 3), (7, 7), (7, 7))],  # 4
        2: [_player(2, (3, 3), (7, 7), (7, 7))],  # 4 -> team A all perfect, +1 each
        3: [_player(3, (3, 3), (7, 7), (7, 6))],  # 2.857
    }
    s = Standings(weeks, pw, {1: 0.5, 3: 1.0}, teams={1: "A", 2: "A", 3: "B"})
    assert s.team_total("A") == pytest.approx((4 + 4 + 2 + 0.5) / 2)
    assert s.team_total("B") == pytest.approx(2.857 + 1.0, abs=1e-3)
    assert s.ranked_teams()[0][0] == "A"
    assert s.ranked_players()[0] == (1, 4.5)

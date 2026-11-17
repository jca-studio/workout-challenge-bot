"""Pure scoring rules for the challenge. No Discord, no database.

Rules (from the proposed rules doc):
- Every player has 3 goals, each "do X on N days per week" (N = 1..7).
- Weeks run Saturday through Friday. A partial week at the start or end of the
  challenge scales each target down to fit, rounding up.
- Each goal is worth up to 10 points a week: 10 x days done / target, capped at 10.
- Perfect week (every goal at target): goal points x 1.5.
- Team mode: if every player on a team has a perfect week, the team's goal
  points for that week get +25%. Individual multipliers apply first.
- Mini missions are added after multipliers, capped at 20 points per player
  for the whole challenge, and never count toward a perfect week.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

POINTS_PER_GOAL = 10.0
PERFECT_WEEK_MULTIPLIER = 1.5
TEAM_BONUS = 0.25
MISSION_CAP = 20.0
WEEK_START_WEEKDAY = 5  # Saturday (Monday = 0)


@dataclass(frozen=True)
class Week:
    number: int
    start: date
    end: date  # inclusive

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


def week_start_for(d: date) -> date:
    """The Saturday on or before d."""
    return d - timedelta(days=(d.weekday() - WEEK_START_WEEKDAY) % 7)


def challenge_weeks(start: date, end: date) -> list[Week]:
    """Split the challenge into Saturday-Friday weeks, clipped to its dates."""
    if end < start:
        raise ValueError("challenge ends before it starts")
    weeks: list[Week] = []
    cursor = start
    n = 1
    while cursor <= end:
        week_end = min(week_start_for(cursor) + timedelta(days=6), end)
        weeks.append(Week(n, cursor, week_end))
        cursor = week_end + timedelta(days=1)
        n += 1
    return weeks


def week_for(d: date, weeks: list[Week]) -> Week | None:
    for w in weeks:
        if w.contains(d):
            return w
    return None


def scaled_target(target: int, days_in_week: int) -> int:
    """Shrink a weekly target to fit a short week, rounding up.

    7-day goal in a 4-day week -> 4. 3-day goal in a 4-day week -> 2.
    """
    if not 1 <= target <= 7:
        raise ValueError("target must be 1-7 days")
    if days_in_week >= 7:
        return target
    return min(days_in_week, math.ceil(target * days_in_week / 7))


def goal_points(days_done: int, target: int) -> float:
    if target <= 0:
        return 0.0
    return POINTS_PER_GOAL * min(days_done, target) / target


@dataclass
class GoalWeek:
    goal_id: int
    name: str
    target: int  # already scaled for the week
    days_done: int

    @property
    def points(self) -> float:
        return goal_points(self.days_done, self.target)

    @property
    def perfect(self) -> bool:
        return self.days_done >= self.target


@dataclass
class PlayerWeek:
    user_id: int
    goals: list[GoalWeek]

    @property
    def raw_points(self) -> float:
        return sum(g.points for g in self.goals)

    @property
    def perfect(self) -> bool:
        return bool(self.goals) and all(g.perfect for g in self.goals)

    @property
    def multiplier(self) -> float:
        return PERFECT_WEEK_MULTIPLIER if self.perfect else 1.0

    @property
    def points(self) -> float:
        return self.raw_points * self.multiplier


@dataclass
class TeamWeek:
    team: str
    players: list[PlayerWeek]

    @property
    def perfect(self) -> bool:
        return bool(self.players) and all(p.perfect for p in self.players)

    @property
    def goal_points(self) -> float:
        base = sum(p.points for p in self.players)
        return base * (1 + TEAM_BONUS) if self.perfect else base


def cap_missions(completions: list[tuple[date, float]], cap: float = MISSION_CAP) -> list[float]:
    """Apply the season cap in date order. Returns the points that count for each completion."""
    counted: list[float] = []
    total = 0.0
    for _, pts in sorted(completions, key=lambda c: c[0]):
        allowed = max(0.0, min(pts, cap - total))
        counted.append(allowed)
        total += allowed
    return counted


@dataclass
class Standings:
    weeks: list[Week]
    player_weeks: dict[int, list[PlayerWeek]]  # user_id -> one per week
    mission_points: dict[int, float]  # user_id -> capped season total
    teams: dict[int, str] = field(default_factory=dict)  # user_id -> team name
    team_average: bool = True

    def player_total(self, user_id: int) -> float:
        goals = sum(pw.points for pw in self.player_weeks.get(user_id, []))
        return goals + self.mission_points.get(user_id, 0.0)

    def team_weeks(self, team: str) -> list[TeamWeek]:
        members = [u for u, t in self.teams.items() if t == team]
        out = []
        for i, _ in enumerate(self.weeks):
            out.append(TeamWeek(team, [self.player_weeks[u][i] for u in members if u in self.player_weeks]))
        return out

    def team_total(self, team: str) -> float:
        members = [u for u, t in self.teams.items() if t == team]
        if not members:
            return 0.0
        total = sum(tw.goal_points for tw in self.team_weeks(team))
        total += sum(self.mission_points.get(u, 0.0) for u in members)
        return total / len(members) if self.team_average else total

    def ranked_players(self) -> list[tuple[int, float]]:
        return sorted(((u, self.player_total(u)) for u in self.player_weeks), key=lambda x: -x[1])

    def ranked_teams(self) -> list[tuple[str, float]]:
        names = sorted(set(self.teams.values()))
        return sorted(((t, self.team_total(t)) for t in names), key=lambda x: -x[1])


def fmt(points: float) -> str:
    """Display rounding only. Scores are kept at full precision."""
    return f"{points:.1f}".rstrip("0").rstrip(".") if points != int(points) else str(int(points))


def progress_bar(days_done: int, target: int) -> str:
    done = min(days_done, target)
    return "✅" * done + "⬜" * (target - done)

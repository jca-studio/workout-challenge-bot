"""Settings. Everything the group hasn't decided yet is a switch here, set with environment variables."""
import os
from zoneinfo import ZoneInfo


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
DATABASE_PATH = _env("DATABASE_PATH", "data/challenge.db")

# All times (reminders, daily mission, weekly lock, standings) use this time zone.
TZ = ZoneInfo(_env("TIMEZONE", "America/New_York"))

REMINDER_HOUR = int(_env("REMINDER_HOUR", "18"))          # 6 p.m. reminder if you haven't checked in
MISSION_POST_HOUR = int(_env("MISSION_POST_HOUR", "4"))    # kickoff + daily mini mission
STANDINGS_HOUR = int(_env("STANDINGS_HOUR", "5"))          # final standings after the last day
ROUNDUP_HOUR = int(_env("ROUNDUP_HOUR", "5"))              # daily roundup of yesterday
SIGNUP_REMINDER_HOUR = int(_env("SIGNUP_REMINDER_HOUR", "10"))  # daily nudge while sign-ups are open
OVERVIEW_HOUR = int(_env("OVERVIEW_HOUR", "19"))           # what-to-expect post, the night before the start
GOAL_NUDGE_DAYS = int(_env("GOAL_NUDGE_DAYS", "3"))        # DM players without goals this many days before the deadline
# The day before the start: players vote teams or individual, and the commissioner makes the final call.
MODE_VOTE_HOUR = int(_env("MODE_VOTE_HOUR", "12"))         # vote post goes up
MODE_DECIDE_HOUR = int(_env("MODE_DECIDE_HOUR", "21"))     # nudge the commissioner if she hasn't picked
WARNING_HOUR = int(_env("WARNING_HOUR", "20"))             # removal warning, the night before the deadline
# At the deadline: remove players without 3 goals, and approve everyone else's goals automatically.
REMOVE_WITHOUT_GOALS = _env("REMOVE_WITHOUT_GOALS", "true").lower() == "true"
AUTO_APPROVE_AT_LOCK = _env("AUTO_APPROVE_AT_LOCK", "true").lower() == "true"
MISSION_WINDOW_HOURS = int(_env("MISSION_WINDOW_HOURS", "24"))

TEAM_MODE_MIN_PLAYERS = int(_env("TEAM_MODE_MIN_PLAYERS", "8"))
TEAM_NAMES = tuple(n.strip() for n in _env("TEAM_NAMES", "Bad Bitches,Muscle Mamis").split(","))[:2]
# With an odd number of players, compare teams by average per player so headcount can't win it.
TEAM_SCORE_AVERAGE = _env("TEAM_SCORE_AVERAGE", "true").lower() == "true"

# Photos: "optional" (encouraged, never required), "required" (every goal), or "per_goal"
# (optional by default; the commissioner turns it on goal by goal with /commish photo).
PHOTO_MODE = _env("PHOTO_MODE", "optional")

MISSION_WEEKLY_CAP = float(_env("MISSION_WEEKLY_CAP", "0"))  # most mini mission points per week; 0 = no cap
MIXUP_CHANCE = float(_env("MIXUP_CHANCE", "0.15"))  # about once a week

WINNER_ROLE_NAME = _env("WINNER_ROLE_NAME", "Challenge Champion")
TOP_SCORER_ROLE_NAME = _env("TOP_SCORER_ROLE_NAME", "That Girl")

# Winners get an emoji in front of their server nickname until the next challenge ends.
CROWN_NICKNAMES = _env("CROWN_NICKNAMES", "true").lower() == "true"
WINNER_EMOJI = _env("WINNER_EMOJI", "👑")
TOP_SCORER_EMOJI = _env("TOP_SCORER_EMOJI", "💎")

# Scoring weights (see the Game Rules doc)
PERFECT_WEEK_BONUS = float(_env("PERFECT_WEEK_BONUS", "1"))
TEAM_BONUS_PER_PLAYER = float(_env("TEAM_BONUS_PER_PLAYER", "1"))

# Who gets install alerts and can run /botstats. Defaults to the bot's owner in the Developer Portal.
OWNER_ID = int(_env("OWNER_ID", "0") or 0)

# Photos posted between midnight and this hour can count for the day before, and each week stays open
# until this hour on Saturday morning, so late-night check-ins aren't lost.
LATE_NIGHT_GRACE_HOUR = int(_env("LATE_NIGHT_GRACE_HOUR", "3"))

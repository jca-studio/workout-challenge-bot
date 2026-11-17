"""Settings. Everything the group hasn't decided yet is a switch here, set with environment variables."""
import os
from zoneinfo import ZoneInfo


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
DATABASE_PATH = _env("DATABASE_PATH", "data/challenge.db")

# All times (reminders, daily mission, weekly lock, standings) use this time zone.
TZ = ZoneInfo(_env("TIMEZONE", "America/New_York"))

REMINDER_HOUR = int(_env("REMINDER_HOUR", "20"))          # 8 p.m. reminder if you haven't checked in
MISSION_POST_HOUR = int(_env("MISSION_POST_HOUR", "7"))    # daily mini mission
STANDINGS_HOUR = int(_env("STANDINGS_HOUR", "9"))          # Saturday standings
MISSION_WINDOW_HOURS = int(_env("MISSION_WINDOW_HOURS", "24"))

TEAM_MODE_MIN_PLAYERS = int(_env("TEAM_MODE_MIN_PLAYERS", "8"))
TEAM_NAMES = tuple(n.strip() for n in _env("TEAM_NAMES", "Bad Bitches,Muscle Mamis").split(","))[:2]
# With an odd number of players, compare teams by average per player so headcount can't win it.
TEAM_SCORE_AVERAGE = _env("TEAM_SCORE_AVERAGE", "true").lower() == "true"

# Photos: "optional" (encouraged, never required), "required" (every goal), or "per_goal"
# (optional by default; the commissioner turns it on goal by goal with /commish photo).
PHOTO_MODE = _env("PHOTO_MODE", "optional")

MISSION_CAP = float(_env("MISSION_CAP", "20"))
MIXUP_CHANCE = float(_env("MIXUP_CHANCE", "0.15"))  # about once a week

WINNER_ROLE_NAME = _env("WINNER_ROLE_NAME", "Challenge Champion")
TOP_SCORER_ROLE_NAME = _env("TOP_SCORER_ROLE_NAME", "That Girl")

# Winners get an emoji in front of their server nickname until the next challenge ends.
CROWN_NICKNAMES = _env("CROWN_NICKNAMES", "true").lower() == "true"
WINNER_EMOJI = _env("WINNER_EMOJI", "👑")
TOP_SCORER_EMOJI = _env("TOP_SCORER_EMOJI", "💎")

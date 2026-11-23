"""SQLite storage. One active challenge per Discord server at a time."""
from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    announce_channel_id INTEGER,
    proof_channel_id INTEGER,
    commissioner_role_id INTEGER,
    mission_channel_id INTEGER
);
CREATE TABLE IF NOT EXISTS challenges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL,            -- signup | ready | active | finished | cancelled
    mode TEXT,                       -- teams | individual
    signup_channel_id INTEGER,
    signup_message_id INTEGER,
    signup_deadline TEXT,            -- last day to join (inclusive)
    mode_pick TEXT,                  -- the commissioner's call (teams | individual), applied at the start
    vote_channel_id INTEGER,
    vote_message_id INTEGER,
    created_by INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS players (
    challenge_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    team TEXT,
    PRIMARY KEY (challenge_id, user_id)
);
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    challenge_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    slot INTEGER NOT NULL,
    name TEXT NOT NULL,
    target_days INTEGER NOT NULL CHECK (target_days BETWEEN 1 AND 7),
    photo_required INTEGER NOT NULL DEFAULT 0,
    approved INTEGER NOT NULL DEFAULT 0,
    UNIQUE (challenge_id, user_id, slot)
);
CREATE TABLE IF NOT EXISTS checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    proof_url TEXT,
    created_at TEXT NOT NULL,
    added_by INTEGER,
    UNIQUE (goal_id, day)
);
CREATE TABLE IF NOT EXISTS missions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    theme TEXT NOT NULL,
    text TEXT NOT NULL,
    points REAL NOT NULL,
    effort TEXT,
    proof TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS daily_missions (
    challenge_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    mission_id INTEGER NOT NULL,
    posted_at TEXT NOT NULL,
    PRIMARY KEY (challenge_id, day)
);
CREATE TABLE IF NOT EXISTS mission_completions (
    challenge_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    points REAL NOT NULL,
    proof_url TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (challenge_id, user_id, day)
);
CREATE TABLE IF NOT EXISTS crowns (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    original_nick TEXT,              -- what to put back when the crown moves on
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS mode_votes (
    challenge_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    choice TEXT NOT NULL,            -- teams | individual
    PRIMARY KEY (challenge_id, user_id)
);
CREATE TABLE IF NOT EXISTS job_log (
    challenge_id INTEGER NOT NULL,
    job TEXT NOT NULL,
    key TEXT NOT NULL,
    done_at TEXT NOT NULL,
    PRIMARY KEY (challenge_id, job, key)
);
"""

THEMES = [
    "Munchie Monday", "Thoughtful Tuesday", "Work It Wednesday", "Trash It Thursday",
    "Feel Good Friday", "Satisfying Saturday", "Slow Down Sunday",
]
MIX_UP = "Random Mix-Up"


def theme_for(d: date) -> str:
    return THEMES[d.weekday()]


@dataclass
class Challenge:
    id: int
    guild_id: int
    name: str
    start: date
    end: date
    status: str
    mode: str | None
    signup_channel_id: int | None
    signup_message_id: int | None
    signup_deadline: date | None = None
    created_by: int | None = None
    mode_pick: str | None = None
    vote_channel_id: int | None = None
    vote_message_id: int | None = None


@dataclass
class Goal:
    id: int
    user_id: int
    slot: int
    name: str
    target_days: int
    photo_required: bool
    approved: bool


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        # Databases created before the sign-up deadline existed get the new column.
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(challenges)")}
        for col, kind in (("signup_deadline", "TEXT"), ("mode_pick", "TEXT"),
                          ("vote_channel_id", "INTEGER"), ("vote_message_id", "INTEGER")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE challenges ADD COLUMN {col} {kind}")
        gcols = {r[1] for r in self.conn.execute("PRAGMA table_info(guild_settings)")}
        if "mission_channel_id" not in gcols:
            self.conn.execute("ALTER TABLE guild_settings ADD COLUMN mission_channel_id INTEGER")
        self.conn.commit()

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    # ---- settings ----
    def set_settings(self, guild_id: int, announce: int, proof: int, commissioner_role: int | None,
                     mission: int | None = None):
        self._x(
            "INSERT INTO guild_settings (guild_id,announce_channel_id,proof_channel_id,commissioner_role_id,"
            "mission_channel_id) VALUES (?,?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET "
            "announce_channel_id=excluded.announce_channel_id, proof_channel_id=excluded.proof_channel_id, "
            "commissioner_role_id=excluded.commissioner_role_id, mission_channel_id=excluded.mission_channel_id",
            (guild_id, announce, proof, commissioner_role, mission),
        )

    def settings(self, guild_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,)).fetchone()

    def all_guild_ids(self) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT guild_id FROM guild_settings")]

    # ---- challenges ----
    @staticmethod
    def _challenge(r: sqlite3.Row | None) -> Challenge | None:
        if r is None:
            return None
        return Challenge(r["id"], r["guild_id"], r["name"], date.fromisoformat(r["start_date"]),
                         date.fromisoformat(r["end_date"]), r["status"], r["mode"],
                         r["signup_channel_id"], r["signup_message_id"],
                         date.fromisoformat(r["signup_deadline"]) if r["signup_deadline"] else None,
                         r["created_by"], r["mode_pick"], r["vote_channel_id"], r["vote_message_id"])

    def current_challenge(self, guild_id: int) -> Challenge | None:
        r = self.conn.execute(
            "SELECT * FROM challenges WHERE guild_id=? AND status IN ('signup','ready','active') "
            "ORDER BY id DESC LIMIT 1", (guild_id,)).fetchone()
        return self._challenge(r)

    def latest_challenge(self, guild_id: int) -> Challenge | None:
        r = self.conn.execute(
            "SELECT * FROM challenges WHERE guild_id=? AND status != 'cancelled' ORDER BY id DESC LIMIT 1",
            (guild_id,)).fetchone()
        return self._challenge(r)

    def create_challenge(self, guild_id: int, name: str, start: date, end: date, created_by: int,
                         signup_deadline: date | None = None) -> Challenge:
        cur = self._x(
            "INSERT INTO challenges (guild_id,name,start_date,end_date,status,created_by,created_at,signup_deadline) "
            "VALUES (?,?,?,?,'signup',?,?,?)",
            (guild_id, name, start.isoformat(), end.isoformat(), created_by, datetime.now(UTC).isoformat(),
             signup_deadline.isoformat() if signup_deadline else None),
        )
        return self.get_challenge(cur.lastrowid)

    def get_challenge(self, challenge_id: int) -> Challenge | None:
        return self._challenge(self.conn.execute("SELECT * FROM challenges WHERE id=?", (challenge_id,)).fetchone())

    def update_challenge(self, challenge_id: int, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE challenges SET {cols} WHERE id=?", (*fields.values(), challenge_id))

    # ---- players ----
    def join(self, challenge_id: int, user_id: int) -> bool:
        cur = self._x("INSERT OR IGNORE INTO players (challenge_id,user_id) VALUES (?,?)", (challenge_id, user_id))
        return cur.rowcount == 1

    def leave(self, challenge_id: int, user_id: int) -> bool:
        cur = self._x("DELETE FROM players WHERE challenge_id=? AND user_id=?", (challenge_id, user_id))
        self._x("DELETE FROM goals WHERE challenge_id=? AND user_id=?", (challenge_id, user_id))
        return cur.rowcount == 1

    def players(self, challenge_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM players WHERE challenge_id=? ORDER BY user_id", (challenge_id,)).fetchall()

    def is_player(self, challenge_id: int, user_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM players WHERE challenge_id=? AND user_id=?",
                                 (challenge_id, user_id)).fetchone() is not None

    def set_vote(self, challenge_id: int, user_id: int, choice: str):
        self._x("INSERT INTO mode_votes (challenge_id,user_id,choice) VALUES (?,?,?) "
                "ON CONFLICT(challenge_id,user_id) DO UPDATE SET choice=excluded.choice",
                (challenge_id, user_id, choice))

    def votes(self, challenge_id: int) -> dict[int, str]:
        """Votes from players still in the challenge (anyone who left or was removed doesn't count)."""
        rows = self.conn.execute(
            "SELECT v.user_id, v.choice FROM mode_votes v JOIN players p "
            "ON p.challenge_id=v.challenge_id AND p.user_id=v.user_id WHERE v.challenge_id=?", (challenge_id,))
        return {r[0]: r[1] for r in rows}

    def set_team(self, challenge_id: int, user_id: int, team: str | None):
        self._x("UPDATE players SET team=? WHERE challenge_id=? AND user_id=?", (team, challenge_id, user_id))

    def teams(self, challenge_id: int) -> dict[int, str]:
        return {r["user_id"]: r["team"] for r in self.players(challenge_id) if r["team"]}

    # ---- goals ----
    def set_goals(self, challenge_id: int, user_id: int, goals: list[tuple[str, int]], photo_default: bool):
        self._x("DELETE FROM goals WHERE challenge_id=? AND user_id=?", (challenge_id, user_id))
        for slot, (name, days) in enumerate(goals, start=1):
            self._x("INSERT INTO goals (challenge_id,user_id,slot,name,target_days,photo_required) VALUES (?,?,?,?,?,?)",
                    (challenge_id, user_id, slot, name.strip(), days, int(photo_default)))

    def add_goal(self, challenge_id: int, user_id: int, name: str, days: int, photo_default: bool) -> int | None:
        """Add one goal in the next open slot (1-3). Returns the slot, or None if she already has 3."""
        used = {g.slot for g in self.goals(challenge_id, user_id)}
        free = [n for n in (1, 2, 3) if n not in used]
        if not free:
            return None
        self._x("INSERT INTO goals (challenge_id,user_id,slot,name,target_days,photo_required) VALUES (?,?,?,?,?,?)",
                (challenge_id, user_id, free[0], name.strip(), days, int(photo_default)))
        return free[0]

    def remove_goal(self, challenge_id: int, user_id: int, slot: int) -> bool:
        """Remove one goal and renumber the rest 1, 2, 3 so slots stay in order."""
        g = next((x for x in self.goals(challenge_id, user_id) if x.slot == slot), None)
        if not g:
            return False
        self._x("DELETE FROM checkins WHERE goal_id=?", (g.id,))
        self._x("DELETE FROM goals WHERE id=?", (g.id,))
        for new_slot, rest in enumerate(self.goals(challenge_id, user_id), start=1):
            self._x("UPDATE goals SET slot=? WHERE id=?", (new_slot, rest.id))
        return True

    def goals(self, challenge_id: int, user_id: int, approved_only: bool = False) -> list[Goal]:
        sql = "SELECT * FROM goals WHERE challenge_id=? AND user_id=?"
        if approved_only:
            sql += " AND approved=1"
        rows = self.conn.execute(sql + " ORDER BY slot", (challenge_id, user_id)).fetchall()
        return [Goal(r["id"], r["user_id"], r["slot"], r["name"], r["target_days"],
                     bool(r["photo_required"]), bool(r["approved"])) for r in rows]

    def goal(self, goal_id: int) -> Goal | None:
        r = self.conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if not r:
            return None
        return Goal(r["id"], r["user_id"], r["slot"], r["name"], r["target_days"],
                    bool(r["photo_required"]), bool(r["approved"]))

    def approve_goals(self, challenge_id: int, user_id: int | None = None) -> int:
        if user_id is None:
            return self._x("UPDATE goals SET approved=1 WHERE challenge_id=?", (challenge_id,)).rowcount
        return self._x("UPDATE goals SET approved=1 WHERE challenge_id=? AND user_id=?",
                       (challenge_id, user_id)).rowcount

    def set_photo_required(self, challenge_id: int, user_id: int, slot: int, required: bool) -> bool:
        return self._x("UPDATE goals SET photo_required=? WHERE challenge_id=? AND user_id=? AND slot=?",
                       (int(required), challenge_id, user_id, slot)).rowcount == 1

    # ---- check-ins ----
    def add_checkin(self, goal_id: int, user_id: int, day: date, proof_url: str | None, added_by: int | None = None) -> bool:
        cur = self._x(
            "INSERT OR IGNORE INTO checkins (goal_id,user_id,day,proof_url,created_at,added_by) VALUES (?,?,?,?,?,?)",
            (goal_id, user_id, day.isoformat(), proof_url, datetime.now(UTC).isoformat(), added_by))
        if cur.rowcount == 0 and proof_url:
            self._x("UPDATE checkins SET proof_url=? WHERE goal_id=? AND day=? AND proof_url IS NULL",
                    (proof_url, goal_id, day.isoformat()))
        return cur.rowcount == 1

    def remove_checkin(self, goal_id: int, day: date) -> bool:
        return self._x("DELETE FROM checkins WHERE goal_id=? AND day=?", (goal_id, day.isoformat())).rowcount == 1

    def checkin_days(self, goal_id: int, start: date, end: date) -> set[date]:
        rows = self.conn.execute("SELECT day FROM checkins WHERE goal_id=? AND day BETWEEN ? AND ?",
                                 (goal_id, start.isoformat(), end.isoformat()))
        return {date.fromisoformat(r[0]) for r in rows}

    def checked_in_on(self, challenge_id: int, user_id: int, day: date) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM checkins c JOIN goals g ON g.id=c.goal_id WHERE g.challenge_id=? AND c.user_id=? AND c.day=?",
            (challenge_id, user_id, day.isoformat())).fetchone() is not None

    def checkin_users_between(self, challenge_id: int, start: date, end: date) -> set[int]:
        """Players who logged at least one goal check-in between two days (inclusive)."""
        rows = self.conn.execute(
            "SELECT DISTINCT c.user_id FROM checkins c JOIN goals g ON g.id=c.goal_id "
            "WHERE g.challenge_id=? AND c.day BETWEEN ? AND ?", (challenge_id, start.isoformat(), end.isoformat()))
        return {r[0] for r in rows}

    def challenge_counts(self, guild_id: int) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) FROM challenges WHERE guild_id=? GROUP BY status", (guild_id,))
        return {r[0]: r[1] for r in rows}

    # ---- missions ----
    def import_missions(self, guild_id: int, csv_text: str, replace: bool = True) -> int:
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = []
        for r in reader:
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            theme, text, pts = r.get("day", ""), r.get("mission", ""), r.get("points", "")
            if not (theme and text and pts):
                continue
            if theme not in THEMES and theme != MIX_UP:
                raise ValueError(f"Unknown day theme: {theme!r}")
            rows.append((guild_id, theme, text, float(pts), r.get("effort"), r.get("proof")))
        if not rows:
            raise ValueError("No missions found. Columns needed: Day, Mission, Points.")
        if replace:
            self._x("UPDATE missions SET active=0 WHERE guild_id=?", (guild_id,))
        self.conn.executemany(
            "INSERT INTO missions (guild_id,theme,text,points,effort,proof) VALUES (?,?,?,?,?,?)", rows)
        self.conn.commit()
        return len(rows)

    def add_mission(self, guild_id: int, theme: str, text: str, points: float) -> int:
        return self._x("INSERT INTO missions (guild_id,theme,text,points) VALUES (?,?,?,?)",
                       (guild_id, theme, text, points)).lastrowid

    def missions(self, guild_id: int, theme: str | None = None) -> list[sqlite3.Row]:
        if theme:
            return self.conn.execute("SELECT * FROM missions WHERE guild_id=? AND active=1 AND theme=?",
                                     (guild_id, theme)).fetchall()
        return self.conn.execute("SELECT * FROM missions WHERE guild_id=? AND active=1", (guild_id,)).fetchall()

    def mission(self, mission_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()

    def used_mission_ids(self, challenge_id: int) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT mission_id FROM daily_missions WHERE challenge_id=?", (challenge_id,))}

    def set_daily_mission(self, challenge_id: int, day: date, mission_id: int, posted_at: datetime):
        self._x("INSERT OR REPLACE INTO daily_missions VALUES (?,?,?,?)",
                (challenge_id, day.isoformat(), mission_id, posted_at.isoformat()))

    def daily_mission(self, challenge_id: int, day: date) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT d.day, d.posted_at, m.* FROM daily_missions d JOIN missions m ON m.id=d.mission_id "
            "WHERE d.challenge_id=? AND d.day=?", (challenge_id, day.isoformat())).fetchone()

    def complete_mission(self, challenge_id: int, user_id: int, day: date, points: float, proof_url: str | None) -> bool:
        cur = self._x("INSERT OR IGNORE INTO mission_completions VALUES (?,?,?,?,?,?)",
                      (challenge_id, user_id, day.isoformat(), points, proof_url, datetime.now(UTC).isoformat()))
        return cur.rowcount == 1

    def upgrade_mission(self, challenge_id: int, user_id: int, day: date, points: float, proof_url: str | None) -> bool:
        """Raise an existing completion to the bonus points. False if there's nothing to raise."""
        cur = self._x("UPDATE mission_completions SET points=?, proof_url=COALESCE(?, proof_url) "
                      "WHERE challenge_id=? AND user_id=? AND day=? AND points<?",
                      (points, proof_url, challenge_id, user_id, day.isoformat(), points))
        return cur.rowcount == 1

    def set_mission_points(self, challenge_id: int, user_id: int, day: date, points: float):
        if points <= 0:
            self._x("DELETE FROM mission_completions WHERE challenge_id=? AND user_id=? AND day=?",
                    (challenge_id, user_id, day.isoformat()))
        else:
            self._x("INSERT INTO mission_completions VALUES (?,?,?,?,NULL,?) ON CONFLICT DO UPDATE SET points=excluded.points",
                    (challenge_id, user_id, day.isoformat(), points, datetime.now(UTC).isoformat()))

    def mission_completions(self, challenge_id: int, user_id: int) -> list[tuple[date, float]]:
        rows = self.conn.execute("SELECT day, points FROM mission_completions WHERE challenge_id=? AND user_id=?",
                                 (challenge_id, user_id))
        return [(date.fromisoformat(r[0]), r[1]) for r in rows]

    # ---- crowns (winner nicknames) ----
    def crowns(self, guild_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM crowns WHERE guild_id=?", (guild_id,)).fetchall()

    def add_crown(self, guild_id: int, user_id: int, original_nick: str | None):
        self._x("INSERT OR REPLACE INTO crowns VALUES (?,?,?)", (guild_id, user_id, original_nick))

    def clear_crown(self, guild_id: int, user_id: int):
        self._x("DELETE FROM crowns WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    # ---- job log (so scheduled posts happen once) ----
    def job_done(self, challenge_id: int, job: str, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM job_log WHERE challenge_id=? AND job=? AND key=?",
                                 (challenge_id, job, key)).fetchone() is not None

    def mark_job(self, challenge_id: int, job: str, key: str):
        self._x("INSERT OR IGNORE INTO job_log VALUES (?,?,?,?)",
                (challenge_id, job, key, datetime.now(UTC).isoformat()))

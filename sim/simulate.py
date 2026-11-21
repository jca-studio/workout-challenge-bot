"""Full-season simulation: sign-ups through the final crowning, hour by hour, with realistic players.

Run:  python sim/simulate.py            (writes sim/transcript.md and prints a findings summary)

Each player follows a persona (early bird, night owl, Friday catch-up, quitter...). Every message the bot
sends is checked against Discord's limits, and the bot's scores are compared with an independent tally
built from what each player actually did.
"""
import asyncio
import os
import random
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["TIMEZONE"] = "America/Los_Angeles"
os.environ["DATABASE_PATH"] = "/tmp/momsbot-sim/sim.db"
os.environ["OWNER_ID"] = "100000000000000001"
if os.path.exists(os.environ["DATABASE_PATH"]):
    os.remove(os.environ["DATABASE_PATH"])

import discord  # noqa: E402

from bot import config, game, main, scoring  # noqa: E402

START, END, DEADLINE, OPENED = date(2026, 11, 22), date(2026, 12, 21), date(2026, 11, 21), date(2026, 11, 14)
SIM_FROM, SIM_TO = OPENED, END + timedelta(days=2)
problems: list[str] = []
transcript: list[str] = []
clock = {"now": datetime.combine(OPENED, time(9), tzinfo=config.TZ)}


def now():
    return clock["now"]


def stamp():
    return now().strftime("%a %b %d %H:%M")


def problem(text):
    problems.append(f"[{stamp()}] {text}")


# ---------- Discord limit checks ----------

def check_view(view, where):
    if not view:
        return
    kids = list(view.children)
    if len(kids) > 25:
        problem(f"{where}: {len(kids)} buttons (max 25)")
    for b in kids:
        if b.label and len(b.label) > 80:
            problem(f"{where}: button label over 80 chars: {b.label!r}")
        if b.custom_id and len(b.custom_id) > 100:
            problem(f"{where}: custom_id over 100 chars")


def check_message(where, content=None, embed=None, embeds=None, view=None):
    if content and len(content) > 2000:
        problem(f"{where}: message text is {len(content)} chars (max 2000)")
    embeds = list(embeds or []) + ([embed] if embed else [])
    if len(embeds) > 10:
        problem(f"{where}: {len(embeds)} embeds (max 10)")
    total = 0
    for e in embeds:
        d = e.to_dict()
        t, desc = d.get("title", ""), d.get("description", "")
        if len(t) > 256:
            problem(f"{where}: embed title {len(t)} chars")
        if len(desc) > 4096:
            problem(f"{where}: embed description {len(desc)} chars (max 4096)")
        fields = d.get("fields", [])
        if len(fields) > 25:
            problem(f"{where}: {len(fields)} fields")
        size = len(t) + len(desc) + len(d.get("footer", {}).get("text", ""))
        for f in fields:
            if len(f["name"]) > 256 or not f["name"]:
                problem(f"{where}: bad field name {f['name']!r}")
            if len(f["value"]) > 1024 or not f["value"]:
                problem(f"{where}: field '{f['name']}' value is {len(f['value'])} chars (max 1024, not empty)")
            size += len(f["name"]) + len(f["value"])
        total += size
    if total > 6000:
        problem(f"{where}: embeds total {total} chars in one message (max 6000) -> Discord rejects it")
    check_view(view, where)


def summarize(content, kw):
    bits = []
    if content:
        bits.append(content)
    for e in ([kw["embed"]] if kw.get("embed") else []) + list(kw.get("embeds") or []):
        bits.append(f"[embed] {e.title or ''}\n{e.description or ''}"
                    + "".join(f"\n  · {f.name}: {f.value}" for f in e.fields))
    if kw.get("view"):
        bits.append("[buttons] " + " | ".join(b.label or "" for b in kw["view"].children))
    return "\n".join(bits)


# ---------- fakes ----------

class FakeMsg:
    def __init__(self, chan, mid, content, kw):
        self.id, self.channel, self.content, self.kw = mid, chan, content, kw
        self.jump_url = f"https://discord.com/channels/1/{chan.id}/{mid}"

    async def edit(self, content=None, embed=None, view=None, **kw):
        check_message(f"edit in #{self.channel.name}", content, embed=embed, view=view)
        if embed:
            self.kw["embed"] = embed
        if content:
            self.content = content


class FakeChannel:
    def __init__(self, cid, name):
        self.id, self.name, self.sent, self.mention = cid, name, [], f"#{name}"
        self.msgs = {}

    async def send(self, content=None, **kw):
        check_message(f"#{self.name}", content, kw.get("embed"), kw.get("embeds"), kw.get("view"))
        m = FakeMsg(self, 5000 + len(self.sent), content, kw)
        self.msgs[m.id] = m
        self.sent.append((now(), content, kw))
        transcript.append(f"**[{stamp()}] #{self.name}**\n```\n{summarize(content, kw)}\n```")
        return m

    async def fetch_message(self, mid):
        return self.msgs[mid]


class FakeRole:
    def __init__(self, name):
        self.name, self.members, self.id = name, [], abs(hash(name))


class FakeMember:
    def __init__(self, uid, guild, name):
        self.id, self.guild = uid, guild
        self.nick, self.global_name, self.name = None, name, name.lower()
        self.mention = f"<@{uid}>"
        self.roles = []

    async def edit(self, nick=None, reason=None):
        if nick and len(nick) > 32:
            problem(f"nickname over 32 chars: {nick!r}")
        if self.id == self.guild.owner_id:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="owner"), "owner")
        self.nick = nick

    async def add_roles(self, role, reason=None):
        if self not in role.members:
            role.members.append(self)

    async def remove_roles(self, role, reason=None):
        if self in role.members:
            role.members.remove(self)


class FakeGuild:
    def __init__(self):
        self.id, self.name = 900000000000000001, "Mom Squad"
        self.ann = FakeChannel(910000000000000001, "announcements")
        self.proof = FakeChannel(910000000000000002, "proof")
        self.missions = FakeChannel(910000000000000003, "mini-missions")
        self.roles, self.members = [], {}
        self.owner_id = OWNER  # Jenna owns the server and plays

    def get_channel(self, cid):
        return {c.id: c for c in (self.ann, self.proof, self.missions)}.get(cid)

    async def create_role(self, name, **kw):
        r = FakeRole(name)
        self.roles.append(r)
        return r

    def get_member(self, uid):
        return self.members.get(uid)

    async def fetch_member(self, uid):
        if uid not in self.members:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Unknown Member"), "Unknown Member")
        return self.members[uid]


DMS = defaultdict(list)  # uid -> [(time, content, view)]
DMS_OFF = set()


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.mention = f"<@{uid}>"

    async def send(self, content=None, **kw):
        if self.id in DMS_OFF:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="closed"), "Cannot send messages to this user")
        check_message(f"DM to {NAMES.get(self.id, self.id)}", content, kw.get("embed"), kw.get("embeds"), kw.get("view"))
        DMS[self.id].append((now(), content or "", kw.get("view"), kw))


class Followup:
    def __init__(self, resp):
        self.resp = resp

    async def send(self, content=None, **kw):
        self.resp.sent.append((content, kw))


class FakeResponse:
    def __init__(self):
        self.sent = []

    def is_done(self):
        return bool(self.sent)

    async def send_message(self, content=None, **kw):
        check_message("interaction reply", content, kw.get("embed"), kw.get("embeds"), kw.get("view"))
        self.sent.append((content, kw))

    async def defer(self, **kw):
        self.sent.append(("(deferred)", {}))

    async def edit_message(self, **kw):
        self.sent.append(("edit", kw))

    async def send_modal(self, modal):
        self.sent.append(("modal", {"modal": modal}))


def inter(uid, message=None, channel_id=None):
    resp = FakeResponse()
    member = GUILD.members.get(uid) or FakeUser(uid)
    i = SimpleNamespace(user=member, guild=GUILD, guild_id=GUILD.id, response=resp, followup=Followup(resp),
                        channel_id=channel_id, message=message)
    return i


def last_reply(i):
    texts = [c for c, _ in i.response.sent if isinstance(c, str) and c not in ("(deferred)", "edit", "modal")]
    return texts[-1] if texts else ""


class PhotoPost:
    """A photo someone posts in a channel."""

    def __init__(self, chan, uid):
        self.author = SimpleNamespace(id=uid, bot=False)
        self.guild, self.channel = GUILD, chan
        self.attachments = [object()]
        self.replies, self.reactions = [], []

    async def reply(self, content=None, **kw):
        check_message("photo reply", content, view=kw.get("view"))
        self.replies.append((content, kw))

    async def add_reaction(self, e):
        self.reactions.append(e)


# ---------- the players ----------

def snow(n):
    return 300000000000000000 + n * 7919


OWNER = int(os.environ["OWNER_ID"])
PERSONAS = {
    # name: (persona, goals typed into the form)
    "Jenna": ("owner-perfect", ["Lift, 3 days", "10k steps, 5 days", "Affirmations daily"]),
    "Caroline": ("early-bird", ["Run, 4 days", "Read 10 pages every day", "Alcohol-free, 6 days"]),
    "Larin": ("night-owl", ["Yoga, 3 days", "7 hours of sleep, 5 days", "No sugar, 5 days"]),
    "Slaycie": ("friday-catchup", ["Workout, 4 days", "Drink 80oz water, 7 days", "Journal, 3 days"]),
    "Maya": ("friday-after-midnight", ["Pilates, 2 days", "Veggies every day", "Stretch, 4 days"]),
    "Priya": ("partial", ["Gym, 4 days", "Meditate, 5 days", "No phone in bed, 7 days"]),
    "Dani": ("mission-fan", ["Walk, 3 days", "Vitamins every day", "Floss, 7 days"]),
    "Becca": ("quitter", ["Spin, 3 days", "Read, 4 days", "Skincare every day"]),
    "Tash": ("removed", ["HIIT, 3 days", "Water, 7 days", "Bed by 10, 5 days"]),
    "Nicole": ("no-goals", None),
    "Erin": ("dms-off", ["Run, 3 days", "Protein, 7 days", "Gratitude, 7 days"]),
    "Kim": ("reminder-tapper", ["Barre, 3 days", "Steps, 7 days", "Read, 3 days"]),
    "Sophia": ("partial", ["Swim, 2 days", "Meal prep, 1 day", "Alcohol-free, 5 days"]),
    "Rachel": ("bad-goal-then-fix", ["Sleep 7 hours", "Lift, 3 days", "Walk the dog daily"]),
}
IDS = {name: (OWNER if name == "Jenna" else snow(i)) for i, name in enumerate(PERSONAS, 1)}
NAMES = {v: k for k, v in IDS.items()}
LATE_JOINER = snow(99)
NAMES[LATE_JOINER] = "Late Lucy"

# ground truth: what each player successfully logged, and mission points she earned
TRUTH_CHECKINS = defaultdict(set)   # (uid, slot) -> {dates}
TRUTH_MISSIONS = defaultdict(dict)  # uid -> {date: pts}
REJECTED = []                       # (who, what, why) for attempts the bot turned down
rng = random.Random(2026)

GUILD = None


def setup_world(monkeypatch_targets):
    global GUILD
    GUILD = FakeGuild()
    for name, uid in IDS.items():
        GUILD.members[uid] = FakeMember(uid, GUILD, name)
    GUILD.members[LATE_JOINER] = FakeMember(LATE_JOINER, GUILD, "Late Lucy")
    DMS_OFF.add(IDS["Erin"])


def goal_slots(uid):
    ch = main.db.current_challenge(GUILD.id) or main.db.latest_challenge(GUILD.id)
    return {g.slot: g for g in main.db.goals(ch.id, uid)}


def scaled(uid, slot, week):
    g = goal_slots(uid)[slot]
    return scoring.scaled_target(g.target_days, week.days)


def hit_rate(persona):
    return {"owner-perfect": 1.0, "early-bird": 1.0, "night-owl": 1.0, "friday-catchup": 0.95,
            "friday-after-midnight": 0.9, "partial": 0.6, "mission-fan": 0.4, "quitter": 0.8, "removed": 0.7,
            "dms-off": 0.75, "reminder-tapper": 0.85, "bad-goal-then-fix": 0.8}.get(persona, 0.0)


async def log_via_photo(uid, day, slot_list, chan=None):
    """Post a photo in #proof and tap the buttons for these goals (for `day`)."""
    post = PhotoPost(chan or GUILD.proof, uid)
    await main.on_message(post)
    if not post.replies:
        REJECTED.append((NAMES[uid], f"photo for {day}", f"no buttons (reacted {post.reactions})"))
        return []
    view = post.replies[0][1]["view"]
    logged = []
    for slot in slot_list:
        g = goal_slots(uid)[slot]
        b = next((b for b in view.children if b.custom_id.startswith(f"mb:p:{uid}:g:{g.id}:{day.isoformat()}")), None)
        if not b:
            REJECTED.append((NAMES[uid], f"{g.name} {day}", "no button offered"))
            continue
        parts = b.custom_id.split(":")
        i = inter(uid, message=SimpleNamespace(reference=SimpleNamespace(message_id=1), jump_url="x", content="",
                                               edit=_noop_edit), channel_id=GUILD.proof.id)
        await main.handle_proof_button(i, int(parts[2]), parts[3], parts[4], parts[5])
        r = last_reply(i)
        if r.startswith("Logged"):
            TRUTH_CHECKINS[(uid, slot)].add(day)
            logged.append(slot)
        else:
            REJECTED.append((NAMES[uid], f"{g.name} {day}", r))
    return logged


async def _noop_edit(**kw):
    pass


async def log_via_command(uid, day, slot):
    i = inter(uid)
    await main.checkin.callback(i, goal=slot, day=day.isoformat(), photo=None)
    r = last_reply(i)
    if r.startswith("Logged"):
        TRUTH_CHECKINS[(uid, slot)].add(day)
        return True
    REJECTED.append((NAMES[uid], f"/checkin slot {slot} {day}", r))
    return False


async def do_mission(uid):
    ch = main.db.current_challenge(GUILD.id)
    m = game.open_mission(main.db, ch, now()) if ch else None
    i = inter(uid)
    await main.mission_done.callback(i, photo=None)
    r = last_reply(i)
    if r.startswith("⚡") and m:
        TRUTH_MISSIONS[uid][date.fromisoformat(m["day"])] = m["points"]
    elif not r.startswith("You already"):
        REJECTED.append((NAMES[uid], "mission", r))


def wants(uid, slot, day, persona):
    """Does she do this goal today? Perfect personas hit exactly their (scaled) targets."""
    ch = main.db.current_challenge(GUILD.id)
    week = scoring.week_for(day, scoring.challenge_weeks(ch.start, ch.end))
    target = scaled(uid, slot, week)
    done = len(TRUTH_CHECKINS[(uid, slot)] & {week.start + timedelta(days=k) for k in range(week.days)})
    days_left = (week.end - day).days + 1
    need = target - done
    if need <= 0:
        return False
    if hit_rate(persona) >= 1.0:
        return need >= days_left or rng.random() < need / days_left + 0.2
    return rng.random() < hit_rate(persona) * need / days_left + 0.1


async def player_actions(hour):
    ch = main.db.current_challenge(GUILD.id)
    today = now().date()
    # ---- sign-up phase ----
    if ch and ch.status == "signup":
        if today == date(2026, 11, 15) and hour == 12:
            for name, (persona, goals) in PERSONAS.items():
                uid = IDS[name]
                i = inter(uid)
                await main.handle_signup(i, ch.id, True)
                if goals and name not in ("Slaycie", "Tash"):
                    res = main.save_goals_from_form(ch, uid, goals)
                    if name == "Rachel":
                        assert "Nothing was saved" in res["content"], res
                        res = main.save_goals_from_form(ch, uid, ["7 hours of sleep, 5 days", goals[1], goals[2]])
                    if not res["content"].startswith("Goals saved"):
                        problem(f"{name}'s goals didn't save: {res['content']}")
        if today == date(2026, 11, 21) and hour == 21:  # Slaycie gets the warning, adds goals at 9 p.m.
            main.save_goals_from_form(ch, IDS["Slaycie"], PERSONAS["Slaycie"][1])
        if today == date(2026, 11, 21) and hour == 22:  # Sophia leaves the Discord server (still signed up)
            GUILD.members.pop(IDS["Sophia"], None)
            transcript.append(f"_[{stamp()}] Sophia leaves the Discord server but is still signed up._")
        if today == date(2026, 11, 21) and hour == 23:  # Tash squeaks in at 11 p.m.
            main.save_goals_from_form(ch, IDS["Tash"], PERSONAS["Tash"][1])
    if today == START and hour == 1:
        missing = [NAMES[p["user_id"]] for p in main.db.players(1)
                   if p["user_id"] in GUILD.members and p["team"]
                   and not any(GUILD.members[p["user_id"]] in r.members for r in GUILD.roles if r.name == p["team"])]
        if missing:
            problem(f"after the lock these players got no team role: {missing}")
    if today == START and hour == 2:  # Lucy tries to join after the deadline
        old = main.db.get_challenge(1)
        i = inter(LATE_JOINER)
        await main.handle_signup(i, old.id, True)
        transcript.append(f"_[{stamp()}] Late Lucy taps I'm in → {last_reply(i)!r}_")

    if not ch:
        return
    # Day one, 6 a.m.: Caroline (early bird) posts her run photo before the kickoff
    if today == START and hour == 6:
        post = PhotoPost(GUILD.proof, IDS["Caroline"])
        await main.on_message(post)
        i = inter(IDS["Caroline"])
        await main.checkin.callback(i, goal=1, day=None, photo=None)
        if last_reply(i).startswith("Logged"):
            TRUTH_CHECKINS[(IDS["Caroline"], 1)].add(today)
        transcript.append(f"_[{stamp()}] Caroline posts a 6 a.m. photo → replies={len(post.replies)} "
                          f"reactions={post.reactions}; /checkin → {last_reply(i)!r}_")
        if not post.replies and not post.reactions:
            problem("Day one before 7 a.m.: Caroline's proof photo got no reply at all, and /checkin says "
                    f"{last_reply(i)!r}. Early-morning workouts on day one can't be logged until the kickoff.")
    if ch.status != "active":
        return
    if not (START <= today <= END + timedelta(days=1)):
        return
    in_challenge = START <= today <= END
    week = scoring.week_for(min(today, END), scoring.challenge_weeks(ch.start, ch.end))

    for name, (persona, _) in PERSONAS.items():
        uid = IDS[name]
        if not main.db.is_player(ch.id, uid):
            continue
        slots = sorted(goal_slots(uid))
        if persona in ("owner-perfect", "early-bird", "partial", "dms-off", "removed", "quitter",
                       "mission-fan", "bad-goal-then-fix") and in_challenge:
            at = {"early-bird": 6, "owner-perfect": 12, "partial": 18, "dms-off": 19, "removed": 17,
                  "quitter": 9, "mission-fan": 13, "bad-goal-then-fix": 21}[persona]
            if hour == at:
                todo = [s for s in slots if wants(uid, s, today, persona)]
                if todo:
                    if persona in ("dms-off", "partial"):
                        for s in todo:
                            await log_via_command(uid, today, s)
                    else:
                        await log_via_photo(uid, today, todo)
        if persona == "night-owl" and hour == 1 and START < today <= END + timedelta(days=1):
            y = today - timedelta(days=1)
            todo = [s for s in slots if wants(uid, s, y, persona)]
            if todo:
                await log_via_photo(uid, y, todo)
        if persona == "friday-catchup" and in_challenge and hour == 22 and (today.weekday() == 4 or today == END):
            for d in [week.start + timedelta(days=k) for k in range(week.days)]:
                if d > today:
                    break
                for s in slots:
                    if wants(uid, s, d, persona):
                        await log_via_command(uid, d, s)
        if persona == "friday-after-midnight" and hour == 1 and today.weekday() == 5 and today > START:
            # Saturday 1 a.m.: logs the whole week she just finished (incl. Friday) after midnight
            fri = today - timedelta(days=1)
            wk = scoring.week_for(fri, scoring.challenge_weeks(ch.start, ch.end))
            if wk:
                for d in [wk.start + timedelta(days=k) for k in range(wk.days)]:
                    for s in slots:
                        if wants(uid, s, d, persona):
                            await log_via_command(uid, d, s)
        if persona == "friday-after-midnight" and hour == 1 and today == END + timedelta(days=1):
            wk = scoring.week_for(END, scoring.challenge_weeks(ch.start, ch.end))
            for d in [wk.start + timedelta(days=k) for k in range(wk.days)]:
                for s in slots:
                    if wants(uid, s, d, persona):
                        await log_via_command(uid, d, s)
        # mini missions
        if in_challenge or (today == END + timedelta(days=1) and hour < 7):
            mrate = {"mission-fan": 1.0, "owner-perfect": 0.6, "early-bird": 0.5, "night-owl": 0.5}.get(persona, 0.3)
            mhour = {"night-owl": 23, "mission-fan": 8}.get(persona, 15)
            if hour == mhour and rng.random() < mrate:
                await do_mission(uid)

    # reminder tapper: taps the first goal button in tonight's 8 p.m. DM
    kim = IDS["Kim"]
    if hour == 21 and in_challenge and main.db.is_player(ch.id, kim):
        todays = [x for x in DMS[kim] if x[0].date() == today and "Check-in time" in x[1]]
        if todays:
            view = todays[-1][2]
            for b in view.children:
                if b.custom_id.startswith("mb:yes:"):
                    _, _, gid, d = b.custom_id.split(":")
                    slot = next(s for s, g in goal_slots(kim).items() if g.id == int(gid))
                    if not wants(kim, slot, today, "reminder-tapper"):
                        continue
                    i = inter(kim)
                    await main.handle_reminder(i, int(gid), date.fromisoformat(d), True)
                    if last_reply(i).startswith("Logged"):
                        TRUTH_CHECKINS[(kim, slot)].add(today)
                    else:
                        REJECTED.append(("Kim", f"reminder {d}", last_reply(i)))

    # Becca quits Dec 5; the commissioner removes Tash Dec 10
    if today == date(2026, 12, 5) and hour == 10:
        i = inter(IDS["Becca"])
        await main.challenge_leave.callback(i)
        await main.handle_quit(inter(IDS["Becca"]), ch.id)
        transcript.append(f"_[{stamp()}] Becca leaves the challenge._")
    if today == date(2026, 12, 10) and hour == 10:
        main.is_commish = lambda inter: True
        i = inter(OWNER)
        await main.commish_remove_player.callback(i, GUILD.members[IDS["Tash"]], "REMOVE")
        transcript.append(f"_[{stamp()}] Commissioner removes Tash → {last_reply(i)!r}_")

    # edge: someone tries to log a future day, and a locked week
    if today == date(2026, 12, 2) and hour == 12:
        i = inter(IDS["Priya"])
        await main.checkin.callback(i, goal=1, day="2026-11-24", photo=None)  # last week, now locked
        transcript.append(f"_[{stamp()}] Priya tries to log last week (Nov 24) → {last_reply(i)!r}_")
        i = inter(IDS["Priya"])
        await main.checkin.callback(i, goal=1, day="2026-12-04", photo=None)
        transcript.append(f"_[{stamp()}] Priya tries to log Dec 4 early → {last_reply(i)!r}_")


# ---------- independent scoring ----------

def expected_totals(ch):
    weeks = scoring.challenge_weeks(ch.start, ch.end)
    out, perfect = {}, defaultdict(list)
    for p in main.db.players(ch.id):
        uid = p["user_id"]
        total = 0.0
        for w in weeks:
            wk_days = {w.start + timedelta(days=k) for k in range(w.days)}
            parts = []
            for slot, g in goal_slots(uid).items():
                t = scoring.scaled_target(g.target_days, w.days)
                done = len(TRUTH_CHECKINS[(uid, slot)] & wk_days)
                parts.append(min(done, t) / t)
            pts = sum(parts) + (config.PERFECT_WEEK_BONUS if all(x >= 1 for x in parts) else 0)
            perfect[uid].append(all(x >= 1 for x in parts))
            total += pts
        total += sum(TRUTH_MISSIONS[uid].values())
        out[uid] = total
    return out, perfect


# ---------- run ----------

async def run():
    setup_world(None)
    db = main.db
    game.now_local = now
    main.bot.get_user = lambda uid: FakeUser(uid)
    main.bot.get_guild = lambda gid: GUILD
    main.bot.fetch_user = lambda uid: asyncio.sleep(0, FakeUser(uid))
    discord.utils.get = lambda roles, name: next((r for r in roles if r.name == name), None)
    owner_dms = []

    async def notify(text):
        owner_dms.append((stamp(), text))
    main.notify_owner = notify

    db.set_settings(GUILD.id, GUILD.ann.id, GUILD.proof.id, None, mission=GUILD.missions.id)
    db.import_missions(GUILD.id, Path(main.BUILT_IN_MISSIONS).read_text())
    clock["now"] = datetime.combine(OPENED, time(9, 0), tzinfo=config.TZ)
    ch = db.create_challenge(GUILD.id, "Workout & Accountability Challenge", START, END, OWNER,
                             signup_deadline=DEADLINE)
    err = await main.post_signup(GUILD, ch, GUILD.ann)
    if err:
        problem(f"sign-up post failed: {err}")

    d = SIM_FROM
    while d <= SIM_TO:
        for hour in range(24):
            clock["now"] = datetime.combine(d, time(hour, 0), tzinfo=config.TZ)
            await player_actions(hour)
            for minute in (1, 31):  # the scheduler runs every minute; twice an hour also catches double-posts
                clock["now"] = datetime.combine(d, time(hour, minute), tzinfo=config.TZ)
                cur = db.current_challenge(GUILD.id)
                if cur:
                    await main.run_jobs(GUILD, cur, now())
        d += timedelta(days=1)

    ch = db.latest_challenge(GUILD.id)
    report(ch, owner_dms)


def report(ch, owner_dms):
    db = main.db
    if ch.status != "finished":
        problem(f"challenge ended in status {ch.status!r}, not finished")
    s = game.standings(db, ch)
    exp, perfect = expected_totals(ch)
    lines = ["| Player | Persona | Bot total | Expected | Perfect weeks |", "|---|---|---|---|---|"]
    for uid, total in s.ranked_players():
        name = NAMES[uid]
        ok = abs(total - exp[uid]) < 1e-6
        if not ok:
            problem(f"score mismatch for {name}: bot {total:.4f} vs expected {exp[uid]:.4f}")
        lines.append(f"| {name} | {PERSONAS[name][0]} | {scoring.fmt(total)} | {scoring.fmt(exp[uid])}"
                     f"{'' if ok else ' ❌'} | {sum(perfect[uid])}/{len(s.weeks)} |")
    if ch.mode == "teams":
        for team, total in s.ranked_teams():
            lines.append(f"\n**{team}**: {scoring.fmt(total)} (avg)")

    # posting cadence
    ann = GUILD.ann.sent
    by_day = defaultdict(list)
    for t, c, kw in ann:
        title = ((kw.get("embeds") or [kw.get("embed")] or [None])[0])
        label = (title.title if title is not None and getattr(title, "title", None) else (c or "")[:40])
        by_day[t.date()].append(f"{t.strftime('%H:%M')} {label}")
    missions = [(t.date(), kw["embed"].title) for t, c, kw in GUILD.missions.sent]
    mission_days = [d for d, _ in missions]
    want = [START + timedelta(days=k) for k in range((END - START).days + 1)]
    if mission_days != want:
        problem(f"mini missions posted on {len(mission_days)} days; expected one each day {START}..{END}")
    roundups = [t.date() for t, c, kw in ann if kw.get("embeds") and kw["embeds"][0].title.startswith(("☀️", "🏆 Week"))]
    want_r = [START + timedelta(days=k) for k in range(1, (END - START).days + 2)]
    if roundups != want_r:
        problem(f"roundups on {roundups[:3]}...({len(roundups)}); expected daily {want_r[0]}..{want_r[-1]}")

    # reminders: anyone DM'd at 8 p.m. after already checking in that day?
    for uid, msgs in DMS.items():
        for t, c, v, _ in msgs:
            if "Check-in time" in c:
                for slot, days in ((s_, TRUTH_CHECKINS[(uid, s_)]) for s_ in (1, 2, 3)):
                    pass
    reminder_counts = {NAMES[u]: sum("Check-in time" in c for _, c, _, _ in m) for u, m in DMS.items()}

    out = ["# Season simulation transcript", "",
           f"Challenge **{ch.name}**: {START} → {END}, sign-up deadline {DEADLINE}, TIMEZONE={config.TZ}", "",
           "## Final scores (bot vs independent tally)", *lines, "",
           "## Problems found", *(f"- {p}" for p in problems or ["None"]), "",
           "## Check-ins the bot turned down", *(f"- {w}: {what} → {why}" for w, what, why in REJECTED[:80]), "",
           f"## 8 p.m. reminder DMs per player\n{reminder_counts}", "",
           "## Owner DMs", *(f"- {t}: {x}" for t, x in owner_dms), "",
           "## Announce channel, day by day",
           *(f"- **{d}**: " + " · ".join(v) for d, v in sorted(by_day.items())), "",
           "## Sample DMs",
           *(f"- **{NAMES.get(u, u)}** [{t:%a %b %d %H:%M}] {c[:300]!r}" for u in (IDS['Kim'], IDS['Nicole'], IDS['Slaycie'])
             for t, c, _, _ in DMS[u][:4]), "",
           "## Full transcript", *transcript]
    Path(ROOT / "sim" / "transcript.md").write_text("\n".join(out))
    print("\n".join(out[: out.index("## Announce channel, day by day")]))
    print(f"\n{len(GUILD.ann.sent)} announce posts, {len(GUILD.missions.sent)} missions, "
          f"{sum(len(v) for v in DMS.values())} DMs, {len(problems)} problems")


async def stress():
    """Render every big post with lots of players and long goal names, and check Discord's limits."""
    db = main.db
    for n in (25, 40, 60):
        gid = 800 + n
        db.set_settings(gid, GUILD.ann.id, GUILD.proof.id, None, mission=GUILD.missions.id)
        g = FakeGuild()
        g.id = gid
        g.get_member = lambda uid, g=g: g.members.setdefault(uid, FakeMember(uid, g, f"Player{uid % 1000}"))
        ch = db.create_challenge(gid, "Workout & Accountability Challenge", START, END, OWNER, signup_deadline=DEADLINE)
        ids = [snow(1000 + n * 100 + k) for k in range(n)]
        for k, uid in enumerate(ids):
            db.join(ch.id, uid)
            if k % 7:  # every 7th has no goals
                db.set_goals(ch.id, uid, [("Strength training at the gym or at home", 4),
                                          ("Drink 80 ounces of water before dinner", 7),
                                          ("No phone in bed after 9 p.m. lights out", 6)], False)
        clock["now"] = datetime.combine(DEADLINE, time(19), tzinfo=config.TZ)
        where = f"[{n} players]"
        check_message(f"{where} sign-up post", embed=main.signup_embed(g, ch))
        check_message(f"{where} sign-up reminder", main.signup_reminder_text(g, ch, DEADLINE))
        for page in main.message_pages(main.overview_embeds(g, ch)):
            check_message(f"{where} overview page", embeds=page)
        clock["now"] = datetime.combine(START, time(0, 1), tzinfo=config.TZ)
        lock = await main.lock_signups(g, ch)
        check_message(f"{where} sign-ups closed post", embed=lock)
        ch = db.get_challenge(ch.id)
        db.update_challenge(ch.id, status="active")
        ch = db.get_challenge(ch.id)
        for page in main.message_pages(main.kickoff_embeds(g, ch)):
            check_message(f"{where} kickoff page", embeds=page)
        for uid in db.players(ch.id):
            for goal in db.goals(ch.id, uid["user_id"]):
                for k in range(5):
                    db.add_checkin(goal.id, uid["user_id"], START + timedelta(days=k), None)
        check_message(f"{where} roundup", embeds=main.roundup_embeds(ch, START + timedelta(days=4)))
        check_message(f"{where} standings", embed=main.standings_embed(ch, None, "final standings"))
        i = inter(ids[1])
        await main.handle_who(i, ch.id)
        e, _ = main.all_goals_embeds(ch)
        for page in main.message_pages(e):
            check_message(f"{where} /goals all page", embeds=page)


if __name__ == "__main__":
    asyncio.run(run())
    n_before = len(problems)
    asyncio.run(stress())
    print("\nStress test (25/40/60 players):")
    print("\n".join(problems[n_before:]) or "no limit problems")

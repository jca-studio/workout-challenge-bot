"""Runs the scheduled jobs for a whole month against fake Discord objects."""
import asyncio
import os
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import discord

os.environ["DATABASE_PATH"] = "/tmp/momsbot-test/flow.db"
if os.path.exists(os.environ["DATABASE_PATH"]):
    os.remove(os.environ["DATABASE_PATH"])

from bot import config, game  # noqa: E402
from bot import main  # noqa: E402

CSV = (Path(__file__).parent.parent / "data" / "missions.csv").read_text()


class FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.sent = []
        self.mention = f"<#{cid}>"

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))
        return SimpleNamespace(id=len(self.sent), jump_url="https://x")


class FakeRole:
    def __init__(self, name):
        self.name, self.members, self.id = name, [], hash(name)


class FakeMember:
    def __init__(self, uid, guild):
        self.id, self.guild = uid, guild
        self.nick, self.global_name, self.name = None, f"Mom{uid}", f"mom{uid}"

    async def edit(self, nick=None, reason=None):
        if self.id == self.guild.owner_id:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="owner"), "owner")
        self.nick = nick

    async def add_roles(self, role, reason=None):
        role.members.append(self)

    async def remove_roles(self, role, reason=None):
        role.members.remove(self)


class FakeGuild:
    def __init__(self):
        self.id = 1
        self.name = "Moms"
        self.ann, self.proof, self.missions = FakeChannel(10), FakeChannel(11), FakeChannel(12)
        self.roles = []
        self.members = {}
        self.owner_id = 777

    def get_channel(self, cid):
        return {10: self.ann, 11: self.proof, 12: self.missions}.get(cid)

    async def create_role(self, name, **kw):
        r = FakeRole(name)
        self.roles.append(r)
        return r

    def get_member(self, uid):
        return self.members.setdefault(uid, FakeMember(uid, self))

    async def fetch_member(self, uid):
        return self.get_member(uid)


def at(d, h):
    return datetime.combine(d, time(h, 5), tzinfo=config.TZ)


def test_month_of_jobs(monkeypatch):
    db = main.db
    guild = FakeGuild()
    db.set_settings(1, 10, 11, None, mission=12)
    db.import_missions(1, CSV)
    dms = []

    class FakeUser:
        def __init__(self, uid):
            self.id = uid

        async def send(self, content=None, **kw):
            if self.id == 9:  # this player has DMs off
                raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "closed")
            dms.append((self.id, content))

    monkeypatch.setattr(main.bot, "get_user", lambda uid: FakeUser(uid))
    monkeypatch.setattr(discord.utils, "get", lambda roles, name: next((r for r in roles if r.name == name), None))

    ch = db.create_challenge(1, "December Challenge", date(2026, 12, 1), date(2026, 12, 31), 99)
    for uid in range(1, 10):
        db.join(ch.id, uid)
        db.set_goals(ch.id, uid, [("Work out", 3), ("Brush teeth", 7), ("Veggies", 7)], False)
    db.approve_goals(ch.id)
    db.update_challenge(ch.id, mode="teams", status="ready")
    teams = game.assign_teams(list(range(1, 10)), random.Random(4))
    for uid, t in teams.items():
        db.set_team(ch.id, uid, t)

    rng = random.Random(5)
    loop = asyncio.new_event_loop()
    d = date(2026, 11, 30)
    while d <= date(2027, 1, 1):
        for hour in (6, 7, 9, 12, 20, 22):
            ch = db.get_challenge(ch.id)
            if hour == 12 and ch.status == "active" and ch.start <= d <= ch.end:
                for uid in range(1, 9):  # player 9 never checks in -> gets reminders
                    for g in game.open_goals_today(db, ch, uid, d):
                        if uid <= 2 or rng.random() > 0.25:
                            db.add_checkin(g.id, uid, d, None)
            loop.run_until_complete(main.run_jobs(guild, ch, at(d, hour)))
        d += timedelta(days=1)

    ch = db.get_challenge(ch.id)
    texts = [c or "" for c, _ in guild.ann.sent]
    embeds = [kw.get("embed") for _, kw in guild.ann.sent if kw.get("embed")]
    roundups = [kw["embeds"] for _, kw in guild.ann.sent
                if kw.get("embeds") and not kw["embeds"][0].title.endswith("what to expect")]
    overviews = [kw["embeds"] for _, kw in guild.ann.sent
                 if kw.get("embeds") and kw["embeds"][0].title.endswith("what to expect")]
    assert len(overviews) == 1  # posted on Nov 30, the day before the start
    missions = [kw["embed"] for _, kw in guild.missions.sent]
    assert ch.status == "finished"
    assert any("starts now" in t for t in texts)
    # 31 missions, all in the mini mission channel, none in the announce channel
    assert len(missions) == 31 and all(e.title.startswith("⚡") for e in missions)
    assert not any(e.title.startswith("⚡") for e in embeds)
    # a roundup every morning from Dec 2 to Dec 31 (Jan 1 gets the final crowning instead)
    assert len(roundups) == 30
    finals = [r[0].title for r in roundups if "final" in r[0].title]
    assert [t.split(" (")[0] for t in finals] == [f"🏆 Week {n} final" for n in (1, 2, 3, 4)]
    first = roundups[0]
    assert first[0].title == "☀️ Daily roundup: Tue Dec 1" and first[1].title == "Week 1 so far"
    # Dec 1 is a 4-day week: "every day" shrinks to 4 and "3 days" to 2
    assert "**<@1>** · " in first[1].description and "Work out ✅⬜ · Brush teeth ✅⬜⬜⬜" in first[1].description
    assert "**<@9>** · 0 pts\nWork out ⬜⬜ · Brush teeth ⬜⬜⬜⬜ · Veggies ⬜⬜⬜⬜" in first[1].description
    week_end = next(r for r in roundups if r[0].title.startswith("🏆 Week 2 final"))
    assert ("**<@1>** · 4 pts ✨\nWork out ✅✅✅ · Brush teeth ✅✅✅✅✅✅✅ · Veggies ✅✅✅✅✅✅✅"
            in week_end[1].description)  # perfect: 3 goals + bonus
    print("\n" + week_end[0].title + "\n" + week_end[0].fields[-1].value + "\n\n" + week_end[1].description)
    assert any("Cue the fanfare" in t for t in texts)
    assert any("<@9>" in t and "check in" in t for t in texts)  # DMs off -> channel ping
    assert dms and all(uid != 9 for uid, _ in dms)
    winner_role = next(r for r in guild.roles if r.name == config.WINNER_ROLE_NAME)
    top_role = next(r for r in guild.roles if r.name == config.TOP_SCORER_ROLE_NAME)
    assert {m.id for m in top_role.members} == {1, 2}  # tie shares the crown
    assert {m.id for m in winner_role.members} <= set(range(1, 10)) and winner_role.members
    # Jobs don't double-post when run again
    n = len(guild.ann.sent)
    loop.run_until_complete(main.run_jobs(guild, ch, at(date(2027, 1, 1), 22)))
    assert len(guild.ann.sent) == n
    # print the final standings text for a visual check
    final = embeds[-1]
    print("\n" + final.title)
    for f in final.fields:
        print(f.name, "\n", f.value)


def test_checkin_messages_and_photo_rule(monkeypatch):
    db = main.db
    guild = FakeGuild()
    db.set_settings(1, 10, 11, None)
    ch = db.create_challenge(1, "Jan", date(2027, 1, 2), date(2027, 1, 8), 99)  # one full Sat-Fri week
    db.join(ch.id, 50)
    db.set_goals(ch.id, 50, [("Work out", 2), ("Read", 7), ("Walk", 7)], False)
    db.approve_goals(ch.id)
    db.set_photo_required(ch.id, 50, 1, True)
    db.update_challenge(ch.id, status="active", mode="individual")
    ch = db.get_challenge(ch.id)
    user = SimpleNamespace(id=50)
    g1, g2, _ = db.goals(ch.id, 50)
    monkeypatch.setattr(game, "now_local", lambda: at(date(2027, 1, 5), 21))
    run = asyncio.new_event_loop().run_until_complete

    msg = run(main.log_checkin(ch, user, g1, date(2027, 1, 5), None, guild))
    assert "needs a photo" in msg
    msg = run(main.log_checkin(ch, user, g1, date(2027, 1, 4), "https://proof", guild))
    assert msg.startswith("Logged") and "✅⬜" in msg
    msg = run(main.log_checkin(ch, user, g1, date(2027, 1, 5), "https://proof", guild))
    assert "done for the week" in msg and "Perfect" not in msg
    assert "already logged" in run(main.log_checkin(ch, user, g1, date(2027, 1, 5), "https://p", guild))
    assert "hasn't happened" in run(main.log_checkin(ch, user, g2, date(2027, 1, 6), None, guild))


def test_auto_lock_on_start_day(monkeypatch):
    db = main.db
    guild = FakeGuild()
    db.set_settings(1, 10, 11, None)
    monkeypatch.setattr(discord.utils, "get", lambda roles, name: next((r for r in roles if r.name == name), None))
    ch = db.create_challenge(1, "Feb", date(2027, 2, 1), date(2027, 2, 28), 99)
    for uid in range(100, 109):
        db.join(ch.id, uid)
    run = asyncio.new_event_loop().run_until_complete
    run(main.run_jobs(guild, ch, at(date(2027, 1, 31), 9)))   # day before: nothing happens
    assert db.get_challenge(ch.id).status == "signup"
    run(main.run_jobs(guild, db.get_challenge(ch.id), at(date(2027, 2, 1), 7)))
    ch = db.get_challenge(ch.id)
    assert ch.status == "active" and ch.mode == "teams"
    assert len(set(db.teams(ch.id).values())) == 2
    titles = [kw["embed"].title for _, kw in guild.ann.sent if kw.get("embed")]
    assert titles[0].startswith("Sign-ups closed: 9 players, teams mode")
    desc = guild.ann.sent[0][1]["embed"].description
    assert "Still need goals" in desc


def test_crowns_move_between_months():
    db = main.db
    guild = FakeGuild()
    run = asyncio.new_event_loop().run_until_complete
    guild.get_member(1).nick = "Jenna"
    run(main.move_crowns(guild, winners=[1, 2, 777], top=[2], ann=guild.ann))
    assert guild.get_member(1).nick == "👑 Jenna"
    assert guild.get_member(2).nick == "👑💎 Mom2"
    assert "server owner" in guild.ann.sent[-1][0]
    # Next month: 2 wins again, 1 doesn't. 1 gets her name back; 2 keeps her real name under the new crown.
    run(main.move_crowns(guild, winners=[2, 3], top=[3], ann=guild.ann))
    assert guild.get_member(1).nick == "Jenna"
    assert guild.get_member(2).nick == "👑 Mom2"
    assert guild.get_member(3).nick == "👑💎 Mom3"
    assert {r["user_id"] for r in db.crowns(guild.id)} == {2, 3}


def test_signup_post_has_rules_players_and_deadline():
    db = main.db
    guild = FakeGuild()
    db.set_settings(1, 10, 11, None)
    ch = db.create_challenge(1, "March Challenge", date(2027, 3, 1), date(2027, 3, 31), 99,
                             signup_deadline=date(2027, 2, 26))
    for uid in (201, 202, 203):
        db.join(ch.id, uid)
    e = main.signup_embed(guild, ch)
    names = [f.name for f in e.fields]
    assert names[:7] == ["🎯 Goals", "💪🏾 Teams", "📸 Checking in", "💋 Points", "⚡ Mini missions",
                         "🍑 Weekly schedule", "🏆 Prizes"]
    assert names[-1] == "👯‍♀️ Signed up (3)" and "<@202>" in e.fields[-1].value
    assert "Sign up by Fri Feb 26 at 11:59 p.m. ET" in e.description
    assert "8 p.m. ET" in e.fields[2].value and "<#11>" in e.fields[2].value
    assert len(e) < 6000 and all(len(f.value) <= 1024 for f in e.fields)
    closed = main.signup_embed(guild, ch, closed=True)
    assert "Sign-ups are closed" in closed.description
    print("\n" + e.description)
    for f in e.fields:
        print(f"\n{f.name}\n{f.value}")


def test_deadline_closes_signups_and_makes_teams(monkeypatch):
    db = main.db
    guild = FakeGuild()
    db.set_settings(1, 10, 11, None)
    monkeypatch.setattr(discord.utils, "get", lambda roles, name: next((r for r in roles if r.name == name), None))
    ch = db.create_challenge(1, "April", date(2027, 4, 1), date(2027, 4, 30), 99, signup_deadline=date(2027, 3, 28))
    for uid in range(300, 310):
        db.join(ch.id, uid)
    run = asyncio.new_event_loop().run_until_complete
    assert game.signups_open(ch, at(date(2027, 3, 28), 23))           # 11 p.m. on deadline day: still open
    run(main.run_jobs(guild, ch, at(date(2027, 3, 28), 23)))
    assert db.get_challenge(ch.id).status == "signup"
    late = at(date(2027, 3, 29), 0)
    assert not game.signups_open(ch, late)                              # midnight: closed
    run(main.run_jobs(guild, ch, late))
    ch = db.get_challenge(ch.id)
    assert ch.status == "ready" and ch.mode == "teams"                  # locked 3 days before the start
    run(main.run_jobs(guild, ch, at(date(2027, 3, 30), 12)))
    assert db.get_challenge(ch.id).status == "ready"                    # doesn't start early
    run(main.run_jobs(guild, ch, at(date(2027, 4, 1), 7)))
    assert db.get_challenge(ch.id).status == "active"


def test_deadline_with_too_few_players_stays_open():
    db = main.db
    guild = FakeGuild()
    db.set_settings(1, 10, 11, None)
    ch = db.create_challenge(1, "May", date(2027, 5, 1), date(2027, 5, 31), 99, signup_deadline=date(2027, 4, 28))
    db.join(ch.id, 400)
    run = asyncio.new_event_loop().run_until_complete
    run(main.run_jobs(guild, ch, at(date(2027, 4, 29), 9)))
    ch = db.get_challenge(ch.id)
    assert ch.status == "signup" and ch.signup_deadline is None
    assert game.signups_open(ch, at(date(2027, 4, 29), 10))
    assert "staying open" in guild.ann.sent[-1][0]


def test_old_database_gets_deadline_column(tmp_path):
    import sqlite3
    from bot.db import DB
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE challenges (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, "
                "name TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, status TEXT NOT NULL, "
                "mode TEXT, signup_channel_id INTEGER, signup_message_id INTEGER, created_by INTEGER, "
                "created_at TEXT NOT NULL)")
    con.execute("INSERT INTO challenges (guild_id,name,start_date,end_date,status,created_at) "
                "VALUES (1,'Old','2026-12-01','2026-12-31','active','x')")
    con.commit()
    con.close()
    d = DB(path)
    assert d.current_challenge(1).signup_deadline is None
    c = d.create_challenge(2, "New", date(2027, 1, 1), date(2027, 1, 31), 1, signup_deadline=date(2026, 12, 30))
    assert c.signup_deadline == date(2026, 12, 30)


def test_failed_signup_post_reports_permissions():
    guild = FakeGuild()

    async def refuse(*a, **kw):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Missing Permissions"), "Missing Permissions")
    guild.ann.send = refuse
    ch = main.db.create_challenge(1, "June", date(2027, 6, 1), date(2027, 6, 30), 99)
    err = asyncio.new_event_loop().run_until_complete(main.post_signup(guild, ch, guild.ann))
    assert "Embed Links" in err


class FakeResponse:
    def __init__(self):
        self.sent = []

    def is_done(self):
        return bool(self.sent)

    async def send_message(self, content=None, **kw):
        self.sent.append((content, kw))

    async def edit_message(self, **kw):
        self.sent.append(("edit", kw))

    async def send_modal(self, modal):
        self.sent.append(("modal", {"modal": modal}))


def fake_inter(guild, uid):
    return SimpleNamespace(user=SimpleNamespace(id=uid), guild=guild, guild_id=guild.id, response=FakeResponse())


def test_im_in_reply_and_who_is_playing(monkeypatch):
    db = main.db
    guild = FakeGuild()
    db.set_settings(1, 10, 11, None)
    ch = db.create_challenge(1, "July Challenge", date(2027, 7, 1), date(2027, 7, 31), 99,
                             signup_deadline=date(2027, 6, 30))
    monkeypatch.setattr(game, "now_local", lambda: at(date(2027, 6, 20), 12))
    run = asyncio.new_event_loop().run_until_complete

    a = fake_inter(guild, 501)
    run(main.handle_signup(a, ch.id, True))
    msg, kw = a.response.sent[0]
    buttons = kw["view"].children
    assert msg.startswith("You're in!") and kw["ephemeral"]
    assert buttons[0].disabled and buttons[0].label == "You're in"
    assert [b.label for b in buttons] == ["You're in", "Add my goals", "See who else is playing"]
    assert not buttons[2].disabled

    again = fake_inter(guild, 501)
    run(main.handle_signup(again, ch.id, True))
    assert again.response.sent[0][0].startswith("You're already in")
    assert again.response.sent[0][1]["view"].children[0].disabled

    run(main.handle_signup(fake_inter(guild, 502), ch.id, True))
    who = fake_inter(guild, 502)
    run(main.handle_who(who, ch.id))
    text = who.response.sent[0][0]
    assert "2 playing" in text and "<@501>" in text and "<@502> (you)" in text

    # after the deadline, I'm in is refused but the lineup still works
    monkeypatch.setattr(game, "now_local", lambda: at(date(2027, 7, 1), 1))
    late = fake_inter(guild, 503)
    run(main.handle_signup(late, ch.id, True))
    assert "closed" in late.response.sent[0][0]
    db.update_challenge(ch.id, mode="teams", status="ready")
    db.set_team(ch.id, 501, config.TEAM_NAMES[0])
    db.set_team(ch.id, 502, config.TEAM_NAMES[1])
    who2 = fake_inter(guild, 503)
    run(main.handle_who(who2, db.get_challenge(ch.id).id))
    assert f"**{config.TEAM_NAMES[0]}** (1)" in who2.response.sent[0][0]


def test_goals_all_and_approve_all(monkeypatch):
    db = main.db
    guild = FakeGuild()
    monkeypatch.setattr(main, "is_commish", lambda inter: True)
    ch = db.create_challenge(1, "August Challenge", date(2027, 8, 1), date(2027, 8, 31), 99)
    for uid in (601, 602, 603):
        db.join(ch.id, uid)
    db.set_goals(ch.id, 601, [("Work out", 3), ("Brush teeth", 7), ("Alcohol-free", 6)], False)
    db.set_goals(ch.id, 602, [("Walk", 5), ("Read", 1), ("Veggies", 7)], False)
    db.approve_goals(ch.id, 602)
    run = asyncio.new_event_loop().run_until_complete

    inter = fake_inter(guild, 99)
    run(main.goals_all.callback(inter))
    _, kw = inter.response.sent[0]
    text = kw["embeds"][0].description
    assert "⏳ <@601>" in text and "✅ <@602>" in text and "Work out: **3** days/week" in text
    assert "Read: **1** day/week" in text and "**No goals yet:** <@603>" in text
    button = kw["view"].children[0]
    assert button.label == "Approve all (1)" and not button.disabled

    click = fake_inter(guild, 99)
    run(main.handle_approve_all(click, ch.id))
    _, kw = click.response.sent[0]
    assert "⏳" not in kw["embeds"][0].description.split("No goals yet")[0]
    assert kw["view"].children[0].disabled and kw["view"].children[0].label == "All approved"
    assert all(g.approved for g in db.goals(ch.id, 601))


def test_goals_all_splits_long_lists():
    db = main.db
    ch = db.create_challenge(1, "Big", date(2027, 9, 1), date(2027, 9, 30), 99)
    for uid in range(700, 760):
        db.join(ch.id, uid)
        db.set_goals(ch.id, uid, [("A really quite long goal name here", 3)] * 3, False)
    embeds, waiting = main.all_goals_embeds(ch)
    assert waiting == 60 and len(embeds) > 1
    pages = main.message_pages(embeds)
    assert len(pages) > 1 and sum(len(p) for p in pages) == len(embeds)
    assert all(sum(len(e) for e in p) <= 6000 and len(p) <= 10 for p in pages)
    assert all(len(e.description) <= 4096 for e in embeds)


def test_goals_add_one_at_a_time_and_remove():
    db = main.db
    guild = FakeGuild()
    ch = db.create_challenge(1, "Oct", date(2027, 10, 1), date(2027, 10, 31), 99)
    db.join(ch.id, 801)
    run = asyncio.new_event_loop().run_until_complete

    def add(goal, days):
        i = fake_inter(guild, 801)
        run(main.goals_add.callback(i, goal, days))
        return i.response.sent[0][0]

    assert "2 more to go" in add("Yoga", 3) and "1 more to go" in add("Affirmations", 7)
    last = add("7 hours of sleep", 7)
    assert "That's all 3" in last and "I will **Yoga** on **3** days per week" in last
    assert "already have 3" in add("Walk", 5)

    rm = fake_inter(guild, 801)
    run(main.goals_remove.callback(rm, 1))
    assert [g.name for g in db.goals(ch.id, 801)] == ["Affirmations", "7 hours of sleep"]
    assert [g.slot for g in db.goals(ch.id, 801)] == [1, 2]
    assert "Added goal 3" in add("Walk", 5)

    # once the challenge is running and her goals are approved, they're locked
    db.approve_goals(ch.id)
    db.update_challenge(ch.id, status="active")
    rm2 = fake_inter(guild, 801)
    run(main.goals_remove.callback(rm2, 1))
    assert "locked in" in rm2.response.sent[0][0]

    outsider = fake_inter(guild, 899)
    run(main.goals_add.callback(outsider, "Run", 3))
    assert "Join the challenge first" in outsider.response.sent[0][0]


def test_goals_form_all_three_at_once():
    db = main.db
    guild = FakeGuild()
    ch = db.create_challenge(1, "Nov", date(2027, 11, 1), date(2027, 11, 30), 99)
    run = asyncio.new_event_loop().run_until_complete
    db.join(ch.id, 901)

    # Tapping Add my goals opens one form with 3 lines
    click = fake_inter(guild, 901)
    run(main.handle_goals_button(click, ch.id))
    kind, kw = click.response.sent[0]
    form = kw["modal"]
    assert kind == "modal" and form.title == "Your 3 goals"
    assert len(form.to_dict()["components"]) == 3  # what Discord receives; fails if malformed

    # One line is unclear: nothing is saved, and Fix my goals reopens with what she typed
    bad = main.save_goals_from_form(ch, 901, ["Yoga, 3 days", "Sleep 7 hours", "Affirmations daily"])
    assert "Nothing was saved" in bad["content"] and "Goal 2" in bad["content"] and "Goal 1" not in bad["content"]
    assert bad["view"].children[0].label == "Fix my goals"
    assert db.goals(ch.id, 901) == []
    reopen = fake_inter(guild, 901)
    run(main.handle_goals_button(reopen, ch.id))
    assert [b.default for b in reopen.response.sent[0][1]["modal"].lines] == ["Yoga, 3 days", "Sleep 7 hours",
                                                                             "Affirmations daily"]

    # Fixed: all 3 saved together
    ok = main.save_goals_from_form(ch, 901, ["Yoga, 3 days", "7 hours of sleep, 5 days", "Affirmations daily"])
    assert ok["content"].startswith("Goals saved!") and "That's all 3" in ok["content"]
    assert [(g.name, g.target_days) for g in db.goals(ch.id, 901)] == [
        ("Yoga", 3), ("7 hours of sleep", 5), ("Affirmations", 7)]

    # Editing later shows her saved goals, and saving replaces them
    edit = fake_inter(guild, 901)
    run(main.handle_goals_button(edit, ch.id))
    assert [b.default for b in edit.response.sent[0][1]["modal"].lines] == [
        "Yoga, 3 days", "7 hours of sleep, 5 days", "Affirmations, every day"]
    main.save_goals_from_form(ch, 901, ["Pilates, 2 days", "7 hours of sleep, 5 days", "Affirmations daily"])
    assert db.goals(ch.id, 901)[0].name == "Pilates" and len(db.goals(ch.id, 901)) == 3

    # Locked once the challenge is running and goals are approved
    db.approve_goals(ch.id)
    db.update_challenge(ch.id, status="active")
    locked = main.save_goals_from_form(db.get_challenge(ch.id), 901, ["A, 1", "B, 2", "C, 3"])
    assert "locked in" in locked["content"]


def test_restart_reloads_old_mission_bank(monkeypatch):
    db = main.db
    guild = FakeGuild()
    guild.id = 55
    db.set_settings(55, 10, 11, None)
    old_csv = "Day,Mission,Points\nWork It Wednesday,5-minute plank,2\nFeel Good Friday,Everything shower,1\n"
    db.import_missions(55, old_csv)
    assert main.mission_bank_is_outdated(55)
    monkeypatch.setattr(main.bot, "get_guild", lambda gid: guild if gid == 55 else None)
    asyncio.new_event_loop().run_until_complete(main.bring_servers_up_to_date())
    assert not main.mission_bank_is_outdated(55)
    assert len(db.missions(55)) == 59 and {m["points"] for m in db.missions(55)} == {0.25, 0.5}


def test_rules_text_has_no_blank_underscores():
    guild = FakeGuild()
    ch = main.db.create_challenge(1, "Dec", date(2027, 12, 1), date(2027, 12, 31), 99)
    e = main.signup_embed(guild, ch)
    assert "__" not in e.fields[0].value and "[goal]" in e.fields[0].value


def test_daily_signup_reminder_until_signups_close(monkeypatch):
    db = main.db
    guild = FakeGuild()
    guild.id = 77
    db.set_settings(77, 10, 11, None)
    monkeypatch.setattr(discord.utils, "get", lambda roles, name: next((r for r in roles if r.name == name), None))
    ch = db.create_challenge(77, "January Challenge", date(2028, 1, 1), date(2028, 1, 31), 99,
                             signup_deadline=date(2027, 12, 31))
    db.update_challenge(ch.id, signup_channel_id=10, signup_message_id=555)
    for uid in (1001, 1002):
        db.join(ch.id, uid)
    db.set_goals(ch.id, 1001, [("Yoga", 3), ("Read", 5), ("Walk", 7)], False)
    run = asyncio.new_event_loop().run_until_complete
    d = date(2027, 12, 26)
    while d <= date(2028, 1, 1):
        for hour in (9, 10, 15):
            run(main.run_jobs(guild, db.get_challenge(ch.id), at(d, hour)))
        d += timedelta(days=1)
    reminders = [(c, kw) for c, kw in guild.ann.sent if c and "sign up" in c.lower() and "⏰" in c]
    assert len(reminders) == 6  # Dec 26-31, once a day, none after sign-ups close
    first, last = reminders[0][0], reminders[-1][0]
    assert first.startswith("⏰ **5 days left to sign up for January Challenge!**")
    assert "https://discord.com/channels/77/10/555" in first and "2 signed up so far" in first
    assert "Still need to set goals" in first and "<@1002>" in first.split("Still need")[1]
    assert "<@1001>" not in first.split("Still need")[1]
    assert last.startswith("⏰ **Last day to sign up for January Challenge!**")
    assert all(kw["allowed_mentions"].users is False for _, kw in reminders)  # names show, nobody gets pinged
    assert db.get_challenge(ch.id).status == "active"


def test_owner_alerts_and_botstats(monkeypatch):
    db = main.db
    sent = []

    class Owner:
        async def send(self, text):
            sent.append(text)
    monkeypatch.setattr(main.config, "OWNER_ID", 4242)
    monkeypatch.setattr(main.bot, "get_user", lambda uid: Owner() if uid == 4242 else None)
    run = asyncio.new_event_loop().run_until_complete

    a, b, c = (SimpleNamespace(id=9001, name="Run Club", member_count=42),
               SimpleNamespace(id=9002, name="Book Moms", member_count=12),
               SimpleNamespace(id=9003, name="New Server", member_count=5))
    monkeypatch.setattr(type(main.bot), "guilds", property(lambda self: [a, b, c]))
    run(main.on_guild_join(c))
    assert sent[-1] == "👋 Added to **New Server** (5 members). Now in 3 servers."

    # Run Club: active challenge, 3 players, 2 checked in this week. Book Moms: set up, nothing running.
    db.set_settings(9001, 1, 2, None)
    db.set_settings(9002, 1, 2, None)
    ch = db.create_challenge(9001, "Run Club March", date(2028, 3, 1), date(2028, 3, 31), 1)
    db.update_challenge(ch.id, status="active", mode="individual")
    for uid in (1, 2, 3):
        db.join(ch.id, uid)
        db.set_goals(ch.id, uid, [("Run", 3), ("Read", 5), ("Sleep", 7)], False)
    db.approve_goals(ch.id)
    for uid in (1, 2):
        db.add_checkin(db.goals(ch.id, uid)[0].id, uid, date(2028, 3, 9), None)
    text = main.botstats_text([a, b, c], date(2028, 3, 10))
    assert text.startswith("**3 servers** · 2 set up · 1 with a challenge · 3 players · 2 checked in this past week")
    assert "• **Run Club** (42 members): Run Club March: active, 3 players · 2/3 checked in this past week" in text
    assert "• **Book Moms** (12 members): set up, no challenge running · 0 finished" in text
    assert "• **New Server** (5 members): not set up yet" in text

    # Only the owner can run /botstats
    stranger = fake_inter(FakeGuild(), 77)
    run(main.botstats.callback(stranger))
    assert "Only the bot's owner" in stranger.response.sent[0][0]


def test_challenge_overview_channel_and_dm(monkeypatch):
    db = main.db
    guild = FakeGuild()
    guild.id = 88
    db.set_settings(88, 10, 11, None, mission=12)
    monkeypatch.setattr(main, "is_commish", lambda inter: True)
    ch = db.create_challenge(88, "December Challenge", date(2026, 12, 1), date(2026, 12, 31), 99)
    db.join(ch.id, 1201)
    db.join(ch.id, 1202)
    db.join(ch.id, 1203)
    db.set_goals(ch.id, 1201, [("Yoga", 3), ("Affirmations", 7), ("Read", 5)], False)
    db.set_goals(ch.id, 1202, [("Run", 2), ("Water", 7), ("Stretch", 4)], False)

    embeds = main.overview_embeds(guild, ch)
    titles = [e.title for e in embeds]
    assert titles == ["📣 December Challenge: what to expect", "💋 How points work",
                      "⏰ Reminders and the daily schedule", "⌨️ Commands to know", "👯‍♀️ Who's playing (3)"]
    who = embeds[-1].description
    assert "**<@1201>**: Yoga, 3 days · Affirmations, every day · Read, 5 days" in who
    assert "**Still need goals:** <@1203>" in who
    assert "<#12>" in embeds[2].description and "8 p.m." in embeds[2].description
    assert all(sum(len(e) for e in page) <= 6000 for page in main.message_pages(embeds))

    def inter():
        i = fake_inter(guild, 99)
        i.followup = FakeResponse()
        i.followup.send = i.followup.send_message

        async def defer(**kw):
            pass
        i.response.defer = defer
        return i
    run = asyncio.new_event_loop().run_until_complete
    a = inter()
    run(main.challenge_overview.callback(a, "channel"))
    assert "Posted the overview" in a.followup.sent[0][0]
    assert len(guild.ann.sent[-1][1]["embeds"]) == 5

    dms = []

    class U:
        def __init__(self, uid):
            self.uid = uid

        async def send(self, **kw):
            if self.uid == 1202:
                raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "closed")
            dms.append(self.uid)
    monkeypatch.setattr(main.bot, "get_user", lambda uid: U(uid))
    b = inter()
    run(main.challenge_overview.callback(b, "dm"))
    assert set(dms) == {1201, 1203}
    assert "DM'd the overview to 2 players" in b.followup.sent[0][0] and "<@1202>" in b.followup.sent[0][0]


def test_goal_nudges_and_day_before_overview(monkeypatch):
    db = main.db
    guild = FakeGuild()
    guild.id = 66
    db.set_settings(66, 10, 11, None)
    monkeypatch.setattr(discord.utils, "get", lambda roles, name: next((r for r in roles if r.name == name), None))
    dms = []

    class U:
        def __init__(self, uid):
            self.uid = uid

        async def send(self, content=None, **kw):
            dms.append((self.uid, content, kw.get("view")))
    monkeypatch.setattr(main.bot, "get_user", lambda uid: U(uid))
    ch = db.create_challenge(66, "February Challenge", date(2029, 2, 1), date(2029, 2, 28), 99)  # deadline: Jan 31
    for uid in (1301, 1302, 1303):
        db.join(ch.id, uid)
    db.add_goal(ch.id, 1302, "Yoga", 3, False)  # 1 of 3
    db.set_goals(ch.id, 1303, [("Run", 3), ("Read", 5), ("Sleep", 7)], False)  # done
    run = asyncio.new_event_loop().run_until_complete
    d = date(2029, 1, 25)
    while d <= date(2029, 2, 1):
        for hour in (9, 10, 12, 15):
            run(main.run_jobs(guild, db.get_challenge(ch.id), at(d, hour)))
        d += timedelta(days=1)

    # Nudges: Jan 28, 29, 30, 31 (3, 2, 1, 0 days before the deadline), only to the two without 3 goals
    assert len(dms) == 8 and {u for u, _, _ in dms} == {1301, 1302}
    first_1301 = next(c for u, c, _ in dms if u == 1301)
    assert "haven't set your goals yet" in first_1301 and "Wed Jan 31 at 11:59 p.m." in first_1301
    assert "only set 1 of 3 goals" in next(c for u, c, _ in dms if u == 1302)
    assert "tonight at 11:59 p.m." in dms[-1][1]
    assert dms[0][2].children[0].custom_id == f"mb:goals:{ch.id}"

    # The overview posted once, on Jan 31 (the day before the start)
    overviews = [kw["embeds"] for _, kw in guild.ann.sent
                 if kw.get("embeds") and kw["embeds"][0].title.endswith("what to expect")]
    assert len(overviews) == 1
    who = overviews[0][4].description
    assert "Still need goals:" in who and "<@1301>" in who.split("Still need goals:")[1]
    assert db.get_challenge(ch.id).status == "active"

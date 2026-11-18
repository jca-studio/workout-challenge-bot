"""The Discord bot: slash commands, buttons, proof channel and the daily schedule."""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import config, game
from .goal_parse import format_goal, parse_goal
from .db import DB, MIX_UP, THEMES, Challenge, Goal
from .scoring import challenge_weeks, fmt, progress_bar, scaled_target, week_for

log = logging.getLogger("momsbot")

PINK = discord.Color.from_rgb(236, 72, 153)
GOLD = discord.Color.from_rgb(234, 179, 8)

intents = discord.Intents.default()
intents.message_content = True  # needed to see photos posted in the proof channel
bot = commands.Bot(command_prefix="!", intents=intents)
os.makedirs(os.path.dirname(config.DATABASE_PATH) or ".", exist_ok=True)
db = DB(config.DATABASE_PATH)


# ---------- helpers ----------

def is_commish(inter: discord.Interaction) -> bool:
    member = inter.user
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    s = db.settings(inter.guild_id)
    return bool(s and s["commissioner_role_id"] and any(r.id == s["commissioner_role_id"] for r in member.roles))


async def deny(inter: discord.Interaction, msg: str):
    if inter.response.is_done():
        await inter.followup.send(msg, ephemeral=True)
    else:
        await inter.response.send_message(msg, ephemeral=True)


def channel(guild: discord.Guild, which: str) -> discord.TextChannel | None:
    s = db.settings(guild.id)
    if not s or not s[which]:
        return None
    return guild.get_channel(s[which])


def parse_day(text: str | None, today: date) -> date | None:
    if not text or text.lower() == "today":
        return today
    if text.lower() == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def pretty(d: date) -> str:
    return d.strftime("%a %b %-d")


def photo_needed(g: Goal) -> bool:
    return config.PHOTO_MODE == "required" or g.photo_required


def make_view(buttons: list[discord.ui.Button]) -> discord.ui.View:
    """Buttons are handled in on_interaction by custom_id, so they keep working after a restart."""
    view = discord.ui.View(timeout=None)
    for b in buttons[:25]:
        view.add_item(b)
    view.stop()  # don't keep it in memory; on_interaction routes the clicks
    return view


def week_progress_lines(ch: Challenge, user_id: int, d: date) -> list[str]:
    week = week_for(d, challenge_weeks(ch.start, ch.end))
    if not week:
        return []
    pw = game.player_week(db, ch, user_id, week)
    return [f"**{g.name}**  {progress_bar(g.days_done, g.target)}  {fmt(g.points)} pts" for g in pw.goals]


async def log_checkin(ch: Challenge, user: discord.abc.User, goal: Goal, day: date, proof_url: str | None,
                      guild: discord.Guild, attachment_url: str | None = None) -> str:
    ok, why = game.checkin_allowed(ch, day, game.now_local())
    if not ok:
        return why
    if photo_needed(goal) and not proof_url:
        proof = channel(guild, "proof_channel_id")
        where = proof.mention if proof else "the proof channel"
        return f"**{goal.name}** needs a photo. Post it in {where} and tap the goal, or use `/checkin` with a photo."
    new = db.add_checkin(goal.id, user.id, day, proof_url)
    if not new:
        return f"**{goal.name}** is already logged for {pretty(day)}."
    week = week_for(day, challenge_weeks(ch.start, ch.end))
    target = scaled_target(goal.target_days, week.days)
    done = len(db.checkin_days(goal.id, week.start, week.end))
    msg = f"Logged **{goal.name}** for {pretty(day)}.  {progress_bar(done, target)}"
    if done == target:
        msg += "\nThat goal is done for the week. 💪🏾"
        pw = game.player_week(db, ch, user.id, week)
        if pw.perfect:
            msg += f"\n✨ **Perfect week!** That's a +{fmt(config.PERFECT_WEEK_BONUS)} bonus this week."
    return msg


# ---------- setup ----------

@bot.tree.command(description="Admins: set the channels the bot uses.")
@app_commands.describe(announce_channel="Where sign-ups, missions and standings go",
                       proof_channel="Where people post photos",
                       commissioner_role="Role that can run the challenge (optional)")
@app_commands.default_permissions(manage_guild=True)
async def setup(inter: discord.Interaction, announce_channel: discord.TextChannel,
                proof_channel: discord.TextChannel, commissioner_role: discord.Role | None = None):
    db.set_settings(inter.guild_id, announce_channel.id, proof_channel.id, commissioner_role.id if commissioner_role else None)
    has_missions = len(db.missions(inter.guild_id))
    if not has_missions:
        path = os.path.join(os.path.dirname(__file__), "..", "data", "missions.csv")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                has_missions = db.import_missions(inter.guild_id, f.read())
    await inter.response.send_message(
        f"Set. Announcements in {announce_channel.mention}, proof in {proof_channel.mention}"
        + (f", commissioners: {commissioner_role.mention}" if commissioner_role else "")
        + f". {has_missions} mini missions loaded.", ephemeral=True)


# ---------- challenge ----------

challenge_group = app_commands.Group(name="challenge", description="Run the monthly challenge")


@challenge_group.command(name="open", description="Commissioner: open sign-ups for a new challenge.")
@app_commands.describe(start="First day, YYYY-MM-DD", end="Last day, YYYY-MM-DD",
                       signup_deadline="Last day to join, YYYY-MM-DD (default: the day before the start)",
                       name="e.g. December Challenge")
async def challenge_open(inter: discord.Interaction, start: str, end: str, signup_deadline: str | None = None,
                         name: str | None = None):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    if db.current_challenge(inter.guild_id):
        return await deny(inter, "There's already a challenge running. Finish or cancel it first.")
    ann = channel(inter.guild, "announce_channel_id")
    if not ann:
        return await deny(inter, "Run `/setup` first.")
    today = game.now_local().date()
    try:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
        assert s <= e
    except Exception:
        return await deny(inter, "Dates need to look like 2026-12-01, and the end can't be before the start.")
    if signup_deadline:
        try:
            dl = date.fromisoformat(signup_deadline)
        except ValueError:
            return await deny(inter, "The sign-up deadline needs to look like 2026-11-28.")
        if dl > s or dl < today:
            return await deny(inter, "The sign-up deadline has to be between today and the start date.")
    else:
        dl = game.default_deadline(s, today)
    await inter.response.defer(ephemeral=True)
    ch = db.create_challenge(inter.guild_id, name or f"{s.strftime('%B')} Challenge", s, e, inter.user.id, dl)
    error = await post_signup(inter.guild, ch, ann)
    if error:
        db.update_challenge(ch.id, status="cancelled")  # don't leave a challenge with no sign-up post
        return await inter.followup.send(error, ephemeral=True)
    await inter.followup.send(f"Sign-ups are open in {ann.mention} until the end of {pretty(dl)}.", ephemeral=True)


def who_button(challenge_id: int, label: str = "See who's playing") -> discord.ui.Button:
    return discord.ui.Button(label=label, style=discord.ButtonStyle.primary, emoji="👯‍♀️",
                             custom_id=f"mb:who:{challenge_id}")


async def post_signup(guild: discord.Guild, ch: Challenge, ann: discord.TextChannel) -> str | None:
    """Send the sign-up post. Returns an error message if Discord refused."""
    view = make_view([
        discord.ui.Button(label="I'm in", style=discord.ButtonStyle.success, emoji="👑", custom_id=f"mb:join:{ch.id}"),
        who_button(ch.id),
        discord.ui.Button(label="Leave", style=discord.ButtonStyle.secondary, custom_id=f"mb:leave:{ch.id}"),
    ])
    try:
        msg = await ann.send(embed=signup_embed(guild, ch), view=view)
    except discord.Forbidden:
        return (f"I couldn't post in {ann.mention}. In that channel's settings → Permissions, give my role "
                "**View Channel, Send Messages, Embed Links** and **Read Message History**, then try again.")
    except discord.HTTPException as err:
        log.exception("sign-up post failed")
        return f"Discord wouldn't take the sign-up post ({err.text or err.status}). Nothing was opened; try again."
    db.update_challenge(ch.id, signup_channel_id=ann.id, signup_message_id=msg.id)
    return None


@challenge_group.command(name="repost", description="Commissioner: post the sign-up again (if it got lost or deleted).")
async def challenge_repost(inter: discord.Interaction):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    ch = db.current_challenge(inter.guild_id)
    ann = channel(inter.guild, "announce_channel_id")
    if not ch or ch.status != "signup" or not ann:
        return await deny(inter, "There's no challenge taking sign-ups.")
    await inter.response.defer(ephemeral=True)
    error = await post_signup(inter.guild, ch, ann)
    await inter.followup.send(error or f"Posted the sign-up again in {ann.mention}.", ephemeral=True)


def hour_label(h: int) -> str:
    return {0: "midnight", 12: "noon"}.get(h, f"{h % 12} {'a.m.' if h < 12 else 'p.m.'}")


def tz_label() -> str:
    return {"America/New_York": "ET", "America/Chicago": "CT", "America/Denver": "MT",
            "America/Los_Angeles": "PT"}.get(str(config.TZ), str(config.TZ))


def signup_embed(guild: discord.Guild, ch: Challenge, closed: bool = False) -> discord.Embed:
    """The sign-up post: dates, deadline, the full rules and who's in so far."""
    tz = tz_label()
    proof = channel(guild, "proof_channel_id")
    proof_name = proof.mention if proof else "the proof channel"
    deadline = (f"**Sign up by {pretty(ch.signup_deadline)} at 11:59 p.m. {tz}**"
                if ch.signup_deadline else "**Sign up before the start date**")
    e = discord.Embed(
        title=f"❤️✨ {ch.name} ✨❤️", color=PINK,
        description=(f"🗓️ **{pretty(ch.start)} – {pretty(ch.end)}**\n"
                     + ("🔒 **Sign-ups are closed.**" if closed else
                        f"⏰ {deadline}\n\nTap **I'm in** below, then **Add my goals** to fill in all 3 at once.")))
    photo = {"required": "A photo is required for every check-in.",
             "per_goal": "Some goals need a photo; the commissioner will let you know which.",
             }.get(config.PHOTO_MODE, "Photos aren't required, but they're encouraged. Show off! 📸")
    e.add_field(name="🎯 Goals", inline=False, value=(
        "Set exactly **3 goals** (tap **Add my goals** after **I'm in**, or use `/goals set`). Every goal is:\n"
        "*I will [goal] on [1–7] days per week*, written like \"Yoga, 3 days\".\n"
        "\"Don't\" goals get flipped: \"only drink 1 day\" becomes **alcohol-free 6 days**. "
        "One check-in per goal per day. The commissioner approves goals before the start."))
    e.add_field(name="💪🏾 Teams", inline=False, value=(
        f"**{config.TEAM_MODE_MIN_PLAYERS}+ players:** split randomly into **{' vs '.join(config.TEAM_NAMES)}**.\n"
        f"**Fewer:** everyone plays for herself."
        + (" Team scores are compared by average per player." if config.TEAM_SCORE_AVERAGE else "")))
    e.add_field(name="📸 Checking in", inline=False, value=(
        f"Post a photo in {proof_name} and tap which goal it's for, or use `/checkin`. "
        f"{photo}\nAt {hour_label(config.REMINDER_HOUR)} {tz} you'll get a reminder if you haven't checked in that day."))
    e.add_field(name="💋 Points", inline=False, value=(
        "Each goal is worth **up to 1 point a week**. Hit your target and you get the full point. "
        "Fall short and you get the share you hit (2 of 3 workouts = 0.67). Going past your target doesn't earn extra.\n"
        f"**Perfect week** (all 3 goals hit): **+{fmt(config.PERFECT_WEEK_BONUS)} bonus "
        f"point{'' if config.PERFECT_WEEK_BONUS == 1 else 's'}**.\n"
        "**Team bonus:** if every teammate has a perfect week, the team gets **+1 point per player** that week."))
    e.add_field(name="⚡ Mini missions", inline=False, value=(
        f"A new one posts every morning at {hour_label(config.MISSION_POST_HOUR)} {tz}. "
        "Worth **0.25** (easy) or **0.5** (harder) points, and you have 24 hours to do it. "
        + (f"Up to **{fmt(config.MISSION_WEEKLY_CAP)} points a week**. " if config.MISSION_WEEKLY_CAP > 0
           else "Do them every day to stack up points! ")
        + "They don't count toward a perfect week."))
    e.add_field(name="🍑 Weekly schedule", inline=False, value=(
        f"Weeks run **Saturday–Friday**. You can log any day of the current week until **Friday at midnight {tz}**, "
        f"then that week locks. Standings post **Saturday at {hour_label(config.STANDINGS_HOUR)} {tz}**. "
        "Short weeks at the start or end of the challenge shrink your targets to fit."))
    crown = (f" and a {config.WINNER_EMOJI} / {config.TOP_SCORER_EMOJI} on their name until the next challenge"
             if config.CROWN_NICKNAMES else "")
    e.add_field(name="🏆 Prizes", inline=False, value=(
        f"**Winners** get the **{config.WINNER_ROLE_NAME}** role. **Top scorer** gets **{config.TOP_SCORER_ROLE_NAME}**"
        f"{crown}. Ties share the win. Bragging rights included. 👑"))
    players = db.players(ch.id)
    e.add_field(name=f"👯‍♀️ Signed up ({len(players)})", inline=False,
                value=(" ".join(f"<@{p['user_id']}>" for p in players) or "Nobody yet. Be the first!")[:1024])
    e.set_footer(text="Commands: /howtoplay · /goals set · /checkin · /mystats · /leaderboard")
    return e


async def refresh_signup(guild: discord.Guild, ch: Challenge, closed: bool = False):
    """Rebuild the sign-up post so the player list (and closed status) stay current."""
    if not ch.signup_message_id:
        return
    try:
        msg = await guild.get_channel(ch.signup_channel_id).fetch_message(ch.signup_message_id)
        if closed:
            await msg.edit(embed=signup_embed(guild, ch, closed=True), view=make_view([who_button(ch.id)]))
        else:
            await msg.edit(embed=signup_embed(guild, ch))
    except Exception:
        log.warning("couldn't update the sign-up post", exc_info=True)


@challenge_group.command(name="lock", description="Commissioner: close sign-ups, pick the mode and make teams.")
@app_commands.describe(mode="Auto uses the player count")
@app_commands.choices(mode=[app_commands.Choice(name=n, value=n) for n in ("auto", "teams", "individual")])
async def challenge_lock(inter: discord.Interaction, mode: str = "auto"):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    ch = db.current_challenge(inter.guild_id)
    if not ch or ch.status != "signup":
        return await deny(inter, "There's no challenge taking sign-ups.")
    if len(db.players(ch.id)) < 2:
        return await deny(inter, "You need at least 2 players.")
    await inter.response.defer()
    await inter.followup.send(embed=await lock_signups(inter.guild, ch, mode))


async def lock_signups(guild: discord.Guild, ch: Challenge, mode: str = "auto") -> discord.Embed:
    """Close sign-ups, pick the mode and make teams. Runs from /challenge lock, or by itself on the start date."""
    players = [p["user_id"] for p in db.players(ch.id)]
    chosen = game.choose_mode(len(players), mode)
    db.update_challenge(ch.id, mode=chosen, status="ready")
    lines = []
    if chosen == "teams":
        teams = game.assign_teams(players)
        for uid, team in teams.items():
            db.set_team(ch.id, uid, team)
        await sync_team_roles(guild, teams)
        for team in config.TEAM_NAMES:
            members = " ".join(f"<@{u}>" for u, t in teams.items() if t == team)
            lines.append(f"**{team}**\n{members}")
    else:
        lines.append("Everyone plays for herself. Top scorer takes the crown. 👑")
    missing = [u for u in players if len(db.goals(ch.id, u)) < 3]
    if missing:
        lines.append("Still need goals (`/goals set`): " + " ".join(f"<@{u}>" for u in missing))
    unapproved = [u for u in players if any(not g.approved for g in db.goals(ch.id, u))]
    if unapproved:
        lines.append("Commissioner, these goals need approval (`/commish approve`): "
                     + " ".join(f"<@{u}>" for u in unapproved))
    lines.append(f"The challenge starts **{pretty(ch.start)}**.")
    await refresh_signup(guild, ch, closed=True)
    return discord.Embed(title=f"Sign-ups closed: {len(players)} players, {chosen} mode",
                         description="\n\n".join(lines), color=PINK)


async def sync_team_roles(guild: discord.Guild, teams: dict[int, str]):
    """Give each player her team's role. Skips quietly if the bot can't manage roles."""
    try:
        roles = {}
        for name in config.TEAM_NAMES:
            roles[name] = discord.utils.get(guild.roles, name=name) or await guild.create_role(name=name, mentionable=True)
        for uid, team in teams.items():
            member = guild.get_member(uid) or await guild.fetch_member(uid)
            await member.add_roles(roles[team], reason="Challenge teams")
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning("team roles skipped: %s", e)


@challenge_group.command(name="status", description="Where the challenge stands.")
async def challenge_status(inter: discord.Interaction):
    ch = db.current_challenge(inter.guild_id)
    if not ch:
        return await deny(inter, "No challenge running. A commissioner can start one with `/challenge open`.")
    players = db.players(ch.id)
    rows = []
    for p in players:
        gs = db.goals(ch.id, p["user_id"])
        state = "no goals" if not gs else ("✅ approved" if all(g.approved for g in gs) else "waiting for approval")
        team = f" · {p['team']}" if p["team"] else ""
        rows.append(f"<@{p['user_id']}>{team}: {state}")
    embed = discord.Embed(title=ch.name, color=PINK,
                          description=f"{pretty(ch.start)} – {pretty(ch.end)} · **{ch.status}**"
                                      + (f" · {ch.mode} mode" if ch.mode else "")
                                      + (f"\nSign-up deadline: {pretty(ch.signup_deadline)}"
                                         if ch.signup_deadline and ch.status == "signup" else ""))
    embed.add_field(name=f"Players ({len(players)})", value="\n".join(rows)[:1024] or "Nobody yet", inline=False)
    await inter.response.send_message(embed=embed, ephemeral=True)


@challenge_group.command(name="cancel", description="Commissioner: cancel the current challenge.")
@app_commands.describe(confirm="Type CANCEL to confirm")
async def challenge_cancel(inter: discord.Interaction, confirm: str):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    ch = db.current_challenge(inter.guild_id)
    if not ch or confirm != "CANCEL":
        return await deny(inter, "Nothing cancelled.")
    db.update_challenge(ch.id, status="cancelled")
    await inter.response.send_message(f"{ch.name} is cancelled.")


bot.tree.add_command(challenge_group)


# ---------- goals ----------

goals_group = app_commands.Group(name="goals", description="Your 3 goals")


async def own_goal_choices(inter: discord.Interaction, current: str):
    ch = db.current_challenge(inter.guild_id)
    if not ch:
        return []
    return [app_commands.Choice(name=f"{g.slot}. {g.name} ({g.target_days} days/week)", value=g.slot)
            for g in db.goals(ch.id, inter.user.id) if current.lower() in g.name.lower()][:25]


DAY_CHOICES = [app_commands.Choice(name="1 day a week", value=1)] + [
    app_commands.Choice(name=f"{n} days a week", value=n) for n in range(2, 7)] + [
    app_commands.Choice(name="7 days a week (every day)", value=7)]


def goals_summary(ch: Challenge, user_id: int) -> str:
    gs = db.goals(ch.id, user_id)
    lines = [f"{g.slot}. I will **{g.name}** on **{g.target_days}** day{'s' if g.target_days > 1 else ''} per week"
             for g in gs]
    if len(gs) < 3:
        lines.append(f"*{3 - len(gs)} more to go: run `/goals set` to fill them in.*")
    else:
        lines.append("*That's all 3! 🎉 The commissioner will approve them before the start.*")
    return "\n".join(lines)


def goals_locked(ch: Challenge, user_id: int) -> bool:
    gs = db.goals(ch.id, user_id)
    return ch.status == "active" and len(gs) == 3 and all(g.approved for g in gs)


def add_goal_button(ch: Challenge, user_id: int) -> discord.ui.Button | None:
    have = len(db.goals(ch.id, user_id))
    if have >= 3:
        return None
    return discord.ui.Button(label="Add my goals" if have == 0 else "Finish my goals", emoji="➕",
                             style=discord.ButtonStyle.success, custom_id=f"mb:goals:{ch.id}")


def try_add_goal(ch: Challenge, user_id: int, goal: str, days: int) -> tuple[bool, str]:
    """Shared by /goals add and the goal form. Returns (added, message)."""
    if not db.is_player(ch.id, user_id):
        return False, "Join the challenge first (the **I'm in** button on the sign-up post)."
    if goals_locked(ch, user_id):
        return False, "Your goals are locked in for this challenge. Ask the commissioner if something needs to change."
    goal = goal.strip()
    if not goal:
        return False, "Your goal needs a name."
    slot = db.add_goal(ch.id, user_id, goal, days, photo_default=(config.PHOTO_MODE == "required"))
    if slot is None:
        return False, ("You already have 3 goals. To swap one, remove it first with `/goals remove`.\n\n"
                       + goals_summary(ch, user_id))
    return True, f"Added goal {slot}! 💪🏾\n\n" + goals_summary(ch, user_id)


GOAL_EXAMPLES = ["e.g. Yoga, 3 days", "e.g. Affirmations, every day", "e.g. 7 hours of sleep, 5 days"]
DRAFTS: dict[tuple[int, int], list[str]] = {}  # what someone typed, kept if a line needs fixing


class GoalsForm(discord.ui.Modal):
    """All 3 goals in one form. Each line is the goal plus how many days a week."""

    def __init__(self, ch: Challenge, prefill: list[str]):
        super().__init__(title="Your 3 goals")
        self.ch = ch
        self.lines: list[discord.ui.TextInput] = []
        for i in range(3):
            box = discord.ui.TextInput(placeholder=GOAL_EXAMPLES[i], max_length=100,
                                       default=(prefill[i] if i < len(prefill) and prefill[i] else None))
            self.lines.append(box)
            self.add_item(discord.ui.Label(
                text=f"Goal {i + 1}", component=box,
                description="What you'll do, then how many days a week (1–7)" if i == 0 else None))

    async def on_submit(self, inter: discord.Interaction):
        texts = [b.value for b in self.lines]
        await inter.response.send_message(**save_goals_from_form(self.ch, inter.user.id, texts))


def save_goals_from_form(ch: Challenge, user_id: int, texts: list[str]) -> dict:
    """Check all 3 lines; save them if they all read cleanly, otherwise say which to fix."""
    if not db.is_player(ch.id, user_id):
        return {"content": "Join the challenge first (the **I'm in** button on the sign-up post).", "ephemeral": True}
    if goals_locked(ch, user_id):
        return {"content": "Your goals are locked in for this challenge. Ask the commissioner if something needs to change.",
                "ephemeral": True}
    parsed = [parse_goal(t) for t in texts]
    problems = [f"**Goal {i + 1}** (“{texts[i].strip() or 'blank'}”): {p}" for i, p in enumerate(parsed) if isinstance(p, str)]
    if problems:
        DRAFTS[(ch.id, user_id)] = texts
        fix = discord.ui.Button(label="Fix my goals", emoji="✏️", style=discord.ButtonStyle.primary,
                                custom_id=f"mb:goals:{ch.id}")
        return {"content": "Almost! Nothing was saved yet:\n" + "\n".join(problems),
                "view": make_view([fix]), "ephemeral": True}
    DRAFTS.pop((ch.id, user_id), None)
    db.set_goals(ch.id, user_id, parsed, photo_default=(config.PHOTO_MODE == "required"))
    edit = discord.ui.Button(label="Edit my goals", emoji="✏️", style=discord.ButtonStyle.secondary,
                             custom_id=f"mb:goals:{ch.id}")
    return {"content": "Goals saved! 💪🏾\n\n" + goals_summary(ch, user_id), "view": make_view([edit]), "ephemeral": True}


async def open_goals_form(inter: discord.Interaction, ch: Challenge):
    if not db.is_player(ch.id, inter.user.id):
        return await deny(inter, "Join the challenge first (the **I'm in** button on the sign-up post).")
    if goals_locked(ch, inter.user.id):
        return await deny(inter, "Your goals are locked in for this challenge. Ask the commissioner if something needs to change.")
    prefill = DRAFTS.get((ch.id, inter.user.id)) or [format_goal(g.name, g.target_days)
                                                     for g in db.goals(ch.id, inter.user.id)]
    await inter.response.send_modal(GoalsForm(ch, prefill))


async def handle_goals_button(inter: discord.Interaction, challenge_id: int):
    ch = db.get_challenge(challenge_id)
    if not ch or ch.status in ("finished", "cancelled"):
        return await deny(inter, "That challenge isn't running.")
    await open_goals_form(inter, ch)


@goals_group.command(name="set", description="Fill in (or edit) all 3 of your goals in one form.")
async def goals_set_form(inter: discord.Interaction):
    ch = db.current_challenge(inter.guild_id)
    if not ch:
        return await deny(inter, "No challenge running right now.")
    await open_goals_form(inter, ch)


@goals_group.command(name="add", description="Add one of your 3 goals: I will [goal] on [1-7] days per week.")
@app_commands.rename(days="how-many-days")
@app_commands.describe(goal="What you'll do, e.g. Yoga, Affirmations, 7 hours of sleep, Alcohol-free",
                       days="How many days per week")
@app_commands.choices(days=DAY_CHOICES)
async def goals_add(inter: discord.Interaction, goal: app_commands.Range[str, 1, 80], days: int):
    ch = db.current_challenge(inter.guild_id)
    if not ch:
        return await deny(inter, "No challenge running right now.")
    added, msg = try_add_goal(ch, inter.user.id, goal, days)
    if not added:
        return await deny(inter, msg)
    nxt = add_goal_button(ch, inter.user.id)
    await inter.response.send_message(msg, ephemeral=True, view=make_view([nxt]) if nxt else discord.utils.MISSING)


@goals_group.command(name="remove", description="Remove one of your goals so you can add a different one.")
@app_commands.autocomplete(goal=own_goal_choices)
async def goals_remove(inter: discord.Interaction, goal: int):
    ch = db.current_challenge(inter.guild_id)
    if not ch:
        return await deny(inter, "No challenge running right now.")
    if goals_locked(ch, inter.user.id):
        return await deny(inter, "Your goals are locked in for this challenge. Ask the commissioner if something needs to change.")
    if not db.remove_goal(ch.id, inter.user.id, goal):
        return await deny(inter, "Pick one of your goals from the list.")
    await inter.response.send_message("Removed. Add a new one with `/goals add` or `/goals set`.\n\n" + goals_summary(ch, inter.user.id),
                                      ephemeral=True)


@goals_group.command(name="view", description="See your goals, or someone else's.")
async def goals_view(inter: discord.Interaction, member: discord.Member | None = None):
    ch = db.current_challenge(inter.guild_id) or db.latest_challenge(inter.guild_id)
    who = member or inter.user
    if not ch:
        return await deny(inter, "No challenge yet.")
    gs = db.goals(ch.id, who.id)
    if not gs:
        return await deny(inter, f"{who.display_name} hasn't set goals.")
    text = "\n".join(f"{g.slot}. {g.name}: {g.target_days} days/week" + (" 📸" if photo_needed(g) else "")
                     + ("" if g.approved else " *(not approved yet)*") for g in gs)
    await inter.response.send_message(f"**{who.display_name}'s goals**\n{text}", ephemeral=True)


def all_goals_embeds(ch: Challenge) -> tuple[list[discord.Embed], int]:
    """Everyone's goals in one view. Returns the embeds and how many players are waiting for approval."""
    blocks, waiting, no_goals = [], 0, []
    for p in db.players(ch.id):
        uid = p["user_id"]
        gs = db.goals(ch.id, uid)
        if not gs:
            no_goals.append(f"<@{uid}>")
            continue
        approved = all(g.approved for g in gs)
        waiting += 0 if approved else 1
        team = f" · {p['team']}" if p["team"] else ""
        lines = [f"{'✅' if approved else '⏳'} <@{uid}>{team}"]
        lines += [f"\u2003{g.slot}. {g.name}: **{g.target_days}** day{'s' if g.target_days > 1 else ''}/week"
                  + (" 📸" if photo_needed(g) else "") for g in gs]
        blocks.append("\n".join(lines))
    if no_goals:
        blocks.append("**No goals yet:** " + " ".join(no_goals))
    if not blocks:
        blocks = ["Nobody has signed up yet."]
    # Split across embeds so long lists fit Discord's size limits.
    embeds, current = [], ""
    for b in blocks:
        if current and len(current) + len(b) + 2 > 2800:
            embeds.append(current)
            current = ""
        current += ("\n\n" if current else "") + b
    embeds.append(current)
    out = []
    for i, text in enumerate(embeds):
        e = discord.Embed(description=text, color=PINK)
        if i == 0:
            e.title = f"{ch.name}: everyone's goals"
        out.append(e)
    out[-1].set_footer(text=f"✅ approved · ⏳ waiting ({waiting}) · 📸 photo required")
    return out, waiting


def message_pages(embeds: list[discord.Embed], limit: int = 5800) -> list[list[discord.Embed]]:
    """Discord allows about 6,000 characters across all embeds in one message, so split long lists."""
    pages, page, size = [], [], 0
    for e in embeds:
        if page and (size + len(e) > limit or len(page) == 10):
            pages.append(page)
            page, size = [], 0
        page.append(e)
        size += len(e)
    pages.append(page)
    return pages


def approve_view(ch: Challenge, waiting: int) -> discord.ui.View:
    done = waiting == 0
    return make_view([discord.ui.Button(
        label="All approved" if done else f"Approve all ({waiting})", emoji="✅",
        style=discord.ButtonStyle.secondary if done else discord.ButtonStyle.success,
        disabled=done, custom_id=f"mb:approveall:{ch.id}")])


@goals_group.command(name="all", description="Commissioner: see everyone's goals and approve them all at once.")
async def goals_all(inter: discord.Interaction):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    ch = db.current_challenge(inter.guild_id)
    if not ch:
        return await deny(inter, "No challenge running.")
    embeds, waiting = all_goals_embeds(ch)
    pages = message_pages(embeds)
    await inter.response.send_message(embeds=pages[0], view=approve_view(ch, waiting), ephemeral=True)
    for page in pages[1:]:
        await inter.followup.send(embeds=page, ephemeral=True)


async def handle_approve_all(inter: discord.Interaction, challenge_id: int):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    ch = db.get_challenge(challenge_id)
    if not ch:
        return await deny(inter, "I couldn't find that challenge.")
    n = db.approve_goals(ch.id)
    embeds, waiting = all_goals_embeds(ch)
    pages = message_pages(embeds)
    await inter.response.edit_message(embeds=pages[0], view=approve_view(ch, waiting))
    if len(pages) > 1:
        await inter.followup.send(f"Approved everyone's goals ({n} goals). Run `/goals all` again for the updated list.",
                                  ephemeral=True)


bot.tree.add_command(goals_group)


# ---------- check-ins ----------

async def goal_autocomplete(inter: discord.Interaction, current: str):
    ch = db.current_challenge(inter.guild_id)
    if not ch:
        return []
    member = getattr(inter.namespace, "member", None)
    uid = member.id if member else inter.user.id
    return [app_commands.Choice(name=f"{g.slot}. {g.name} ({g.target_days} days/week)", value=g.slot)
            for g in db.goals(ch.id, uid, approved_only=member is None) if current.lower() in g.name.lower()][:25]


async def day_autocomplete(inter: discord.Interaction, current: str):
    ch = db.current_challenge(inter.guild_id)
    today = game.now_local().date()
    if not ch:
        return []
    week = week_for(today, challenge_weeks(ch.start, ch.end))
    start = week.start if week else today
    out, d = [], today
    while d >= start and d >= ch.start:
        label = "Today" if d == today else ("Yesterday" if d == today - timedelta(days=1) else pretty(d))
        out.append(app_commands.Choice(name=f"{label} ({pretty(d)})", value=d.isoformat()))
        d -= timedelta(days=1)
    return out[:25]


@bot.tree.command(description="Check in on a goal. Add a photo if you have one!")
@app_commands.describe(goal="Which goal", day="Defaults to today. You can log earlier days until Friday midnight.",
                       photo="Proof photo (optional unless your goal needs one)")
@app_commands.autocomplete(goal=goal_autocomplete, day=day_autocomplete)
async def checkin(inter: discord.Interaction, goal: int, day: str | None = None, photo: discord.Attachment | None = None):
    ch = db.current_challenge(inter.guild_id)
    if not ch or ch.status != "active":
        return await deny(inter, "The challenge isn't running right now.")
    g = next((x for x in db.goals(ch.id, inter.user.id, approved_only=True) if x.slot == goal), None)
    if not g:
        return await deny(inter, "Pick one of your approved goals from the list.")
    d = parse_day(day, game.now_local().date())
    if not d:
        return await deny(inter, "I couldn't read that day.")
    ok, why = game.checkin_allowed(ch, d, game.now_local())
    if not ok:
        return await deny(inter, why)
    if d in db.checkin_days(g.id, d, d):
        return await deny(inter, f"**{g.name}** is already logged for {pretty(d)}.")
    await inter.response.defer(ephemeral=True)
    proof_url = None
    if photo:
        proof = channel(inter.guild, "proof_channel_id")
        if proof:
            file = await photo.to_file()
            posted = await proof.send(f"📸 {inter.user.mention} checked in: **{g.name}** ({pretty(d)})", file=file)
            proof_url = posted.jump_url
        else:
            proof_url = photo.url
    msg = await log_checkin(ch, inter.user, g, d, proof_url, inter.guild)
    await inter.followup.send(msg, ephemeral=True)


@bot.event
async def on_message(message: discord.Message):
    """A photo in the proof channel gets a reply with buttons: tap which goal (or mission) it's for."""
    if message.author.bot or not message.guild or not message.attachments:
        return
    s = db.settings(message.guild.id)
    if not s or message.channel.id != s["proof_channel_id"]:
        return
    ch = db.current_challenge(message.guild.id)
    if not ch or ch.status != "active" or not db.is_player(ch.id, message.author.id):
        return
    now = game.now_local()
    buttons = [discord.ui.Button(label=g.name[:80], emoji="✅", style=discord.ButtonStyle.primary,
                                 custom_id=f"mb:p:{message.author.id}:g:{g.id}")
               for g in game.open_goals_today(db, ch, message.author.id, now.date())]
    m = game.open_mission(db, ch, now)
    done_days = {d for d, _ in db.mission_completions(ch.id, message.author.id)}
    if m and date.fromisoformat(m["day"]) not in done_days:
        buttons.append(discord.ui.Button(label="Today's mini mission", emoji="⚡", style=discord.ButtonStyle.success,
                                         custom_id=f"mb:p:{message.author.id}:m:{m['day']}"))
    if not buttons:
        await message.add_reaction("🔥")
        return
    await message.reply("What's this for? Tap it:", view=make_view(buttons), mention_author=False)


# ---------- mini missions ----------

mission_group = app_commands.Group(name="mission", description="Daily mini missions")


@mission_group.command(name="today", description="See today's mini mission.")
async def mission_today(inter: discord.Interaction):
    ch = db.current_challenge(inter.guild_id)
    m = game.open_mission(db, ch, game.now_local()) if ch else None
    if not m:
        return await deny(inter, "No mini mission open right now.")
    await inter.response.send_message(embed=mission_embed(m), ephemeral=True)


@mission_group.command(name="done", description="Mark today's mini mission done. Add a photo if you have one!")
async def mission_done(inter: discord.Interaction, photo: discord.Attachment | None = None):
    ch = db.current_challenge(inter.guild_id)
    if not ch or ch.status != "active" or not db.is_player(ch.id, inter.user.id):
        return await deny(inter, "You're not in a running challenge.")
    await inter.response.defer(ephemeral=True)
    proof_url = None
    if photo:
        proof = channel(inter.guild, "proof_channel_id")
        if proof:
            posted = await proof.send(f"⚡ {inter.user.mention} did the mini mission!", file=await photo.to_file())
            proof_url = posted.jump_url
    await inter.followup.send(complete_mission(ch, inter.user.id, proof_url), ephemeral=True)


def complete_mission(ch: Challenge, user_id: int, proof_url: str | None, day: date | None = None) -> str:
    now = game.now_local()
    m = game.open_mission(db, ch, now)
    if not m or (day and m["day"] != day.isoformat()):
        return "That mini mission has closed (you get 24 hours from when it posts)."
    left = game.mission_points_left(db, ch, user_id, date.fromisoformat(m["day"]))
    if left <= 0:
        return (f"Nice! You've already hit this week's {fmt(config.MISSION_WEEKLY_CAP)}-point mini mission cap, "
                "so this one's for pride. 💅🏾")
    if not db.complete_mission(ch.id, user_id, date.fromisoformat(m["day"]), m["points"], proof_url):
        return "You already did this one."
    earned = min(m["points"], left)
    return f"⚡ Mini mission done: **+{fmt(earned)}** pts."


def mission_embed(m) -> discord.Embed:
    e = discord.Embed(title=f"⚡ {m['theme']}", description=f"**{m['text']}**", color=GOLD)
    e.add_field(name="Points", value=fmt(m["points"]))
    if m["proof"]:
        e.add_field(name="Proof", value=m["proof"])
    e.set_footer(text="You have 24 hours. Post a photo in the proof channel or use /mission done.")
    return e


bot.tree.add_command(mission_group)

missions_group = app_commands.Group(name="missions", description="Commissioner: manage the mini mission bank")


@missions_group.command(name="import", description="Replace the mission bank from a CSV (Day, Mission, Points columns).")
async def missions_import(inter: discord.Interaction, file: discord.Attachment):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    try:
        n = db.import_missions(inter.guild_id, (await file.read()).decode("utf-8-sig"))
    except Exception as e:
        return await deny(inter, f"Couldn't import that file: {e}")
    await inter.response.send_message(f"Loaded {n} mini missions.", ephemeral=True)


BUILT_IN_MISSIONS = os.path.join(os.path.dirname(__file__), "..", "data", "missions.csv")


@missions_group.command(name="reset", description="Reload the built-in mission bank (replaces the current one).")
async def missions_reset(inter: discord.Interaction):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    with open(BUILT_IN_MISSIONS, encoding="utf-8") as f:
        n = db.import_missions(inter.guild_id, f.read())
    await inter.response.send_message(f"Reloaded {n} built-in mini missions (0.25 easy / 0.5 harder).", ephemeral=True)


@missions_group.command(name="add", description="Add one mini mission.")
@app_commands.choices(day=[app_commands.Choice(name=t, value=t) for t in THEMES + [MIX_UP]])
async def missions_add(inter: discord.Interaction, day: str, mission: str, points: app_commands.Range[float, 0.25, 1.0]):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    db.add_mission(inter.guild_id, day, mission, points)
    await inter.response.send_message(f"Added to {day}: {mission} ({fmt(points)} pts)", ephemeral=True)


@missions_group.command(name="count", description="How many missions each day has.")
async def missions_count(inter: discord.Interaction):
    lines = [f"{t}: {len(db.missions(inter.guild_id, t))}" for t in THEMES + [MIX_UP]]
    await inter.response.send_message("\n".join(lines), ephemeral=True)


bot.tree.add_command(missions_group)


# ---------- stats ----------

@bot.tree.command(description="Your week so far and your challenge total.")
async def mystats(inter: discord.Interaction):
    ch = db.current_challenge(inter.guild_id)
    if not ch or not db.is_player(ch.id, inter.user.id):
        return await deny(inter, "You're not in a running challenge.")
    today = min(max(game.now_local().date(), ch.start), ch.end)
    lines = week_progress_lines(ch, inter.user.id, today)
    s = game.standings(db, ch)
    week = week_for(today, s.weeks)
    pw = s.player_weeks[inter.user.id][week.number - 1]
    e = discord.Embed(title=f"Week {week.number} ({pretty(week.start)} – {pretty(week.end)})", color=PINK,
                      description="\n".join(lines) or "No approved goals yet.")
    e.add_field(name="This week", value=f"{fmt(pw.points)} pts" + (" ✨ perfect so far" if pw.perfect else ""))
    used = game.mission_points_this_week(db, ch, inter.user.id, today)
    cap = f" / {fmt(config.MISSION_WEEKLY_CAP)}" if config.MISSION_WEEKLY_CAP > 0 else ""
    e.add_field(name="Mini missions this week", value=f"{fmt(used)}{cap} pts")
    e.add_field(name="Challenge total", value=f"{fmt(s.player_total(inter.user.id))} pts")
    await inter.response.send_message(embed=e, ephemeral=True)


def standings_embed(ch: Challenge, through_week: int | None, title: str) -> discord.Embed:
    s = game.standings(db, ch, through_week)
    e = discord.Embed(title=title, color=PINK)
    if ch.mode == "teams":
        lines = []
        for i, (team, total) in enumerate(s.ranked_teams()):
            tw = s.team_weeks(team)[-1] if s.weeks else None
            bonus = " · 💋 team perfect week (+1 each)" if tw and tw.perfect else ""
            lines.append(f"{'🏆' if i == 0 else '💪🏾'} **{team}**: {fmt(total)}{bonus}")
        e.add_field(name="Teams" + (" (average per player)" if config.TEAM_SCORE_AVERAGE else ""),
                    value="\n".join(lines), inline=False)
    rows = []
    for i, (uid, total) in enumerate(s.ranked_players()[:15], 1):
        last = s.player_weeks[uid][-1] if s.weeks else None
        star = " ✨" if last and last.perfect else ""
        rows.append(f"{i}. <@{uid}>: {fmt(total)}{star}")
    e.add_field(name="Players", value="\n".join(rows) or "—", inline=False)
    e.set_footer(text=f"✨ = perfect week (+{fmt(config.PERFECT_WEEK_BONUS)})")
    return e


@bot.tree.command(description="Current standings.")
async def leaderboard(inter: discord.Interaction):
    ch = db.current_challenge(inter.guild_id) or db.latest_challenge(inter.guild_id)
    if not ch or not ch.mode:
        return await deny(inter, "No standings yet.")
    await inter.response.send_message(embed=standings_embed(ch, None, f"{ch.name}: standings so far"), ephemeral=True)


@bot.tree.command(name="howtoplay", description="How the challenge works.")
async def howtoplay(inter: discord.Interaction):
    proof = channel(inter.guild, "proof_channel_id")
    p = proof.mention if proof else "the proof channel"
    text = (
        "**Goals.** Everyone sets 3, each written like \"Yoga, 3 days\" or \"Affirmations, every day\". "
        "Fill them in with `/goals set`.\n"
        "**Check in.** Post a photo in " + p + " and tap the goal, or use `/checkin`. "
        f"At {config.REMINDER_HOUR % 12 or 12} p.m. you'll get a reminder if you haven't checked in.\n"
        "**Points.** Each goal is worth up to 1 point a week: hit your target for the full point, "
        "or get the share you hit (2 of 3 = 0.67). Extra days don't add more. "
        f"Hit all 3 for a **+{fmt(config.PERFECT_WEEK_BONUS)} perfect week bonus**. In team mode, a team of all perfect weeks gets **+1 per player**.\n"
        "**Mini missions.** One posts every morning. 0.25 or 0.5 points, 24 hours to do it"
        + (f", up to {fmt(config.MISSION_WEEKLY_CAP)} points a week.\n" if config.MISSION_WEEKLY_CAP > 0
           else ". Do them every day to stack up points.\n")
        + "**Deadlines.** Weeks run Saturday–Friday and lock Friday at midnight. Standings post Saturday morning.\n"
        "`/mystats` for your week, `/leaderboard` for everyone."
    )
    await inter.response.send_message(text, ephemeral=True)


# ---------- commissioner tools ----------

commish = app_commands.Group(name="commish", description="Commissioner tools")


@commish.command(name="approve", description="Approve goals for one player, or everyone.")
async def commish_approve(inter: discord.Interaction, member: discord.Member | None = None):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    ch = db.current_challenge(inter.guild_id)
    if not ch:
        return await deny(inter, "No challenge running.")
    n = db.approve_goals(ch.id, member.id if member else None)
    await inter.response.send_message(f"Approved {n} goals" + (f" for {member.mention}." if member else " for everyone."),
                                      ephemeral=True)


@commish.command(name="photo", description="Require (or stop requiring) a photo for one of someone's goals.")
@app_commands.autocomplete(goal=goal_autocomplete)
async def commish_photo(inter: discord.Interaction, member: discord.Member, goal: int, required: bool):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    ch = db.current_challenge(inter.guild_id)
    ok = ch and db.set_photo_required(ch.id, member.id, goal, required)
    await inter.response.send_message("Updated." if ok else "Couldn't find that goal.", ephemeral=True)


async def _fix(inter: discord.Interaction, member: discord.Member, goal: int, day: str, add: bool):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    ch = db.current_challenge(inter.guild_id) or db.latest_challenge(inter.guild_id)
    g = ch and next((x for x in db.goals(ch.id, member.id) if x.slot == goal), None)
    d = parse_day(day, game.now_local().date())
    if not g or not d or not (ch.start <= d <= ch.end):
        return await deny(inter, "Couldn't find that goal or day.")
    changed = db.add_checkin(g.id, member.id, d, None, added_by=inter.user.id) if add else db.remove_checkin(g.id, d)
    await inter.response.send_message(
        f"{'Added' if add else 'Removed'} {member.mention}'s **{g.name}** for {pretty(d)}." if changed else "No change.",
        ephemeral=True)
    week = week_for(d, challenge_weeks(ch.start, ch.end))
    if changed and game.now_local() >= game.lock_time(week):
        ann = channel(inter.guild, "announce_channel_id")
        if ann:
            await ann.send(f"📝 Score update: the commissioner corrected week {week.number}.",
                           embed=standings_embed(ch, None, "Updated standings"))


@commish.command(name="add-checkin", description="Add a check-in someone missed, even after the week locked.")
@app_commands.autocomplete(goal=goal_autocomplete)
@app_commands.describe(day="YYYY-MM-DD, today or yesterday")
async def commish_add(inter: discord.Interaction, member: discord.Member, goal: int, day: str):
    await _fix(inter, member, goal, day, True)


@commish.command(name="remove-checkin", description="Remove a check-in that doesn't count.")
@app_commands.autocomplete(goal=goal_autocomplete)
@app_commands.describe(day="YYYY-MM-DD, today or yesterday")
async def commish_remove(inter: discord.Interaction, member: discord.Member, goal: int, day: str):
    await _fix(inter, member, goal, day, False)


@commish.command(name="mission-points", description="Set someone's mini mission points for a day (0 removes it).")
@app_commands.describe(day="YYYY-MM-DD", points="e.g. 0.5 for the recipe bonus")
async def commish_mission_points(inter: discord.Interaction, member: discord.Member, day: str,
                                 points: app_commands.Range[float, 0, 1]):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    ch = db.current_challenge(inter.guild_id) or db.latest_challenge(inter.guild_id)
    d = parse_day(day, game.now_local().date())
    if not ch or not d:
        return await deny(inter, "Couldn't find that day.")
    db.set_mission_points(ch.id, member.id, d, points)
    await inter.response.send_message(f"{member.mention}'s mini mission for {pretty(d)}: {fmt(points)} pts.", ephemeral=True)


@commish.command(name="post-standings", description="Post the current standings now.")
async def commish_post(inter: discord.Interaction):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    ch = db.current_challenge(inter.guild_id)
    if not ch or not ch.mode:
        return await deny(inter, "No standings yet.")
    await inter.response.send_message(embed=standings_embed(ch, None, f"{ch.name}: standings so far"))


bot.tree.add_command(commish)


# ---------- buttons ----------

@bot.listen()
async def on_interaction(inter: discord.Interaction):
    if inter.type != discord.InteractionType.component:
        return
    cid = (inter.data or {}).get("custom_id", "")
    if not cid.startswith("mb:"):
        return
    parts = cid.split(":")
    kind = parts[1]
    try:
        if kind in ("join", "leave"):
            await handle_signup(inter, int(parts[2]), kind == "join")
        elif kind == "who":
            await handle_who(inter, int(parts[2]))
        elif kind == "approveall":
            await handle_approve_all(inter, int(parts[2]))
        elif kind in ("goals", "addgoal"):
            await handle_goals_button(inter, int(parts[2]))
        elif kind == "p":
            await handle_proof_button(inter, int(parts[2]), parts[3], parts[4])
        elif kind in ("yes", "no"):
            await handle_reminder(inter, int(parts[2]), date.fromisoformat(parts[3]), kind == "yes")
    except Exception:
        log.exception("button failed: %s", cid)
        await deny(inter, "Something went wrong. Try `/checkin` instead.")


async def handle_signup(inter: discord.Interaction, challenge_id: int, join: bool):
    ch = db.get_challenge(challenge_id)
    if not ch or not game.signups_open(ch, game.now_local()):
        return await deny(inter, "Sign-ups are closed. Catch the next one! 💕")
    changed = db.join(ch.id, inter.user.id) if join else db.leave(ch.id, inter.user.id)
    if join:
        have = len(db.goals(ch.id, inter.user.id))
        if changed:
            msg = "You're in! 👑 Next, tap **Add my goals** to fill in all 3 at once."
        elif have < 3:
            msg = f"You're already in! 👑 You have {have} of 3 goals. Tap **Finish my goals** to add the rest."
        else:
            msg = "You're already in, with all 3 goals set! 👑 To change them, use `/goals set`."
        buttons = [discord.ui.Button(label="You're in", emoji="✅", style=discord.ButtonStyle.success, disabled=True,
                                     custom_id=f"mb:noop:{ch.id}")]
        nxt = add_goal_button(ch, inter.user.id)
        if nxt:
            buttons.append(nxt)
        buttons.append(who_button(ch.id, "See who else is playing"))
        view = make_view(buttons)
        await inter.response.send_message(msg, view=view, ephemeral=True)
    else:
        await inter.response.send_message("You're out." if changed else "You weren't signed up.", ephemeral=True)
    if changed:
        await refresh_signup(inter.guild, ch)


def lineup_text(ch: Challenge, viewer_id: int) -> str:
    players = db.players(ch.id)
    if not players:
        return "Nobody's signed up yet. Be the first! 👑"

    def name(uid: int) -> str:
        return f"<@{uid}>" + (" (you)" if uid == viewer_id else "")

    header = f"**{ch.name}: {len(players)} playing**"
    teams = db.teams(ch.id)
    if ch.mode == "teams" and teams:
        blocks = []
        for team in config.TEAM_NAMES:
            members = [u for u, t in teams.items() if t == team]
            blocks.append(f"**{team}** ({len(members)})\n" + "\n".join(name(u) for u in members))
        return header + "\n\n" + "\n\n".join(blocks)
    return header + "\n" + "\n".join(name(p["user_id"]) for p in players)


async def handle_who(inter: discord.Interaction, challenge_id: int):
    ch = db.get_challenge(challenge_id)
    if not ch:
        return await deny(inter, "I couldn't find that challenge.")
    await inter.response.send_message(lineup_text(ch, inter.user.id)[:2000], ephemeral=True,
                                      allowed_mentions=discord.AllowedMentions.none())


async def handle_proof_button(inter: discord.Interaction, owner: int, what: str, ref: str):
    if inter.user.id != owner:
        return await deny(inter, "Only the person who posted this can tap these. 😉")
    ch = db.current_challenge(inter.guild_id)
    if not ch:
        return await deny(inter, "The challenge isn't running.")
    original = inter.message.reference
    proof_url = (f"https://discord.com/channels/{inter.guild_id}/{inter.channel_id}/{original.message_id}"
                 if original else inter.message.jump_url)
    today = game.now_local().date()
    if what == "g":
        g = db.goal(int(ref))
        if not g or g.user_id != owner:
            return await deny(inter, "That goal isn't yours.")
        result = await log_checkin(ch, inter.user, g, today, proof_url, inter.guild)
    else:
        result = complete_mission(ch, owner, proof_url, date.fromisoformat(ref))
    await inter.response.send_message(result, ephemeral=True)
    if result.startswith(("Logged", "⚡")):
        try:
            await inter.message.edit(content=inter.message.content + f"\n✅ {result.splitlines()[0]}")
        except discord.HTTPException:
            pass


async def handle_reminder(inter: discord.Interaction, goal_id: int, day: date, yes: bool):
    if not yes:
        return await inter.response.send_message("No worries. Tomorrow's a new day. 💕", ephemeral=True)
    g = db.goal(goal_id)
    if not g or g.user_id != inter.user.id:
        return await deny(inter, "That goal isn't yours.")
    ch = db.get_challenge(db.conn.execute("SELECT challenge_id FROM goals WHERE id=?", (goal_id,)).fetchone()[0])
    guild = bot.get_guild(ch.guild_id)
    await inter.response.send_message(await log_checkin(ch, inter.user, g, day, None, guild))


# ---------- schedule ----------

@tasks.loop(minutes=1)
async def scheduler():
    now = game.now_local()
    for guild_id in db.all_guild_ids():
        guild = bot.get_guild(guild_id)
        ch = db.current_challenge(guild_id)
        if not guild or not ch:
            continue
        try:
            await run_jobs(guild, ch, now)
        except Exception:
            log.exception("scheduler failed for guild %s", guild_id)


async def run_jobs(guild: discord.Guild, ch: Challenge, now: datetime):
    ann = channel(guild, "announce_channel_id")
    today = now.date()
    if game.should_lock(ch, now):
        if len(db.players(ch.id)) < 2:
            # Not enough players: keep sign-ups open (drop the deadline) instead of shutting everyone out.
            if ch.signup_deadline:
                db.update_challenge(ch.id, signup_deadline=None)
                await refresh_signup(guild, db.get_challenge(ch.id))
            if ann and not db.job_done(ch.id, "too_few", key := today.isoformat()):
                db.mark_job(ch.id, "too_few", key)
                await ann.send(f"{ch.name} needs at least 2 players, so sign-ups are staying open. "
                               "Tap **I'm in** on the sign-up post!")
            return
        embed = await lock_signups(guild, ch)
        if ann:
            await ann.send(embed=embed)
        ch = db.get_challenge(ch.id)
    if ch.status == "ready" and today >= ch.start and now.hour >= config.MISSION_POST_HOUR:
        db.update_challenge(ch.id, status="active")
        ch = db.get_challenge(ch.id)
        if ann and not db.job_done(ch.id, "kickoff", "1"):
            await ann.send(f"🔥 **{ch.name} starts now!** Post proof, tap your goal, and let's get it. "
                           "`/howtoplay` if you need a refresher.")
            db.mark_job(ch.id, "kickoff", "1")
    if ch.status != "active":
        return
    in_range = ch.start <= today <= ch.end
    key = today.isoformat()

    if in_range and now.hour >= config.MISSION_POST_HOUR and not db.job_done(ch.id, "mission", key):
        m = game.pick_mission(db, ch, today)
        if m:
            db.set_daily_mission(ch.id, today, m["id"], now)
            if ann:
                await ann.send(embed=mission_embed(db.daily_mission(ch.id, today)))
        db.mark_job(ch.id, "mission", key)

    if in_range and now.hour >= config.REMINDER_HOUR and not db.job_done(ch.id, "reminder", key):
        db.mark_job(ch.id, "reminder", key)
        await send_reminders(guild, ch, today)

    weeks = challenge_weeks(ch.start, ch.end)
    for w in weeks[:-1]:
        lock = game.lock_time(w)
        if now >= lock and (now.date() > lock.date() or now.hour >= config.STANDINGS_HOUR) \
                and not db.job_done(ch.id, "standings", str(w.number)):
            db.mark_job(ch.id, "standings", str(w.number))
            if ann:
                await ann.send(embed=standings_embed(ch, w.number, f"🏆 Week {w.number} standings"))

    if today > ch.end and now.hour >= config.STANDINGS_HOUR and not db.job_done(ch.id, "final", "1"):
        db.mark_job(ch.id, "final", "1")
        await finish(guild, ch, ann)


async def send_reminders(guild: discord.Guild, ch: Challenge, today: date):
    unreachable = []
    for p in db.players(ch.id):
        uid = p["user_id"]
        if db.checked_in_on(ch.id, uid, today):
            continue
        open_goals = game.open_goals_today(db, ch, uid, today)
        if not open_goals:
            continue
        lines = [f"You said you'd **{g.name}** {g.target_days} days this week. Did you today?" for g in open_goals]
        buttons = [discord.ui.Button(label=g.name[:70], emoji="✅", style=discord.ButtonStyle.success,
                                     custom_id=f"mb:yes:{g.id}:{today.isoformat()}") for g in open_goals]
        buttons.append(discord.ui.Button(label="Not today", style=discord.ButtonStyle.secondary,
                                         custom_id=f"mb:no:0:{today.isoformat()}"))
        if any(photo_needed(g) for g in open_goals):
            lines.append("📸 = needs a photo in the proof channel.")
        try:
            user = bot.get_user(uid) or await bot.fetch_user(uid)
            await user.send(f"💕 Check-in time ({guild.name})\n" + "\n".join(lines), view=make_view(buttons))
        except (discord.Forbidden, discord.HTTPException):
            unreachable.append(uid)
    ann = channel(guild, "announce_channel_id")
    if unreachable and ann:
        await ann.send(" ".join(f"<@{u}>" for u in unreachable)
                       + " don't forget to check in today! (Allow DMs from this server to get reminders privately.)")


async def finish(guild: discord.Guild, ch: Challenge, ann: discord.TextChannel | None):
    s = game.standings(db, ch)
    db.update_challenge(ch.id, status="finished")
    if not ann:
        return
    players = s.ranked_players()
    best = round(players[0][1], 6) if players else None
    top = [u for u, pts in players if round(pts, 6) == best and pts > 0]  # ties share the crown
    lines = []
    winners: list[int] = []
    if ch.mode == "teams":
        teams = s.ranked_teams()
        if teams:
            best_team = round(teams[0][1], 6)
            win_teams = [t for t, pts in teams if round(pts, 6) == best_team]
            winners = [u for u, t in s.teams.items() if t in win_teams]
            lines.append(f"🏆 **{' & '.join(win_teams)}** {'tie' if len(win_teams) > 1 else 'win'} "
                         f"with {fmt(teams[0][1])}!")
    else:
        winners = top
    if top:
        who = " & ".join(f"<@{u}>" for u in top)
        lines.append(f"💎 Top scorer: {who} with {fmt(players[0][1])} pts. That girl.")
    await ann.send("🎉 **Cue the fanfare!**\n" + "\n".join(lines),
                   embed=standings_embed(ch, None, f"{ch.name}: final standings"))
    await give_role(guild, config.WINNER_ROLE_NAME, winners, ann)
    if top:
        await give_role(guild, config.TOP_SCORER_ROLE_NAME, top, ann)
    if config.CROWN_NICKNAMES:
        await move_crowns(guild, winners, top, ann)
    for name in config.TEAM_NAMES:
        role = discord.utils.get(guild.roles, name=name)
        for m in list(role.members) if role else []:
            try:
                await m.remove_roles(role, reason="Challenge over")
            except discord.HTTPException:
                pass


async def give_role(guild: discord.Guild, name: str, user_ids: list[int], ann: discord.TextChannel):
    """Moves the role to this month's winners."""
    try:
        role = discord.utils.get(guild.roles, name=name) or await guild.create_role(name=name, hoist=True)
        for m in list(role.members):
            await m.remove_roles(role, reason="New challenge winners")
        for uid in user_ids:
            member = guild.get_member(uid) or await guild.fetch_member(uid)
            await member.add_roles(role, reason="Challenge winner")
    except (discord.Forbidden, discord.HTTPException):
        await ann.send(f"(I couldn't hand out the **{name}** role. Give me Manage Roles and put my role above it.)")


def strip_crowns(name: str) -> str:
    for emoji in (config.WINNER_EMOJI, config.TOP_SCORER_EMOJI):
        name = name.replace(emoji, "")
    return name.strip()


async def move_crowns(guild: discord.Guild, winners: list[int], top: list[int], ann: discord.TextChannel):
    """Take last month's crowns off, then put 👑 (winners) and 💎 (top scorer) in front of new winners' nicknames."""
    skipped = []
    previous = {r["user_id"]: r["original_nick"] for r in db.crowns(guild.id)}
    for row in db.crowns(guild.id):
        try:
            member = guild.get_member(row["user_id"]) or await guild.fetch_member(row["user_id"])
            current = member.nick or ""
            if config.WINNER_EMOJI in current or config.TOP_SCORER_EMOJI in current:
                await member.edit(nick=row["original_nick"], reason="Crown moves to new winners")
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass
        db.clear_crown(guild.id, row["user_id"])

    for uid in dict.fromkeys(winners + top):  # keeps order, no duplicates
        prefix = (config.WINNER_EMOJI if uid in winners else "") + (config.TOP_SCORER_EMOJI if uid in top else "")
        try:
            member = guild.get_member(uid) or await guild.fetch_member(uid)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            continue
        # A repeat winner keeps her real nickname; if she renamed herself since, her new name wins.
        has_crown = bool(member.nick) and strip_crowns(member.nick) != member.nick
        original = previous.get(uid) if has_crown else member.nick
        base = strip_crowns(original or member.global_name or member.name)
        if uid == guild.owner_id:
            skipped.append(f"<@{uid}> (add {prefix} yourself; Discord won't let bots rename the server owner)")
            continue
        try:
            await member.edit(nick=f"{prefix} {base}"[:32], reason="Challenge winner")
            db.add_crown(guild.id, uid, original)
        except (discord.Forbidden, discord.HTTPException):
            skipped.append(f"<@{uid}>")
    if skipped:
        await ann.send("I couldn't add the crown for: " + ", ".join(skipped)
                       + ". (I need Manage Nicknames, and my role has to be above theirs.)")


@scheduler.before_loop
async def before_scheduler():
    await bot.wait_until_ready()


@bot.event
async def setup_hook():
    await bot.tree.sync()
    scheduler.start()


@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)
    await bring_servers_up_to_date()


def mission_bank_is_outdated(guild_id: int) -> bool:
    """Banks loaded before the 1-point scoring have missions worth more than the new max of 0.5."""
    return any(m["points"] > 0.5 for m in db.missions(guild_id))


async def bring_servers_up_to_date():
    """After a deploy: reload an old-scale mission bank, and refresh open sign-up posts so they show
    the current rules."""
    for guild_id in db.all_guild_ids():
        guild = bot.get_guild(guild_id)
        if not guild:
            continue
        try:
            if mission_bank_is_outdated(guild_id):
                with open(BUILT_IN_MISSIONS, encoding="utf-8") as f:
                    n = db.import_missions(guild_id, f.read())
                log.info("reloaded %s missions for %s (old point scale)", n, guild.name)
            ch = db.current_challenge(guild_id)
            if ch and ch.signup_message_id:
                await refresh_signup(guild, ch, closed=ch.status != "signup")
        except Exception:
            log.exception("couldn't bring %s up to date", guild_id)


def run():
    logging.basicConfig(level=logging.INFO)
    if not config.DISCORD_TOKEN:
        raise SystemExit("Set DISCORD_TOKEN (see README).")
    bot.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    run()

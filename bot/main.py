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


BUILT_IN_MISSIONS = os.path.join(os.path.dirname(__file__), "..", "data", "missions.csv")


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


def mentions(user_ids, limit: int = 1024, sep: str = " ") -> str:
    """@mentions joined up to `limit` characters, ending in "+N more" instead of cutting a mention in half."""
    ids = list(user_ids)
    out = ""
    for i, uid in enumerate(ids):
        piece = (sep if out else "") + f"<@{uid}>"
        more = f" +{len(ids) - i} more"
        if len(out) + len(piece) + (len(more) if i < len(ids) - 1 else 0) > limit:
            return out + more
        out += piece
    return out


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


def undo_view(goal_id: int, day: date) -> discord.ui.View:
    return make_view([discord.ui.Button(label="Undo", emoji="↩️", style=discord.ButtonStyle.secondary,
                                        custom_id=f"mb:undo:{goal_id}:{day.isoformat()}")])


def checkin_reply(result: str, goal: Goal, day: date) -> dict:
    """A check-in reply, with an Undo button when something was actually logged."""
    return {"content": result, **({"view": undo_view(goal.id, day)} if result.startswith("Logged") else {})}


def undo_checkin(ch: Challenge, user_id: int, goal: Goal, day: date) -> str:
    """Players can take back their own check-in until that week locks."""
    if goal.user_id != user_id:
        return "That goal isn't yours."
    ok, why = game.checkin_allowed(ch, day, game.now_local())
    if not ok and "locked" in why:
        return "That week is locked, so it can't be changed. Ask the commissioner."
    if not db.remove_checkin(goal.id, day):
        return f"**{goal.name}** wasn't logged for {pretty(day)}, so there's nothing to undo."
    week = week_for(day, challenge_weeks(ch.start, ch.end))
    done = len(db.checkin_days(goal.id, week.start, week.end))
    return f"↩️ Removed **{goal.name}** for {pretty(day)}.  {progress_bar(done, scaled_target(goal.target_days, week.days))}"


# ---------- setup ----------

@bot.tree.command(description="Admins: set the channels the bot uses.")
@app_commands.describe(announce_channel="Where sign-ups, daily roundups and standings go",
                       proof_channel="Where people post photos",
                       mission_channel="Where the daily mini mission is announced (optional; default: announce channel)",
                       commissioner_role="Role that can run the challenge (optional)")
@app_commands.default_permissions(manage_guild=True)
async def setup(inter: discord.Interaction, announce_channel: discord.TextChannel,
                proof_channel: discord.TextChannel, mission_channel: discord.TextChannel | None = None,
                commissioner_role: discord.Role | None = None):
    first_setup = db.settings(inter.guild_id) is None
    db.set_settings(inter.guild_id, announce_channel.id, proof_channel.id,
                    commissioner_role.id if commissioner_role else None,
                    mission_channel.id if mission_channel else None)
    has_missions = len(db.missions(inter.guild_id))
    if not has_missions:
        with open(BUILT_IN_MISSIONS, encoding="utf-8") as f:
            has_missions = db.import_missions(inter.guild_id, f.read())
    if first_setup:
        await notify_owner(f"⚙️ **{inter.guild.name}** ran `/setup`. {len(db.all_guild_ids())} servers set up so far.")
    missions_in = mission_channel or announce_channel
    await inter.response.send_message(
        f"Set. Sign-ups and roundups in {announce_channel.mention}, proof in {proof_channel.mention}, "
        f"mini missions in {missions_in.mention}"
        + (f", commissioners: {commissioner_role.mention}" if commissioner_role else "")
        + f". {has_missions} mini missions loaded.", ephemeral=True)


def mission_channel(guild: discord.Guild) -> discord.TextChannel | None:
    return channel(guild, "mission_channel_id") or channel(guild, "announce_channel_id")


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
    db.mark_job(ch.id, "signup_reminder", today.isoformat())  # the post itself is today's reminder
    await notify_owner(f"🏁 **{inter.guild.name}** opened **{ch.name}** ({pretty(s)} – {pretty(e)}).")
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


def signup_reminder_text(guild: discord.Guild, ch: Challenge, today: date) -> str:
    """Daily nudge while sign-ups are open: countdown, a link to the sign-up post, and who's in."""
    tz = tz_label()
    last_day = ch.signup_deadline or (ch.start - timedelta(days=1))
    days_left = (last_day - today).days
    if days_left <= 0:
        when = f"⏰ **Last day to sign up for {ch.name}!** Sign-ups close tonight at 11:59 p.m. {tz}."
    elif days_left == 1:
        when = f"⏰ **One more day to sign up for {ch.name}!** Sign-ups close tomorrow night ({pretty(last_day)})."
    else:
        when = f"⏰ **{days_left} days left to sign up for {ch.name}!** Sign-ups close {pretty(last_day)} at 11:59 p.m. {tz}."
    link = (f"https://discord.com/channels/{guild.id}/{ch.signup_channel_id}/{ch.signup_message_id}"
            if ch.signup_message_id else None)
    players = db.players(ch.id)
    lines = [when,
             f"👉 Tap **I'm in** on the sign-up post{f': {link}' if link else ''}",
             f"👯‍♀️ **{len(players)} signed up so far**"
             + (": " + mentions((p["user_id"] for p in players), 900) if players else ". Be the first! 👑")]
    missing = [p["user_id"] for p in players if len(db.goals(ch.id, p["user_id"])) < 3]
    if missing:
        lines.append(f"📝 Still need to set goals by {pretty(last_day)} (tap **I'm in** again, then **Add my goals**): "
                     + mentions(missing, 600))
    lines.append(f"The challenge starts {pretty(ch.start)}. 💪🏾")
    return "\n".join(lines)


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
    e.add_field(name="💪🏾 Teams or solo", inline=False, value=(
        "The day before we start, everyone who's signed up **votes: teams or individual**, and the commissioner "
        f"makes the final call. If it's teams, you're **split at random** into **{' vs '.join(config.TEAM_NAMES)}**."
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
        f"A new one posts every morning at {hour_label(config.MISSION_POST_HOUR)} {tz}"
        + (f" in {mission_channel(guild).mention}. " if mission_channel(guild) else ". ")
        + 
        "Worth **0.25** (easy) or **0.5** (harder) points, and you have 24 hours to do it. "
        + (f"Up to **{fmt(config.MISSION_WEEKLY_CAP)} points a week**. " if config.MISSION_WEEKLY_CAP > 0
           else "Do them every day to stack up points! ")
        + "They don't count toward a perfect week."))
    e.add_field(name="🍑 Weekly schedule", inline=False, value=(
        f"Weeks run **Saturday–Friday**. You can log any day of the current week until **Friday at midnight {tz}**, "
        f"then that week locks. Every morning at {hour_label(config.ROUNDUP_HOUR)} {tz}, a **daily roundup** "
        "shows the leaderboard and everyone's ✅ progress on each goal. Saturday's roundup is the week's final standings. "
        "Short weeks at the start or end of the challenge shrink your targets to fit."))
    crown = (f" and a {config.WINNER_EMOJI} / {config.TOP_SCORER_EMOJI} on their name until the next challenge"
             if config.CROWN_NICKNAMES else "")
    e.add_field(name="🏆 Prizes", inline=False, value=(
        f"**Winners** get the **{config.WINNER_ROLE_NAME}** role. **Top scorer** gets **{config.TOP_SCORER_ROLE_NAME}**"
        f"{crown}. Ties share the win. Bragging rights included. 👑"))
    players = db.players(ch.id)
    e.add_field(name=f"👯‍♀️ Signed up ({len(players)})", inline=False,
                value=mentions(p["user_id"] for p in players) or "Nobody yet. Be the first!")
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


async def lock_signups(guild: discord.Guild, ch: Challenge, mode: str = "auto",
                       today: date | None = None) -> discord.Embed:
    """Close sign-ups: remove players without 3 goals, approve the rest, pick the mode and make teams.
    Runs from /challenge lock, or by itself at the deadline."""
    lines = []
    gone = []
    for p in db.players(ch.id):
        try:
            guild.get_member(p["user_id"]) or await guild.fetch_member(p["user_id"])
        except discord.NotFound:
            gone.append(p["user_id"])
        except discord.HTTPException:
            pass
    if gone and len(db.players(ch.id)) - len(gone) >= 2:
        for uid in gone:
            db.leave(ch.id, uid)
        lines.append("Removed (left the server): " + mentions(gone, 900))
    everyone = [p["user_id"] for p in db.players(ch.id)]
    no_goals = [u for u in everyone if len(db.goals(ch.id, u)) < 3]
    if config.REMOVE_WITHOUT_GOALS and no_goals and len(everyone) - len(no_goals) >= 2:
        for uid in no_goals:
            db.leave(ch.id, uid)
            await dm(uid, f"😢 You were removed from **{ch.name}** in {guild.name} because your 3 goals weren't in "
                          "by the deadline. We'd love to have you next time! Watch for the next sign-up post. 💕")
        lines.append("Removed (no goals by the deadline): " + mentions(no_goals, 900))
        no_goals = []
    players = [p["user_id"] for p in db.players(ch.id)]
    if config.AUTO_APPROVE_AT_LOCK:
        db.approve_goals(ch.id)
    if mode in ("teams", "individual"):
        db.update_challenge(ch.id, mode_pick=mode)
    db.update_challenge(ch.id, status="ready")
    ch = db.get_challenge(ch.id)
    chosen = None
    if ch.mode:  # already settled by the vote or the commissioner: keep the teams
        chosen = ch.mode
        lines.append(mode_summary(ch))
    elif mode in ("teams", "individual") or (today or game.now_local().date()) >= ch.start:
        chosen, mode_lines = await finalize_mode(guild, ch)
        lines += mode_lines
    else:
        lines.append(f"🗳️ Teams or individual gets decided the day before we start: watch for the vote on "
                     f"{pretty(game.mode_vote_day(ch))}.")
    if no_goals:
        lines.append("Still need goals (`/goals set`): " + " ".join(f"<@{u}>" for u in no_goals))
    unapproved = [u for u in players if any(not g.approved for g in db.goals(ch.id, u))]
    if unapproved:
        lines.append("Commissioner, these goals need approval (`/goals all`): " + " ".join(f"<@{u}>" for u in unapproved))
    lines.append(f"The challenge starts **{pretty(ch.start)}**.")
    await refresh_signup(guild, ch, closed=True)
    title = f"Sign-ups closed: {len(players)} players" + (f", {chosen} mode" if chosen else "")
    return discord.Embed(title=title, description="\n\n".join(lines), color=PINK)


async def finalize_mode(guild: discord.Guild, ch: Challenge,
                        decided: tuple[str, str] | None = None) -> tuple[str, list[str]]:
    """Settle team or solo and make random teams. `decided` is (mode, why) when the vote or the commissioner
    already settled it; otherwise: the commissioner's call, else the vote, else the player count."""
    players = [p["user_id"] for p in db.players(ch.id)]
    chosen, why = decided or game.final_mode(db, ch, len(players))
    db.update_challenge(ch.id, mode=chosen)
    lines = [f"It's **{'team' if chosen == 'teams' else 'individual'} mode**, by {why}."]
    if chosen == "teams":
        teams = game.assign_teams(players)
        for uid, team in teams.items():
            db.set_team(ch.id, uid, team)
        await sync_team_roles(guild, teams)
        lines[0] += " Teams were picked at random. 🎲"
        for team in config.TEAM_NAMES:
            members = " ".join(f"<@{u}>" for u, t in teams.items() if t == team)
            lines.append(f"**{team}**\n{members}")
    else:
        lines.append("Everyone plays for herself. Top scorer takes the crown. 👑")
    await refresh_vote(guild, db.get_challenge(ch.id))
    return chosen, lines


def mode_summary(ch: Challenge) -> str:
    if ch.mode != "teams":
        return "It's **individual mode**. Everyone plays for herself. Top scorer takes the crown. 👑"
    teams = db.teams(ch.id)
    blocks = [f"**{t}** ({sum(1 for v in teams.values() if v == t)})\n"
              + (" ".join(f"<@{u}>" for u, v in teams.items() if v == t) or "—") for t in config.TEAM_NAMES]
    return "It's **team mode**. 🎲\n\n" + "\n\n".join(blocks)


async def add_to_smaller_team(guild: discord.Guild, ch: Challenge, uid: int):
    """Someone joins after teams were picked: she goes on the smaller team (random if they're even)."""
    teams = db.teams(ch.id)
    sizes = {t: sum(1 for v in teams.values() if v == t) for t in config.TEAM_NAMES}
    low = min(sizes.values())
    team = game.random.choice([t for t, n in sizes.items() if n == low])
    db.set_team(ch.id, uid, team)
    await sync_team_roles(guild, {uid: team})
    return team


def hours_until_start(ch: Challenge, now: datetime) -> int:
    start = datetime.combine(ch.start, datetime.min.time(), tzinfo=config.TZ)
    return max(0, -(-int((start - now).total_seconds()) // 3600))  # round up


async def decide_now(guild: discord.Guild, ch: Challenge, mode: str, why: str):
    """The vote has a majority (or the commissioner called it): settle it, pick teams, and post the roundup."""
    chosen, lines = await finalize_mode(guild, ch, (mode, why))
    ch = db.get_challenge(ch.id)
    hours = hours_until_start(ch, game.now_local())
    when = (f"⏳ **{ch.name} starts in {hours} hour{'s' if hours != 1 else ''}** "
            f"(midnight, {pretty(ch.start)}). Get those goals ready! 🔥")
    if config.REMOVE_WITHOUT_GOALS and ch.status == "signup":
        missing = [p["user_id"] for p in db.players(ch.id) if len(db.goals(ch.id, p["user_id"])) < 3]
        if missing:
            lines.append("📝 Still need 3 goals by midnight or you're out: " + mentions(missing, 600))
    if chosen == "teams" and ch.status == "signup":
        lines.append("Anyone who joins from here goes on the smaller team.")
    e = discord.Embed(title=f"🗳️ The votes are in: it's {mode_name(chosen).lower()}!", color=PINK,
                      description=when + "\n\n" + "\n\n".join(lines))
    ann = channel(guild, "announce_channel_id")
    if ann:
        await ann.send(embed=e)


# ---------- teams or solo: the day-before vote ----------

def mode_name(mode: str) -> str:
    return "Teams" if mode == "teams" else "Individual"


def vote_embed(ch: Challenge) -> discord.Embed:
    """The day-before post: who's signed up, the live vote, and who makes the call."""
    players = db.players(ch.id)
    t, i = game.vote_tally(db, ch)
    commish = f"<@{ch.created_by}>" if ch.created_by else "The commissioner"
    when = (f"tomorrow ({pretty(ch.start)})" if game.now_local().date() == game.mode_vote_day(ch)
            else pretty(ch.start))
    e = discord.Embed(title=f"🗳️ {ch.name}: teams or individual?", color=PINK, description=(
        f"Okay ladies, we start **{when}** and we have **{len(players)} signed up**! 🎉\n\n"
        "Vote below: **Teams** or **Individual**. As soon as more than half of the players pick one, it's decided.\n"
        f"🎲 If it's teams, everyone is **split at random** into **{' vs '.join(config.TEAM_NAMES)}**."))
    if ch.mode:
        e.add_field(name="✅ Decided", inline=False, value=f"**{mode_name(ch.mode)}**. Voting is closed.")
    e.add_field(name="📊 The vote so far", inline=False, value=f"💪🏾 Teams: **{t}**\n👑 Individual: **{i}**")
    e.add_field(name=f"👯‍♀️ Signed up ({len(players)})", inline=False,
                value=mentions(p["user_id"] for p in players) or "Nobody yet")
    missing = [p["user_id"] for p in players if len(db.goals(ch.id, p["user_id"])) < 3]
    if missing and config.REMOVE_WITHOUT_GOALS and ch.status == "signup":
        e.add_field(name="📝 Still need goals by midnight", inline=False, value=mentions(missing, 1000))
    if not ch.mode:
        e.add_field(name="👩‍⚖️ Who decides", inline=False, value=(
            (f"{commish} picked **{mode_name(ch.mode_pick)}**. "
             if ch.mode_pick else f"{commish} makes the final call tonight. ")
            + "If nothing has a majority by midnight before day one, the side with more votes wins."))
    return e


def vote_view(ch: Challenge) -> discord.ui.View:
    return make_view([
        discord.ui.Button(label="Teams", emoji="💪🏾", style=discord.ButtonStyle.primary,
                          custom_id=f"mb:vote:{ch.id}:teams"),
        discord.ui.Button(label="Individual", emoji="👑", style=discord.ButtonStyle.primary,
                          custom_id=f"mb:vote:{ch.id}:individual"),
    ])


def pick_view(ch: Challenge) -> discord.ui.View:
    return make_view([
        discord.ui.Button(label="Make it teams", emoji="💪🏾", style=discord.ButtonStyle.success,
                          custom_id=f"mb:pick:{ch.id}:teams"),
        discord.ui.Button(label="Make it individual", emoji="👑", style=discord.ButtonStyle.success,
                          custom_id=f"mb:pick:{ch.id}:individual"),
    ])


def pick_prompt(ch: Challenge) -> str:
    t, i = game.vote_tally(db, ch)
    n = len(db.players(ch.id))
    return (f"👩‍⚖️ **You're the commissioner for {ch.name}.** {n} signed up, and the vote is **{t} teams, "
            f"{i} individual**. Make the final call by midnight with a button below, or `/challenge mode` in the "
            "server. A majority vote settles it on the spot. If neither side has a majority by midnight, the side with "
            "more votes wins (a tie or no votes goes by the player count).")


async def post_mode_vote(guild: discord.Guild, ch: Challenge, ann: discord.TextChannel | None):
    if ann:
        msg = await ann.send(embed=vote_embed(ch), view=vote_view(ch), allowed_mentions=discord.AllowedMentions.none())
        db.update_challenge(ch.id, vote_channel_id=ann.id, vote_message_id=msg.id)
    await nudge_commissioner(guild, ch, ann)


async def nudge_commissioner(guild: discord.Guild, ch: Challenge, ann: discord.TextChannel | None):
    """DM the commissioner the tally with pick buttons; fall back to a ping in the channel."""
    if not ch.created_by:
        return
    if not await dm(ch.created_by, pick_prompt(ch), pick_view(ch)) and ann:
        await ann.send(f"<@{ch.created_by}> " + pick_prompt(ch), view=pick_view(ch))


async def refresh_vote(guild: discord.Guild, ch: Challenge):
    if not ch.vote_message_id:
        return
    try:
        msg = await guild.get_channel(ch.vote_channel_id).fetch_message(ch.vote_message_id)
        await msg.edit(embed=vote_embed(ch), view=None if ch.mode else vote_view(ch))
    except Exception:
        log.warning("couldn't update the vote post", exc_info=True)


async def handle_vote(inter: discord.Interaction, challenge_id: int, choice: str):
    ch = db.get_challenge(challenge_id)
    if not ch or ch.mode or ch.status not in ("signup", "ready"):
        return await deny(inter, "Voting is closed. 💕")
    if not db.is_player(ch.id, inter.user.id):
        return await deny(inter, "Only players who signed up can vote. Tap **I'm in** on the sign-up post first!")
    db.set_vote(ch.id, inter.user.id, choice)
    t, i = game.vote_tally(db, ch)
    n = len(db.players(ch.id))
    winner = "teams" if t * 2 > n else "individual" if i * 2 > n else None
    if winner:
        await inter.response.send_message(f"Got it: you voted **{mode_name(choice)}**, and that's a majority! "
                                          f"It's **{mode_name(winner)}**.", ephemeral=True)
        await decide_now(inter.guild, ch, winner, f"majority vote ({t} teams, {i} individual, of {n} players)")
        return
    await inter.response.send_message(f"Got it: you voted **{mode_name(choice)}**. Tap the other button to change it.",
                                      ephemeral=True)
    await refresh_vote(inter.guild, ch)


def can_pick(inter: discord.Interaction, ch: Challenge) -> bool:
    return inter.user.id == ch.created_by or (inter.guild is not None and is_commish(inter))


async def set_pick(guild: discord.Guild, ch: Challenge, mode: str, by: int) -> str:
    db.update_challenge(ch.id, mode_pick=mode)
    await decide_now(guild, db.get_challenge(ch.id), mode, "the commissioner's call")
    return f"Done: **{mode_name(mode)}**." + (" Teams are picked and posted." if mode == "teams" else "")


async def handle_pick(inter: discord.Interaction, challenge_id: int, mode: str):
    ch = db.get_challenge(challenge_id)
    if not ch or ch.mode or ch.status not in ("signup", "ready"):
        return await deny(inter, "It's already decided. 💕")
    if not can_pick(inter, ch):
        return await deny(inter, "Only the commissioner can make the call. Vote with the buttons on the vote post!")
    guild = inter.guild or bot.get_guild(ch.guild_id)
    msg = await set_pick(guild, ch, mode, inter.user.id)
    await inter.response.send_message(msg, ephemeral=inter.guild is not None)


@challenge_group.command(name="vote", description="Commissioner: post the teams-or-individual vote now.")
async def challenge_vote(inter: discord.Interaction):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    ch = db.current_challenge(inter.guild_id)
    if not ch or ch.status not in ("signup", "ready"):
        return await deny(inter, "There's no challenge waiting to start.")
    if ch.mode:
        return await deny(inter, f"It's already decided: **{mode_name(ch.mode)}**.")
    ann = channel(inter.guild, "announce_channel_id")
    if not ann:
        return await deny(inter, "Run `/setup` first.")
    await inter.response.defer(ephemeral=True)
    db.mark_job(ch.id, "mode_vote", "posted")  # so the noon post doesn't go up again
    await post_mode_vote(inter.guild, ch, ann)
    await inter.followup.send(f"Posted the vote in {ann.mention}. Votes so far carry over.", ephemeral=True)


@challenge_group.command(name="mode", description="Commissioner: make the final call, teams or individual.")
@app_commands.choices(mode=[app_commands.Choice(name="Teams", value="teams"),
                            app_commands.Choice(name="Individual", value="individual")])
async def challenge_mode(inter: discord.Interaction, mode: str):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    ch = db.current_challenge(inter.guild_id)
    if not ch or ch.status not in ("signup", "ready"):
        return await deny(inter, "There's no challenge waiting to start.")
    if ch.mode:
        return await deny(inter, f"It's already set: **{mode_name(ch.mode)}**.")
    await inter.response.send_message(await set_pick(inter.guild, ch, mode, inter.user.id), ephemeral=True)


async def dm(user_id: int, text: str, view: discord.ui.View | None = None) -> bool:
    """DM a player. Returns False if her DMs are closed (or anything else goes wrong)."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(text, view=view) if view else await user.send(text)
        return True
    except Exception:
        log.info("couldn't DM %s", user_id)
        return False


async def sync_team_roles(guild: discord.Guild, teams: dict[int, str]):
    """Give each player her team's role. Skips quietly if the bot can't manage roles."""
    try:
        roles = {}
        for name in config.TEAM_NAMES:
            roles[name] = discord.utils.get(guild.roles, name=name) or await guild.create_role(name=name, mentionable=True)
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning("team roles skipped: %s", e)
        return
    for uid, team in teams.items():
        try:
            member = guild.get_member(uid) or await guild.fetch_member(uid)
            await member.add_roles(roles[team], reason="Challenge teams")
        except discord.Forbidden as e:
            log.warning("team roles skipped: %s", e)
            return
        except discord.HTTPException:
            log.info("couldn't give %s a team role (left the server?)", uid)


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
                                      + (f"\nCommissioner's call: {ch.mode_pick}" if ch.mode_pick and not ch.mode else "")
                                      + ("\nVote: {} teams, {} individual".format(*game.vote_tally(db, ch))
                                         if not ch.mode and db.job_done(ch.id, "mode_vote", "posted") else "")
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


def mode_line(ch: Challenge) -> str:
    """Team or solo: final once sign-ups close, otherwise where it's headed based on who has goals."""
    if ch.mode == "teams":
        return f"💪🏾 It's **team mode**: **{' vs '.join(config.TEAM_NAMES)}**. Teams are listed below."
    if ch.mode == "individual":
        return "👑 It's **individual mode**: everyone plays for herself."
    ready = sum(1 for p in db.players(ch.id) if len(db.goals(ch.id, p["user_id"])) >= 3)
    removal = " Anyone without 3 goals by midnight is removed." if config.REMOVE_WITHOUT_GOALS else ""
    count = f"👯‍♀️ **{ready} player{'s' if ready != 1 else ''} with goals so far.**"
    if ch.mode_pick:
        return (f"{count} The commissioner picked **{'team' if ch.mode_pick == 'teams' else 'individual'} mode**"
                + (", and teams get picked at random at midnight." if ch.mode_pick == "teams" else ".") + removal)
    if db.job_done(ch.id, "mode_vote", "posted"):
        t, i = game.vote_tally(db, ch)
        return (f"{count} 🗳️ **Teams or individual?** Vote on the vote post. So far: **{t} teams, {i} individual**. "
                "A majority decides it on the spot (or the commissioner's call); if it's teams, they're picked at random."
                + removal)
    return (f"{count} 🗳️ On {pretty(game.mode_vote_day(ch))}, everyone votes **teams or individual** and the "
            "commissioner makes the final call. If it's teams, they're picked at random.")


def commands_embed() -> discord.Embed:
    return discord.Embed(title="⌨️ Commands to know", color=GOLD, description=(
        "`/goals set` · fill in or edit your 3 goals (before the start)\n"
        "`/goals view` · see your goals, or someone else's\n"
        "`/checkin` · log a goal, for today or an earlier day this week\n"
        "`/undo-checkin` · take back a check-in you logged by mistake\n"
        "`/mission today` · see today's mini mission\n"
        "`/mission done` · mark the mini mission done\n"
        "`/mystats` · your week so far and your total\n"
        "`/leaderboard` · everyone's standings\n"
        "`/feedback` · send the organizer a private note\n"
        "`/howtoplay` · the rules in one message"))


def proof_feedback_embed(guild: discord.Guild) -> discord.Embed:
    """How to check in, how to see your progress, and how to send feedback."""
    proof = channel(guild, "proof_channel_id")
    missions = mission_channel(guild)
    where = proof.mention if proof else "the proof channel"
    photo = {"required": "**A photo is required** for every check-in.",
             "per_goal": "Some goals need a photo; the commissioner will tell you which.",
             }.get(config.PHOTO_MODE, "Photos are **encouraged, not required**.")
    e = discord.Embed(title="✅ How to check in and track your progress", color=PINK, description=(
        "A check-in is how you tell the bot you did a goal that day. Each one counts toward that goal's "
        f"weekly target. {photo}"))
    e.add_field(name="📸 Post a photo (easiest)", inline=False, value=(
        f"Post a photo in {where}. I'll reply with a button for each of your goals. **Tap the goal it's for** "
        "and that photo is your check-in for that goal today. One photo can cover more than one goal: tap each "
        "one it counts for. Tapped the wrong one? Hit **↩️ Undo** on the reply, or use `/undo-checkin`."))
    e.add_field(name="⌨️ Use /checkin", inline=False, value=(
        "`/checkin` → pick the goal → pick the day (today or an earlier day this week). You can attach a photo, "
        "or skip it. Good for catching up on a day you forgot."))
    e.add_field(name=f"💌 Tap the {hour_label(config.REMINDER_HOUR)} reminder", inline=False, value=(
        "If you haven't checked in by then, I'll DM you a button for each goal you still need. Tap the ones "
        "you did."))
    e.add_field(name="⚡ Mini missions", inline=False, value=(
        f"Post a photo in {missions.mention if missions else where} and tap **Today's mini mission**, "
        "or use `/mission done`. You have 24 hours from when it posts."))
    e.add_field(name="⏳ Deadlines", inline=False, value=(
        "Check in anytime that day. Forgot? You can log any day of the current week until **Friday at midnight** "
        f"{tz_label()}. After that the week locks; ask the commissioner if something was missed. "
        f"Night owls: a photo posted before {hour_label(config.LATE_NIGHT_GRACE_HOUR)} can count for the day before."))
    e.add_field(name="📊 See your progress", inline=False, value=(
        "`/mystats` · your week so far, goal by goal, and your total (only you see it)\n"
        f"**Morning roundup** at {hour_label(config.ROUNDUP_HOUR)} · everyone's ✅ for the week plus the leaderboard\n"
        "`/leaderboard` · the standings anytime\n"
        "Every check-in reply also shows your ✅⬜ for that goal this week."))
    e.add_field(name="💬 Feedback about the challenge", inline=False, value=(
        "Something confusing, a bug, or an idea? Use `/feedback` to send a private note to the organizer. "
        "You can stay anonymous."))
    return e


def kickoff_embeds(guild: discord.Guild, ch: Challenge) -> list[discord.Embed]:
    """Day one, first thing: it's on, here are the teams, here's what to do today."""
    missions = mission_channel(guild)
    tz = tz_label()
    e = discord.Embed(title=f"🎉 {ch.name} is officially ON!", color=PINK, description=(
        f"🗓️ **{pretty(ch.start)} – {pretty(ch.end)}** · Let's get it! 🔥\n\n{mode_line(ch)}"))
    teams = db.teams(ch.id)
    if ch.mode == "teams" and teams:
        for team in config.TEAM_NAMES:
            members = mentions(u for u, t in teams.items() if t == team)
            e.add_field(name=team, value=members or "—", inline=True)
    else:
        e.add_field(name=f"Players ({len(db.players(ch.id))})", inline=False,
                    value=mentions(p["user_id"] for p in db.players(ch.id)) or "—")
    e.add_field(name="✅ Your day one", inline=False, value=(
        "1. **Do your goals** and check in: post a photo in the proof channel and tap the goal.\n"
        "2. **Do today's mini mission**, posting right after this"
        + (f" in {missions.mention}" if missions else "") + ".\n"
        f"3. Haven't checked in by **{hour_label(config.REMINDER_HOUR)} {tz}**? I'll DM you a reminder.\n"
        f"4. Tomorrow at **{hour_label(config.ROUNDUP_HOUR)} {tz}**: your first morning roundup."))
    e.add_field(name="💋 Points in one line", inline=False, value=(
        f"Up to 1 point per goal each week, +{fmt(config.PERFECT_WEEK_BONUS)} for a perfect week, plus mini mission "
        "bonus points. `/howtoplay` has the full rules."))
    return [e, proof_feedback_embed(guild), commands_embed()]


def overview_embeds(guild: discord.Guild, ch: Challenge) -> list[discord.Embed]:
    """Everything a player needs before the start: how it works, points, reminders, commands, and who's in."""
    tz = tz_label()
    proof = channel(guild, "proof_channel_id")
    missions = mission_channel(guild)
    proof_name = proof.mention if proof else "the proof channel"
    bonus = fmt(config.PERFECT_WEEK_BONUS)
    starts = ("**We start tomorrow!** " if game.now_local().date() == ch.start - timedelta(days=1) else "")
    intro = discord.Embed(title=f"📣 {ch.name}: what to expect", color=PINK, description=(
        f"🗓️ **{pretty(ch.start)} – {pretty(ch.end)}**\n{mode_line(ch)}\n\n"
        f"{starts}Here's everything you need. Save this post! 💕"))
    intro.add_field(name="🤖 How the bot works", inline=False, value=(
        "**Set 3 goals** before the start: tap **I'm in** on the sign-up post, then **Add my goals**.\n"
        f"**Check in** by posting a photo in {proof_name} and tapping the goal it's for, or use `/checkin`.\n"
        "**Do the mini mission** each day for bonus points.\n"
        "**Watch the roundup** every morning to see the leaderboard and who's on pace.\n"
        f"**Win** the challenge for the winner role and a {config.WINNER_EMOJI} on your name until next month."))

    points = discord.Embed(title="💋 How points work", color=PINK)
    points.add_field(name="Goals", inline=False, value=(
        "Each goal is worth **up to 1 point a week**. Hit your target for the full point. "
        "Fall short and you get the share you hit (2 of 3 workouts = 0.67). Extra days don't add more."))
    points.add_field(name="Perfect week", inline=False, value=(
        f"Hit all 3 goals: **+{bonus} bonus point{'' if config.PERFECT_WEEK_BONUS == 1 else 's'}**."
        + (f" If your whole team is perfect, everyone gets **+{fmt(config.TEAM_BONUS_PER_PLAYER)}** more."
           if ch.mode == "teams" else "")))
    cap = (f" Up to {fmt(config.MISSION_WEEKLY_CAP)} points a week." if config.MISSION_WEEKLY_CAP > 0 else " No cap.")
    points.add_field(name="Mini missions", inline=False, value=(
        f"**0.25** (easy) or **0.5** (harder) each.{cap} They don't count toward a perfect week."))

    reminders = discord.Embed(title="⏰ Reminders and the daily schedule", color=GOLD, description=(
        f"All times are {tz}.\n\n"
        f"**{hour_label(config.MISSION_POST_HOUR)}** · Today's mini mission posts"
        + (f" in {missions.mention}" if missions else "") + ". You have 24 hours.\n"
        f"**{hour_label(config.ROUNDUP_HOUR)}** · The morning roundup: leaderboard plus everyone's ✅ progress.\n"
        f"**{hour_label(config.REMINDER_HOUR)}** · If you haven't checked in that day, I'll DM you a reminder "
        "with a button for each goal.\n"
        "**Friday at midnight** · The week locks. You can log any day of the current week until then.\n"
        "**Saturday morning** · The roundup is the week's final standings.\n\n"
        "📩 Not getting DMs? Right-click (or long-press) the server → **Privacy Settings** → turn on "
        "**Direct Messages**."))

    commands = commands_embed()
    proof_feedback = proof_feedback_embed(guild)


    rows, missing = [], []
    for p in db.players(ch.id):
        uid = p["user_id"]
        gs = db.goals(ch.id, uid)
        if not gs:
            missing.append(f"<@{uid}>")
            continue
        team = f" ({p['team']})" if p["team"] else ""
        rows.append(f"**<@{uid}>**{team}: " + " · ".join(format_goal(g.name, g.target_days) for g in gs))
    who = "\n".join(rows) or "Nobody has set goals yet."
    if missing:
        who += "\n\n**Still need goals:** " + " ".join(missing) + " (tap **I'm in** on the sign-up post, then **Add my goals**)"
    players = discord.Embed(title=f"👯‍♀️ Who's playing ({len(db.players(ch.id))})", color=PINK)
    chunks, current = [], ""
    for line in who.split("\n"):
        if len(current) + len(line) + 1 > 4000:
            chunks.append(current)
            current = ""
        current += ("\n" if current else "") + line
    chunks.append(current)
    players.description = chunks[0]
    extra = [discord.Embed(description=c, color=PINK) for c in chunks[1:]]
    return [intro, proof_feedback, points, reminders, commands, players, *extra]


@challenge_group.command(name="overview", description="Commissioner: send players a what-to-expect overview.")
@app_commands.describe(send_to="Post it in the announce channel, or DM it to every player")
@app_commands.choices(send_to=[app_commands.Choice(name="Announce channel", value="channel"),
                               app_commands.Choice(name="DM every player", value="dm")])
async def challenge_overview(inter: discord.Interaction, send_to: str = "channel"):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    ch = db.current_challenge(inter.guild_id)
    if not ch:
        return await deny(inter, "No challenge running right now.")
    await inter.response.defer(ephemeral=True)
    pages = message_pages(overview_embeds(inter.guild, ch))
    if send_to == "channel":
        ann = channel(inter.guild, "announce_channel_id")
        if not ann:
            return await inter.followup.send("Run `/setup` first.", ephemeral=True)
        for page in pages:
            await ann.send(embeds=page, allowed_mentions=discord.AllowedMentions.none())
        return await inter.followup.send(f"Posted the overview in {ann.mention}.", ephemeral=True)
    sent, failed = 0, []
    for p in db.players(ch.id):
        try:
            user = bot.get_user(p["user_id"]) or await bot.fetch_user(p["user_id"])
            for page in pages:
                await user.send(embeds=page)
            sent += 1
        except (discord.Forbidden, discord.HTTPException):
            failed.append(f"<@{p['user_id']}>")
    msg = f"DM'd the overview to {sent} player{'s' if sent != 1 else ''}."
    if failed:
        msg += (" Couldn't reach " + " ".join(failed) + " (DMs off). Post it in the channel too, or ask them to "
                "turn on DMs from this server.")
    await inter.followup.send(msg, ephemeral=True)


@challenge_group.command(name="leave", description="Drop out of the current challenge.")
async def challenge_leave(inter: discord.Interaction):
    ch = db.current_challenge(inter.guild_id)
    if not ch or not db.is_player(ch.id, inter.user.id):
        return await deny(inter, "You're not in the current challenge.")
    note = (" Your points so far will stop counting, and you won't be able to rejoin this challenge."
            if ch.status in ("ready", "active") else "")
    button = discord.ui.Button(label="Yes, leave the challenge", style=discord.ButtonStyle.danger,
                               custom_id=f"mb:quit:{ch.id}")
    await inter.response.send_message(f"Are you sure you want to leave **{ch.name}**?{note}",
                                      view=make_view([button]), ephemeral=True)


async def handle_quit(inter: discord.Interaction, challenge_id: int):
    ch = db.get_challenge(challenge_id)
    if not ch or not db.is_player(ch.id, inter.user.id):
        return await deny(inter, "You're not in that challenge anymore.")
    await remove_player(inter.guild, ch, inter.user.id)
    if ch.status == "signup":
        await refresh_signup(inter.guild, ch)
    await notify_owner(f"🚪 <@{inter.user.id}> left **{ch.name}** in **{inter.guild.name}**.")
    await inter.response.send_message("You've left the challenge. We'll miss you! Catch the next one. 💕",
                                      ephemeral=True)


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
    await inter.followup.send(**checkin_reply(msg, g, d), ephemeral=True)


@bot.tree.command(name="undo-checkin", description="Remove one of your own check-ins (until that week locks).")
@app_commands.describe(goal="Which goal", day="The day to remove. Defaults to today.")
@app_commands.autocomplete(goal=goal_autocomplete, day=day_autocomplete)
async def undo_checkin_cmd(inter: discord.Interaction, goal: int, day: str | None = None):
    ch = db.current_challenge(inter.guild_id)
    if not ch or ch.status != "active":
        return await deny(inter, "The challenge isn't running right now.")
    g = next((x for x in db.goals(ch.id, inter.user.id) if x.slot == goal), None)
    if not g:
        return await deny(inter, "Pick one of your goals from the list.")
    d = parse_day(day, game.now_local().date())
    if not d:
        return await deny(inter, "I couldn't read that day.")
    await inter.response.send_message(undo_checkin(ch, inter.user.id, g, d), ephemeral=True)


@bot.event
async def on_message(message: discord.Message):
    """A photo in the proof channel gets a reply with buttons: tap which goal (or mission) it's for."""
    if message.author.bot or not message.guild or not message.attachments:
        return
    s = db.settings(message.guild.id)
    in_proof = s and message.channel.id == s["proof_channel_id"]
    in_missions = s and s["mission_channel_id"] and message.channel.id == s["mission_channel_id"]
    if not (in_proof or in_missions):
        return
    ch = db.current_challenge(message.guild.id)
    if not ch or ch.status != "active" or not db.is_player(ch.id, message.author.id):
        return
    now = game.now_local()
    buttons = []
    if in_proof:
        days = [now.date()]
        if now.hour < config.LATE_NIGHT_GRACE_HOUR:  # after midnight: offer yesterday first
            days.insert(0, now.date() - timedelta(days=1))
        for d in days:
            if not game.checkin_allowed(ch, d, now)[0]:
                continue
            tag = " (yesterday)" if d < now.date() else (" (today)" if len(days) > 1 else "")
            for g in game.open_goals_today(db, ch, message.author.id, d):
                buttons.append(discord.ui.Button(label=(g.name[:60] + tag), emoji="✅",
                                                 style=discord.ButtonStyle.primary,
                                                 custom_id=f"mb:p:{message.author.id}:g:{g.id}:{d.isoformat()}"))
    m = game.open_mission(db, ch, now)
    done = dict(db.mission_completions(ch.id, message.author.id))
    if m:
        mday = date.fromisoformat(m["day"])
        extra = game.mission_bonus(m)
        if mday not in done:
            buttons.append(discord.ui.Button(label="Today's mini mission", emoji="⚡",
                                             style=discord.ButtonStyle.success,
                                             custom_id=f"mb:p:{message.author.id}:m:{m['day']}"))
        if extra and done.get(mday, 0) < extra[0]:
            buttons.append(discord.ui.Button(label="Mini mission + bonus", emoji="🌟",
                                             style=discord.ButtonStyle.success,
                                             custom_id=f"mb:p:{message.author.id}:mb:{m['day']}"))
    if not buttons:
        await message.add_reaction("🔥")
        return
    await message.reply(PROOF_ASK, view=make_view(buttons), mention_author=False)


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
@app_commands.describe(bonus="Did the bonus too? It needs a photo.")
async def mission_done(inter: discord.Interaction, photo: discord.Attachment | None = None, bonus: bool = False):
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
    if bonus and not photo:
        return await inter.followup.send("The bonus needs a photo. Run `/mission done` again with `bonus` on and a "
                                         "photo attached, or post it in the proof channel. 📸", ephemeral=True)
    await inter.followup.send(complete_mission(ch, inter.user.id, proof_url, bonus=bonus), ephemeral=True)


def complete_mission(ch: Challenge, user_id: int, proof_url: str | None, day: date | None = None,
                     bonus: bool = False) -> str:
    now = game.now_local()
    m = game.open_mission(db, ch, now)
    if not m or (day and m["day"] != day.isoformat()):
        return "That mini mission has closed (you get 24 hours from when it posts)."
    left = game.mission_points_left(db, ch, user_id, date.fromisoformat(m["day"]))
    if left <= 0:
        return (f"Nice! You've already hit this week's {fmt(config.MISSION_WEEKLY_CAP)}-point mini mission cap, "
                "so this one's for pride. 💅🏾")
    day_m = date.fromisoformat(m["day"])
    extra = game.mission_bonus(m)
    if bonus and not extra:
        bonus = False  # this mission has no bonus
    pts = extra[0] if bonus else m["points"]
    if not db.complete_mission(ch.id, user_id, day_m, pts, proof_url):
        if bonus and db.upgrade_mission(ch.id, user_id, day_m, pts, proof_url):
            return f"⚡ Bonus done! This mini mission is now worth **{fmt(pts)}** pts total."
        return "You already did this one."
    earned = min(pts, left)
    if bonus:
        return f"⚡ Mini mission + bonus done: **+{fmt(earned)}** pts."
    tip = (f"\nDid the bonus too? Post a photo in the proof channel and tap **Mini mission + bonus** for "
           f"{fmt(extra[0])} total." if extra else "")
    return f"⚡ Mini mission done: **+{fmt(earned)}** pts.{tip}"


async def post_mission(guild: discord.Guild, day: date, m) -> bool:
    """Post the day's mission in the mission channel, or the announce channel if the bot can't post there."""
    tried = []
    for where in (channel(guild, "mission_channel_id"), channel(guild, "announce_channel_id")):
        if not where or where in tried:
            continue
        tried.append(where)
        try:
            await where.send(f"☀️ **Today's mini mission** ({pretty(day)})", embed=mission_embed(m))
            return True
        except discord.HTTPException:
            log.exception("couldn't post the mini mission in #%s; check the bot's permissions there", getattr(where, "name", where.id))
    return False


def mission_embed(m) -> discord.Embed:
    e = discord.Embed(title=f"⚡ {m['theme']}", description=f"**{m['text']}**", color=GOLD)
    extra = game.mission_bonus(m)
    e.add_field(name="Points", value=fmt(m["points"]) + (f" · **{fmt(extra[0])} total with the bonus** (photo)"
                                                          if extra else ""))
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


def roundup_embeds(ch: Challenge, day: date) -> list[discord.Embed]:
    """Yesterday's roundup: the leaderboard, plus each player's progress on each goal this week
    (✅ for each day she hit it, ⬜ for days still to go)."""
    weeks = challenge_weeks(ch.start, ch.end)
    week = week_for(day, weeks)
    s = game.standings(db, ch, week.number)
    week_over = day == week.end
    title = (f"🏆 Week {week.number} final ({pretty(week.start)} – {pretty(week.end)})" if week_over
             else f"☀️ Daily roundup: {pretty(day)}")
    board = standings_embed(ch, week.number, title)
    rows = []
    for uid, pws in s.player_weeks.items():
        pw = pws[-1]
        star = " ✨" if pw.perfect else ""
        goals = "\n".join(f"{progress_bar(g.days_done, g.target)} {g.name[:60]}" for g in pw.goals) or "no goals yet"
        rows.append((pw.points, f"**<@{uid}>** · {fmt(pw.points)} pts{star}\n{goals}"))
    rows.sort(key=lambda r: -r[0])
    title = f"Week {week.number} so far" if not week_over else f"Week {week.number} goals"
    # One block per player (name, then a line per goal), split across embeds so nothing gets cut off.
    chunks, cur = [], "✅ = a day she hit that goal · ⬜ = days still to go this week"
    for _, block in rows:
        if len(cur) + len(block) + 2 > 3900:
            chunks.append(cur)
            cur = block
        else:
            cur += "\n\n" + block
    chunks.append(cur)
    out = [board]
    for i, text in enumerate(chunks):
        e = discord.Embed(title=title if i == 0 else f"{title} (cont.)",
                          description=text if rows else "Nobody's playing yet.", color=GOLD)
        out.append(e)
    out[-1].set_footer(text="Week points include partial credit and the perfect week bonus ✨"
                       + ("" if week_over else ". Late check-ins for this week still count until Friday midnight."))
    return out


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
        + "**Deadlines.** Weeks run Saturday–Friday and lock Friday at midnight. A daily roundup with the "
        "leaderboard posts every morning; Saturday's is the week's final.\n"
        "`/mystats` for your week, `/leaderboard` for everyone. Feedback for the organizer? `/feedback`."
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


@commish.command(name="warn", description="Ping and DM everyone without 3 goals: goals by the deadline or they're removed.")
async def commish_warn(inter: discord.Interaction):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    ch = db.current_challenge(inter.guild_id)
    if not ch or ch.status != "signup":
        return await deny(inter, "There's no challenge taking sign-ups.")
    await inter.response.defer(ephemeral=True)
    deadline = ch.signup_deadline or (ch.start - timedelta(days=1))
    n = await warn_missing_goals(inter.guild, ch, deadline, channel(inter.guild, "announce_channel_id"))
    db.mark_job(ch.id, "goal_warning", game.now_local().date().isoformat())
    await inter.followup.send(f"Warned {n} player{'s' if n != 1 else ''} without goals." if n
                              else "Everyone has their goals in. Nobody to warn! 🎉", ephemeral=True)


async def remove_player(guild: discord.Guild, ch: Challenge, uid: int):
    """Take someone out of the challenge: her goals, check-ins and points stop counting, and team averages
    are recalculated without her."""
    team = next((p["team"] for p in db.players(ch.id) if p["user_id"] == uid), None)
    db.leave(ch.id, uid)
    if team:
        try:
            role = discord.utils.get(guild.roles, name=team)
            member = guild.get_member(uid) or await guild.fetch_member(uid)
            if role and member:
                await member.remove_roles(role, reason="Left the challenge")
        except Exception:
            log.info("couldn't remove team role from %s", uid)


@commish.command(name="remove", description="Remove a player from the challenge (her points stop counting).")
async def commish_remove_player(inter: discord.Interaction, member: discord.Member, confirm: str):
    if not is_commish(inter):
        return await deny(inter, "Only the commissioner can do that.")
    ch = db.current_challenge(inter.guild_id)
    if not ch or not db.is_player(ch.id, member.id):
        return await deny(inter, f"{member.display_name} isn't in the current challenge.")
    if confirm != "REMOVE":
        return await deny(inter, "Nothing changed. Type REMOVE in the confirm box to remove her.")
    await remove_player(inter.guild, ch, member.id)
    await dm(member.id, f"You've been removed from **{ch.name}** in {inter.guild.name} by the commissioner. "
                        "Reach out to her if you have questions. 💕")
    await inter.response.send_message(f"Removed {member.mention} from {ch.name}. Her points no longer count.",
                                      ephemeral=True)


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
        elif kind == "vote":
            await handle_vote(inter, int(parts[2]), parts[3])
        elif kind == "pick":
            await handle_pick(inter, int(parts[2]), parts[3])
        elif kind == "who":
            await handle_who(inter, int(parts[2]))
        elif kind == "quit":
            await handle_quit(inter, int(parts[2]))
        elif kind == "approveall":
            await handle_approve_all(inter, int(parts[2]))
        elif kind in ("goals", "addgoal"):
            await handle_goals_button(inter, int(parts[2]))
        elif kind == "p":
            await handle_proof_button(inter, int(parts[2]), parts[3], parts[4], parts[5] if len(parts) > 5 else None)
        elif kind == "undo":
            await handle_undo(inter, int(parts[2]), date.fromisoformat(parts[3]))
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
            if ch.mode == "teams":
                team = await add_to_smaller_team(inter.guild, ch, inter.user.id)
                msg += f"\nTeams are already picked, so you're on **{team}**! 💪🏾"
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
        await refresh_vote(inter.guild, ch)


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


async def handle_proof_button(inter: discord.Interaction, owner: int, what: str, ref: str, day: str | None = None):
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
        d = date.fromisoformat(day) if day else today
        result = await log_checkin(ch, inter.user, g, d, proof_url, inter.guild)
        await inter.response.send_message(**checkin_reply(result, g, d), ephemeral=True)
    else:
        result = complete_mission(ch, owner, proof_url, date.fromisoformat(ref), bonus=(what == "mb"))
        await inter.response.send_message(result, ephemeral=True)
    if result.startswith(("Logged", "⚡")):
        try:
            await inter.message.edit(**proof_reply_after_tap(inter.message, (getattr(inter, "data", None) or {}).get("custom_id", ""),
                                                            result.splitlines()[0]))
        except discord.HTTPException:
            pass


PROOF_ASK = "What's this for? Tap it:"
PROOF_ASK_MORE = "Does this photo count for anything else? Tap it:"
PROOF_DONE = "All set for this photo. 💪"


def proof_reply_after_tap(message, tapped: str, result_line: str) -> dict:
    """Swap the question for what she logged, and keep only the buttons that are still open."""
    logged = [l for l in (message.content or "").splitlines() if l.startswith(("✅", "⚡"))]
    logged.append(result_line if result_line.startswith("⚡") else f"✅ {result_line}")
    parts = tapped.split(":")
    gone = {tapped}
    if len(parts) > 4 and parts[3] == "mb":  # the bonus covers the base mission too
        gone.add(":".join(parts[:3] + ["m"] + parts[4:]))
    left = []
    for row in getattr(message, "components", None) or []:
        for b in getattr(row, "children", []):
            if getattr(b, "custom_id", None) and b.custom_id not in gone:
                left.append(discord.ui.Button(label=b.label, emoji=b.emoji, style=b.style, custom_id=b.custom_id))
    content = "\n".join(logged + [PROOF_ASK_MORE if left else PROOF_DONE])
    return {"content": content, "view": make_view(left) if left else None}


async def handle_reminder(inter: discord.Interaction, goal_id: int, day: date, yes: bool):
    if not yes:
        return await inter.response.send_message("No worries. Tomorrow's a new day. 💕", ephemeral=True)
    g = db.goal(goal_id)
    if not g or g.user_id != inter.user.id:
        return await deny(inter, "That goal isn't yours.")
    ch = db.get_challenge(db.conn.execute("SELECT challenge_id FROM goals WHERE id=?", (goal_id,)).fetchone()[0])
    guild = bot.get_guild(ch.guild_id)
    await inter.response.send_message(**checkin_reply(await log_checkin(ch, inter.user, g, day, None, guild), g, day))


async def handle_undo(inter: discord.Interaction, goal_id: int, day: date):
    g = db.goal(goal_id)
    if not g or g.user_id != inter.user.id:
        return await deny(inter, "That goal isn't yours.")
    ch = db.get_challenge(db.conn.execute("SELECT challenge_id FROM goals WHERE id=?", (goal_id,)).fetchone()[0])
    await inter.response.edit_message(content=undo_checkin(ch, inter.user.id, g, day), view=None)


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
    key = today.isoformat()
    if (ch.status == "signup" and game.signups_open(ch, now) and now.hour >= config.SIGNUP_REMINDER_HOUR
            and not db.job_done(ch.id, "signup_reminder", today.isoformat())):
        db.mark_job(ch.id, "signup_reminder", today.isoformat())
        if ann:
            await ann.send(signup_reminder_text(guild, ch, today), allowed_mentions=discord.AllowedMentions.none())

    # DM players who signed up but haven't set goals, in the last few days before the deadline.
    if ch.status == "signup" and game.signups_open(ch, now) and now.hour >= config.SIGNUP_REMINDER_HOUR:
        deadline = ch.signup_deadline or (ch.start - timedelta(days=1))
        warning_day = (deadline - today).days == 1  # the night-before warning DMs them instead
        if ((deadline - today).days <= config.GOAL_NUDGE_DAYS and not warning_day
                and not db.job_done(ch.id, "goal_nudge", key)):
            db.mark_job(ch.id, "goal_nudge", key)
            await nudge_missing_goals(guild, ch, deadline, today)

    # The removal warning, the night before the deadline.
    if ch.status == "signup" and game.signups_open(ch, now) and now.hour >= config.WARNING_HOUR:
        deadline = ch.signup_deadline or (ch.start - timedelta(days=1))
        if (deadline - today).days == 1 and not db.job_done(ch.id, "goal_warning", key):
            db.mark_job(ch.id, "goal_warning", key)
            await warn_missing_goals(guild, ch, deadline, ann, today)

    # The what-to-expect overview, posted the day before the challenge starts.
    if (ch.status in ("signup", "ready") and today == ch.start - timedelta(days=1)
            and now.hour >= config.OVERVIEW_HOUR and not db.job_done(ch.id, "overview", "auto")):
        db.mark_job(ch.id, "overview", "auto")
        if ann:
            for page in message_pages(overview_embeds(guild, ch)):
                await ann.send(embeds=page, allowed_mentions=discord.AllowedMentions.none())

    # The day before the start: the teams-or-individual vote, then a nudge for the commissioner that night.
    if ch.status in ("signup", "ready") and not ch.mode and today == game.mode_vote_day(ch):
        if now.hour >= config.MODE_VOTE_HOUR and not db.job_done(ch.id, "mode_vote", "posted"):
            db.mark_job(ch.id, "mode_vote", "posted")
            await post_mode_vote(guild, ch, ann)
        elif (now.hour >= config.MODE_DECIDE_HOUR and not ch.mode_pick
              and not db.job_done(ch.id, "mode_vote", "nudge")):
            db.mark_job(ch.id, "mode_vote", "nudge")
            await nudge_commissioner(guild, ch, ann)

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
        embed = await lock_signups(guild, ch, today=today)
        if ann:
            await ann.send(embed=embed)
        ch = db.get_challenge(ch.id)
    if ch.status == "ready" and today >= ch.start and not ch.mode:
        # Sign-ups closed early: settle team or solo now, at midnight before day one.
        chosen, lines = await finalize_mode(guild, ch)
        if ann:
            await ann.send(embed=discord.Embed(title=f"{ch.name}: {chosen} mode", description="\n\n".join(lines),
                                               color=PINK))
        ch = db.get_challenge(ch.id)
    if ch.status == "ready" and today >= ch.start:
        # Live from midnight on day one, so a 5 a.m. workout can be logged; the kickoff post waits for the morning.
        db.update_challenge(ch.id, status="active")
        ch = db.get_challenge(ch.id)
    if (ch.status == "active" and ch.start <= today <= ch.start + timedelta(days=1)
            and now.hour >= config.MISSION_POST_HOUR and not db.job_done(ch.id, "kickoff", "1")):
        db.mark_job(ch.id, "kickoff", "1")
        if ann:
            for page in message_pages(kickoff_embeds(guild, ch)):
                await ann.send(embeds=page, allowed_mentions=discord.AllowedMentions.none())
    if ch.status != "active":
        return
    in_range = ch.start <= today <= ch.end

    if in_range and now.hour >= config.MISSION_POST_HOUR and not db.job_done(ch.id, "mission", key):
        # Pick the day's mission once. If posting fails, the next try posts the same one.
        if not db.daily_mission(ch.id, today):
            m = game.pick_mission(db, ch, today)
            if m:
                db.set_daily_mission(ch.id, today, m["id"], now)
        today_m = db.daily_mission(ch.id, today)
        if today_m and not await post_mission(guild, today, today_m):
            return  # couldn't post anywhere: try again next minute
        db.mark_job(ch.id, "mission", key)

    if in_range and now.hour >= config.REMINDER_HOUR and not db.job_done(ch.id, "reminder", key):
        db.mark_job(ch.id, "reminder", key)
        await send_reminders(guild, ch, today)

    # Daily roundup of yesterday. The morning after a week ends, it doubles as that week's final standings.
    # (The morning after the last day, the final crowning post covers it instead.)
    if ch.start < today <= ch.end and now.hour >= config.ROUNDUP_HOUR and not db.job_done(ch.id, "roundup", key):
        db.mark_job(ch.id, "roundup", key)
        if ann:
            for page in message_pages(roundup_embeds(ch, today - timedelta(days=1))):
                await ann.send(embeds=page, allowed_mentions=discord.AllowedMentions.none())

    if today > ch.end and now.hour >= config.STANDINGS_HOUR and not db.job_done(ch.id, "final", "1"):
        db.mark_job(ch.id, "final", "1")
        if ann:  # the last week's checkmarks first (it never gets a Saturday roundup), then the crowning
            for page in message_pages(roundup_embeds(ch, ch.end)):
                await ann.send(embeds=page, allowed_mentions=discord.AllowedMentions.none())
        await finish(guild, ch, ann)


async def warn_missing_goals(guild: discord.Guild, ch: Challenge, deadline: date, ann, today: date | None = None) -> int:
    """The night before the deadline: warn (and ping) everyone without 3 goals that they'll be removed."""
    missing = [p["user_id"] for p in db.players(ch.id) if len(db.goals(ch.id, p["user_id"])) < 3]
    if not missing:
        return 0
    days_left = (deadline - (today or game.now_local().date())).days
    when = (f"11:59 p.m. {tz_label()} tonight" if days_left <= 0 else
            f"11:59 p.m. {tz_label()} tomorrow ({pretty(deadline)})" if days_left == 1 else
            f"11:59 p.m. {tz_label()} {pretty(deadline)}")
    rule = "you'll be removed from the challenge" if config.REMOVE_WITHOUT_GOALS else "you won't be able to play"
    if ann:
        await ann.send(
            "⚠️ **Goal deadline warning** ⚠️\n" + " ".join(f"<@{u}>" for u in missing)
            + f"\nYou're signed up for **{ch.name}**, but your 3 goals aren't in. If they're not submitted by "
            f"**{when}**, {rule}. Tap **I'm in** on the sign-up post, then **Add my goals**. 💪🏾",
            allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False))
    for uid in missing:
        button = discord.ui.Button(label="Add my goals", emoji="➕", style=discord.ButtonStyle.danger,
                                   custom_id=f"mb:goals:{ch.id}")
        await dm(uid, f"⚠️ Heads up: your goals for **{ch.name}** in {guild.name} are due by **{when}**. "
                      f"If they're not in, {rule}. Tap below to add them now.", make_view([button]))
    return len(missing)


async def nudge_missing_goals(guild: discord.Guild, ch: Challenge, deadline: date, today: date):
    """DM each signed-up player who hasn't set 3 goals, with a button that opens the goals form."""
    days_left = (deadline - today).days
    when = ("tonight at 11:59 p.m. " + tz_label()) if days_left <= 0 else (
        "tomorrow night" if days_left == 1 else f"{pretty(deadline)} at 11:59 p.m. {tz_label()}")
    for p in db.players(ch.id):
        uid = p["user_id"]
        have = len(db.goals(ch.id, uid))
        if have >= 3:
            continue
        msg = (f"👋 You're signed up for **{ch.name}** in {guild.name}, but "
               + ("you haven't set your goals yet." if have == 0 else f"you've only set {have} of 3 goals.")
               + f"\nGoals are due **{when}**. Tap below to fill them in. It takes a minute! 💪🏾")
        button = discord.ui.Button(label="Add my goals" if have == 0 else "Finish my goals", emoji="➕",
                                   style=discord.ButtonStyle.success, custom_id=f"mb:goals:{ch.id}")
        try:
            user = bot.get_user(uid) or await bot.fetch_user(uid)
            await user.send(msg, view=make_view([button]))
        except Exception:
            log.info("couldn't DM %s about goals", uid)  # the daily channel reminder still lists her


async def send_reminders(guild: discord.Guild, ch: Challenge, today: date):
    unreachable = []
    for p in db.players(ch.id):
        uid = p["user_id"]
        if db.checked_in_on(ch.id, uid, today):
            continue
        open_goals = game.open_goals_today(db, ch, uid, today)
        if not open_goals:
            continue
        week = week_for(today, challenge_weeks(ch.start, ch.end))
        lines = []
        for g in open_goals:
            target = scaled_target(g.target_days, week.days)
            left = target - len(db.checkin_days(g.id, week.start, week.end))
            lines.append(f"**{g.name}**: {left} of {target} still to go this week. Did you today?")
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
            try:
                member = guild.get_member(uid) or await guild.fetch_member(uid)
            except discord.NotFound:
                continue  # she left the server
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
        perms = any("yourself" not in x for x in skipped)
        await ann.send("I couldn't add the crown for: " + ", ".join(skipped) + "."
                       + (" (I need Manage Nicknames, and my role has to be above theirs.)" if perms else ""))


@scheduler.before_loop
async def before_scheduler():
    await bot.wait_until_ready()


@bot.event
async def setup_hook():
    await bot.tree.sync()
    scheduler.start()


# ---------- owner: install alerts and /botstats ----------

_owner_id: int | None = None


async def owner_id() -> int | None:
    """The person who gets alerts: OWNER_ID if set, otherwise the bot's owner in the Developer Portal."""
    global _owner_id
    if config.OWNER_ID:
        return config.OWNER_ID
    if _owner_id is None:
        try:
            app = await bot.application_info()
            _owner_id = app.team.owner_id if app.team else app.owner.id
        except discord.HTTPException:
            return None
    return _owner_id


async def notify_owner(text: str):
    """DM the owner. Never let an alert break the command that triggered it."""
    try:
        uid = await owner_id()
        if uid:
            user = bot.get_user(uid) or await bot.fetch_user(uid)
            await user.send(text)
    except Exception:
        log.warning("couldn't send owner alert", exc_info=True)


@bot.event
async def on_guild_join(guild: discord.Guild):
    await notify_owner(f"👋 Added to **{guild.name}** ({guild.member_count or '?'} members). "
                       f"Now in {len(bot.guilds)} server{'s' if len(bot.guilds) != 1 else ''}.")


@bot.event
async def on_guild_remove(guild: discord.Guild):
    await notify_owner(f"👋 Removed from **{guild.name}**. Now in {len(bot.guilds)} "
                       f"server{'s' if len(bot.guilds) != 1 else ''}.")


def botstats_text(guilds: list[discord.Guild], today: date) -> str:
    """Install and usage numbers across every server, plus check-in rates for running challenges."""
    set_up = set(db.all_guild_ids())
    lines, totals = [], {"running": 0, "players": 0, "active7": 0}
    for g in sorted(guilds, key=lambda g: -(g.member_count or 0)):
        ch = db.current_challenge(g.id)
        done = db.challenge_counts(g.id).get("finished", 0)
        if g.id not in set_up:
            status = "not set up yet"
        elif not ch:
            status = f"set up, no challenge running · {done} finished"
        else:
            players = len(db.players(ch.id))
            week_ago = max(ch.start, today - timedelta(days=6))
            active = len(db.checkin_users_between(ch.id, week_ago, today)) if ch.status == "active" else 0
            rate = f" · {active}/{players} checked in this past week" if ch.status == "active" else ""
            status = f"{ch.name}: {ch.status}, {players} players{rate} · {done} finished"
            totals["running"] += 1
            totals["players"] += players
            totals["active7"] += active
        lines.append(f"• **{g.name}** ({g.member_count or '?'} members): {status}")
    head = (f"**{len(guilds)} servers** · {len(set_up & {g.id for g in guilds})} set up · "
            f"{totals['running']} with a challenge · {totals['players']} players")
    if totals["players"]:
        head += f" · {totals['active7']} checked in this past week"
    return (head + "\n\n" + "\n".join(lines))[:2000]


class FeedbackForm(discord.ui.Modal):
    """Private feedback about the challenge or the bot, sent to the organizer by DM."""

    def __init__(self):
        super().__init__(title="Feedback for the organizer")
        self.note = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=1500,
                                         placeholder="What's confusing, broken, or could be better? Ideas welcome!")
        self.anon = discord.ui.Select(options=[
            discord.SelectOption(label="Include my name", value="named", default=True),
            discord.SelectOption(label="Send anonymously", value="anon")])
        self.add_item(discord.ui.Label(text="Your feedback", component=self.note))
        self.add_item(discord.ui.Label(text="Name or anonymous?", component=self.anon))

    async def on_submit(self, inter: discord.Interaction):
        anonymous = (self.anon.values or ["named"])[0] == "anon"
        who = "Anonymous" if anonymous else f"{inter.user.display_name} (<@{inter.user.id}>)"
        where = inter.guild.name if inter.guild else "a DM"
        await notify_owner(f"💬 **Feedback** from {who} in **{where}**:\n>>> {self.note.value}")
        await inter.response.send_message("Thank you! Your feedback went to the organizer. 💕", ephemeral=True)


@bot.tree.command(description="Send private feedback about the challenge to the organizer.")
async def feedback(inter: discord.Interaction):
    await inter.response.send_modal(FeedbackForm())


@bot.tree.command(description="Bot owner only: how many servers use the bot, and how they're doing.")
@app_commands.allowed_contexts(guilds=False, dms=True, private_channels=False)
async def botstats(inter: discord.Interaction):
    if inter.user.id != await owner_id():
        return await deny(inter, "Only the bot's owner can see this.")
    await inter.response.send_message(botstats_text(list(bot.guilds), game.now_local().date()))


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

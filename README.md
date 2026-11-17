# Bad Bitches vs Muscle Mamis: the Discord bot

Runs the monthly accountability challenge inside Discord. Players join, set 3 goals, check in with photos, do daily mini missions, and the bot does all the scoring, reminders, standings and crowning.

- **Part 1** is one-time setup, about 30 minutes.
- **Part 2** is the monthly checklist for whoever is commissioner.

---

## Part 1: One-time setup

### 1. Create the bot in Discord

1. Go to https://discord.com/developers/applications and click **New Application**. Name it (e.g. "Challenge Bot").
2. Open the **Bot** tab:
   - Click **Reset Token**, then copy the token. This is your `DISCORD_TOKEN`. Treat it like a password.
   - Under **Privileged Gateway Intents**, turn on **Message Content Intent**. The bot needs it to see photos in the proof channel.
3. Open **OAuth2 → URL Generator**:
   - Scopes: `bot` and `applications.commands`
   - Bot permissions: View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Add Reactions, Manage Roles, Manage Nicknames
4. Open the generated URL and add the bot to your server.
5. In **Server Settings → Roles**, drag the bot's role **above** the team and winner roles, and above everyone's roles, so it can hand out roles and add crowns to nicknames.
6. Optional: give the winner role a bright color in **Server Settings → Roles**, so winners' names show in that color in chat.

### 2. Put the code on GitHub

Create a private repo and upload this folder.

### 3. Host it on Railway (about $5/month)

1. Sign up at https://railway.com and choose the Hobby plan.
2. **New Project → Deploy from GitHub repo**, and pick the repo. Railway finds the `Dockerfile` on its own.
3. In the service, open **Variables** and add:
   - `DISCORD_TOKEN`: the token from step 1
   - `DATABASE_PATH`: `/data/challenge.db`
   - Anything else from `.env.example` you want to change
4. Right-click the service, choose **Attach Volume**, and set the mount path to `/data`. **Don't skip this.** Without it, all scores are wiped every time the bot restarts.
5. Deploy. The logs should say `Logged in as Challenge Bot`.

### 4. Connect it to your channels

In Discord, an admin runs:

```
/setup announce_channel:#challenge proof_channel:#proof commissioner_role:@Commissioner
```

This loads the 59 mini missions from `data/missions.csv`. The commissioner role is optional: anyone with Manage Server can already run commissioner commands.

---

## Part 2: Running a month (commissioner checklist)

| When | What to do |
| --- | --- |
| A week or so before | `/challenge open start:2026-12-01 end:2026-12-31` posts the sign-up with an **I'm in** button |
| During sign-ups | Players tap **I'm in**, then `/goals set`. Check who's missing with `/challenge status` |
| Before the start date | Review goals, then `/commish approve` (everyone) or `/commish approve member:@name` |
| Optional | `/challenge lock` closes sign-ups early. Otherwise the bot locks them on the start date by itself |
| During the month | Nothing. The bot runs it |
| If something's wrong | `/commish add-checkin`, `/commish remove-checkin`, `/commish mission-points` |
| After the last day | At 9 a.m. the bot posts final standings, hands out the winner roles and crowns the winners' nicknames |

**Team or individual:** 8 or more players makes teams, fewer is individual. To override, use `/challenge lock mode:teams` or `mode:individual`.

### What the bot does on its own each day (Eastern time by default)

- **7 a.m.:** posts the day's mini mission, from that day's theme. About once a week it's a Random Mix-Up mission instead.
- **8 p.m.:** sends a DM reminder to anyone who hasn't checked in that day, with a button for each goal. Players with DMs off get pinged in the announce channel.
- **Friday at midnight:** the week locks.
- **Saturday at 9 a.m.:** posts the weekly standings.

---

## How players use it

- **Check in with a photo:** post it in the proof channel and tap which goal it's for. A button for today's mini mission shows up there too.
- **Check in without a photo:** `/checkin`, or tap the button in the 8 p.m. reminder.
- **Log an earlier day:** `/checkin` and pick the day. This works until Friday at midnight.
- **Other commands:**
  - `/mystats` shows your week, with progress like ✅✅⬜
  - `/leaderboard` shows everyone's standings
  - `/mission today` shows today's mini mission
  - `/howtoplay` explains the rules

---

## Scoring (matches the rules doc)

- **Goal points:** each goal is worth up to 10 points a week (10 × days done ÷ target, capped at 10). Everyone has 3 goals, so the max is 30 a week.
- **Perfect week:** every goal hits its target, so the week's goal points × 1.5.
- **Team bonus:** every player on a team has a perfect week, so the team's goal points for that week get +25%. Individual multipliers apply first.
- **Mini missions:** 0.5 to 2 points each, open for 24 hours, capped at 20 points per player for the month. They're added after multipliers.
- **Partial weeks:** in a short week at the start or end of the month, targets shrink to fit, rounding up.
- **Team scores:** compared by average per player, so a team with one extra player can't win on headcount.
- **Ties:** players tied at the top share the Top Scorer role.

## Crowns

When a challenge ends, the winners get 👑 in front of their server nickname, and the top scorer gets 💎 ("👑💎 Jenna" if she's both). The crowns stay until the next challenge ends. Then the bot moves them to the new winners and puts everyone else's nickname back.

Discord doesn't let bots rename the server owner. If the owner wins, the bot says so in the channel and she adds the crown herself.

---

## Settings for rules the group is still deciding

Change these in Railway's **Variables**. The bot restarts with the new value.

| Variable | Default | Options |
| --- | --- | --- |
| `PHOTO_MODE` | `optional` | `optional` (encouraged, never required), `required` (every goal), `per_goal` (the commissioner picks with `/commish photo`) |
| `TEAM_MODE_MIN_PLAYERS` | `8` | Any number |
| `TEAM_SCORE_AVERAGE` | `true` | `false` compares team totals instead of averages |
| `TIMEZONE` | `America/New_York` | e.g. `America/Los_Angeles` |
| `REMINDER_HOUR` | `20` | 0–23 |
| `TEAM_NAMES` | `Bad Bitches,Muscle Mamis` | Two names, separated by a comma |
| `WINNER_ROLE_NAME` / `TOP_SCORER_ROLE_NAME` | `Challenge Champion` / `That Girl` | Any role name |
| `CROWN_NICKNAMES` | `true` | `false` turns the nickname crowns off |
| `WINNER_EMOJI` / `TOP_SCORER_EMOJI` | `👑` / `💎` | Any emoji |

## Updating the mini missions

1. Edit the Mini Mission Bank sheet and download it as CSV. It needs the columns Day, Mission and Points.
2. Run `/missions import` and attach the file. This replaces the whole bank.
3. To add a single mission, use `/missions add`. `/missions count` shows how many each day has.

## For whoever maintains the code

```
pip install -r requirements.txt pytest
python -m pytest                 # 28 tests, including a simulated month
DISCORD_TOKEN=... python -m bot.main
```

What's in each file:

- `bot/scoring.py`: the rules, kept separate from Discord and the database
- `bot/game.py`: connects the rules to the data
- `bot/db.py`: SQLite storage
- `bot/main.py`: the Discord commands and the daily schedule

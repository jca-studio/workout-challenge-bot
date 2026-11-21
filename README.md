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
/setup announce_channel:#challenge proof_channel:#proof mission_channel:#mini-missions commissioner_role:@Commissioner
```

This loads the 59 mini missions from `data/missions.csv`. The mission channel is optional; without it, missions post in the announce channel. The commissioner role is optional: anyone with Manage Server can already run commissioner commands.

---

## Part 2: Running a month (commissioner checklist)

| When | What to do |
| --- | --- |
| A week or so before | `/challenge open start:2026-12-01 end:2026-12-31 signup_deadline:2026-11-28` posts the sign-up with the full rules, the deadline, an **I'm in** button and a live list of who's in. The deadline is optional and defaults to the day before the start |
| Every day during sign-ups | At 10 a.m. the bot posts a reminder with a countdown, a link to the sign-up post, who's in, and who still needs goals (names show, nobody gets pinged). It stops when sign-ups close |
| During sign-ups | Players tap **I'm in**, then **Add my goals**, which opens one form for all 3 goals, each written like "Yoga, 3 days" or "Affirmations, every day" (`/goals set` opens the same form). Check who's missing with `/challenge status` |
| Last 3 days before the deadline | The bot DMs anyone signed up without 3 goals, once a day, with an **Add my goals** button |
| The night before the deadline | At 8 p.m. the bot posts a warning that pings anyone without 3 goals (and DMs them): goals by the deadline, or they're removed |
| The night before the start | At 7 p.m. the bot posts the overview by itself, including whether it's team or solo. To send it earlier, or by DM, run `/challenge overview` |
| At the deadline | Players without 3 goals are removed and DM'd, everyone else's goals are approved, the mode is set and teams are made |
| Morning of day one | At 7 a.m. the bot posts the kickoff welcome (teams, what to do today, proof, feedback, commands), then the first mini mission |
| Anytime | `/challenge overview` posts a what-to-expect message in the announce channel (or `send_to: DM every player`): how it works, points, reminders and the daily schedule, commands, and everyone's goals |
| Before the deadline (optional) | `/goals all` shows everyone's goals. Goals are approved automatically at the deadline, so you only need this to review them |
| At the deadline | The bot closes sign-ups at midnight after the deadline day and makes teams. `/challenge lock` closes them early. If fewer than 2 people have joined, sign-ups stay open until the start date |
| During the month | Nothing. The bot runs it |
| If something's wrong | `/commish add-checkin`, `/commish remove-checkin`, `/commish mission-points` |
| After the last day | At 9 a.m. the bot posts final standings, hands out the winner roles and crowns the winners' nicknames |

**Team or individual:** 8 or more players makes teams, fewer is individual. To override, use `/challenge lock mode:teams` or `mode:individual`.

### What the bot does on its own each day (Eastern time by default)

- **7 a.m.:** posts the day's mini mission in the mission channel, from that day's theme. About once a week it's a Random Mix-Up mission instead. Photos posted in the mission channel get a "Today's mini mission" button.
- **8 a.m.:** posts a daily roundup of yesterday in the announce channel: the leaderboard, plus each player's progress on each goal this week (✅ for each day she hit it, ⬜ for days still to go). Saturday's roundup is the week's final standings.
- **8 p.m.:** sends a DM reminder to anyone who hasn't checked in that day, with a button for each goal. Players with DMs off get pinged in the announce channel.
- **Friday at midnight:** the week locks.

---

## How players use it

- **Check in with a photo:** post it in the proof channel and tap which goal it's for. A button for today's mini mission shows up there too.
- **Check in without a photo:** `/checkin`, or tap the button in the 8 p.m. reminder.
- **Log an earlier day:** `/checkin` and pick the day. This works until Friday at midnight.
- **Feedback:** `/feedback` sends the organizer a private note, with an option to stay anonymous.
- **Other commands:**
  - `/mystats` shows your week, with progress like ✅✅⬜
  - `/leaderboard` shows everyone's standings
  - `/mission today` shows today's mini mission
  - `/howtoplay` explains the rules

---

## Scoring (matches the Game Rules doc)

- **Goal points:** each goal is worth up to **1 point a week**. Hit your target for the full point, or get the share you hit (2 of 3 workouts = 0.67). Going past the target earns nothing extra. Everyone has 3 goals, so the max is 3 a week.
- **Perfect week:** hit all 3 goals and you get **+1 bonus point**, for a max of 4 a week.
- **Team bonus:** every player on a team has a perfect week, so the team gets **+1 point per player** that week.
- **Mini missions:** 0.25 (easy) or 0.5 (harder), open for 24 hours, no cap. They never count toward a perfect week.
- **Partial weeks:** in a short week at the start or end of the month, targets shrink to fit, rounding up.
- **Team scores:** compared by average per player, so a team with one extra player can't win on headcount.
- **Ties:** players or teams tied at the top share the win.

## Knowing who uses the bot

- **Alerts by DM:** the bot messages its owner (you, as set in the Developer Portal) when it's added to or removed from a server, when a server runs `/setup` for the first time, and when a server opens a challenge.
- **`/botstats`:** type it in your DMs with the bot (it doesn't appear in servers). It lists every server the bot is in, which ones are set up, which have a challenge running, how many players they have, and how many checked in during the past week.
- To send alerts to someone else, set `OWNER_ID` in Railway to their Discord user ID.
- The Developer Portal's **Install Count** shows a rough daily total too.

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
| `MISSION_WEEKLY_CAP` | `0` | Most mini mission points per player per week (`0` = no cap) |
| `PERFECT_WEEK_BONUS` | `1` | Bonus points for hitting all 3 goals in a week |
| `TEAM_SCORE_AVERAGE` | `true` | `false` compares team totals instead of averages |
| `TIMEZONE` | `America/New_York` | e.g. `America/Los_Angeles` |
| `REMINDER_HOUR` | `20` | 0–23 |
| `ROUNDUP_HOUR` | `8` | 0–23, when the daily roundup posts |
| `SIGNUP_REMINDER_HOUR` | `10` | 0–23, when the daily sign-up reminder posts |
| `OVERVIEW_HOUR` | `19` | 0–23, when the overview posts the night before the start |
| `WARNING_HOUR` | `20` | 0–23, when the removal warning posts the night before the deadline |
| `REMOVE_WITHOUT_GOALS` | `true` | `false` keeps players without goals instead of removing them |
| `AUTO_APPROVE_AT_LOCK` | `true` | `false` makes the commissioner approve goals by hand |
| `GOAL_NUDGE_DAYS` | `3` | How many days before the deadline to start DMing players without goals |
| `TEAM_NAMES` | `Bad Bitches,Muscle Mamis` | Two names, separated by a comma |
| `WINNER_ROLE_NAME` / `TOP_SCORER_ROLE_NAME` | `Challenge Champion` / `That Girl` | Any role name |
| `CROWN_NICKNAMES` | `true` | `false` turns the nickname crowns off |
| `WINNER_EMOJI` / `TOP_SCORER_EMOJI` | `👑` / `💎` | Any emoji |

## Updating the mini missions

1. Edit the Mini Mission Bank sheet and download it as CSV. It needs the columns Day, Mission and Points.
2. Run `/missions import` and attach the file. This replaces the whole bank.
3. To add a single mission, use `/missions add`. To go back to the built-in list, use `/missions reset`. `/missions count` shows how many each day has.

## For whoever maintains the code

```
pip install -r requirements.txt pytest
python -m pytest                 # 73 tests, including a simulated month
DISCORD_TOKEN=... python -m bot.main
```

What's in each file:

- `bot/scoring.py`: the rules, kept separate from Discord and the database
- `bot/game.py`: connects the rules to the data
- `bot/db.py`: SQLite storage
- `bot/main.py`: the Discord commands and the daily schedule

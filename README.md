# TARS
T.A.R.S. is a Discord bot for a private server with moderation, utility, and light recreational tooling.

## Features
- Moderation and safety: auto-moderation hooks, reports, lock/slowmode tools, and a configurable banned-word list.
- Utility automation: reminders, message quoting, and server/user diagnostics.
- Community engagement: chat revive prompts, MOTD rotation, and fun commands like dice and 8-ball.
- Boost points: reward boosters and let them redeem perks through a simple shop flow.

## Quick Start
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Configure environment variables:
   - `DISCORD_TOKEN`
   - `OWNER_ID`
   - `GUILD_ID`
   - `OPENAI_API_KEY` (optional, required for AI replies and revive prompts)
3. Run the bot:
   ```bash
   python tars_bot.py
   ```

## Command Highlights
### Moderation
- `/tarsreport` — report a user to staff.
- `/clean` — delete the last N messages (admin only).
- `/lock` / `/unlock` — lock or unlock a channel (admin only).
- `/slowmode` — set a channel slowmode delay (admin only).
- `/addbannedword` / `/removebannedword` / `/listbannedwords` — manage banned words.

### Utility & Diagnostics
- `/tars` — command console and categorized help.
- `/userinfo` / `/roleinfo` / `/serverinfo` — quick info commands.
- `/status` — show system health.
- `/remindme` — set reminders with flexible durations (e.g. `1h 30m`).
- `/reactionrole` — create a reaction role (admin only).
- `/setmotd` — set MOTD channel (owner only).
- `/motd_add` / `/motd_remove` / `/motd_list` — manage the MOTD rotation (owner only).

### Recreational
- `/8ball` — ask the magic 8-ball.
- `/dice` — roll dice with optional modifiers (e.g. `2d6+1`).
- `/quote` / `/getquote` — save and retrieve memorable quotes.

## Notes
- MOTD messages rotate hourly when configured.
- The AI subsystem can be disabled automatically if too many errors occur in a short window.

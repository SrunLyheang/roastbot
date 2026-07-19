# RoastBot 💀🔥

A Telegram bot that roasts people. Add it to a group and it cooks your friends
on command. DM it and it roasts everything you send. It quietly remembers what
people say and uses their own words against them.

## Setup (do this once)

### 1. Install dependencies
Reuse an existing venv or make a new one, then:
```bash
pip install python-telegram-bot google-genai groq requests
```
(You can also do `pip install -r requirements.txt`.)

### 2. Create the bot with BotFather
1. In Telegram, open **@BotFather** → send `/newbot` → pick a name and username.
2. Copy the **token** it gives you into `config.py` (`TELEGRAM_TOKEN`).
3. Set `BOT_USERNAME` in `main.py` to your bot's @name (keep the `@`).
4. **IMPORTANT — turn off privacy mode** so the bot can see group messages
   (this is what makes the "remember what people said" feature work):
   BotFather → `/setprivacy` → pick your bot → **Disable**.
5. Paste this into BotFather → `/setcommands` → pick your bot:
   ```
   start - Meet the roast bot
   help - How to get roasted
   roast - Reply to someone or /roast @username
   roastme - Roast yourself
   battle - Roast battle: two humans fight, AI judges
   versus - AI roasts two people and picks a winner
   leaderboard - Hall of Shame for this chat
   ```

### 3. Add at least one AI key to `config.py`
You only need **one** to start. The bot tries them best-first and skips any you
leave blank:
- **DeepSeek** (best roasts, cheap paid): https://platform.deepseek.com
- **Gemini** (free): https://aistudio.google.com/apikey
- **Groq** (free, fast): https://console.groq.com/keys
- **NVIDIA NIM** (free): https://build.nvidia.com

### 4. Run it
```bash
python main.py
```
Leave the terminal open — the bot only works while this is running.

## Commands
- `/roast` — reply to someone's message to roast them, or `/roast @username`
- `/roastme` — roast yourself
- `/battle @username` — you both roast each other, the AI judges who won 🥊
- `/versus @a @b` — the AI roasts both and picks a winner
- `/leaderboard` — this chat's Hall of Shame

## Notes
- `bot.db` → here it's `roastbot.db`, created automatically on first run. Delete
  it any time you want to wipe all memory and leaderboards.
- Never commit `config.py` — it's gitignored (it holds your keys).
- Hardcoded replies live in `replies.py` — add your own jokes there.

# Founder Terminal

One Telegram chat that tells you what is happening in tech. Headline, what happened, what it means for you. Urgent news arrives the moment it is found. Everything else lands in short briefs every two hours from 8 AM to 10 PM. Costs nothing to run.

## How it works

Every 5 minutes GitHub runs `terminal.py`:

1. Reads your Telegram commands and 👍/👎 votes.
2. Pulls about 30 sources: Techmeme, TechCrunch, The Information, Bloomberg, WSJ, CNBC, The Verge, Ars, Wired, VentureBeat, Crunchbase News, Axios, Stratechery, Hacker News front page, SEC IPO filings (S-1, F-1, 424B4), and Google News searches for funding, IPOs, M&A, AI labs, antitrust and your watchlist.
3. Drops anything already seen, and the same story told by a second outlet.
4. A free AI model (Groq) scores each headline 0 to 10 against your profile, watchlist, standing instructions and past votes.
5. Scores 8+ push instantly (🔴 = 10, 🟠 = 8 to 9). Scores 5 to 7 wait for the next brief. Below 5 is dropped.

Tap "more" (the collapsed quote) on any item for extra context without opening the article.

## Setup (about 5 minutes)

1. **Telegram bot.** In Telegram, message `@BotFather`, send `/newbot`, pick a name. Copy the token. Open your new bot and send it "hi".
2. **Free AI key.** Sign in at console.groq.com, go to API Keys, create one.
3. **Secrets.** In this repo: Settings → Secrets and variables → Actions → New repository secret.
   - `TELEGRAM_BOT_TOKEN` = the BotFather token
   - `GROQ_API_KEY` = the Groq key
4. **Start.** Actions tab → enable workflows if asked → "terminal" → Run workflow. Your bot replies within a minute. After that it runs on its own.
5. **Tell it who you are.** In the bot: `/profile ...` and `/watch company, company, topic`.

Optional secrets: `GEMINI_API_KEY` (backup AI if Groq is down), `SEC_USER_AGENT` (your name and email, which SEC asks for), `TELEGRAM_CHAT_ID`.

## Commands

| | |
|---|---|
| `/brief` | brief now |
| `/profile text` | who you are and what you care about |
| `/watch a, b` | track companies or topics |
| `/unwatch a` | stop tracking |
| `/briefs 8,12,18` | brief hours |
| `/pause` `/resume` | quiet mode |
| `/status` | health, broken sources, AI usage |
| any text | standing instruction, like "less crypto" |

Quiet hours are 1 AM to 7 AM. Only a 10 gets through. The rest waits for the 8 AM brief.

## Privacy

The repo is public so GitHub Actions is free. Your profile, watchlist, notes and votes live in `state.enc` on the `state` branch, encrypted with a key derived from your bot token. Nobody can read it without the token.

## Limits of the free version

- GitHub runs the schedule every 5 minutes at best and sometimes later under load. Expect news within roughly 5 to 20 minutes of publishing.
- Groq's free tier allows about 200,000 tokens a day. The terminal budgets 180,000 and gets stricter if it runs out.
- No X/Twitter. The X API is paid. Most stories that blow up on X hit Techmeme and Google News within minutes.

## Test

`python tests/test_offline.py` runs the full flow with fake feeds, fake AI and fake Telegram.

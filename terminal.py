#!/usr/bin/env python3
"""Founder Terminal.

One run: read Telegram commands, pull every source, drop what was already
seen, let an LLM score what is left, push urgent items now, batch the rest
into briefs at set hours. GitHub Actions runs this every few minutes.
"""
import base64
import concurrent.futures as cf
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet, InvalidToken

from sources import EDGAR_FORMS, HN_MIN_POINTS, NEWS_QUERIES, RSS_FEEDS


def env(name, default=""):
    v = os.getenv(name)
    return v if v not in (None, "") else default


TZ = ZoneInfo(env("TIMEZONE", "America/New_York"))
STATE_PATH = env("STATE_PATH", "state/state.enc")
TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")


def _clean_token(t):
    """Pull the bare BotFather token out of whatever was pasted (spaces, "bot" prefix, full URL)."""
    m = TOKEN_RE.search(t or "")
    return m.group(0) if m else (t or "").strip()


BOT_TOKEN = _clean_token(env("TELEGRAM_BOT_TOKEN"))
URGENT_SCORE = int(env("URGENT_SCORE", "8"))
BRIEF_SCORE = int(env("BRIEF_SCORE", "5"))
QUIET_START = int(env("QUIET_START", "1"))   # hour, local time
QUIET_END = int(env("QUIET_END", "7"))
MAX_URGENT_PER_HOUR = int(env("MAX_URGENT_PER_HOUR", "4"))
TRIAGE_BATCH = int(env("TRIAGE_BATCH", "25"))
TRIAGE_MAX_WAIT_MIN = int(env("TRIAGE_MAX_WAIT_MIN", "15"))
DAILY_TOKEN_BUDGET = int(env("DAILY_TOKEN_BUDGET", "180000"))
MAX_ITEM_AGE_H = int(env("MAX_ITEM_AGE_H", "36"))
BRIEF_MAX_ITEMS = int(env("BRIEF_MAX_ITEMS", "12"))
DEFAULT_BRIEF_HOURS = [8, 10, 12, 14, 16, 18, 20, 22]
UA = "Mozilla/5.0 (FounderTerminal/1.0; +https://github.com/%s)" % env("GITHUB_REPOSITORY", "founder-terminal")

DEFAULT_PROFILE = (
    "A startup founder building a data company. Wants full awareness of the tech "
    "industry: startups, venture funding, IPOs, M&A, big tech, AI, and policy that "
    "affects tech and data companies."
)

STOP = set("the a an and or of to in on for with by at from as is are was be its it this that "
           "new says said after over into about how why what will can has have just more".split())

NOW = time.time()


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- state

def _fernet():
    key = hashlib.sha256(("founder-terminal:" + BOT_TOKEN).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def load_state():
    """State lives encrypted on a public branch, keyed off the bot token."""
    try:
        with open(STATE_PATH, "rb") as f:
            return json.loads(_fernet().decrypt(f.read()))
    except FileNotFoundError:
        return {}
    except (InvalidToken, ValueError):
        log("state unreadable (bot token changed?), starting fresh")
        return {}


def save_state(s):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "wb") as f:
        f.write(_fernet().encrypt(json.dumps(s, separators=(",", ":")).encode()))


def fresh_state():
    return {
        "initialized": False, "chat_id": env("TELEGRAM_CHAT_ID") or None, "offset": 0,
        "seen": {}, "recent_titles": [], "pending": [], "queue": [], "sent": [],
        "feedback": [], "notes": [], "watch": [], "profile": "",
        "brief_hours": DEFAULT_BRIEF_HOURS, "last_brief_slot": "", "paused": False,
        "tokens": {"date": "", "used": 0}, "health": {}, "urgent_log": [],
        "force_brief": False, "stats": {"date": "", "fetched": 0, "scored": 0, "pushed": 0},
    }


# ---------------------------------------------------------------- text helpers

def clean(text, limit=None):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if limit else text


def norm_url(u):
    try:
        p = urllib.parse.urlsplit(u.strip())
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
             if not k.lower().startswith(("utm_", "ref", "fbclid", "gclid", "mc_"))]
        return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"),
                                        urllib.parse.urlencode(q), ""))
    except Exception:
        return u


def item_id(url, title):
    return hashlib.sha1((norm_url(url) or title).encode()).hexdigest()[:12]


def tokens(title):
    words = re.findall(r"[a-z0-9$]+", title.lower())
    return sorted({w for w in words if len(w) > 2 and w not in STOP})


def similar(a, b):
    a, b = set(a), set(b)
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= 0.55


def esc(s):
    return html.escape(str(s or ""), quote=False)


# ---------------------------------------------------------------- fetching

def http_get(url, headers=None, timeout=15):
    h = {"User-Agent": UA}
    h.update(headers or {})
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return NOW
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except Exception:
        pass
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except Exception:
        return NOW


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(content):
    """Parse RSS 2.0, RSS 1.0 (RDF) or Atom into simple dicts."""
    root = ET.fromstring(content)
    out = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        e = {"title": "", "link": "", "summary": "", "date": ""}
        for c in el:
            t = _local(c.tag)
            text = "".join(c.itertext()) if len(c) else (c.text or "")
            if t == "title":
                e["title"] = text
            elif t == "link":
                href = c.get("href")
                if href and (c.get("rel") in (None, "alternate")):
                    e["link"] = e["link"] or href
                elif text.strip():
                    e["link"] = e["link"] or text.strip()
            elif t in ("description", "summary") or (t in ("content", "encoded") and not e["summary"]):
                e["summary"] = e["summary"] or text
            elif t in ("pubdate", "published", "date", "updated") and not e["date"]:
                e["date"] = text
        out.append(e)
    return out


def fetch_feed(name, url, headers=None):
    out = []
    for e in parse_feed(http_get(url, headers=headers).content)[:60]:
        title = clean(e["title"])
        if not title:
            continue
        out.append({"title": title, "url": e["link"], "source": name,
                    "summary": clean(e["summary"], 500), "published": parse_date(e["date"])})
    return out


def fetch_gnews(query):
    q = urllib.parse.quote(query + " when:1d")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    items = fetch_feed("Google News", url)
    for it in items:
        # "Headline - Publisher"
        if " - " in it["title"]:
            head, pub = it["title"].rsplit(" - ", 1)
            it["title"], it["source"] = head.strip(), pub.strip()
        it["summary"] = ""  # Google News summaries just repeat the title
    return items


def fetch_hn():
    r = http_get("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=50").json()
    out = []
    for h in r.get("hits", []):
        if (h.get("points") or 0) < HN_MIN_POINTS:
            continue
        hn_link = f"https://news.ycombinator.com/item?id={h['objectID']}"
        out.append({"title": clean(h.get("title")), "url": h.get("url") or hn_link,
                    "source": f"Hacker News ({h.get('points')} pts)",
                    "summary": clean(h.get("story_text"), 400),
                    "published": h.get("created_at_i") or NOW})
    return out


def fetch_edgar(form, label):
    ua = env("SEC_USER_AGENT", UA)
    url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type="
           f"{urllib.parse.quote(form)}&company=&dateb=&owner=include&start=0&count=40&output=atom")
    out = []
    for e in fetch_feed("SEC EDGAR", url, headers={"User-Agent": ua}):
        t = e["title"]
        # "S-1 - Acme Data Inc (0001234567) (Filer)"; skip amendments like S-1/A
        if not t.startswith(form + " - "):
            continue
        company = re.sub(r"\s*\(\d+\)\s*\((Filer|Subject)\)\s*$", "", t[len(form) + 3:]).strip()
        e["title"] = f"{company} {label}"
        e["summary"] = e["summary"][:300]
        out.append(e)
    return out


def fetch_all(state):
    jobs = [(f"rss:{n}", fetch_feed, (n, u)) for n, u in RSS_FEEDS]
    queries = list(NEWS_QUERIES) + [f'"{w}"' for w in all_watch(state)]
    jobs += [(f"gnews:{i}", fetch_gnews, (q,)) for i, q in enumerate(queries)]
    jobs += [("hn", fetch_hn, ())]
    jobs += [(f"edgar:{f}", fetch_edgar, (f, l)) for f, l in EDGAR_FORMS.items()]
    items, health = [], state.setdefault("health", {})
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(fn, *args): key for key, fn, args in jobs}
        for fut in cf.as_completed(futs):
            key = futs[fut]
            try:
                got = fut.result()
                items += got
                health[key] = {"ok": NOW, "fails": 0, "err": ""}
            except Exception as e:
                h = health.setdefault(key, {"ok": 0, "fails": 0, "err": ""})
                h["fails"] += 1
                h["err"] = type(e).__name__
                log(f"source failed: {key} ({type(e).__name__})")
    # forget health for sources that no longer exist
    live = {k for k, _, _ in jobs}
    for k in list(health):
        if k not in live:
            del health[k]
    return items


def all_watch(state):
    env_watch = [w.strip() for w in env("WATCHLIST").split(",") if w.strip()]
    return list(dict.fromkeys(env_watch + state.get("watch", [])))


# ---------------------------------------------------------------- dedupe

def dedupe(items, state):
    seen, recent = state["seen"], state["recent_titles"]
    fresh = []
    for it in items:
        if NOW - it["published"] > MAX_ITEM_AGE_H * 3600:
            continue
        iid = item_id(it["url"], it["title"])
        if iid in seen:
            continue
        seen[iid] = NOW
        tk = tokens(it["title"])
        if any(similar(tk, r[0]) for r in recent):
            continue
        recent.append([tk, NOW])
        it["id"] = iid
        fresh.append(it)
    state["seen"] = {k: v for k, v in seen.items() if NOW - v < 5 * 86400}
    state["recent_titles"] = [r for r in recent if NOW - r[1] < 48 * 3600][-3000:]
    return fresh


# ---------------------------------------------------------------- LLM

def providers():
    out = []
    groq = env("LLM_API_KEY") or env("GROQ_API_KEY")
    if groq:
        out.append((env("LLM_BASE_URL", "https://api.groq.com/openai/v1"), groq,
                    env("LLM_MODEL", "openai/gpt-oss-120b")))
    if env("GEMINI_API_KEY"):
        out.append(("https://generativelanguage.googleapis.com/v1beta/openai", env("GEMINI_API_KEY"),
                    env("GEMINI_MODEL", "gemini-flash-latest")))
    return out


def parse_json(text):
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text or "", re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return None


def llm(state, system, user):
    for base, key, model in providers():
        body = {"model": model, "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        if "gpt-oss" in model:
            body["reasoning_effort"] = "low"
        for attempt in range(2):
            try:
                r = requests.post(base.rstrip("/") + "/chat/completions", json=body, timeout=90,
                                  headers={"Authorization": f"Bearer {key}"})
                if r.status_code == 429 and attempt == 0:
                    wait = float(r.headers.get("retry-after", "20") or 20)
                    if wait <= 40:
                        time.sleep(wait)
                        continue
                if r.status_code == 400 and attempt == 0:
                    # some models reject json mode or reasoning settings; retry plain
                    log(f"llm {model}: 400, retrying without extras")
                    body.pop("response_format", None)
                    body.pop("reasoning_effort", None)
                    continue
                r.raise_for_status()
                data = r.json()
                used = (data.get("usage") or {}).get("total_tokens", 0)
                state["tokens"]["used"] += used
                out = parse_json(data["choices"][0]["message"]["content"])
                if out is not None:
                    return out
                log(f"llm {model}: unparseable reply")
                break
            except Exception as e:
                log(f"llm {model} failed: {type(e).__name__} {getattr(getattr(e, 'response', None), 'status_code', '')}")
                break
    return None


def reader_context(state):
    parts = [f"Reader profile: {state.get('profile') or env('PROFILE', DEFAULT_PROFILE)}"]
    watch = all_watch(state)
    if watch:
        parts.append("Reader's watchlist (anything touching these matters a lot): " + ", ".join(watch))
    if state["notes"]:
        parts.append("Standing instructions from the reader:\n- " + "\n- ".join(n["t"] for n in state["notes"]))
    likes = [f["h"] for f in state["feedback"] if f["v"] == "+"][-12:]
    dislikes = [f["h"] for f in state["feedback"] if f["v"] == "-"][-12:]
    if likes:
        parts.append("Reader liked items like:\n- " + "\n- ".join(likes))
    if dislikes:
        parts.append("Reader did NOT want items like:\n- " + "\n- ".join(dislikes))
    return "\n\n".join(parts)


TRIAGE_RULES = """You are the filter for a founder's news terminal. Score each numbered headline 0-10 for how much this reader needs to know it.
10 = must know within the hour: notable tech IPO filing or pricing, mega funding round, major acquisition, major AI or platform launch, big regulatory action on tech, anything directly hitting the reader's market or watchlist.
8-9 = important today.
5-7 = worth one line in a brief.
0-4 = noise: opinion, reviews, gadget news, deals and sales, how-tos, listicles, minor updates, stock moves without news, tiny or shell-company filings.
When several headlines are the same story, score only the most informative one and give the others 0.
Give 0 to stories already sent (listed below).
Return json: {"keep": [[index, score], ...]} with only items scoring 5 or more."""

WRITE_RULES = """Write news terminal entries. For each item return:
"h": headline, at most 80 characters, plain and specific, no hype.
"what": one sentence on what happened, using only facts in the item.
"why": one sentence on what it means for this reader specifically. Concrete, not generic.
"more": 2 to 4 sentences of extra context: key numbers and details from the item, plus widely known background.
Never invent numbers, names or facts. Use simple words. Avoid dashes and semicolons.
Return json: {"items": [{"i": index, "h": "...", "what": "...", "why": "...", "more": "..."}]}"""


def triage(state, batch):
    sent = [s["h"] for s in state["sent"][-40:]]
    system = TRIAGE_RULES + "\n\n" + reader_context(state) + \
        ("\n\nAlready sent:\n- " + "\n- ".join(sent) if sent else "")
    user = "\n".join(f"{i}. [{it['source']}] {it['title']}" for i, it in enumerate(batch))
    out = llm(state, system, user)
    if out is None:
        return None
    scores = {}
    for pair in out.get("keep", []):
        try:
            i, s = int(pair[0]), int(pair[1])
            if 0 <= i < len(batch) and s >= BRIEF_SCORE:
                scores[i] = s
        except Exception:
            continue
    return scores


def write(state, items):
    system = WRITE_RULES + "\n\n" + reader_context(state)
    user = "\n\n".join(f"### {i}\nsource: {it['source']}\ntitle: {it['title']}\ntext: {it['summary'] or '(none)'}"
                       for i, it in enumerate(items))
    out = llm(state, system, user) or {}
    written = {}
    for w in out.get("items", []):
        try:
            i = int(w["i"])
            if 0 <= i < len(items) and w.get("h") and w.get("what"):
                written[i] = w
        except Exception:
            continue
    return written


# ---------------------------------------------------------------- telegram

def tg(method, **params):
    if not BOT_TOKEN:
        log(f"[dry run] telegram.{method}: {json.dumps(params)[:300]}")
        return {"ok": True, "result": {"message_id": 0}}
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params, timeout=20)
        data = r.json()
        if not data.get("ok"):
            log(f"telegram {method} error: {data.get('description')}")
        return data
    except Exception as e:
        log(f"telegram {method} failed: {type(e).__name__}")
        return {"ok": False}


def send(state, text, buttons=None):
    if not state.get("chat_id"):
        return None
    params = {"chat_id": state["chat_id"], "text": text, "parse_mode": "HTML",
              "link_preview_options": {"is_disabled": True}}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    return tg("sendMessage", **params)


HELP = """<b>Founder Terminal</b>
Urgent news arrives the moment it is found. Everything else comes in briefs.

/brief  send a brief now
/profile your text  tell the filter who you are
/watch a, b, c  track companies or topics
/unwatch a  stop tracking
/watching  show your watchlist
/briefs 8,12,18  set brief hours
/notes  show your standing instructions
/forget  clear standing instructions
/pause  /resume  stop or restart pushes
/status  health and usage

Any other message becomes a standing instruction, like "less crypto, more AI chips".
Tap 👍 or 👎 on urgent items to train it."""


def handle_updates(state):
    data = tg("getUpdates", offset=state["offset"], timeout=0,
              allowed_updates=["message", "callback_query"])
    if not BOT_TOKEN or not data.get("ok"):
        return
    for u in data.get("result", []):
        state["offset"] = u["update_id"] + 1
        if "callback_query" in u:
            handle_vote(state, u["callback_query"])
            continue
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        text = (msg.get("text") or "").strip()
        if not state.get("chat_id") and chat.get("type") == "private":
            state["chat_id"] = chat["id"]
            send(state, "✅ Linked. This chat is now your terminal.\n\n" + HELP)
            log("linked chat")
            continue
        if str(chat.get("id")) != str(state.get("chat_id")) or not text:
            continue
        handle_command(state, text)


def handle_vote(state, cq):
    msg = cq.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != str(state.get("chat_id")):
        return
    parts = (cq.get("data") or "").split(":")
    if len(parts) != 3 or parts[0] != "v":
        return
    _, v, iid = parts
    hit = next((s for s in state["sent"] if s["id"] == iid), None)
    if hit:
        state["feedback"] = (state["feedback"] + [{"v": v, "h": hit["h"]}])[-40:]
    tg("answerCallbackQuery", callback_query_id=cq["id"], text="Noted")
    tg("editMessageReplyMarkup", chat_id=state["chat_id"], message_id=msg.get("message_id"),
       reply_markup={"inline_keyboard": [[{"text": ("👍 noted" if v == "+" else "👎 noted"),
                                           "callback_data": "noop"}]]})


def handle_command(state, text):
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower().split("@")[0]
    arg = arg.strip()
    if cmd in ("/start", "/help"):
        send(state, HELP)
    elif cmd == "/brief":
        state["force_brief"] = True
    elif cmd == "/pause":
        state["paused"] = True
        send(state, "⏸ Paused. I keep reading but stay quiet. /resume to restart.")
    elif cmd == "/resume":
        state["paused"] = False
        send(state, "▶️ Back on.")
    elif cmd == "/profile":
        if arg:
            state["profile"] = arg[:1500]
            send(state, "Profile saved. The filter now scores everything against it.")
        else:
            send(state, "<b>Profile</b>\n" + esc(state.get("profile") or env("PROFILE", DEFAULT_PROFILE)))
    elif cmd == "/watch":
        new = [w.strip() for w in arg.split(",") if w.strip()]
        state["watch"] = list(dict.fromkeys(state["watch"] + new))[:40]
        send(state, "Watching: " + esc(", ".join(all_watch(state)) or "nothing yet"))
    elif cmd == "/unwatch":
        drop = {w.strip().lower() for w in arg.split(",")}
        state["watch"] = [w for w in state["watch"] if w.lower() not in drop]
        send(state, "Watching: " + esc(", ".join(all_watch(state)) or "nothing"))
    elif cmd == "/watching":
        send(state, "Watching: " + esc(", ".join(all_watch(state)) or "nothing yet. Use /watch a, b, c"))
    elif cmd == "/briefs":
        hours = sorted({int(h) for h in re.findall(r"\d+", arg) if 0 <= int(h) <= 23})
        if hours:
            state["brief_hours"] = hours
        send(state, "Briefs at: " + ", ".join(fmt_hour(h) for h in state["brief_hours"]))
    elif cmd == "/notes":
        notes = "\n".join(f"• {esc(n['t'])}" for n in state["notes"]) or "None yet."
        send(state, "<b>Standing instructions</b>\n" + notes)
    elif cmd == "/forget":
        state["notes"] = []
        send(state, "Cleared your standing instructions.")
    elif cmd == "/status":
        send(state, status_text(state))
    elif text.startswith("/"):
        send(state, "Unknown command.\n\n" + HELP)
    else:
        state["notes"] = (state["notes"] + [{"t": text[:300], "ts": NOW}])[-20:]
        send(state, "Got it. I'll weigh that from now on.")


def fmt_hour(h):
    return f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"


def status_text(state):
    broken = [k for k, h in state.get("health", {}).items() if h.get("fails", 0) >= 3]
    st = state["stats"]
    lines = [
        "<b>Status</b>",
        f"Paused: {'yes' if state['paused'] else 'no'}",
        f"Today: {st['fetched']} new items read, {st['scored']} kept, {st['pushed']} pushed",
        f"Waiting for next brief: {len(state['queue'])}",
        f"Waiting to be scored: {len(state['pending'])}",
        f"AI tokens today: {state['tokens']['used']:,} of {DAILY_TOKEN_BUDGET:,} budget",
        f"Sources: {len(state.get('health', {})) - len(broken)} healthy"
        + (f", broken: {esc(', '.join(broken))}" if broken else ""),
        "Briefs at: " + ", ".join(fmt_hour(h) for h in state["brief_hours"]),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- delivery

def render(it, with_more=True, icon=""):
    out = f"{icon}<b>{esc(it['h'])}</b>\n{esc(it['what'])}"
    if it.get("why"):
        out += f"\n↳ <i>{esc(it['why'])}</i>"
    if with_more and it.get("more"):
        out += f"\n<blockquote expandable>{esc(it['more'])}</blockquote>"
    if it.get("url"):
        out += f"\n<a href=\"{html.escape(it['url'], quote=True)}\">{esc(it['source'])}</a>"
    else:
        out += f"\n{esc(it['source'])}"
    return out


def push_urgent(state, it):
    icon = "🔴 " if it["score"] >= 10 else "🟠 "
    buttons = [[{"text": "👍", "callback_data": f"v:+:{it['id']}"},
                {"text": "👎", "callback_data": f"v:-:{it['id']}"}]]
    send(state, render(it, icon=icon), buttons)
    mark_sent(state, it)
    state["urgent_log"].append(NOW)


def mark_sent(state, it):
    state["sent"] = (state["sent"] + [{"id": it["id"], "h": it["h"], "ts": NOW}])[-200:]
    state["stats"]["pushed"] += 1


def quiet(local):
    if QUIET_START <= QUIET_END:
        return QUIET_START <= local.hour < QUIET_END
    return local.hour >= QUIET_START or local.hour < QUIET_END


def send_brief(state, local, label=None):
    queue = sorted(state["queue"], key=lambda x: (-x["score"], -x["ts"]))
    take, rest = queue[:BRIEF_MAX_ITEMS], queue[BRIEF_MAX_ITEMS:]
    state["queue"] = [q for q in rest if NOW - q["ts"] < 24 * 3600]
    if not take:
        return False
    icon = "☀️" if local.hour < 11 else ("🌙" if local.hour >= 20 else "📰")
    label = label or f"{fmt_hour(local.hour)} brief"
    header = f"{icon} <b>{label}</b> · {len(take)} item{'s' if len(take) != 1 else ''}"
    chunks, cur = [], header
    for it in take:
        block = render(it)
        if len(cur) + len(block) + 2 > 3900:
            chunks.append(cur)
            cur = block
        else:
            cur += "\n\n" + block
        mark_sent(state, it)
    chunks.append(cur)
    for c in chunks:
        send(state, c)
    return True


def maybe_brief(state, local):
    if state.pop("force_brief", False):
        if not send_brief(state, local, "Brief on demand"):
            send(state, "Nothing new worth your time since the last brief.")
        return
    slots = [h for h in state["brief_hours"] if h <= local.hour]
    if not slots:
        return
    slot = f"{local:%Y-%m-%d} {slots[-1]:02d}"
    if slot == state["last_brief_slot"]:
        return
    state["last_brief_slot"] = slot
    if local.hour - slots[-1] <= 2 and not state["paused"]:
        send_brief(state, local.replace(hour=slots[-1]))


# ---------------------------------------------------------------- main

def process_pending(state, local, force=False):
    pending = state["pending"]
    if not pending or not providers():
        return
    over_budget = state["tokens"]["used"] >= DAILY_TOKEN_BUDGET
    oldest = min(p["added"] for p in pending)
    wait = 60 if over_budget else TRIAGE_MAX_WAIT_MIN
    if not force and len(pending) < TRIAGE_BATCH and NOW - oldest < wait * 60:
        return
    for _ in range(3):  # at most 3 batches per run
        batch, state["pending"] = state["pending"][:80], state["pending"][80:]
        if not batch:
            break
        scores = triage(state, batch)
        if scores is None:  # LLM down: put back and try next run
            state["pending"] = batch + state["pending"]
            break
        keep = sorted(scores.items(), key=lambda kv: -kv[1])
        if over_budget:
            keep = [kv for kv in keep if kv[1] >= URGENT_SCORE]
        keep = keep[:10]
        state["stats"]["scored"] += len(keep)
        if not keep:
            continue
        chosen = [dict(batch[i], score=s) for i, s in keep]
        written = write(state, chosen)
        for i, it in enumerate(chosen):
            w = written.get(i)
            if not w:
                continue
            it.update(h=clean(w["h"], 120), what=clean(w["what"], 400),
                      why=clean(w.get("why"), 400), more=clean(w.get("more"), 900), ts=NOW)
            it.pop("summary", None)
            route(state, it, local)


def route(state, it, local):
    state["urgent_log"] = [t for t in state["urgent_log"] if NOW - t < 3600]
    urgent = it["score"] >= URGENT_SCORE
    can_push = (not state["paused"]
                and (not quiet(local) or it["score"] >= 10)
                and len(state["urgent_log"]) < MAX_URGENT_PER_HOUR)
    if urgent and can_push:
        push_urgent(state, it)
    else:
        state["queue"].append(it)


def main():
    state = fresh_state() | load_state()
    local = dt.datetime.fromtimestamp(NOW, TZ)
    today = f"{local:%Y-%m-%d}"
    if state["tokens"]["date"] != today:
        state["tokens"] = {"date": today, "used": 0}
    if state["stats"]["date"] != today:
        state["stats"] = {"date": today, "fetched": 0, "scored": 0, "pushed": 0}

    handle_updates(state)

    items = fetch_all(state)
    fresh = dedupe(items, state)
    log(f"fetched {len(items)} items, {len(fresh)} new")

    if not state["initialized"]:
        # First run: everything currently out there counts as seen, so you
        # are not flooded with yesterday's news.
        state["initialized"] = True
        save_state(state)
        if state.get("chat_id"):
            send(state, "✅ Founder Terminal is live. New stories start flowing from now.")
        log("initialized")
        return

    state["stats"]["fetched"] += len(fresh)
    for it in fresh:
        it["added"] = NOW
    state["pending"] = (state["pending"] + fresh)[-400:]
    state["queue"] = [q for q in state["queue"] if NOW - q["ts"] < 24 * 3600]

    process_pending(state, local, force=bool(state.get("force_brief")))
    maybe_brief(state, local)

    save_state(state)
    log(f"done. pending {len(state['pending'])}, queue {len(state['queue'])}, "
        f"tokens today {state['tokens']['used']}")


if __name__ == "__main__":
    if not providers():
        log("No AI key set (GROQ_API_KEY or GEMINI_API_KEY). Items will be collected but not scored.")
    if not BOT_TOKEN:
        log("No TELEGRAM_BOT_TOKEN set. Running dry.")
    else:
        raw = env("TELEGRAM_BOT_TOKEN")
        me = tg("getMe")
        log(f"telegram token check: pasted len {len(raw)}, cleaned len {len(BOT_TOKEN)}, "
            f"format ok {bool(TOKEN_RE.fullmatch(BOT_TOKEN))}, "
            f"getMe ok {me.get('ok')} {(me.get('result') or {}).get('username', '')}")
    main()
    sys.exit(0)

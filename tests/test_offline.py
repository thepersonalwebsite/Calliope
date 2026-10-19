"""Offline end-to-end test: fake feeds, fake LLM, fake Telegram. Run: python tests/test_offline.py"""
import json, os, sys, tempfile, datetime as dt
from email.utils import format_datetime

tmp = tempfile.mkdtemp()
os.environ.update(TELEGRAM_BOT_TOKEN="123:fake", GROQ_API_KEY="k", STATE_PATH=f"{tmp}/state.enc",
                  WATCHLIST="Example Media, Sample Co")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import terminal as T  # noqa: E402

CLOCK = {"now": dt.datetime(2026, 10, 19, 9, 3, tzinfo=T.TZ).timestamp()}
SENT, UPDATES, LLM_CALLS = [], [], []
STORIES = {"rss": [], "gnews": [], "edgar": []}


def rss(items):
    body = "".join(f"<item><title>{t}</title><link>{u}</link><description>&lt;p&gt;{d}&lt;/p&gt;</description>"
                   f"<pubDate>{format_datetime(dt.datetime.fromtimestamp(CLOCK['now'] - 600, dt.timezone.utc))}</pubDate></item>"
                   for t, u, d in items)
    return f"<?xml version='1.0'?><rss><channel><title>x</title>{body}</channel></rss>".encode()


def atom(entries):
    ts = dt.datetime.fromtimestamp(CLOCK["now"] - 300, dt.timezone.utc).isoformat().replace("+00:00", "Z")
    body = "".join(f"<entry><title>{t}</title><link rel='alternate' type='text/html' href='{u}'/>"
                   f"<summary type='html'>filed</summary><updated>{ts}</updated></entry>" for t, u in entries)
    return f"<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'><title>x</title>{body}</feed>".encode()


class Resp:
    def __init__(self, content=b"", js=None, status=200, headers=None):
        self.content, self._js, self.status_code, self.headers = content, js, status, headers or {}

    def json(self):
        return self._js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise T.requests.HTTPError(response=self)


def fake_get(url, headers=None, timeout=None):
    if "theverge" in url:
        return Resp(status=503)  # one broken source
    if "techcrunch.com" in url:
        return Resp(rss(STORIES["rss"]))
    if "news.google.com" in url:
        return Resp(rss([(t + " - Reuters", u, "") for t, u, _ in STORIES["gnews"]]) if "Example" in url else rss([]))
    if "hn.algolia" in url:
        return Resp(js={"hits": [{"title": "Show HN: tiny thing", "url": "https://x.dev", "points": 40,
                                  "objectID": "1", "created_at_i": CLOCK["now"]}]})
    if "sec.gov" in url and "S-1" in url:
        return Resp(atom(STORIES["edgar"]))
    return Resp(rss([]))


def fake_post(url, json=None, headers=None, timeout=None):
    if "chat/completions" in url:
        system, user = json["messages"][0]["content"], json["messages"][1]["content"]
        LLM_CALLS.append(system[:20])
        if system.startswith("You are the filter"):
            keep = []
            for line in user.splitlines():
                i, _, rest = line.partition(". ")
                score = 10 if "Acme" in rest else 9 if "Example" in rest else 6 if "raises" in rest else 2
                if score >= 5:
                    keep.append([int(i), score])
            out = {"keep": keep}
        else:
            out = {"items": []}
            for block in user.split("### ")[1:]:
                i = int(block.split("\n")[0])
                title = block.split("title: ")[1].split("\n")[0]
                out["items"].append({"i": i, "h": title, "what": f"What: {title}.",
                                     "why": "Why it matters <to you> & more.", "more": "Extra context."})
        return Resp(js={"choices": [{"message": {"content": __import__("json").dumps(out)}}],
                        "usage": {"total_tokens": 1000}})
    method = url.rsplit("/", 1)[-1]
    if method == "getUpdates":
        res = list(UPDATES)
        UPDATES.clear()
        return Resp(js={"ok": True, "result": res})
    SENT.append((method, json))
    return Resp(js={"ok": True, "result": {"message_id": 1}})


T.requests.get = fake_get
T.requests.post = fake_post


def run(minutes_later=0):
    CLOCK["now"] += minutes_later * 60
    T.NOW = CLOCK["now"]
    SENT.clear()
    T.main()
    return T.load_state()


uid = [0]


def user_msg(text):
    uid[0] += 1
    UPDATES.append({"update_id": uid[0], "message": {"chat": {"id": 42, "type": "private"}, "text": text}})


# Run 1: user says hi, first run links chat and marks existing news as seen
STORIES["rss"] = [("Old story already out", "https://tc.com/old", "old")]
user_msg("hi")
s = run()
assert s["chat_id"] == 42 and s["initialized"], s
assert any("Linked" in p["text"] for m, p in SENT if m == "sendMessage")
print("run1 ok: linked + initialized, sent", len(SENT))

# Run 2: new stories arrive, one duplicate headline from another source
STORIES["rss"] = [("Old story already out", "https://tc.com/old", "old"),
                  ("Acme Data files to go public", "https://tc.com/acme", "Acme filed an S-1 showing $400M revenue"),
                  ("Fintech startup raises $50M Series B", "https://tc.com/fin", "raised"),
                  ("Best headphones of 2026", "https://tc.com/hp", "review")]
STORIES["edgar"] = [("S-1 - Bolt Robotics Inc (0001234567) (Filer)", "https://sec.gov/bolt"),
                     ("S-1/A - Old Co (0000000001) (Filer)", "https://sec.gov/old")]
STORIES["gnews"] = [("Example Media buys digital billboard network", "https://reuters.com/example", "")]
user_msg("less consumer gadgets, more data infrastructure")
s = run(5)
titles = [p["text"] for m, p in SENT if m == "sendMessage"]
assert any("Got it" in t for t in titles), titles
# items fewer than TRIAGE_BATCH and too new: should wait
assert len(s["pending"]) > 0 and not any("Acme" in t for t in titles), s["pending"]
print("run2 ok: queued", len(s["pending"]), "items for scoring; note saved:", s["notes"][0]["t"])

# Run 3: 16 minutes later, triage fires. Acme (10) and Example (9) push now, fintech (6) waits for brief
s = run(16)
pushes = [p for m, p in SENT if m == "sendMessage"]
txt = "\n---\n".join(p["text"] for p in pushes)
print(txt)
assert any("🔴" in p["text"] and "Acme" in p["text"] for p in pushes)
assert any("Example" in p["text"] for p in pushes)
assert "&amp; more" in txt and "<to you>" not in txt  # escaped
assert not any("headphones" in p["text"] for p in pushes)
assert any("Fintech" in q["h"] for q in s["queue"]), s["queue"]
assert all(p.get("reply_markup") for p in pushes)
assert s["health"]["rss:The Verge"]["fails"] == 3
print("run3 ok: urgent pushed, brief item queued. LLM calls:", LLM_CALLS)

# Run 4: 10 AM slot -> brief with fintech item. Also a thumbs up on Acme.
acme_id = next(x["id"] for x in s["sent"] if "Acme" in x["h"])
uid[0] += 1
UPDATES.append({"update_id": uid[0], "callback_query": {"id": "c1", "data": f"v:+:{acme_id}",
                "message": {"chat": {"id": 42}, "message_id": 7}}})
CLOCK["now"] = dt.datetime(2026, 10, 19, 10, 2, tzinfo=T.TZ).timestamp()
s = run()
brief = [p["text"] for m, p in SENT if m == "sendMessage"]
print("\n".join(brief))
assert any("10 AM brief" in b and "Fintech" in b for b in brief), brief
assert s["feedback"][0]["v"] == "+" and "Acme" in s["feedback"][0]["h"]
assert not s["queue"]
print("run4 ok: brief sent, feedback stored")

# Run 5: same slot, nothing resent. /status and /watch work
user_msg("/watch Placeholder Inc, Demo Labs")
user_msg("/status")
s = run(5)
out = [p["text"] for m, p in SENT if m == "sendMessage"]
print("\n".join(out))
assert "Placeholder Inc" in s["watch"] and not any("brief</b>" in o for o in out)
assert any("AI tokens today" in o for o in out)

# State file is encrypted
raw = open(f"{tmp}/state.enc", "rb").read()
assert b"Acme" not in raw and b"Placeholder" not in raw
print("\nALL TESTS PASSED")

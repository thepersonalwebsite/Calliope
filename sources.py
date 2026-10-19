"""Every source the terminal reads. Add or remove lines freely.

Your private watchlist (competitors, your market) is NOT here because this
repo is public. Set it from Telegram with /watch, or in the WATCHLIST secret.
"""

# (name, RSS or Atom URL)
RSS_FEEDS = [
    ("Techmeme", "https://www.techmeme.com/feed.xml"),
    ("TechCrunch", "https://techcrunch.com/feed/"),
    ("The Information", "https://www.theinformation.com/feed"),
    ("Bloomberg Tech", "https://feeds.bloomberg.com/technology/news.rss"),
    ("Bloomberg Markets", "https://feeds.bloomberg.com/markets/news.rss"),
    ("WSJ Tech", "https://feeds.a.dj.com/rss/RSSWSJD.xml"),
    ("CNBC Tech", "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("Wired", "https://www.wired.com/feed/rss"),
    ("VentureBeat", "https://venturebeat.com/feed/"),
    ("Crunchbase News", "https://news.crunchbase.com/feed/"),
    ("Axios", "https://api.axios.com/feed/"),
    ("Stratechery", "https://stratechery.com/feed/"),
]

# Google News searches (last 24h). These catch Reuters, AP, Fortune, Forbes,
# Business Insider and others that have no public feed.
NEWS_QUERIES = [
    "startup raises Series A OR Series B OR Series C",
    "startup funding round valuation billion",
    "tech IPO filing OR priced OR debut",
    "acquires startup OR acquisition tech company",
    "OpenAI OR Anthropic OR Nvidia OR Google DeepMind OR xAI",
    "Y Combinator OR Andreessen Horowitz OR Sequoia Capital",
    "FTC OR DOJ antitrust tech",
    "data privacy law OR data broker regulation",
]

# Hacker News front page stories at or above this many points
HN_MIN_POINTS = 150

# SEC EDGAR live filings. S-1/F-1 = files to go public. 424B4 = IPO priced.
EDGAR_FORMS = {
    "S-1": "files to go public (S-1)",
    "F-1": "files to go public in the US (F-1)",
    "424B4": "prices its IPO (final prospectus 424B4)",
}

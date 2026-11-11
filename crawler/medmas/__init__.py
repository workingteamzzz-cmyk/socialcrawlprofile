"""medmascrawler — keyword-based Indonesian news crawler engine.

Collects EVERY news link matching your keywords from multiple sources
(Google News, media sitemaps, RSS feeds, Bing News, DuckDuckGo), stores
them in SQLite (cross-run dedup), then optionally extracts article content
(with people/sentiment analysis) and exports to Excel/CSV — including a
dashboard-ready Upload Online format.
"""

__version__ = "2.0.0"

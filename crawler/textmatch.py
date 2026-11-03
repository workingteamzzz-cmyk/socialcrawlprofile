"""Read a result's text the way a careful person would: drop it if it contains an excluded word, and (optionally)
if it does not mention the keyword at all. Platform searches often ignore NOT/exclude operators (Threads, TikTok,
Instagram, Facebook) or return loosely related posts, so the check is done on what the page actually shows.

Matching is case-insensitive and works on hashtags and mentions too: excluding "promo" also drops "#promo",
"promosi" and "#PromoAkhirTahun"; the keyword "pemadaman listrik" also matches "#pemadamanlistrik"."""
from __future__ import annotations

import re
import unicodedata


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"\s+", " ", t)


def has_word(text_norm: str, word: str) -> bool:
    """`word` appears at the start of a word in the (normalised) text — "#Promo2026" and "promosi" count for "promo"."""
    w = norm(word).strip().lstrip("#@-").strip('"')
    if not w:
        return False
    return re.search(r"(?<![^\W_])" + re.escape(w), text_norm) is not None


def _terms(keyword: str) -> list[str]:
    return [t.lstrip("#@") for t in re.findall(r'[^\s"()]+', keyword) if not t.startswith("-") and t.upper() != "OR"]


def mentions(text: str, keyword: str, exact: bool = False) -> bool:
    k = keyword.strip()
    if not k or re.search(r"\bOR\b|\(|site:", k):
        return True                                   # advanced queries: trust the search engine
    t = norm(text)
    words = _terms(k)
    if not words:
        return True
    joined = "".join(words)
    if has_word(t, joined) or has_word(t.replace(" ", ""), joined):
        return True                                   # "#pemadamanlistrik", "PLN" in "#PLNPeduli"
    if exact or k.startswith('"'):
        return has_word(t, " ".join(words))
    return all(has_word(t, w) for w in words)


def verdict(text: str, keyword: str, exclude: list[str], must_mention: bool, exact: bool = False) -> str | None:
    """None = keep; "excluded" = contains an excluded word; "off_topic" = does not mention the keyword.
    Empty text (nothing readable) is kept."""
    if not (text or "").strip():
        return None
    t = norm(text)
    if any(has_word(t, w) for w in exclude or []):
        return "excluded"
    if must_mention and not mentions(text, keyword, exact):
        return "off_topic"
    return None

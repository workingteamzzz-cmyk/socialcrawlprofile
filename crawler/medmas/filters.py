"""Non-article link filtering & keyword matching."""

import re
from urllib.parse import urlparse

SKIP_DOMAINS = ("instagram.com", "facebook.com", "twitter.com", "x.com",
                "youtube.com", "tiktok.com", "threads.net", "linkedin.com",
                "wikipedia.org", "google.com", "shopee.co.id", "tokopedia.com")
SKIP_PATTERNS = (
    # tag / category / topic listings
    "/tag/", "/tags/", "/topic", "/topik/", "/label/", "/kategori/",
    "/category/", "/rubrik/", "/kanal/", "/channel/",
    # search result pages (WordPress uses ?s=, many CMSs ?q= / ?search=)
    "/search", "/cari", "/pencarian", "?q=", "?s=", "&s=", "?search=",
    # indexes, archives, pagination, author & static pages
    "/indeks", "/index/", "/arsip", "/archive", "/page/", "/author/",
    "/penulis/", "/redaksi", "/tentang-kami", "/kontak", "/sitemap",
    "/terkini", "/populer", "/rss", "/feed",
    # non-article media pages
    "/foto/", "/video/", "/live/", "/galeri",
)


def is_article_link(url: str) -> bool:
    """Reject non-article links: social media, tag/index/search pages,
    section fronts and site home pages."""
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().replace("www.", "")
    except ValueError:
        return False
    if not host:
        return False
    # a home page or a bare one-word section front is never an article
    path = parsed.path.strip("/")
    if not path and not parsed.query:
        return False
    if path and "/" not in path and "-" not in path and "." not in path \
            and len(path) < 20 and not parsed.query:
        return False    # e.g. /ekonomi , /nasional
    if any(host == d or host.endswith("." + d) for d in SKIP_DOMAINS):
        return False
    u = url.lower()
    if any(p in u for p in SKIP_PATTERNS):
        return False
    return True


# ============================================================
#              BOOLEAN KEYWORD QUERIES (AND / OR / NOT)
# ============================================================
# Syntax:
#   "quoted phrase"        must appear verbatim
#   word word              implicit AND (all must appear)
#   a AND b                explicit AND
#   a OR b                 either one is enough
#   NOT a  /  -a           must NOT appear
#   ( ... )                grouping, e.g.  "PLN" AND (padam OR byarpet) NOT promo
# Operators are UPPERCASE (the Google/Boolean convention) so ordinary
# lowercase words like "or" inside a phrase are never mistaken for one.

_TOKEN_RE = re.compile(r'\s*(\(|\)|"[^"]*"|-|[^\s()"]+)')

# Phone/Mac/Word keyboards auto-curl quotes: “pln” instead of "pln".
# Without normalization the curly characters become part of the term and
# match NOTHING — a silent total-miss. Normalize every quote look-alike.
_CURLY_QUOTES = str.maketrans({c: '"' for c in '“”„‟«»＂'})


def _tokenize(query: str):
    query = str(query).translate(_CURLY_QUOTES)
    tokens, pos = [], 0
    while pos < len(query):
        m = _TOKEN_RE.match(query, pos)
        if not m:
            break
        pos = m.end()
        tok = m.group(1)
        if tok in ("(", ")"):
            tokens.append((tok, None))
        elif tok in ("AND", "OR", "NOT"):
            tokens.append((tok, None))
        elif tok == "-":
            tokens.append(("NOT", None))
        elif tok.startswith("-") and len(tok) > 1:
            tokens.append(("NOT", None))
            rest = tok[1:]
            tokens.append(("TERM", rest.strip('"').lower()))
        else:
            tokens.append(("TERM", tok.strip('"').lower()))
    return tokens


def _parse(query: str):
    """Recursive-descent parse into a tree:
    ('term', text) | ('not', node) | ('and', [nodes]) | ('or', [nodes])"""
    tokens = _tokenize(query)
    pos = 0

    def peek():
        return tokens[pos][0] if pos < len(tokens) else None

    def parse_or():
        nonlocal pos
        nodes = [parse_and()]
        while peek() == "OR":
            pos += 1
            nodes.append(parse_and())
        return nodes[0] if len(nodes) == 1 else ("or", nodes)

    def parse_and():
        nonlocal pos
        nodes = []
        while True:
            t = peek()
            if t == "AND":
                pos += 1
                continue
            if t in (None, "OR", ")"):
                break
            node = parse_unary()
            if node is not None:
                nodes.append(node)
        if not nodes:
            return None
        return nodes[0] if len(nodes) == 1 else ("and", nodes)

    def parse_unary():
        nonlocal pos
        t = peek()
        if t == "NOT":
            pos += 1
            inner = parse_unary()
            return ("not", inner) if inner is not None else None
        if t == "(":
            pos += 1
            inner = parse_or()
            if peek() == ")":
                pos += 1
            return inner
        if t == "TERM":
            text = tokens[pos][1]
            pos += 1
            return ("term", text) if text else None
        pos += 1  # stray token
        return None

    return parse_or()


def _evaluate(node, text: str) -> bool:
    if node is None:
        return False
    kind = node[0]
    if kind == "term":
        return node[1] in text
    if kind == "not":
        return not _evaluate(node[1], text)
    if kind == "and":
        return all(_evaluate(n, text) for n in node[1])
    if kind == "or":
        return any(_evaluate(n, text) for n in node[1])
    return False


def _has_positive(node) -> bool:
    """A query of only NOT terms must not match everything."""
    if node is None:
        return False
    kind = node[0]
    if kind == "term":
        return True
    if kind == "not":
        return False
    return any(_has_positive(n) for n in node[1])


def _negative_terms(node, negated=False, out=None):
    """Every term sitting under an odd number of NOTs."""
    out = [] if out is None else out
    if node is None:
        return out
    kind = node[0]
    if kind == "term":
        if negated:
            out.append(node[1])
    elif kind == "not":
        _negative_terms(node[1], not negated, out)
    else:
        for n in node[1]:
            _negative_terms(n, negated, out)
    return out


def _to_engine(node) -> str:
    """Render the tree as a Google/Bing query string."""
    if node is None:
        return ""
    kind = node[0]
    if kind == "term":
        return f'"{node[1]}"' if " " in node[1] else node[1]
    if kind == "not":
        inner = _to_engine(node[1])
        if node[1][0] in ("and", "or"):
            return f"-({inner})"
        return f"-{inner}"
    if kind == "and":
        return " ".join(
            f"({_to_engine(n)})" if n[0] == "or" else _to_engine(n)
            for n in node[1])
    if kind == "or":
        return " OR ".join(_to_engine(n) for n in node[1])
    return ""


def describe_keyword(keyword: str) -> str:
    """Human-readable rendering of how the engine understood the query —
    shown live in the dashboard so a mistyped expression is caught before
    a long crawl starts."""
    def walk(node):
        if node is None:
            return "(nothing)"
        kind = node[0]
        if kind == "term":
            return f'"{node[1]}"'
        if kind == "not":
            return f"NOT {walk(node[1])}"
        joiner = " AND " if kind == "and" else " OR "
        inner = joiner.join(walk(n) for n in node[1])
        return f"({inner})"
    tree = _parse(keyword)
    text = walk(tree)
    return text[1:-1] if text.startswith("(") and text.endswith(")") else text


def validate_keyword(keyword: str) -> str:
    """Return an empty string when the query is usable, otherwise a short
    problem description."""
    if not keyword.strip():
        return "empty"
    if keyword.count("(") != keyword.count(")"):
        return "unbalanced parentheses"
    if keyword.count('"') % 2:
        return "unclosed quote"
    tree = _parse(keyword)
    if tree is None:
        return "no searchable term"
    if not _has_positive(tree):
        return "only exclusions — add at least one term to search for"
    return ""


def keyword_query(keyword: str) -> str:
    """The keyword as a search-engine query string (Google News and Bing
    understand `OR`, parentheses and the minus operator natively)."""
    return _to_engine(_parse(keyword)).strip() or keyword.strip()


def keyword_queries(keyword: str, max_variants: int = 8) -> list:
    """Engine queries for the keyword, ONE PER TOP-LEVEL OR ALTERNATIVE.

    Search engines cap results PER QUERY (Google News: ~100), so
    '"pln" OR "plts" OR "100 gwp"' as one query yields ~100 links total —
    but as three separate queries it yields up to ~300. NOT terms apply
    to every variant. Falls back to the single combined query when the
    keyword has no top-level OR (or too many alternatives)."""
    try:
        tree = _parse(keyword)
    except Exception:
        return [keyword_query(keyword)]
    core, negs = tree, []
    if tree and tree[0] == "and":
        ors = [n for n in tree[1] if n[0] == "or"]
        nots = [n for n in tree[1] if n[0] == "not"]
        rest = [n for n in tree[1] if n[0] not in ("or", "not")]
        if len(ors) == 1 and not rest:
            core, negs = ors[0], nots
    if (not core or core[0] != "or"
            or not 2 <= len(core[1]) <= max_variants):
        return [keyword_query(keyword)]
    out = []
    for branch in core[1]:
        node = ("and", [branch] + negs) if negs else branch
        q = _to_engine(node).strip()
        if q:
            out.append(q)
    return out or [keyword_query(keyword)]


def keyword_excluded(keyword: str, *texts) -> bool:
    """True if any NOT term of the keyword appears in the texts."""
    combined = " ".join(t for t in texts if t).lower()
    if not combined:
        return False
    return any(n in combined for n in _negative_terms(_parse(keyword)))


def keyword_match(keyword: str, *texts) -> bool:
    """True if the boolean keyword expression matches the texts
    (title/content/URL slug combined)."""
    combined = " ".join(t for t in texts if t).lower()
    if not combined:
        return False
    tree = _parse(keyword)
    if not _has_positive(tree):
        return False
    return _evaluate(tree, combined)


def url_slug(url: str) -> str:
    """The URL path as plain text (for keyword matching against the link)."""
    path = urlparse(url).path
    return re.sub(r"[-_/+.]", " ", path)


def within_range(date_str: str, start: str, end: str) -> bool:
    """Check an ISO date against the range. Missing/invalid dates pass
    (kept for manual checking instead of silently disappearing)."""
    t = str(date_str)[:10]
    if not re.match(r"\d{4}-\d{2}-\d{2}", t):
        return True
    return start <= t <= end

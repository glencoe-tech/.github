#!/usr/bin/env python3
"""The check behind the Glencoe Website SEO Standard: one engine for every website and framework.

  audit     check a site over HTTP (production or a PR preview) or a build directory
  preview   wait for the deploy check on a commit (Cloudflare Workers Builds or Pages) and print
            the preview URL it built

Modes of `audit`:
  production  the live site: redirects, status, indexing directives, robots.txt, sitemaps and the
              URLs they list, canonicals, metadata, structured data, private hosts, crawler access
  preview     a PR preview at --base, judged as the production site it will become: canonicals and
              sitemap URLs must name the PRODUCTION host; previews are expected to be noindex
  artifact    a static build directory (--dist): robots.txt, sitemap files and the homepage markup

Severity: critical findings fail (exit 1): the site would be wrongly indexed or not indexed at all.
warn findings are annotations and never fail. info is context. Exit 2: the check could not run or
could not reach the site (it fails closed, and never reports an unreachable site as an SEO defect).

Standard-library Python 3.11+. Used by the composite action in this directory and by
glencoe-infra's Search Console monitor.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import email.utils
import hashlib
import io
import html.parser
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import zipfile
import zlib
import urllib.request
import xml.etree.ElementTree as ET

USER_AGENT = "Mozilla/5.0 (compatible; Glencoe-SEO-Standard/1.0; +https://glencoetech.com)"
SM = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
RETRY_STATUS = {429, 500, 502, 503, 504}
REDIRECTS = (301, 302, 303, 307, 308)
# Sitemap URLs fetched per audit, spread evenly across the whole sitemap (first and last always
# included). A preview is fetched over the network on every pull request, so it samples fewer.
SAMPLE_URLS = {"production": 20, "artifact": 20, "preview": 5}
# Served content: what a crawler that does not run JavaScript reads. Most AI-search crawlers do
# not run it, so a client-rendered page (a shell with a handful of words and no <h1>) is empty to
# them even when Google renders it. 50 words separates a shell from a real page without judging
# length; a short page under a server-rendered <h1> (a form) passes.
SERVED_WORDS = 50
# Not served text. <noscript> is: a crawler that does not run JavaScript reads it.
HIDDEN_ELEMENTS = {"script", "style", "template", "svg"}
# Elements that separate words; inline elements (<span>, <a>, <b>) do not, so "wo<span>rd</span>"
# is one word, as a reader sees it.
BLOCK_ELEMENTS = {"address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "fieldset",
                  "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
                  "li", "main", "nav", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul", "body"}
HEAD_ELEMENTS = {"head", "title", "meta", "link", "script", "style", "base", "noscript", "template"}
SOFT_404_PATH = "/glencoe-seo-standard-missing-page-probe"
MAX_RETRY_WAIT = 60          # seconds; a longer Retry-After is honoured by not retrying in this run
INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
INDEXNOW_BATCH = 10_000      # the protocol's limit per request
INDEXNOW_PAGES = 1_000       # sitemap URLs fetched and fingerprinted per run
INDEXNOW_ATTEMPTS = 5        # runs a refused change is retried before it waits for the next change
INDEXNOW_ACCEPTED = (200, 202)
INDEXNOW_FORGET_DAYS = 365   # a URL not seen for this long leaves the ledger
LEDGER_LOG = 200             # submission records kept in the ledger
LEDGER_ARTIFACT = "seo-standard-indexnow-ledger"
LEDGER_PAGES = 50             # artifact listing pages read (100 each) before giving up
CHILD_SITEMAPS = 10
# Crawlers that put a site in search results, classic or AI. Blocking one hides the site from it.
SEARCH_CRAWLERS = ("googlebot", "bingbot")
AI_SEARCH_CRAWLERS = ("oai-searchbot", "chatgpt-user", "perplexitybot", "perplexity-user",
                      "claude-searchbot", "claude-user", "applebot", "duckduckbot")
# Crawlers that collect training data. Blocking them is a legitimate per-client choice.
TRAINING_CRAWLERS = ("gptbot", "google-extended", "ccbot", "claudebot", "applebot-extended", "bytespider")
# Robots directives that themselves contain a colon, so `name: value` is not a crawler scope.
COLON_DIRECTIVES = {"unavailable_after", "max-snippet", "max-image-preview", "max-video-preview"}


class Unauditable(Exception):
    """The site could not be audited (unreachable, or a preview behind a login). Exit 2, never a
    critical finding: a check that verified nothing is not a verdict on the site."""


# --- HTTP ----------------------------------------------------------------------------------------

class Response:
    def __init__(self, status: int, headers: dict, body: bytes, url: str = ""):
        self.status, self.headers, self.body, self.url = status, headers, body, url

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def headers_of(items) -> dict:
    """Lower-cased headers; a repeated header's values are kept one per line (X-Robots-Tag)."""
    out: dict[str, str] = {}
    for k, v in items:
        k = k.lower()
        out[k] = f"{out[k]}\n{v}" if k in out else v
    return out


def _send(req: urllib.request.Request, timeout: float) -> Response:  # pragma: no cover - real network
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return Response(r.status, headers_of(r.headers.items()), r.read(5_000_000), r.url)
    except urllib.error.HTTPError as e:
        return Response(e.code, headers_of(e.headers.items()), e.read(5_000_000), req.full_url)


def retry_after(resp: "Response", now: datetime.datetime | None = None) -> float | None:
    """Seconds the server asked us to wait (RFC 9110: delay-seconds or an HTTP-date), or None."""
    value = resp.header("retry-after").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return max((when - now).total_seconds(), 0.0)


class Http:
    """HTTP with bounded retries (429 and 5xx, network errors). `send` and `sleep` are injectable."""

    def __init__(self, send=_send, sleep=time.sleep, attempts: int = 4, timeout: float = 20.0):
        self.send, self.sleep, self.attempts, self.timeout = send, sleep, attempts, timeout

    def request(self, method: str, url: str, headers: dict | None = None, body: bytes | None = None) -> Response:
        hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
        last: Exception | None = None
        resp = None
        wait = 0.0
        for n in range(self.attempts):
            if n:
                self.sleep(min(max(2 ** n, wait), MAX_RETRY_WAIT))
            try:
                resp = self.send(urllib.request.Request(url, data=body, headers=hdrs, method=method), self.timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last, resp = e, None
                continue
            if resp.status not in RETRY_STATUS:
                return resp
            asked = retry_after(resp)
            if asked is not None and asked > MAX_RETRY_WAIT:
                return resp  # retrying sooner than the server asked would ignore it; the caller defers
            wait = asked or 0.0
            last = None
        if resp is None:
            raise ConnectionError(f"{method} {url}: {last}")
        return resp

    def get(self, url: str, headers: dict | None = None) -> Response:
        return self.request("GET", url, headers)

    def follow(self, url: str, limit: int = 6) -> list[str]:
        """Every URL a GET visits, following redirects by hand."""
        chain = [url]
        r = self.get(url)
        while r.status in REDIRECTS and r.header("location") and len(chain) <= limit:
            url = urllib.parse.urljoin(url, r.header("location"))
            chain.append(url)
            r = self.get(url)
        return chain


# --- parsing -------------------------------------------------------------------------------------

class Page(html.parser.HTMLParser):
    """What search engines read from a page's markup."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.title, self.canonical, self.robots, self.viewport, self.base = "", "", "", False, ""
        self.meta: dict[str, str] = {}
        self.jsonld: list[str] = []
        self.h1 = 0
        self._in_title = self._in_ld = self._in_head = False
        self._hidden = 0
        self._chunks: list[str] = []
        self.feed(text)
        self.close()
        self.text = " ".join("".join(self._chunks).split())  # the served text, as a reader sees it
        self.words = len(self.text.split())

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "head":
            self._in_head = True
        elif tag == "body" or tag not in HEAD_ELEMENTS:
            self._in_head = False  # the head ends where browsers end it, even without </head>
        if tag in HIDDEN_ELEMENTS:
            self._hidden += 1
        elif tag == "h1" and not self._hidden:
            self.h1 += 1
        if tag in BLOCK_ELEMENTS and not self._hidden:  # hidden subtrees add no word breaks either
            self._chunks.append(" ")
        if tag == "title":
            self._in_title = True
        elif tag == "base" and a.get("href") and not self.base:
            self.base = a["href"]
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split():
            self.canonical = self.canonical or a.get("href", "")
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("robots", "googlebot", "bingbot"):
                self.robots += "," + a.get("content", "").lower()
            elif key == "viewport":
                self.viewport = True
            elif key:
                self.meta.setdefault(key, a.get("content", ""))
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_ld = True
            self.jsonld.append("")

    def handle_endtag(self, tag):
        if tag == "head":
            self._in_head = False
        if tag in HIDDEN_ELEMENTS and self._hidden:
            self._hidden -= 1
        if tag in BLOCK_ELEMENTS and not self._hidden:
            self._chunks.append(" ")
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_ld = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_ld:
            self.jsonld[-1] += data
        elif not self._hidden:
            if data.strip():
                self._in_head = False  # text cannot sit in <head>
            if not self._in_head:
                self._chunks.append(data)


def noindex(directives: str) -> bool:
    """`noindex` or `none` among meta robots directives (comma- or space-separated)."""
    return any(d in ("noindex", "none") for d in re.split(r"[,\s]+", directives.lower()))


def header_noindex(value: str, agents=SEARCH_CRAWLERS) -> bool:
    """X-Robots-Tag: true when a directive applying to a search crawler is noindex. `name: ...`
    scopes the directives after it (on that header line) to one crawler; others are ignored."""
    for line in value.lower().splitlines():
        scope = None
        for part in line.split(","):
            m = re.match(r"\s*([a-z0-9_.-]+)\s*:\s*(.*)$", part)
            if m and m.group(1) not in COLON_DIRECTIVES:
                scope, part = m.group(1), m.group(2)
            if part.strip() in ("noindex", "none") and (scope is None or scope in agents):
                return True
    return False


def host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def norm(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, (p.hostname or "").lower(), p.path or "/", p.query, ""))


def robots_groups(text: str) -> list[tuple[set[str], list[tuple[bool, str]]]]:
    """[(user agents, [(allow, path pattern)])] in file order. Consecutive user-agent lines form one
    group; an empty Disallow is no rule."""
    groups: list[tuple[set[str], list[tuple[bool, str]]]] = []
    agents: set[str] = set()
    rules: list[tuple[bool, str]] = []
    seen_rule = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if seen_rule:
                groups.append((agents, rules))
                agents, rules, seen_rule = set(), [], False
            agents.add(v.lower())
        elif k in ("allow", "disallow"):
            seen_rule = True
            if v:
                rules.append((k == "allow", v))
    groups.append((agents, rules))
    return groups


def _decode(target: str) -> str:
    """A path or rule in one canonical form for comparison (RFC 9309 §2.2.2): non-ASCII characters
    percent-encoded as UTF-8, escapes of unreserved characters decoded, every other escape (`%2F`
    and the rest) kept with upper-case hex."""
    target = "".join(c if ord(c) < 128 else urllib.parse.quote(c, safe="") for c in target)

    def one(m):
        ch = chr(int(m.group(1), 16))
        return ch if (ch.isalnum() and ch.isascii()) or ch in "-._~" else "%" + m.group(1).upper()
    return re.sub(r"%([0-9A-Fa-f]{2})", one, target)


def _pattern(path: str) -> re.Pattern:
    anchored = path.endswith("$")
    body = re.escape(_decode(path[:-1] if anchored else path)).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""))


def allowed(text: str, agent: str, target: str = "/") -> bool:
    """Google's robots.txt evaluation (RFC 9309): the agent's own groups if any exist, even with no
    rules, else `*`; the longest matching rule wins and Allow wins a tie; no match is allowed.
    `target` is the path with its query."""
    groups = robots_groups(text)
    own = [rs for a, rs in groups if agent in a]
    rules = [r for rs in (own or [rs for a, rs in groups if "*" in a]) for r in rs]
    path = _decode(target)
    best: tuple[int, bool] | None = None
    for allow, pat in rules:
        if _pattern(pat).match(path):
            key = (len(_decode(pat)), allow)  # precedence on the normalised rule
            if best is None or key > best:
                best = key
    return best is None or best[1]


def robots_sitemaps(text: str) -> list[str]:
    return [line.split(":", 1)[1].strip() for line in text.splitlines()
            if line.strip().lower().startswith("sitemap:")]


def looks_html(body: bytes) -> bool:
    """The body is an HTML document (a framework's fallback page), judged by content, not MIME."""
    head = body.lstrip(b"\xef\xbb\xbf \t\r\n")[:1024].lower()
    return head.startswith((b"<!doctype html", b"<html")) or (b"<html" in head and b"<head" in head)


def canonical_of(page: Page, url: str) -> str:
    """The page's canonical as an absolute URL, resolved against its <base href> if it has one."""
    return urllib.parse.urljoin(urllib.parse.urljoin(url, page.base), page.canonical)


def finding(severity: str, code: str, detail: str) -> dict:
    return {"severity": severity, "code": code, "detail": detail}


# --- sources: where the files come from ----------------------------------------------------------

class Live:
    """Files fetched over HTTP. In preview mode URLs on the production host are fetched from `base`
    instead, as browser navigations: a Workers preview serves a single-page app's fallback only to
    `Sec-Fetch-Mode: navigate`, where production serves every client (measured 2026-11-20). Any
    other URL is fetched exactly as given."""

    def __init__(self, http: Http, canonical: str, base: str | None = None):
        self.http = http
        self.prod = host(canonical)
        self.base = base.rstrip("/") if base else None
        self.headers = {"Sec-Fetch-Mode": "navigate", "Accept": "text/html,*/*"} if base else None

    def url(self, production_url: str) -> str:
        if not self.base or host(production_url) != self.prod:
            return production_url
        p = urllib.parse.urlsplit(production_url)
        return self.base + (p.path or "/") + (f"?{p.query}" if p.query else "")

    def get(self, production_url: str) -> Response:
        return self.http.get(self.url(production_url), self.headers)

    def get_following(self, production_url: str, hops: int = 5) -> Response:
        """GET following up to `hops` redirects (as crawlers do for robots.txt)."""
        url = self.url(production_url)
        r = self.http.get(url, self.headers)
        for _ in range(hops):
            if r.status not in REDIRECTS or not r.header("location"):
                break
            # every hop is mapped: a redirect naming the production host stays on the preview
            url = self.url(urllib.parse.urljoin(url, r.header("location")))
            r = self.http.get(url, self.headers)
        return r


class Build:
    """Files read from a static build directory, served as a static host would."""

    def __init__(self, dist: str):
        self.dist = dist

    def files(self, path: str) -> list[str]:
        p = path.strip("/")
        return [os.path.join(self.dist, p, "index.html"), os.path.join(self.dist, f"{p}.html"),
                os.path.join(self.dist, p)] if p else [os.path.join(self.dist, "index.html")]

    def get(self, production_url: str) -> Response:
        for f in self.files(urllib.parse.urlsplit(production_url).path or "/"):
            if os.path.isfile(f):
                with open(f, "rb") as fh:
                    body = fh.read()
                ctype = ("text/html" if f.endswith(".html") else "application/xml" if f.endswith(".xml")
                         else "text/plain")
                return Response(200, {"content-type": ctype}, body, f)
        return Response(404, {}, b"", production_url)

    get_following = get


# --- the audit -----------------------------------------------------------------------------------

def read_sitemap(src, url: str, out: list[dict], depth: int = 0) -> list[str]:
    r = src.get(url)
    if r.status != 200:
        out.append(finding("critical", "sitemap-unreachable", f"{url} answered {r.status}"))
        return []
    try:
        root = ET.fromstring(r.body)
    except ET.ParseError as e:
        if looks_html(r.body):
            out.append(finding("critical", "sitemap-is-html", f"{url} returns an HTML page, not XML"))
        else:
            out.append(finding("critical", "sitemap-invalid-xml", f"{url}: {e}"))
        return []
    if "html" in r.header("content-type").lower():
        out.append(finding("warn", "sitemap-content-type", f"{url} is XML served as {r.header('content-type')}"))
    locs = [(e.text or "").strip() for e in root.iter(f"{SM}loc")]
    if root.tag == f"{SM}sitemapindex":
        if depth:
            out.append(finding("warn", "sitemap-nested-index", f"{url} is an index inside an index"))
            return []
        urls: list[str] = []
        for child in locs[:CHILD_SITEMAPS]:
            urls += read_sitemap(src, child, out, depth + 1)
        return urls
    if root.tag != f"{SM}urlset":
        out.append(finding("critical", "sitemap-not-a-sitemap", f"{url}: root element {root.tag}"))
        return []
    if not locs:
        out.append(finding("warn", "sitemap-empty", f"{url} lists no URLs"))
    return locs


def _redirects(http: Http, canonical: str, out: list[dict]) -> None:
    h = host(canonical)
    other = h[4:] if h.startswith("www.") else f"www.{h}"
    for v in (f"http://{h}/", f"https://{other}/"):
        try:
            chain = http.follow(v)
        except ConnectionError:
            if v.startswith("https://"):
                out.append(finding("info", "variant-unresolved", f"{v} does not answer (no record)"))
                continue
            raise
        if norm(chain[-1]) != norm(canonical):
            out.append(finding("critical", "variant-not-redirected", f"{v} ends at {chain[-1]}, not {canonical}"))
        elif http.get(v).status not in (301, 308):
            out.append(finding("warn", "redirect-not-permanent", f"{v} redirects temporarily"))


def _homepage(r: Response, canonical: str, mode: str, out: list[dict], facts: dict) -> None:
    indexing = "info" if mode == "preview" else "critical"
    if header_noindex(r.header("x-robots-tag")):
        out.append(finding(indexing, "homepage-noindex-header", f"X-Robots-Tag: {r.header('x-robots-tag')}"))
    page = Page(r.text())
    facts["title"] = page.title.strip()[:120]
    if noindex(page.robots):
        out.append(finding(indexing, "homepage-noindex-meta", f"meta robots:{page.robots}"))
    if not page.title.strip():
        out.append(finding("warn", "no-title", "the homepage has no <title>"))
    if not page.meta.get("description"):
        out.append(finding("warn", "no-meta-description", "the homepage has no meta description"))
    if not page.canonical:
        out.append(finding("warn", "no-canonical", "the homepage declares no rel=canonical"))
    elif norm(canonical_of(page, canonical)) != norm(canonical):
        out.append(finding("critical", "canonical-mismatch",
                           f"rel=canonical is {page.canonical}, expected {canonical}"))
    missing = [k for k in ("og:title", "og:description", "og:image") if not page.meta.get(k)]
    if missing:
        out.append(finding("warn", "open-graph-incomplete", "missing " + ", ".join(missing)))
    if not page.viewport:
        out.append(finding("warn", "no-viewport", "no meta viewport: not mobile-friendly"))
    facts["served_words"] = page.words
    _served(page, canonical, out)
    facts["structured_data"] = len(page.jsonld)
    if not page.jsonld:
        out.append(finding("info", "no-structured-data", "no JSON-LD on the homepage"))
    for block in page.jsonld:
        try:
            json.loads(block)
        except ValueError:
            out.append(finding("warn", "structured-data-invalid", "a JSON-LD block is not valid JSON"))


def _robots(r: Response, mode: str, out: list[dict], facts: dict) -> str:
    """The robots.txt text crawlers obey ('' when none: everything is allowed)."""
    if r.status == 429 or r.status >= 500 or r.status in REDIRECTS:
        # 429 and 5xx: Google crawls nothing until it can read robots.txt. A redirect still
        # unresolved after 5 hops counts as unreadable too.
        out.append(finding("critical", "robots-unreadable", f"robots.txt answered {r.status}: crawlers "
                           "treat that as disallow-all"))
        return ""
    if r.status != 200:
        # Any other 4xx (404, 410, 401, 403): crawlers treat it as allow-all.
        out.append(finding("warn", "robots-missing", f"robots.txt answered {r.status}: crawlers treat that as "
                           "allow-all, but it should name the sitemap"))
        return ""
    if looks_html(r.body):
        out.append(finding("critical", "robots-is-html", "robots.txt returns an HTML page"))
        return ""
    if "html" in r.header("content-type").lower():
        out.append(finding("warn", "robots-content-type", f"robots.txt is served as {r.header('content-type')}"))
    text = r.text()
    for agent in SEARCH_CRAWLERS:
        if not allowed(text, agent, "/"):
            out.append(finding("info" if mode == "preview" else "critical", "robots-blocks-search",
                               f"robots.txt disallows the homepage for {agent}"))
    ai = [a for a in AI_SEARCH_CRAWLERS if not allowed(text, a, "/")]
    if ai and mode != "preview":
        out.append(finding("warn", "robots-blocks-ai-search", "hidden from AI search: " + ", ".join(ai)))
    facts["blocks_training_crawlers"] = [a for a in TRAINING_CRAWLERS if not allowed(text, a, "/")]
    declared = robots_sitemaps(text)
    if not declared:
        out.append(finding("warn", "robots-no-sitemap", "robots.txt names no Sitemap"))
    facts["robots_sitemaps"] = declared
    return text


def spread(urls: list[str], n: int) -> list[str]:
    """n URLs spread evenly over the list, first and last included (all of them when n or fewer)."""
    if len(urls) <= n:
        return list(urls)
    return [urls[round(i * (len(urls) - 1) / (n - 1))] for i in range(n)]


def _served(page: "Page", url: str, out: list[dict]) -> None:
    """One warning per page: the homepage is checked once even when the sample includes it."""
    seen = {f.get("url") for f in out if f["code"] == "served-content-empty"}
    if page.words < SERVED_WORDS and not page.h1 and norm(url) not in seen:
        out.append({**finding("warn", "served-content-empty",
                              f"{url} serves {page.words} words and no <h1> without JavaScript; most AI-search "
                              "crawlers do not run it and see an empty page. Prerender it."), "url": norm(url)})


def _soft_404(src, canonical: str, out: list[dict]) -> None:
    probe = f"https://{host(canonical)}{SOFT_404_PATH}"
    try:
        r = src.get(probe)
    except ConnectionError:
        return
    if r.status == 200:
        out.append(finding("warn", "soft-404", f"{probe} does not exist but answers 200; missing pages must return 404"))


def _sampled(src, mode: str, robots: str, urls: list[str], out: list[dict]) -> None:
    for u in spread(urls, SAMPLE_URLS[mode]):
        p = urllib.parse.urlsplit(u)
        target = (p.path or "/") + (f"?{p.query}" if p.query else "")
        blocked_for = [a for a in SEARCH_CRAWLERS if robots and not allowed(robots, a, target)]
        if blocked_for and mode != "preview":
            out.append(finding("critical", "sitemap-url-disallowed",
                               f"{u} is listed but robots.txt disallows it for {', '.join(blocked_for)}"))
        s = src.get(u)
        if s.status == 404 and mode == "artifact":
            out.append(finding("info", "sitemap-url-not-static", f"{u} is not a file in the build (rendered at runtime?)"))
            continue
        if s.status != 200:
            out.append(finding("critical", "sitemap-url-status", f"{u} answered {s.status}"))
            continue
        page = Page(s.text())
        if mode != "preview" and (header_noindex(s.header("x-robots-tag")) or noindex(page.robots)):
            out.append(finding("critical", "sitemap-url-noindex", f"{u} is noindex but listed in the sitemap"))
        if page.canonical and norm(canonical_of(page, u)) != norm(u):
            out.append(finding("critical", "sitemap-url-canonical-elsewhere",
                               f"{u} declares rel=canonical {page.canonical}: list only canonical URLs"))
        _served(page, u, out)


def audit(canonical: str, sitemaps: list[str], private_hosts: list[str] = (), mode: str = "production",
          http: Http | None = None, base: str | None = None, dist: str | None = None) -> dict:
    """{"findings", "facts"} for one site, plus "unauditable" (a reason) when it could not be checked."""
    http = http or Http()
    out: list[dict] = []
    facts: dict = {"canonical": canonical, "mode": mode}
    src = Build(dist) if mode == "artifact" else Live(http, canonical, base if mode == "preview" else None)
    try:
        if mode == "production":
            _redirects(http, canonical, out)
        r = src.get(canonical)
        facts["status"] = r.status
        if mode == "preview" and (r.status in (401, 403) or (r.status in REDIRECTS and
                                                             "cloudflareaccess.com" in r.header("location"))):
            raise Unauditable(f"the preview answers {r.status} (behind a login?): it cannot be audited")
        if r.status != 200:
            out.append(finding("critical", "homepage-status", f"{canonical} answered {r.status}"))
            if r.header("cf-mitigated") or r.status == 403:
                out.append(finding("warn", "bot-challenge", "the edge challenged this client; confirm "
                                   "Googlebot is allowed with URL Inspection"))
            return {"findings": out, "facts": facts}
        _homepage(r, canonical, mode, out, facts)
        robots = _robots(src.get_following(f"https://{host(canonical)}/robots.txt"), mode, out, facts)
        declared = facts.get("robots_sitemaps", [])
        for m in sitemaps:
            if declared and m not in declared:
                out.append(finding("warn", "sitemap-not-in-robots", f"{m} is not named in robots.txt"))
        urls: list[str] = []
        for m in sitemaps:
            urls += read_sitemap(src, m, out)
        facts["sitemap_urls"] = len(urls)
        off = [u for u in urls if host(u) != host(canonical) or not u.startswith("https://")]
        if off:
            out.append(finding("critical", "sitemap-off-host",
                               f"{len(off)} URLs not on https://{host(canonical)}, e.g. {off[0]}"))
        fragments = [u for u in urls if "#" in u]
        if fragments:
            out.append(finding("warn", "sitemap-url-fragment",
                               f"{len(fragments)} sitemap URLs carry a #fragment, e.g. {fragments[0]}; crawlers drop "
                               "it, so each is a duplicate of the page without it"))
        _sampled(src, mode, robots, [u for u in urls if u not in off and "#" not in u], out)
        if mode == "production":
            _soft_404(src, canonical, out)
        if mode == "production":
            for h in private_hosts:
                try:
                    p = http.get(f"https://{h}/")
                except ConnectionError:
                    continue
                if p.status == 200 and not (header_noindex(p.header("x-robots-tag")) or noindex(Page(p.text()).robots)):
                    out.append(finding("critical", "private-host-indexable", f"{h} answers 200 without noindex"))
    except ConnectionError as e:
        return {"findings": out, "facts": facts, "unauditable": f"unreachable: {e}"}
    except Unauditable as e:
        return {"findings": out, "facts": facts, "unauditable": str(e)}
    return {"findings": out, "facts": facts}


# --- IndexNow: changed canonical URLs, after a production deploy --------------------------------

def strict(url: str) -> str:
    """A URL compared exactly as a crawler would: scheme and host case-insensitive, default port
    dropped, any other port kept, empty path "/", query kept, fragment dropped."""
    p = urllib.parse.urlsplit(url)
    port = p.port
    scheme = p.scheme.lower()
    netloc = (p.hostname or "").lower() + ("" if port in (None, {"https": 443, "http": 80}.get(scheme)) else f":{port}")
    return urllib.parse.urlunsplit((scheme, netloc, p.path or "/", p.query, ""))


def fingerprint(page: "Page", url: str) -> str:
    """What a search engine would re-read: title, description, resolved canonical, the served text
    as a reader sees it, and JSON-LD in canonical form. Markup, scripts and asset hashes do not
    count, so a rebuild that changes nothing a reader sees changes nothing here."""
    blocks = []
    for b in page.jsonld:
        try:
            blocks.append(json.dumps(json.loads(b), sort_keys=True, separators=(",", ":")))
        except ValueError:
            blocks.append(" ".join(b.split()))
    h = hashlib.sha256()
    for part in (" ".join(page.title.split()), " ".join(page.meta.get("description", "").split()),
                 canonical_of(page, url) if page.canonical else "", page.text, *blocks):
        h.update(part.encode("utf-8") + b"\0")
    return h.hexdigest()


def eligible(http: "Http", canonical: str, sitemaps: list[str], notes: list[str]) -> dict[str, str] | None:
    """{url: fingerprint} for every sitemap URL a search engine should index: on the production
    origin (scheme, host and port), no #fragment, allowed for Bingbot, answering 200, not noindex,
    and naming exactly itself as canonical. None when robots.txt cannot be read: submit nothing."""
    src = Live(http, canonical)
    rr = src.get_following(f"https://{host(canonical)}/robots.txt")
    if rr.status in (404, 410):
        robots = ""
    elif rr.status != 200 or looks_html(rr.body):
        notes.append(f"robots.txt answered {rr.status}: crawlers would fetch nothing, so nothing is submitted")
        return None
    else:
        robots = rr.text()
    urls: list[str] = []
    problems: list[dict] = []
    for m in sitemaps:
        urls += read_sitemap(src, m, problems)
    unreadable = [f for f in problems if f["severity"] == "critical"]
    if unreadable:
        notes.append(f"a sitemap could not be read ({unreadable[0]['detail']}): nothing is submitted or recorded")
        return None
    origin = urllib.parse.urlsplit(strict(canonical))[:2]
    pages: dict[str, str] = {}
    for u in dict.fromkeys(urls):
        if len(pages) >= INDEXNOW_PAGES:
            notes.append(f"only the first {INDEXNOW_PAGES} eligible URLs were fingerprinted; the rest keep their records")
            break
        try:
            p = urllib.parse.urlsplit(strict(u))
        except ValueError:
            continue
        if p[:2] != origin or "#" in u or not allowed(robots, "bingbot", (p.path or "/") + (f"?{p.query}" if p.query else "")):
            continue
        r = src.get(u)
        if r.status != 200:
            continue
        page = Page(r.text())
        if header_noindex(r.header("x-robots-tag"), ("bingbot",)) or noindex(page.robots):
            continue
        try:
            self_canonical = bool(page.canonical) and strict(canonical_of(page, u)) == strict(u)
        except ValueError:
            self_canonical = False
        if self_canonical:
            pages[u] = fingerprint(page, u)
    return pages


def _is_time(value) -> bool:
    try:
        _when(value)
    except (TypeError, ValueError):
        return False
    return True


def _valid_ledger(ledger, h: str) -> tuple[dict | None, str]:
    """(the ledger, '') when it is a well-formed version-1 ledger for this host, every timestamp
    included, else (None, why)."""
    if ledger is None:
        return None, ""
    entry_ok = lambda e: (isinstance(e, dict) and isinstance(e.get("fingerprint"), str)  # noqa: E731
                          and isinstance(e.get("accepted"), str) and isinstance(e.get("attempts"), int)
                          and all(_is_time(e[k]) for k in ("seen", "accepted_at") if k in e))
    if not (isinstance(ledger, dict) and ledger.get("version") == 1 and ledger.get("host") == h
            and isinstance(ledger.get("urls"), dict) and isinstance(ledger.get("log"), list)
            and isinstance(ledger.get("initialized"), bool)
            and isinstance(ledger.get("reset", ""), str)
            and (ledger.get("retry_after", "") == "" or _is_time(ledger.get("retry_after")))
            and all(isinstance(u, str) and entry_ok(e) for u, e in ledger["urls"].items())):
        return None, "the previous ledger is not a valid version-1 ledger for this host: starting a new baseline"
    return ledger, ""


def _when(text: str) -> datetime.datetime:
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def indexnow(canonical: str, sitemaps: list[str], key: str, ledger, http: "Http",
             sha: str = "", now: str | None = None, dry_run: bool = False, clock=_utcnow,
             lost: str = "") -> tuple[dict, dict]:
    """(new ledger, this run's record). Submits only eligible URLs whose fingerprint differs from
    the one IndexNow last accepted. Until a run has recorded a baseline, nothing is submitted, so a
    first run never re-announces a whole site. A URL missing for a while keeps its accepted
    fingerprint, so its return unchanged is not a change. 'accepted' means IndexNow took the URLs;
    it is never proof that anything was crawled or indexed.

    `lost` is why an earlier ledger could not be used (it expired); an invalid one is a reason too.
    That reason is kept in the new ledger ("reset") until a baseline completes, even across runs
    that end early, and the completing baseline reports it as record["unplanned"]."""
    now = now or clock().strftime("%Y-%m-%dT%H:%M:%SZ")
    h = host(canonical)
    record: dict = {"time": now, "sha": sha, "host": h, "submitted": 0, "accepted": 0, "failed": 0,
                    "status": [], "notes": [], "dry_run": dry_run}
    valid, why = _valid_ledger(ledger, h)
    why = lost or why
    if why:
        record["notes"].append(why)
    new = copy.deepcopy(valid) if valid else {"version": 1, "host": h, "initialized": False, "retry_after": "",
                                              "urls": {}, "log": []}
    new.setdefault("retry_after", "")
    if why:
        new["reset"] = why

    def done() -> tuple[dict, dict]:
        new["log"] = (new["log"] + [record])[-LEDGER_LOG:]
        return new, record

    key_file = http.get(f"https://{h}/{key}.txt")
    if key_file.status != 200 or key_file.text().strip() != key:
        record["notes"].append(f"https://{h}/{key}.txt answered {key_file.status} without the key: nothing submitted")
        return done()
    pages = eligible(http, canonical, sitemaps, record["notes"])
    if pages is None:
        return done()
    for u, entry in new["urls"].items():
        entry["eligible"] = u in pages
    for u, fp in pages.items():
        entry = new["urls"].setdefault(u, {"fingerprint": fp, "accepted": "", "attempts": 0})
        if entry["fingerprint"] != fp:
            entry["fingerprint"], entry["attempts"] = fp, 0
        entry["eligible"], entry["seen"] = True, now
    cutoff = _when(now) - datetime.timedelta(days=INDEXNOW_FORGET_DAYS)
    for u in [u for u, e in new["urls"].items() if "seen" in e and _when(e["seen"]) < cutoff]:
        del new["urls"][u]
    if not new["initialized"]:
        for u in pages:
            new["urls"][u]["accepted"] = new["urls"][u]["fingerprint"]
        new["initialized"] = True
        record["baseline"] = True
        if new.get("reset"):
            record["unplanned"] = new.pop("reset")
        record["notes"].append(f"baseline: {len(pages)} eligible URLs recorded, nothing submitted")
        return done()
    changed = [u for u in pages if new["urls"][u]["accepted"] != new["urls"][u]["fingerprint"]
               and new["urls"][u]["attempts"] < INDEXNOW_ATTEMPTS]
    if not changed:
        record["notes"].append("no eligible URL changed since IndexNow last accepted it")
        return done()
    if new["retry_after"] and _when(now) < _when(new["retry_after"]):
        record["notes"].append(f"deferred: IndexNow asked to wait until {new['retry_after']}; {len(changed)} changed URLs kept for the next run")
        return done()
    if dry_run:
        record["notes"].append(f"dry run: {len(changed)} changed URLs would be submitted")
        record["would_submit"] = changed
        return done()
    new["retry_after"] = ""
    for i in range(0, len(changed), INDEXNOW_BATCH):
        batch = changed[i:i + INDEXNOW_BATCH]
        body = json.dumps({"host": h, "key": key, "keyLocation": f"https://{h}/{key}.txt", "urlList": batch}).encode()
        try:
            r = http.request("POST", INDEXNOW_ENDPOINT, {"Content-Type": "application/json; charset=utf-8"}, body)
        except ConnectionError:
            r = Response(0, {}, b"", INDEXNOW_ENDPOINT)
        record["submitted"] += len(batch)
        record["status"].append(r.status)
        accepted = r.status in INDEXNOW_ACCEPTED
        for u in batch:
            entry = new["urls"][u]
            if accepted:
                entry["accepted"], entry["attempts"], entry["accepted_at"] = entry["fingerprint"], 0, now
            else:
                entry["attempts"] += 1
        record["accepted" if accepted else "failed"] += len(batch)
        received = clock()  # after any retries inside Http: the wait counts from this response
        asked = retry_after(r, received)
        if not accepted and asked:
            new["retry_after"] = (received + datetime.timedelta(seconds=asked)).strftime("%Y-%m-%dT%H:%M:%SZ")
            record["notes"].append(f"IndexNow answered {r.status} and asked to wait until {new['retry_after']}; the rest wait too")
            break
    return done()


def ledger_name(canonical: str, workflow: str) -> str:
    """The ledger artifact's name: one per site and per calling workflow file, so another workflow
    (or another site in the same workflow) can never hand this one its ledger."""
    path = workflow.rsplit("@refs/", 1)[0]  # only the ref suffix goes: a filename may itself contain "@"
    return f"{LEDGER_ARTIFACT}-{host(canonical)}-{hashlib.sha256(path.encode()).hexdigest()[:12]}"


def fetch_ledger(http: "Http", repo: str, token: str, branch: str, name: str = LEDGER_ARTIFACT) -> tuple[object, str]:
    """(ledger, state) for the newest artifact with exactly this name that a run of this repository's
    default branch made (never a fork's or another branch's). Every listing page is read first, so a
    ledger behind newer artifacts from other branches is still found. state is:
      "found"      the newest unexpired one, read (its content is still checked by indexnow)
      "none"       there has never been one: the first baseline
      "expired"    there were some, all expired: an unplanned baseline (ledger None)
      "unreadable" the newest downloaded but is not a ledger archive: an unplanned baseline
    Raises ConnectionError when GitHub cannot list or serve it, or answers with something that is not
    a listing: then the run must record nothing, or it would replace a good ledger with a baseline
    and skip every change since."""
    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    listing: list = []
    for n in range(1, LEDGER_PAGES + 1):
        r = http.get(f"https://api.github.com/repos/{repo}/actions/artifacts?name={urllib.parse.quote(name)}"
                     f"&per_page=100&page={n}", hdrs)
        if r.status != 200:
            raise ConnectionError(f"listing the ledger artifacts answered {r.status} (does the job have actions: read?)")
        try:
            page = json.loads(r.body).get("artifacts")
        except (ValueError, AttributeError):
            raise ConnectionError("the ledger artifact listing is not JSON") from None
        if not isinstance(page, list):
            raise ConnectionError("the ledger artifact listing has no artifact list")
        listing += page
        if len(page) < 100:
            break
    else:
        raise ConnectionError(f"more than {LEDGER_PAGES * 100} ledger artifacts: the listing was not read to the end")
    ours = [a for a in listing if isinstance(a, dict) and a.get("name") == name
            and (a.get("workflow_run") or {}).get("head_branch") == branch
            and (a.get("workflow_run") or {}).get("head_repository_id") == (a.get("workflow_run") or {}).get("repository_id")]
    own = [a for a in ours if not a.get("expired")]
    if not own:
        return None, "expired" if ours else "none"
    newest = max(own, key=lambda a: str(a.get("created_at", "")))
    z = http.get(newest.get("archive_download_url", ""), hdrs)
    while z.status in REDIRECTS and z.header("location"):
        z = http.get(z.header("location"))  # signed storage: the GitHub token is never sent there
    if z.status != 200:
        raise ConnectionError(f"downloading the newest ledger answered {z.status}")
    try:
        with zipfile.ZipFile(io.BytesIO(z.body)) as archive:
            return json.loads(archive.read("ledger.json")), "found"
    except (zipfile.BadZipFile, zipfile.LargeZipFile, KeyError, ValueError, zlib.error, EOFError, OSError,
            NotImplementedError, RuntimeError):
        # Downloaded but not readable as a ledger: a new baseline replaces it, so it cannot stall runs.
        return "unreadable", "unreadable"


def indexnow_markdown(record: dict) -> str:
    lines = [f"### IndexNow: {record['host']}", ""]
    lines.append(f"{record['submitted']} changed URLs submitted, {record['accepted']} accepted, {record['failed']} refused"
                 + (f" (HTTP {', '.join(map(str, record['status']))})" if record["status"] else "") + ".")
    lines += [f"- {n}" for n in record["notes"]]
    lines += ["", "_Accepted means IndexNow received the URLs. It is not proof of crawling or indexing._", ""]
    return "\n".join(lines) + "\n"


# --- preview resolution --------------------------------------------------------------------------

PAGES_PREVIEW = re.compile(r"Preview URL:.*?href=['\"](https://[^'\"]+)['\"]", re.S)
WORKERS_BUILD = re.compile(r"/workers/services/view/([^/]+)/[^/]+/previews/([^/]+)/builds/")
NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


def preview_url(run: dict, workers_subdomain: str = "") -> str | None:
    """The URL a Cloudflare deploy check run built, or None."""
    m = PAGES_PREVIEW.search((run.get("output") or {}).get("summary") or "")
    if m:
        return m.group(1)
    m = WORKERS_BUILD.search(run.get("details_url") or "")
    if m and workers_subdomain:
        return f"https://{m.group(2)}-{m.group(1)}.{workers_subdomain}.workers.dev"
    return None


def latest_run(http: Http, url: str, hdrs: dict) -> dict | None:
    """The newest check run of every page of the listing (a re-run supersedes the runs before it)."""
    runs: list[dict] = []
    while url:
        r = http.get(url, hdrs)
        if r.status != 200:
            raise RuntimeError(f"reading check runs answered {r.status}")
        runs += json.loads(r.body).get("check_runs", [])
        m = NEXT_LINK.search(r.header("link"))
        url = m.group(1) if m else ""
    # Ids grow with creation, so the highest id is the newest run, queued or not.
    return max(runs, key=lambda c: c.get("id") or 0, default=None)


def wait_for_check(http: Http, repo: str, sha: str, name: str, token: str, timeout: float = 900,
                   interval: float = 15, clock=time.monotonic, sleep=time.sleep) -> dict:
    """The newest check run `name` on `sha`, once complete; RuntimeError on failure or timeout."""
    url = (f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs?"
           + urllib.parse.urlencode({"check_name": name, "filter": "all", "per_page": 100}))
    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    deadline = clock() + timeout
    while True:
        http.timeout = max(1.0, min(http.timeout, deadline - clock()))
        try:
            run = latest_run(http, url, hdrs)
        except ConnectionError as e:
            raise RuntimeError(f"reading check runs failed: {e}") from e
        if run and run.get("status") == "completed":
            if run.get("conclusion") != "success":
                raise RuntimeError(f"{name} concluded {run.get('conclusion')}")
            return run
        if clock() + interval > deadline:
            raise RuntimeError(f"{name} did not complete on {sha[:12]} within {int(timeout)}s")
        sleep(interval)


# --- output --------------------------------------------------------------------------------------

def markdown(label: str, result: dict) -> str:
    f = result["findings"]
    n = {s: sum(x["severity"] == s for x in f) for s in ("critical", "warn")}
    lines = [f"### SEO standard: {label}", ""]
    if result.get("unauditable"):
        lines += [f"**Not audited:** {result['unauditable']}", ""]
    lines += [f"{n['critical']} critical, {n['warn']} warnings ({result['facts'].get('mode')})", ""]
    lines += [f"- **{x['severity']}** `{x['code']}` {x['detail']}" for x in f if x["severity"] != "info"]
    return "\n".join(lines) + "\n"


def annotate(result: dict) -> str:
    level = {"critical": "error", "warn": "warning"}
    out = "".join(f"::{level[x['severity']]} title=SEO {x['code']}::{x['detail']}\n"
                  for x in result["findings"] if x["severity"] in level)
    if result.get("unauditable"):
        out += f"::error title=SEO not audited::{result['unauditable']}\n"
    return out


def main(argv: list[str] | None = None, http: Http | None = None, env=os.environ) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="command", required=True)
    a = sub.add_parser("audit")
    a.add_argument("--canonical", required=True, help="the production homepage, https://host/")
    a.add_argument("--sitemap", action="append", default=[], help="production sitemap URL (repeatable)")
    a.add_argument("--private-host", action="append", default=[], help="a host that must never be indexable")
    a.add_argument("--mode", choices=["production", "preview", "artifact"], default="production")
    a.add_argument("--base", help="preview: the preview origin")
    a.add_argument("--dist", help="artifact: the build output directory")
    a.add_argument("--json", help="write the result as JSON here")
    a.add_argument("--md", help="append the markdown report here (e.g. $GITHUB_STEP_SUMMARY)")
    a.add_argument("--github", action="store_true", help="print GitHub annotations")
    p = sub.add_parser("preview")
    p.add_argument("--repo", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--check", required=True, help="the deploy check run's name")
    p.add_argument("--workers-subdomain", default="", help="the account's workers.dev subdomain")
    p.add_argument("--timeout", type=float, default=900)
    p.add_argument("--wait-only", action="store_true", help="only wait for the check to succeed")
    n = sub.add_parser("indexnow")
    n.add_argument("--canonical", required=True)
    n.add_argument("--sitemap", action="append", default=[])
    n.add_argument("--key", required=True, help="the site's IndexNow key, hosted at /<key>.txt")
    n.add_argument("--ledger-out", required=True, help="write the new ledger here")
    n.add_argument("--ledger-in", help="the previous ledger (JSON); omit to fetch it from this repo's artifacts")
    n.add_argument("--repo", default="", help="owner/name, to fetch the previous ledger artifact")
    n.add_argument("--branch", default="", help="the default branch whose runs made the ledger")
    n.add_argument("--workflow", default="", help="the calling workflow ($GITHUB_WORKFLOW_REF); names the ledger")
    n.add_argument("--print-ledger-name", action="store_true", help="print the ledger artifact name and exit")
    n.add_argument("--sha", default="")
    n.add_argument("--md", help="append the record as markdown here")
    n.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    http = http or Http()

    if args.command == "indexnow" and args.print_ledger_name:
        print(ledger_name(args.canonical, args.workflow))
        return 0
    if args.command == "indexnow":
        if not args.canonical.startswith("https://") or not args.sitemap or \
                not re.fullmatch(r"[A-Za-z0-9-]{8,128}", args.key) or (args.repo and not args.branch):
            print("indexnow: needs an https --canonical, a --sitemap, an 8-128 character [A-Za-z0-9-] --key, "
                  "and --branch with --repo", file=sys.stderr)
            return 2
        ledger, state = None, "none"
        if args.ledger_in:
            state = "found"
            try:
                with open(args.ledger_in, encoding="utf-8") as f:
                    ledger = json.load(f)
            except (OSError, ValueError):
                ledger = "unreadable"
        elif args.repo:
            try:
                ledger, state = fetch_ledger(http, args.repo, env.get("GITHUB_TOKEN", ""), args.branch,
                                             ledger_name(args.canonical, args.workflow))
            except ConnectionError as e:
                print(f"indexnow: the previous ledger could not be read, so nothing is submitted or recorded: {e}",
                      file=sys.stderr)
                return 2
        lost = "the previous ledger has expired: starting a new baseline" if state == "expired" else ""
        try:
            new, record = indexnow(args.canonical, args.sitemap, args.key, ledger, http, args.sha,
                                   dry_run=args.dry_run, lost=lost)
        except ConnectionError as e:
            print(f"indexnow: unreachable: {e}", file=sys.stderr)
            return 2
        # The first baseline is planned and silent. One that replaces a lost ledger (expired, corrupt or
        # invalid, even several runs earlier) means changes since IndexNow last accepted were skipped.
        if record.get("unplanned"):
            print(f"::warning title=IndexNow baseline::{record['host']}: {record['unplanned']}. URLs changed since "
                  "IndexNow last accepted them were not submitted. The weekly schedule keeps the ledger fresh.")
        with open(args.ledger_out, "w", encoding="utf-8") as f:
            json.dump(new, f, indent=2, sort_keys=True)
        report = indexnow_markdown(record)
        if args.md:
            with open(args.md, "a", encoding="utf-8") as f:
                f.write(report)
        else:
            sys.stdout.write(report)
        return 0

    if args.command == "preview":
        try:
            run = wait_for_check(http, args.repo, args.sha, args.check, env.get("GITHUB_TOKEN", ""), args.timeout)
        except RuntimeError as e:
            print(f"preview: {e}", file=sys.stderr)
            return 2
        if args.wait_only:
            return 0
        url = preview_url(run, args.workers_subdomain)
        if not url:
            print(f"preview: no preview URL in {args.check} (set workers-subdomain for Workers Builds)",
                  file=sys.stderr)
            return 2
        print(url)
        return 0

    canonical = args.canonical if args.canonical.endswith("/") else args.canonical + "/"
    problems = []
    if not canonical.startswith("https://"):
        problems.append("--canonical must be an https URL")
    if not args.sitemap:
        problems.append("at least one --sitemap is required: a public site declares its sitemap")
    if args.mode == "preview" and not args.base:
        problems.append("--base is required in preview mode")
    if args.mode == "artifact" and not (args.dist and os.path.isdir(args.dist)):
        problems.append("--dist must be an existing directory in artifact mode")
    if problems:
        for msg in problems:
            print(f"audit: {msg}", file=sys.stderr)
        return 2
    result = audit(canonical, args.sitemap, args.private_host, args.mode, http, args.base, args.dist)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, sort_keys=True)
    report = markdown(host(canonical), result)
    if args.md:
        with open(args.md, "a", encoding="utf-8") as f:
            f.write(report)
    else:
        sys.stdout.write(report)
    if args.github:
        sys.stdout.write(annotate(result))
    if result.get("unauditable"):
        print(f"audit: {result['unauditable']}", file=sys.stderr)
        return 2
    return 1 if any(x["severity"] == "critical" for x in result["findings"]) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

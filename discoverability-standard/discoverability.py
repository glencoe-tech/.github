#!/usr/bin/env python3
"""Search and AI-search discoverability check for Glencoe websites.

Three commands, standard-library Python only:

  static DIR --origin URL   check a built site (the files a deploy would serve) before merge
  live URL                  check the production site as crawlers see it
  indexnow URL --key KEY    tell IndexNow (Bing, Yandex, Seznam, Naver...) which sitemap URLs
                            changed in the last N hours

An error means a crawler would index the wrong page, the wrong URL, or nothing at all. A warning
is a quality gap that does not stop indexing. Exit status: 0 no errors, 1 errors, 2 bad usage.
The check never decides content: it only reads what the site already serves.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import io
import json
import re
import sys
import zlib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

USER_AGENT = "glencoe-discoverability/1 (+https://github.com/glencoe-tech/.github)"
INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
MAX_PAGES = 500

# Crawlers that put a site into search results and AI answers. A site that blocks one of them
# has opted out of that surface, so blocking is an error.
SEARCH_AGENTS = (
    "Googlebot",
    "Bingbot",
    "OAI-SearchBot",
    "ChatGPT-User",
    "PerplexityBot",
    "Claude-SearchBot",
    "Claude-User",
)
# Crawlers that collect model-training data. Allowing or blocking them is the site owner's
# policy, not a defect, so the check only reports what the site declares.
TRAINING_AGENTS = ("GPTBot", "ClaudeBot", "Google-Extended", "CCBot", "Applebot-Extended")
# Meta-tag names and X-Robots-Tag scopes that address the search crawlers above ("robots" = all).
SEARCH_SCOPES = frozenset({"robots"} | {agent.lower() for agent in SEARCH_AGENTS})
# X-Robots-Tag directives that carry a colon and are not a crawler scope.
VALUED_DIRECTIVES = frozenset({"max-snippet", "max-image-preview", "max-video-preview", "unavailable_after"})

SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
# Text a page needs in its served HTML before a crawler that does not run JavaScript sees content.
# A client-rendered shell carries a handful of words (a title, a noscript line) and no <h1>; a
# real page carries hundreds. Fewer than 50 words AND no <h1> is a shell (an error); fewer than 50
# words under a server-rendered <h1> is a deliberately short page, such as a form (a warning).
MIN_WORDS = 50
HIDDEN_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
# Elements allowed in <head>; any other start tag ends the head implicitly, as browsers parse it.
HEAD_TAGS = frozenset({"head", "title", "meta", "link", "script", "style", "base", "noscript", "template"})
SOFT_404_PATH = "/glencoe-discoverability-missing-page-probe"
PAGE_LIMIT_BYTES = 10 * 1024 * 1024
SITEMAP_LIMIT_BYTES = 50 * 1024 * 1024  # sitemaps.org: at most 50 MB uncompressed
INDEXNOW_BATCH = 10_000  # IndexNow accepts at most 10,000 URLs per request
W3C_DATETIME = re.compile(
    r"^(\d{4})(?:-(\d{2})(?:-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2}))?)?)?$"
)


@dataclass
class Finding:
    level: str  # "error" or "warning"
    rule: str
    url: str
    message: str


@dataclass
class Report:
    mode: str
    target: str
    pages: int = 0
    findings: list[Finding] = field(default_factory=list)
    agents: dict[str, bool] = field(default_factory=dict)

    def error(self, rule: str, url: str, message: str) -> None:
        self.findings.append(Finding("error", rule, url, message))

    def warn(self, rule: str, url: str, message: str) -> None:
        self.findings.append(Finding("warning", rule, url, message))

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.level == "error")

    @property
    def warnings(self) -> int:
        return sum(1 for f in self.findings if f.level == "warning")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["summary"] = {"errors": self.errors, "warnings": self.warnings}
        return data


@dataclass
class Response:
    """One answer. Headers keep every value of a repeated header, in order, under a lower-case name."""

    status: int
    headers: dict = field(default_factory=dict)
    content: str | bytes = b""
    truncated: bool = False

    def __post_init__(self):
        self.raw = self.content.encode("utf-8") if isinstance(self.content, str) else self.content
        self.headers = {k.lower(): (v if isinstance(v, list) else [v]) for k, v in self.headers.items()}

    @property
    def body(self) -> str:
        return self.raw.decode("utf-8", errors="replace")

    def header(self, name: str) -> str:
        return ", ".join(self.headers.get(name, []))


# --------------------------------------------------------------------------------------------
# Sources: where a URL's bytes come from. Static reads the build directory; live asks the network
# without following redirects, because a redirect is itself a finding.
# --------------------------------------------------------------------------------------------


class StaticSource:
    def __init__(self, root: Path, origin: str):
        self.root = root
        self.origin = origin

    def file_for(self, url: str) -> Path | None:
        path = urllib.parse.urlsplit(url).path or "/"
        rel = urllib.parse.unquote(path).lstrip("/")
        candidates = [rel + "index.html"] if rel.endswith("/") or rel == "" else [
            rel,
            rel + ".html",
            rel + "/index.html",
        ]
        for candidate in candidates:
            target = (self.root / candidate).resolve()
            if target.is_file() and self.root.resolve() in target.parents:
                return target
        return None

    def get(self, url: str, limit: int = PAGE_LIMIT_BYTES) -> Response:
        target = self.file_for(url)
        if target is None:
            return Response(404)
        data = target.read_bytes()
        return Response(200, {}, data[:limit], truncated=len(data) > limit)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class LiveSource:
    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_NoRedirect)

    def get(self, url: str, limit: int = PAGE_LIMIT_BYTES) -> Response:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with self.opener.open(request, timeout=self.timeout) as resp:
                data = resp.read(limit + 1)
                return Response(resp.status, _headers(resp.headers), data[:limit], truncated=len(data) > limit)
        except urllib.error.HTTPError as exc:
            return Response(exc.code, _headers(exc.headers))
        except (urllib.error.URLError, OSError) as exc:
            return Response(0, {}, str(exc))


def _headers(message) -> dict[str, list[str]]:
    return {name.lower(): message.get_all(name) for name in dict.fromkeys(message.keys())}


# --------------------------------------------------------------------------------------------
# robots.txt (RFC 9309, with Google's documented group and precedence rules)
# --------------------------------------------------------------------------------------------


def parse_robots(text: str) -> tuple[dict[str, list[tuple[str, str]]], list[str]]:
    """Return ({agent (lower): [(directive, path)]}, sitemap URLs). Groups for one agent merge;
    a Sitemap line belongs to no group."""
    groups: dict[str, list[tuple[str, str]]] = {}
    sitemaps: list[str] = []
    agents: list[str] = []
    in_rules = False
    for raw in text.lstrip("﻿").splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "sitemap":
            sitemaps.append(value)
        elif key == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value.lower())
            for agent in agents:
                groups.setdefault(agent, [])
        elif key in ("allow", "disallow") and agents:
            in_rules = True
            for agent in agents:
                groups[agent].append((key, value))
    return groups, sitemaps


def _agent_rules(groups: dict[str, list[tuple[str, str]]], agent: str) -> list[tuple[str, str]]:
    """The group for `agent`: its own product token if present, else the longest group token it
    starts with (Google: googlebot-news falls back to googlebot), else the * group."""
    name = agent.lower()
    if name in groups:
        return groups[name]
    matches = [g for g in groups if g != "*" and name.startswith(g)]
    return groups[max(matches, key=len)] if matches else groups.get("*", [])


_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def normalise_path(path: str) -> str:
    """RFC 9309 2.2.2: compare paths as octets. Non-ASCII is percent-encoded as UTF-8; an encoded
    unreserved character is decoded; every other escape keeps its meaning, in upper-case hex."""
    out: list[str] = []
    i = 0
    while i < len(path):
        c = path[i]
        hexpair = path[i + 1 : i + 3]
        if c == "%" and len(hexpair) == 2 and all(h in "0123456789abcdefABCDEF" for h in hexpair):
            char = chr(int(hexpair, 16))
            out.append(char if char in _UNRESERVED else "%" + hexpair.upper())
            i += 3
            continue
        out.append(c if ord(c) < 128 else "".join(f"%{b:02X}" for b in c.encode("utf-8")))
        i += 1
    return "".join(out)


def _pattern_matches(pattern: str, path: str) -> bool:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if c == "*" else re.escape(c) for c in body) + ("$" if anchored else "")
    return re.match(regex, path) is not None


def path_allowed(groups: dict[str, list[tuple[str, str]]], agent: str, path: str) -> bool:
    """May `agent` fetch `path` (path plus query)? The matching rule with the longest effective
    pattern wins (a trailing * adds nothing: /fish* equals /fish); Allow wins a tie."""
    if path == "/robots.txt":
        return True
    path = normalise_path(path)
    best = (-1, True)
    for directive, raw_pattern in _agent_rules(groups, agent):
        pattern = normalise_path(raw_pattern)
        if not pattern or not _pattern_matches(pattern, path):
            continue
        effective = len(pattern.rstrip("$").rstrip("*"))
        allow = directive == "allow"
        if effective > best[0] or (effective == best[0] and allow):
            best = (effective, allow)
    return best[1]


def root_allowed(groups: dict[str, list[tuple[str, str]]], agent: str) -> bool:
    return path_allowed(groups, agent, "/")


def check_robots(report: Report, source, origin: str) -> tuple[list[str], dict[str, list[tuple[str, str]]]]:
    """Return (sitemap URLs on the origin, robots groups). RFC 9309: a 4xx robots.txt allows
    everything; a 5xx or unreachable one means the whole site is disallowed."""
    url = origin + "/robots.txt"
    resp, current, hops, broken = source.get(url), url, 0, False
    while 300 <= resp.status < 400 and resp.headers.get("location") and hops < 5:
        hops += 1
        try:  # each Location is resolved against the URL that returned it, and kept whole
            current = urllib.parse.urljoin(current, resp.headers["location"][0].strip())
            parts = urllib.parse.urlsplit(current)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(current)
            parts.port  # noqa: B018 - raises ValueError on an invalid port
        except ValueError:
            broken = True
            break
        resp = source.get(current)
    fallback = [origin + "/sitemap.xml"]
    if broken or 300 <= resp.status < 400:
        report.warn("robots-present", url, "robots.txt redirects to an invalid URL, nowhere, or more than five times; crawlers treat it as missing")
        groups, sitemaps = {}, []
    elif resp.status in (0, 429) or resp.status >= 500:
        report.error("robots-present", url, f"robots.txt answered {resp.status or 'nothing'}: crawlers treat the whole site as disallowed")
        return fallback, {"*": [("disallow", "/")]}
    elif resp.status != 200:
        report.warn("robots-present", url, f"robots.txt answered {resp.status}: crawlers may fetch everything, and no Sitemap: line points them anywhere")
        groups, sitemaps = {}, []
    else:
        if resp.body.lstrip("﻿ \r\n\t")[:200].lower().startswith(("<!doctype html", "<html")):
            report.warn("robots-present", url, "robots.txt is an HTML page (an app fallback), so it sets no rules")
        groups, sitemaps = parse_robots(resp.body)
    for agent in SEARCH_AGENTS:
        report.agents[agent] = root_allowed(groups, agent)
    for agent in TRAINING_AGENTS:
        report.agents[agent] = root_allowed(groups, agent)
    if not sitemaps:
        if resp.status == 200:
            report.warn("robots-sitemap", url, "robots.txt names no Sitemap: line")
        return fallback, groups
    for sitemap in sitemaps:
        if not sitemap.startswith(origin + "/"):
            report.error("sitemap-origin", url, f"Sitemap {sitemap} is not on {origin}")
    return [s for s in sitemaps if s.startswith(origin + "/")], groups


def check_crawlable(report: Report, groups, url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    blocked = [agent for agent in SEARCH_AGENTS if not path_allowed(groups, agent, path)]
    if blocked:
        report.error("search-crawler-allowed", url, "robots.txt blocks " + ", ".join(blocked) + " from this sitemap URL")


# --------------------------------------------------------------------------------------------
# Sitemaps
# --------------------------------------------------------------------------------------------


def _valid_lastmod(value: str) -> dt.datetime | None:
    """A W3C datetime (https://www.w3.org/TR/NOTE-datetime): YYYY, YYYY-MM, YYYY-MM-DD, or a date
    with hh:mm[:ss[.s]] and a required zone. A date alone is read as midnight UTC."""
    match = W3C_DATETIME.match(value.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, zone = match.groups()
    try:
        tz = dt.timezone.utc
        if zone and zone != "Z":
            sign = 1 if zone[0] == "+" else -1
            tz = dt.timezone(sign * dt.timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6])))
        return dt.datetime(int(year), int(month or 1), int(day or 1), int(hour or 0), int(minute or 0), int(second or 0), tzinfo=tz)
    except ValueError:
        return None


def _sitemap_text(report: Report, sitemap: str, resp: Response) -> str | None:
    if resp.status != 200:
        report.error("sitemap-present", sitemap, f"sitemap answered {resp.status}, not 200")
        return None
    raw, truncated = resp.raw, resp.truncated
    if raw[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as handle:
                raw = handle.read(SITEMAP_LIMIT_BYTES + 1)
        except (OSError, EOFError, zlib.error) as exc:
            report.error("sitemap-xml", sitemap, f"gzip sitemap does not decompress: {exc}")
            return None
        truncated = len(raw) > SITEMAP_LIMIT_BYTES
    if truncated:
        report.error("sitemap-size", sitemap, "sitemap is larger than 50 MB, the protocol limit; split it under an index")
        return None
    text = raw.decode("utf-8", errors="replace").lstrip("﻿").strip()
    head = text[:1024].lower()
    if head.startswith(("<!doctype html", "<html")):
        report.error("sitemap-present", sitemap, "served an HTML page (an app fallback), not a sitemap")
        return None
    if "<!doctype" in head:
        report.error("sitemap-xml", sitemap, "sitemap declares a DOCTYPE; sitemaps never need one")
        return None
    return text


def read_sitemaps(report: Report, source, origin: str, sitemap_urls: list[str]):
    """Return [(loc, lastmod datetime or None)] across the sitemaps and one level of index."""
    entries: list[tuple[str, dt.datetime | None]] = []
    seen: set[str] = set()
    queue = list(sitemap_urls)
    depth = {u: 0 for u in queue}
    while queue:
        sitemap = queue.pop(0)
        text = _sitemap_text(report, sitemap, source.get(sitemap, limit=SITEMAP_LIMIT_BYTES))
        if text is None:
            continue
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            report.error("sitemap-xml", sitemap, f"sitemap is not valid XML: {exc}")
            continue
        if root.tag == SITEMAP_NS + "sitemapindex":
            for child in root.findall(f"{SITEMAP_NS}sitemap/{SITEMAP_NS}loc"):
                loc = (child.text or "").strip()
                if depth[sitemap] >= 1:
                    report.error("sitemap-depth", sitemap, "a sitemap index may not list another index")
                elif not loc.startswith(origin + "/"):
                    report.error("sitemap-origin", sitemap, f"child sitemap {loc} is not on {origin}")
                elif loc not in depth:
                    depth[loc] = depth[sitemap] + 1
                    queue.append(loc)
            continue
        if root.tag != SITEMAP_NS + "urlset":
            report.error("sitemap-xml", sitemap, f"root element is {root.tag}, not a sitemaps.org urlset")
            continue
        for node in root.findall(f"{SITEMAP_NS}url"):
            loc = (node.findtext(f"{SITEMAP_NS}loc") or "").strip()
            lastmod_text = node.findtext(f"{SITEMAP_NS}lastmod")
            lastmod = None
            if lastmod_text is not None:
                lastmod = _valid_lastmod(lastmod_text)
                if lastmod is None:
                    report.warn("sitemap-lastmod", loc or sitemap, f"lastmod {lastmod_text!r} is not a W3C datetime")
            if not loc.startswith(origin + "/") and loc != origin:
                report.error("sitemap-origin", sitemap, f"URL {loc or '(empty)'} is not on {origin}")
            elif urllib.parse.urlsplit(loc).fragment or loc.endswith("#"):
                report.error("sitemap-clean-url", loc, "sitemap URL carries a #fragment; crawlers drop it, so it is not a separate page")
            elif loc in seen:
                report.error("sitemap-duplicate", loc, "URL is listed twice")
            else:
                seen.add(loc)
                entries.append((loc, lastmod))
    if sitemap_urls and not entries and not report.errors:
        report.error("sitemap-empty", sitemap_urls[0], "the sitemaps list no URLs")
    return entries


# --------------------------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------------------------


class PageParser(HTMLParser):
    """What a crawler that does not run JavaScript reads. <template> content is inert, so it
    contributes no metadata, headings or text; text inside <head> is not page text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lang = ""
        self.title = ""
        self.base = ""
        self.meta: dict[str, list[str]] = {}
        self.canonicals: list[str] = []
        self.jsonld: list[str] = []
        self.h1 = 0
        self.words = 0
        self._in_head = False
        self._in_title = False
        self._page_title = False
        self._in_jsonld = False
        self._hidden = 0
        self._template = 0
        self._buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "head":
            self._in_head = True
        elif tag == "body" or tag not in HEAD_TAGS:
            self._in_head = False
        if tag in HIDDEN_TAGS:
            self._hidden += 1
        if tag == "template":
            self._template += 1
        if self._template:
            return
        if tag == "html":
            self.lang = a.get("lang", "").strip()
        elif tag == "title":
            self._in_title, self._buffer = True, []
            self._page_title = not self._hidden and not self.title
        elif tag == "base" and not self.base:
            self.base = a.get("href", "").strip()
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key:
                self.meta.setdefault(key, []).append(a.get("content", ""))
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split():
            self.canonicals.append(a.get("href", "").strip())
        elif tag == "script" and a.get("type", "").lower().strip() == "application/ld+json":
            self._in_jsonld, self._buffer = True, []
        elif tag == "h1":
            self.h1 += 1

    def handle_endtag(self, tag):
        if tag == "head":
            self._in_head = False
        if tag in HIDDEN_TAGS and self._hidden:
            self._hidden -= 1
        if tag == "template" and self._template:
            self._template -= 1
        if tag == "title" and self._in_title:
            if self._page_title:
                self.title = "".join(self._buffer).strip()
            self._in_title = False
        elif tag == "script" and self._in_jsonld:
            self.jsonld.append("".join(self._buffer))
            self._in_jsonld = False

    def handle_data(self, data):
        if self._in_title or self._in_jsonld:
            self._buffer.append(data)
            return
        if self._hidden or self._template or not data.strip():
            return
        self._in_head = False  # text cannot sit in <head>; browsers end the head here
        self.words += len(data.split())


def url_key(url: str) -> str:
    """Compare URLs as crawlers resolve them: scheme and host case-insensitive, default port
    dropped, an empty path is "/", the fragment ignored. Path and query are compared exactly."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host if port in (None, {"https": 443, "http": 80}.get(scheme)) else f"{host}:{port}"
    query = f"?{parts.query}" if parts.query else ""
    return f"{scheme}://{netloc}{parts.path or '/'}{query}"


def same_url(a: str, b: str) -> bool:
    return url_key(a) == url_key(b)


def _directive_noindex(directives: str) -> bool:
    return bool({token.strip().lower() for token in directives.split(",")} & {"noindex", "none"})


def meta_noindex(page: PageParser) -> list[str]:
    """Meta tag names (robots, googlebot, bingbot...) that tell a search crawler noindex."""
    return sorted(name for name in SEARCH_SCOPES for value in page.meta.get(name, []) if _directive_noindex(value))


def header_noindex(values: list[str]) -> bool:
    """Does any X-Robots-Tag value tell a search crawler noindex? `GPTBot: noindex` does not."""
    for value in values:
        scope = "robots"  # a value without a crawler prefix addresses every crawler
        for token in value.split(","):
            match = re.match(r"\s*([A-Za-z][A-Za-z0-9_.-]*)\s*:\s*(.*)$", token)
            if match and match.group(1).lower() not in VALUED_DIRECTIVES:
                scope, token = match.group(1).lower(), match.group(2)
            if scope in SEARCH_SCOPES and _directive_noindex(token):
                return True
    return False


def _types(node) -> list[str]:
    if isinstance(node, list):
        return [kind for item in node for kind in _types(item)]
    if not isinstance(node, dict):
        return []
    kind = node.get("@type")
    found = [k for k in (kind if isinstance(kind, list) else [kind]) if isinstance(k, str) and k]
    graph = node.get("@graph")
    if isinstance(graph, (list, dict)):
        found += _types(graph)
    return found


def check_page(report: Report, url: str, resp: Response, origin: str, home: bool) -> None:
    if resp.status != 200:
        where = resp.header("location")
        detail = f" (to {where})" if where else ""
        report.error("page-status", url, f"sitemap URL answered {resp.status}{detail}, not 200")
        return
    if header_noindex(resp.headers.get("x-robots-tag", [])):
        report.error("page-indexable", url, "X-Robots-Tag header says noindex")
    page = PageParser()
    page.feed(resp.body)
    page.close()
    names = meta_noindex(page)
    if names:
        report.error("page-indexable", url, f"meta {', '.join(names)} says noindex on a sitemap URL")
    if not page.title:
        report.error("page-title", url, "no <title>")
    elif len(page.title) > 65:
        report.warn("page-title-length", url, f"title is {len(page.title)} characters; results cut near 60")
    description = (page.meta.get("description") or [""])[0].strip()
    if not description:
        report.error("page-description", url, "no meta description")
    elif len(description) > 170:
        report.warn("page-description-length", url, f"description is {len(description)} characters")
    if len(page.canonicals) != 1 or not page.canonicals[0]:
        report.error("page-canonical", url, f"{len(page.canonicals)} canonical links, expected exactly 1")
    else:
        resolved = urllib.parse.urljoin(urllib.parse.urljoin(url, page.base), page.canonicals[0])
        try:
            matches = same_url(resolved, url)
        except ValueError:
            report.error("page-canonical", url, f"canonical {page.canonicals[0]} is not a valid URL")
        else:
            if not matches:
                report.error("page-canonical", url, f"canonical {resolved} is not the sitemap URL")
    if page.words < MIN_WORDS and page.h1 == 0:
        report.error(
            "page-served-content",
            url,
            f"served HTML holds {page.words} words and no <h1>: the page is rendered by JavaScript, "
            "and crawlers that do not run it (most AI search crawlers) see an empty page. Prerender it.",
        )
    elif page.words < MIN_WORDS:
        report.warn("page-thin-content", url, f"served HTML holds {page.words} words of text")
    if not page.lang:
        report.warn("page-lang", url, "<html> has no lang attribute")
    if page.h1 != 1:
        report.warn("page-h1", url, f"{page.h1} <h1> elements, expected 1")
    for key in ("og:title", "og:description", "og:image"):
        if not any(v.strip() for v in page.meta.get(key, [])):
            report.warn("page-open-graph", url, f"no {key}")
    kinds: list[str] = []
    for block in page.jsonld:
        try:
            data = json.loads(block)
        except json.JSONDecodeError as exc:
            report.error("structured-data", url, f"JSON-LD does not parse: {exc.msg}")
            continue
        block_types = _types(data)
        if not block_types:
            report.error("structured-data", url, "JSON-LD block has no @type")
        kinds += block_types
    if home and not kinds:
        report.warn("structured-data-home", url, "home page has no JSON-LD (Organization or LocalBusiness)")
    if resp.truncated:
        report.warn("page-size", url, f"page is larger than {PAGE_LIMIT_BYTES // (1024 * 1024)} MB; only the start was checked")


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------


def normalise_origin(value: str) -> str:
    parts = urllib.parse.urlsplit(value.strip())
    if parts.scheme != "https" or not parts.netloc or parts.path not in ("", "/") or parts.query:
        raise ValueError(f"origin must look like https://example.com, got {value!r}")
    return f"https://{parts.netloc.lower()}"


def audit(source, origin: str, mode: str) -> tuple[Report, list[tuple[str, dt.datetime | None]]]:
    report = Report(mode=mode, target=origin)
    sitemaps, groups = check_robots(report, source, origin)
    entries = read_sitemaps(report, source, origin, sitemaps)
    for url, _ in entries[:MAX_PAGES]:
        check_crawlable(report, groups, url)
        check_page(report, url, source.get(url), origin, home=url in (origin, origin + "/"))
    report.pages = min(len(entries), MAX_PAGES)
    if len(entries) > MAX_PAGES:
        report.warn("page-limit", origin, f"checked the first {MAX_PAGES} of {len(entries)} URLs")
    return report, entries


def cmd_static(args) -> Report:
    root = Path(args.dir)
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory")
    origin = normalise_origin(args.origin)
    report, entries = audit(StaticSource(root, origin), origin, "static")
    listed = {StaticSource(root, origin).file_for(url) for url, _ in entries}
    for html in sorted(root.rglob("*.html")):
        if html.resolve() in listed or html.name in ("404.html", "500.html"):
            continue
        page = PageParser()
        page.feed(html.read_text(encoding="utf-8", errors="replace"))
        if not meta_noindex(page):
            report.warn("sitemap-coverage", str(html.relative_to(root)), "indexable page is not in the sitemap")
    return report


def cmd_live(args, source=None) -> Report:
    origin = normalise_origin(args.url)
    source = source or LiveSource()
    report, _ = audit(source, origin, "live")
    missing = source.get(origin + SOFT_404_PATH)
    if missing.status == 200:
        report.error(
            "soft-404",
            origin + SOFT_404_PATH,
            "a page that does not exist answers 200; every mistyped URL becomes an indexable duplicate",
        )
    insecure = source.get("http://" + origin[len("https://"):] + "/")
    if not (300 <= insecure.status < 400 and insecure.header("location").startswith(origin)):
        report.warn("https-redirect", "http://" + origin[8:] + "/", "plain HTTP does not redirect to the HTTPS origin")
    return report


def cmd_indexnow(args, source=None, post: Callable[[dict], int] | None = None) -> Report:
    origin = normalise_origin(args.url)
    source = source or LiveSource()
    report = Report(mode="indexnow", target=origin)
    key = args.key.strip()
    if not (8 <= len(key) <= 128 and all(c.isalnum() or c == "-" for c in key)):
        raise ValueError("IndexNow key must be 8-128 letters, digits or dashes")
    key_url = f"{origin}/{key}.txt"
    keyfile = source.get(key_url)
    if keyfile.status != 200 or keyfile.body.strip() != key:
        report.error("indexnow-key", key_url, f"key file answered {keyfile.status} without the key")
        return report
    sitemaps, _ = check_robots(Report(mode="indexnow", target=origin), source, origin)
    entries = read_sitemaps(report, source, origin, sitemaps)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=args.since_hours)
    changed = [url for url, lastmod in entries if lastmod is not None and lastmod >= cutoff]
    if args.dry_run:
        report.pages = len(changed)
        return report
    submit = post or _post_indexnow
    host = urllib.parse.urlsplit(origin).netloc
    for start in range(0, len(changed), INDEXNOW_BATCH):
        batch = changed[start : start + INDEXNOW_BATCH]
        status = submit({"host": host, "key": key, "keyLocation": key_url, "urlList": batch})
        if status in (200, 202):
            report.pages += len(batch)
        else:
            report.error("indexnow-submit", INDEXNOW_ENDPOINT, f"IndexNow answered {status} for {len(batch)} URLs")
    return report


def _post_indexnow(payload: dict) -> int:
    request = urllib.request.Request(
        INDEXNOW_ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return 0


def render(report: Report) -> str:
    lines = [f"discoverability {report.mode} {report.target}: {report.pages} pages, "
             f"{report.errors} errors, {report.warnings} warnings"]
    for f in report.findings:
        lines.append(f"  {f.level.upper():7} {f.rule:24} {f.url}  {f.message}")
    if report.agents:
        allowed = ", ".join(f"{a}={'allow' if ok else 'block'}" for a, ok in report.agents.items())
        lines.append(f"  crawlers: {allowed}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="discoverability", description=__doc__.splitlines()[0])
    parser.add_argument("--json", metavar="FILE", help="also write the report as JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("static", help="check a built site directory")
    p.add_argument("dir")
    p.add_argument("--origin", required=True, help="production origin, e.g. https://glencoetech.com")
    p = sub.add_parser("live", help="check the production site")
    p.add_argument("url")
    p = sub.add_parser("indexnow", help="submit recently changed sitemap URLs to IndexNow")
    p.add_argument("url")
    p.add_argument("--key", required=True)
    p.add_argument("--since-hours", type=float, default=26.0)
    p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = {"static": cmd_static, "live": cmd_live, "indexnow": cmd_indexnow}[args.command](args)
    except ValueError as exc:
        print(f"discoverability: {exc}", file=sys.stderr)
        return 2
    print(render(report))
    if args.json:
        Path(args.json).write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return 1 if report.errors else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

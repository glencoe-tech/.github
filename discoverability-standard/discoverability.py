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
import json
import sys
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

SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
# Text a page needs in its served HTML before a crawler that does not run JavaScript sees content.
# A client-rendered shell carries a handful of words (a title, a noscript line); a real page
# carries hundreds. 50 separates the two without judging how long a page ought to be.
MIN_WORDS = 50
HIDDEN_TAGS = frozenset({"head", "script", "style", "noscript", "template", "svg"})
SOFT_404_PATH = "/glencoe-discoverability-missing-page-probe"


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
    status: int
    headers: dict[str, str]
    body: str


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

    def get(self, url: str) -> Response:
        target = self.file_for(url)
        if target is None:
            return Response(404, {}, "")
        return Response(200, {}, target.read_text(encoding="utf-8", errors="replace"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class LiveSource:
    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_NoRedirect)

    def get(self, url: str, user_agent: str = USER_AGENT) -> Response:
        request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        try:
            with self.opener.open(request, timeout=self.timeout) as resp:
                body = resp.read(5_000_000).decode("utf-8", errors="replace")
                return Response(resp.status, _lower(resp.headers), body)
        except urllib.error.HTTPError as exc:
            return Response(exc.code, _lower(exc.headers), "")
        except (urllib.error.URLError, OSError) as exc:
            return Response(0, {}, str(exc))


def _lower(headers) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


# --------------------------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------------------------


def parse_robots(text: str) -> tuple[dict[str, list[tuple[str, str]]], list[str]]:
    """Return ({agent (lower): [(directive, path)]}, sitemap URLs). Groups for one agent merge."""
    groups: dict[str, list[tuple[str, str]]] = {}
    sitemaps: list[str] = []
    agents: list[str] = []
    in_rules = False
    for raw in text.splitlines():
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


def root_allowed(groups: dict[str, list[tuple[str, str]]], agent: str) -> bool:
    """May `agent` fetch "/"? Google's rules: the most specific matching group, then the longest
    matching path, with Allow winning a tie. Only patterns that match "/" itself matter."""
    name = agent.lower()
    matches = [g for g in groups if g != "*" and g in name]
    rules = groups[max(matches, key=len)] if matches else groups.get("*", [])
    best: tuple[int, bool] = (-1, True)
    for directive, path in rules:
        if path not in ("/", "/*", "/$", "*"):
            continue
        candidate = (len(path), directive == "allow")
        if candidate[0] > best[0] or (candidate[0] == best[0] and candidate[1]):
            best = candidate
    return best[1]


def check_robots(report: Report, source, origin: str) -> list[str]:
    url = origin + "/robots.txt"
    resp = source.get(url)
    if resp.status != 200:
        report.error("robots-present", url, f"robots.txt answered {resp.status}, not 200")
        return [origin + "/sitemap.xml"]
    groups, sitemaps = parse_robots(resp.body)
    for agent in SEARCH_AGENTS:
        allowed = root_allowed(groups, agent)
        report.agents[agent] = allowed
        if not allowed:
            report.error("search-crawler-allowed", url, f"robots.txt blocks {agent} from the site")
    for agent in TRAINING_AGENTS:
        report.agents[agent] = root_allowed(groups, agent)
    if not sitemaps:
        report.warn("robots-sitemap", url, "robots.txt names no Sitemap: line")
        return [origin + "/sitemap.xml"]
    for sitemap in sitemaps:
        if not sitemap.startswith(origin + "/"):
            report.error("sitemap-origin", url, f"Sitemap {sitemap} is not on {origin}")
    return [s for s in sitemaps if s.startswith(origin + "/")]


# --------------------------------------------------------------------------------------------
# Sitemaps
# --------------------------------------------------------------------------------------------


def _valid_lastmod(value: str) -> dt.datetime | None:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def read_sitemaps(report: Report, source, origin: str, sitemap_urls: list[str]):
    """Return [(loc, lastmod datetime or None)] across the sitemaps and one level of index."""
    entries: list[tuple[str, dt.datetime | None]] = []
    seen: set[str] = set()
    queue = list(sitemap_urls)
    depth = {u: 0 for u in queue}
    while queue:
        sitemap = queue.pop(0)
        resp = source.get(sitemap)
        if resp.status != 200:
            report.error("sitemap-present", sitemap, f"sitemap answered {resp.status}, not 200")
            continue
        body = resp.body.lstrip("﻿").strip()
        if body[:200].lower().startswith(("<!doctype html", "<html")):
            report.error("sitemap-present", sitemap, "served an HTML page (an app fallback), not a sitemap")
            continue
        try:
            root = ET.fromstring(body)
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
                    report.warn("sitemap-lastmod", loc or sitemap, f"lastmod {lastmod_text!r} is not a W3C date")
            parts = urllib.parse.urlsplit(loc)
            if not loc.startswith(origin + "/") and loc != origin:
                report.error("sitemap-origin", sitemap, f"URL {loc or '(empty)'} is not on {origin}")
            elif parts.query or parts.fragment:
                report.error("sitemap-clean-url", loc, "sitemap URL carries a query or fragment")
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
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lang = ""
        self.title = ""
        self.meta: dict[str, list[str]] = {}
        self.canonicals: list[str] = []
        self.jsonld: list[str] = []
        self.h1 = 0
        self.words = 0
        self._in_title = False
        self._in_jsonld = False
        self._hidden = 0
        self._buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in HIDDEN_TAGS:
            self._hidden += 1
        if tag == "html":
            self.lang = a.get("lang", "").strip()
        elif tag == "title":
            self._in_title, self._buffer = True, []
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
        if tag in HIDDEN_TAGS and self._hidden:
            self._hidden -= 1
        if tag == "title" and self._in_title:
            self.title, self._in_title = "".join(self._buffer).strip(), False
        elif tag == "script" and self._in_jsonld:
            self.jsonld.append("".join(self._buffer))
            self._in_jsonld = False

    def handle_data(self, data):
        if self._in_title or self._in_jsonld:
            self._buffer.append(data)
        elif not self._hidden:
            self.words += len(data.split())


def same_url(a: str, b: str) -> bool:
    """https://x.com and https://x.com/ are one URL; every other difference is a different URL."""
    return (a + "/" if urllib.parse.urlsplit(a).path == "" else a) == (
        b + "/" if urllib.parse.urlsplit(b).path == "" else b
    )


def _types(node) -> list[str]:
    found: list[str] = []
    if isinstance(node, list):
        for item in node:
            found += _types(item)
    elif isinstance(node, dict):
        kind = node.get("@type")
        found += kind if isinstance(kind, list) else ([kind] if kind else [])
        for item in node.get("@graph", []):
            found += _types(item)
    return [str(k) for k in found]


def check_page(report: Report, url: str, resp: Response, origin: str, home: bool) -> None:
    if resp.status != 200:
        where = resp.headers.get("location", "")
        detail = f" (to {where})" if where else ""
        report.error("page-status", url, f"sitemap URL answered {resp.status}{detail}, not 200")
        return
    if "noindex" in resp.headers.get("x-robots-tag", "").lower():
        report.error("page-indexable", url, "X-Robots-Tag header says noindex")
    page = PageParser()
    page.feed(resp.body)
    robots = " ".join(page.meta.get("robots", []) + page.meta.get("googlebot", [])).lower()
    if "noindex" in robots:
        report.error("page-indexable", url, "meta robots says noindex on a sitemap URL")
    if not page.title:
        report.error("page-title", url, "no <title>")
    elif len(page.title) > 65:
        report.warn("page-title-length", url, f"title is {len(page.title)} characters; results cut near 60")
    description = (page.meta.get("description") or [""])[0].strip()
    if not description:
        report.error("page-description", url, "no meta description")
    elif len(description) > 170:
        report.warn("page-description-length", url, f"description is {len(description)} characters")
    if len(page.canonicals) != 1:
        report.error("page-canonical", url, f"{len(page.canonicals)} canonical links, expected exactly 1")
    elif not same_url(page.canonicals[0], url):
        report.error("page-canonical", url, f"canonical {page.canonicals[0]} is not the sitemap URL")
    if page.words < MIN_WORDS:
        report.error(
            "page-served-content",
            url,
            f"served HTML holds {page.words} words of text; crawlers that do not run JavaScript "
            "(most AI search crawlers) see an empty page. Prerender it.",
        )
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
    sitemaps = check_robots(report, source, origin)
    entries = read_sitemaps(report, source, origin, sitemaps)
    for url, _ in entries[:MAX_PAGES]:
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
        robots = " ".join(page.meta.get("robots", [])).lower()
        if "noindex" not in robots:
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
    if not (300 <= insecure.status < 400 and insecure.headers.get("location", "").startswith(origin)):
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
    sitemaps = check_robots(Report(mode="indexnow", target=origin), source, origin)
    entries = read_sitemaps(report, source, origin, sitemaps)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=args.since_hours)
    changed = [url for url, lastmod in entries if lastmod is not None and lastmod >= cutoff]
    report.pages = len(changed)
    if not changed or args.dry_run:
        return report
    payload = {"host": urllib.parse.urlsplit(origin).netloc, "key": key, "keyLocation": key_url, "urlList": changed[:10000]}
    submit = post or _post_indexnow
    status = submit(payload)
    if status not in (200, 202):
        report.error("indexnow-submit", INDEXNOW_ENDPOINT, f"IndexNow answered {status}")
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

#!/usr/bin/env python3
"""The check behind the Glencoe Website SEO Standard: one engine for every website and framework.

  audit     check a site over HTTP (production or a PR preview) or a build directory
  preview   wait for the deploy check on a commit (Cloudflare Workers Builds or Pages) and print
            the preview URL it built

Modes of `audit`:
  production  the live site: redirects, status, indexing directives, robots.txt, sitemaps and the
              URLs they list, canonical, metadata, structured data, private hosts, crawler access
  preview     a PR preview at --base, judged as the production site it will become: canonicals and
              sitemap URLs must name the PRODUCTION host; previews are expected to be noindex
  artifact    a static build directory (--dist): robots.txt, sitemap files and the homepage markup

Severity: critical findings fail (exit 1): the site would be wrongly indexed or not indexed at all.
warn findings are annotations and never fail. info is context. Exit 2: the check could not run
(it fails closed: a check that verified nothing is not a pass).

Standard-library Python 3.11+. Used by the composite action in this directory, by the reusable
workflow .github/workflows/seo-standard.yml, and by glencoe-infra's Search Console monitor.
"""
from __future__ import annotations

import argparse
import html.parser
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

USER_AGENT = "Mozilla/5.0 (compatible; Glencoe-SEO-Standard/1.0; +https://glencoetech.com)"
SM = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
RETRY_STATUS = {429, 500, 502, 503, 504}
REDIRECTS = (301, 302, 303, 307, 308)
SAMPLE_URLS = 5
CHILD_SITEMAPS = 10
# Crawlers that put a site in search results, classic or AI. Blocking one hides the site from it.
SEARCH_CRAWLERS = ("googlebot", "bingbot")
AI_SEARCH_CRAWLERS = ("oai-searchbot", "chatgpt-user", "perplexitybot", "perplexity-user",
                      "claude-searchbot", "claude-user", "applebot", "duckduckbot")
# Crawlers that collect training data. Blocking them is a legitimate per-client choice.
TRAINING_CRAWLERS = ("gptbot", "google-extended", "ccbot", "claudebot", "applebot-extended", "bytespider")


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


def _send(req: urllib.request.Request, timeout: float) -> Response:  # pragma: no cover - real network
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(5_000_000), r.url)
    except urllib.error.HTTPError as e:
        return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read(5_000_000), req.full_url)


class Http:
    """HTTP with bounded retries (429 and 5xx, network errors). `send` and `sleep` are injectable."""

    def __init__(self, send=_send, sleep=time.sleep, attempts: int = 4, timeout: float = 20.0):
        self.send, self.sleep, self.attempts, self.timeout = send, sleep, attempts, timeout

    def request(self, method: str, url: str, headers: dict | None = None, body: bytes | None = None) -> Response:
        hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
        last: Exception | None = None
        resp = None
        for n in range(self.attempts):
            if n:
                self.sleep(min(2 ** n, 30))
            try:
                resp = self.send(urllib.request.Request(url, data=body, headers=hdrs, method=method), self.timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last, resp = e, None
                continue
            if resp.status not in RETRY_STATUS:
                return resp
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
        self.title, self.canonical, self.robots, self.viewport = "", "", "", False
        self.meta: dict[str, str] = {}
        self.jsonld: list[str] = []
        self._in_title = self._in_ld = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split():
            self.canonical = self.canonical or a.get("href", "")
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("robots", "googlebot"):
                self.robots += " " + a.get("content", "").lower()
            elif key == "viewport":
                self.viewport = True
            elif key:
                self.meta.setdefault(key, a.get("content", ""))
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_ld = True
            self.jsonld.append("")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_ld = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_ld:
            self.jsonld[-1] += data


def noindex(directives: str) -> bool:
    return bool(re.search(r"\b(noindex|none)\b", directives.lower()))


def host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def norm(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, (p.hostname or "").lower(), p.path or "/", p.query, ""))


def robots_groups(text: str) -> list[tuple[set[str], list[str]]]:
    """[(user agents, rules)] in file order; a rule is 'allow:<path>' or 'disallow:<path>'."""
    groups: list[tuple[set[str], list[str]]] = []
    agents: set[str] = set()
    rules: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = set(), []
            agents.add(v.lower())
        elif k in ("allow", "disallow"):
            rules.append(f"{k}:{v}")
    groups.append((agents, rules))
    return groups


def blocked(text: str, agent: str) -> bool:
    """True when the group that applies to `agent` (its own group, else `*`) disallows everything."""
    groups = robots_groups(text)
    own = [r for a, r in groups if agent in a]
    rules = [x for r in (own or [r for a, r in groups if "*" in a]) for x in r]
    return "disallow:/" in rules and not any(r.startswith("allow:") and r != "allow:" for r in rules)


def robots_sitemaps(text: str) -> list[str]:
    return [line.split(":", 1)[1].strip() for line in text.splitlines()
            if line.strip().lower().startswith("sitemap:")]


def is_html(r: Response) -> bool:
    return "html" in r.header("content-type").lower() or r.body.lstrip()[:15].lower().startswith(
        (b"<!doctype html", b"<html"))


def finding(severity: str, code: str, detail: str) -> dict:
    return {"severity": severity, "code": code, "detail": detail}


# --- sources: where the files come from ----------------------------------------------------------

class Live:
    """Files fetched over HTTP. In preview mode `base` replaces the production origin, and requests
    are browser navigations: a Workers preview serves a single-page app's fallback only to
    `Sec-Fetch-Mode: navigate`, where production serves every client (measured 2026-11-20)."""

    def __init__(self, http: Http, canonical: str, base: str | None = None):
        self.http = http
        self.origin = (base or canonical).rstrip("/")
        self.headers = {"Sec-Fetch-Mode": "navigate", "Accept": "text/html,*/*"} if base else None

    def url(self, production_url: str) -> str:
        p = urllib.parse.urlsplit(production_url)
        return self.origin + (p.path or "/") + (f"?{p.query}" if p.query else "")

    def get(self, production_url: str) -> Response:
        return self.http.get(self.url(production_url), self.headers)


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


# --- the audit -----------------------------------------------------------------------------------

def read_sitemap(src, url: str, out: list[dict], depth: int = 0) -> list[str]:
    r = src.get(url)
    if r.status != 200:
        out.append(finding("critical", "sitemap-unreachable", f"{url} answered {r.status}"))
        return []
    if is_html(r):
        out.append(finding("critical", "sitemap-is-html", f"{url} returns an HTML page, not XML"))
        return []
    try:
        root = ET.fromstring(r.body)
    except ET.ParseError as e:
        out.append(finding("critical", "sitemap-invalid-xml", f"{url}: {e}"))
        return []
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
    if noindex(r.header("x-robots-tag")):
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
    elif norm(urllib.parse.urljoin(canonical, page.canonical)) != norm(canonical):
        out.append(finding("critical", "canonical-mismatch",
                           f"rel=canonical is {page.canonical}, expected {canonical}"))
    missing = [k for k in ("og:title", "og:description", "og:image") if not page.meta.get(k)]
    if missing:
        out.append(finding("warn", "open-graph-incomplete", "missing " + ", ".join(missing)))
    if not page.viewport:
        out.append(finding("warn", "no-viewport", "no meta viewport: not mobile-friendly"))
    facts["structured_data"] = len(page.jsonld)
    if not page.jsonld:
        out.append(finding("info", "no-structured-data", "no JSON-LD on the homepage"))
    for block in page.jsonld:
        try:
            json.loads(block)
        except ValueError:
            out.append(finding("warn", "structured-data-invalid", "a JSON-LD block is not valid JSON"))


def _robots(r: Response, mode: str, out: list[dict], facts: dict) -> list[str]:
    if r.status != 200:
        out.append(finding("critical" if mode != "artifact" else "warn", "robots-missing",
                           f"robots.txt answered {r.status}"))
        return []
    if is_html(r):
        out.append(finding("critical", "robots-is-html", "robots.txt returns an HTML page"))
        return []
    text = r.text()
    for agent in SEARCH_CRAWLERS:
        if blocked(text, agent):
            out.append(finding("info" if mode == "preview" else "critical", "robots-blocks-search",
                               f"robots.txt disallows the whole site for {agent}"))
    ai = [a for a in AI_SEARCH_CRAWLERS if blocked(text, a)]
    if ai and mode != "preview":
        out.append(finding("warn", "robots-blocks-ai-search", "hidden from AI search: " + ", ".join(ai)))
    facts["blocks_training_crawlers"] = [a for a in TRAINING_CRAWLERS if blocked(text, a)]
    declared = robots_sitemaps(text)
    if not declared:
        out.append(finding("warn", "robots-no-sitemap", "robots.txt names no Sitemap"))
    facts["robots_sitemaps"] = declared
    return declared


def audit(canonical: str, sitemaps: list[str], private_hosts: list[str] = (), mode: str = "production",
          http: Http | None = None, base: str | None = None, dist: str | None = None) -> dict:
    """Findings and facts for one site. Network failures are findings, never crashes."""
    http = http or Http()
    out: list[dict] = []
    facts: dict = {"canonical": canonical, "mode": mode}
    src = Build(dist) if mode == "artifact" else Live(http, canonical, base if mode == "preview" else None)
    try:
        if mode == "production":
            _redirects(http, canonical, out)
        r = src.get(canonical)
        facts["status"] = r.status
        if r.status != 200:
            out.append(finding("critical", "homepage-status", f"{canonical} answered {r.status}"))
            if r.header("cf-mitigated") or r.status == 403:
                out.append(finding("warn", "bot-challenge", "the edge challenged this client; confirm "
                                   "Googlebot is allowed with URL Inspection"))
            return {"findings": out, "facts": facts}
        _homepage(r, canonical, mode, out, facts)
        declared = _robots(src.get(f"https://{host(canonical)}/robots.txt"), mode, out, facts)
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
        for u in [u for u in urls if u not in off][:SAMPLE_URLS]:
            s = src.get(u)
            if s.status == 404 and mode == "artifact":
                out.append(finding("info", "sitemap-url-not-static", f"{u} is not a file in the build (rendered at runtime?)"))
            elif s.status != 200:
                out.append(finding("critical", "sitemap-url-status", f"{u} answered {s.status}"))
            elif mode != "preview" and (noindex(s.header("x-robots-tag")) or noindex(Page(s.text()).robots)):
                out.append(finding("critical", "sitemap-url-noindex", f"{u} is noindex but listed in the sitemap"))
        if mode == "production":
            for h in private_hosts:
                try:
                    p = http.get(f"https://{h}/")
                except ConnectionError:
                    continue
                if p.status == 200 and not (noindex(p.header("x-robots-tag")) or noindex(Page(p.text()).robots)):
                    out.append(finding("critical", "private-host-indexable", f"{h} answers 200 without noindex"))
    except ConnectionError as e:
        out.append(finding("critical", "unreachable", str(e)))
    return {"findings": out, "facts": facts}


# --- preview resolution --------------------------------------------------------------------------

PAGES_PREVIEW = re.compile(r"Preview URL:.*?href=['\"](https://[^'\"]+)['\"]", re.S)
WORKERS_BUILD = re.compile(r"/workers/services/view/([^/]+)/[^/]+/previews/([^/]+)/builds/")


def preview_url(run: dict, workers_subdomain: str = "") -> str | None:
    """The URL a Cloudflare deploy check run built, or None."""
    m = PAGES_PREVIEW.search((run.get("output") or {}).get("summary") or "")
    if m:
        return m.group(1)
    m = WORKERS_BUILD.search(run.get("details_url") or "")
    if m and workers_subdomain:
        return f"https://{m.group(2)}-{m.group(1)}.{workers_subdomain}.workers.dev"
    return None


def wait_for_check(http: Http, repo: str, sha: str, name: str, token: str, timeout: float = 900,
                   interval: float = 15, clock=time.monotonic, sleep=time.sleep) -> dict:
    """The completed check run `name` on `sha`; raises RuntimeError on failure or timeout."""
    url = (f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs?"
           + urllib.parse.urlencode({"check_name": name}))
    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    deadline = clock() + timeout
    while True:
        r = http.get(url, hdrs)
        if r.status != 200:
            raise RuntimeError(f"reading check runs answered {r.status}")
        runs = json.loads(r.body).get("check_runs", [])
        done = [c for c in runs if c.get("status") == "completed"]
        if done:
            run = done[0]
            if run.get("conclusion") != "success":
                raise RuntimeError(f"{name} concluded {run.get('conclusion')}")
            return run
        if clock() >= deadline:
            raise RuntimeError(f"{name} did not complete on {sha[:12]} within {int(timeout)}s")
        sleep(interval)


# --- output --------------------------------------------------------------------------------------

def markdown(label: str, result: dict) -> str:
    f = result["findings"]
    n = {s: sum(x["severity"] == s for x in f) for s in ("critical", "warn")}
    lines = [f"### SEO standard: {label}", "",
             f"{n['critical']} critical, {n['warn']} warnings ({result['facts'].get('mode')})", ""]
    lines += [f"- **{x['severity']}** `{x['code']}` {x['detail']}" for x in f if x["severity"] != "info"]
    return "\n".join(lines) + "\n"


def annotate(result: dict) -> str:
    level = {"critical": "error", "warn": "warning"}
    return "".join(f"::{level[x['severity']]} title=SEO {x['code']}::{x['detail']}\n"
                   for x in result["findings"] if x["severity"] in level)


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
    args = ap.parse_args(argv)
    http = http or Http()

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
    return 1 if any(x["severity"] == "critical" for x in result["findings"]) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

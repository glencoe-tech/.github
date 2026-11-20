"""Unit tests for discoverability.py. Every rule is exercised failing as well as passing.

No test reaches the network: live and IndexNow paths run against fakes or a local HTTP server,
and the IndexNow endpoint is pointed at that server before any test runs.
"""

import argparse
import datetime as dt
import http.server
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discoverability as d  # noqa: E402

ORIGIN = "https://example.com"
WORDS = " ".join(["word"] * 60)


def page(url, *, title="Example page", description="A plain description of the page.", canonical=None,
         robots=None, lang="en", h1=1, og=True, jsonld=('{"@context":"https://schema.org","@type":"Organization"}',),
         body=WORDS):
    canonical = url if canonical is None else canonical
    head = ['<meta charset="utf-8">', f"<title>{title}</title>" if title is not None else ""]
    if description is not None:
        head.append(f'<meta name="description" content="{description}">')
    if canonical:
        head.append(f'<link rel="canonical" href="{canonical}">')
    if robots:
        head.append(f'<meta name="robots" content="{robots}">')
    if og:
        head += ['<meta property="og:title" content="t">', '<meta property="og:description" content="d">',
                 '<meta property="og:image" content="https://example.com/og.png">']
    head += [f'<script type="application/ld+json">{block}</script>' for block in jsonld]
    lang_attr = f' lang="{lang}"' if lang else ""
    heads = "<h1>Heading</h1>" * h1
    return (f"<!doctype html><html{lang_attr}><head>{''.join(head)}</head><body>{heads}"
            f"<script>var x = 1;</script><p>{body}</p></body></html>")


def urlset(*locs, lastmod=None):
    rows = "".join(
        f"<url><loc>{loc}</loc>{f'<lastmod>{lastmod}</lastmod>' if lastmod else ''}</url>" for loc in locs
    )
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</urlset>'


def index(*locs):
    rows = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</sitemapindex>'


ROBOTS = "User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml\n"


class FakeSource:
    """A dict of URL -> Response, 404 for anything else."""

    def __init__(self, pages):
        self.pages = {}
        for url, value in pages.items():
            self.pages[url] = value if isinstance(value, d.Response) else d.Response(200, {}, value)

    def get(self, url, limit=d.PAGE_LIMIT_BYTES):
        return self.pages.get(url, d.Response(404, {}, ""))


def healthy_site(**extra):
    pages = {
        ORIGIN + "/robots.txt": ROBOTS,
        ORIGIN + "/sitemap.xml": urlset(ORIGIN + "/", ORIGIN + "/about"),
        ORIGIN + "/": page(ORIGIN + "/"),
        ORIGIN + "/about": page(ORIGIN + "/about"),
        "http://example.com/": d.Response(301, {"location": ORIGIN + "/"}, ""),
    }
    pages.update(extra)
    return FakeSource(pages)


def rules(report, level="error"):
    return sorted({f.rule for f in report.findings if f.level == level})


class RobotsTests(unittest.TestCase):
    def test_parse_merges_groups_and_reads_sitemaps(self):
        groups, sitemaps = d.parse_robots(
            "# comment\nUser-agent: A\nUser-agent: B\nDisallow: /x\nnot a directive\n"
            "User-agent: a\nAllow: /\nSitemap: https://e.com/s.xml\nDisallow: /orphan\n"
        )
        # Google: a Sitemap line belongs to no group, so the rule after it stays in group "a".
        self.assertEqual(groups["a"], [("disallow", "/x"), ("allow", "/"), ("disallow", "/orphan")])
        self.assertEqual(groups["b"], [("disallow", "/x")])
        self.assertEqual(sitemaps, ["https://e.com/s.xml"])

    def test_rule_before_any_agent_is_ignored(self):
        groups, _ = d.parse_robots("Disallow: /\nUser-agent: *\nAllow: /\n")
        self.assertEqual(groups, {"*": [("allow", "/")]})

    def test_root_allowed_precedence(self):
        groups, _ = d.parse_robots(
            "User-agent: *\nDisallow: /\n\nUser-agent: Googlebot\nAllow: /\n\n"
            "User-agent: GPTBot\nDisallow: /\nAllow: /\n\nUser-agent: ClaudeBot\nAllow: /\nDisallow: /\n\nUser-agent: Bingbot\nDisallow: /private\n"
            "User-agent: CCBot\nAllow: /\nDisallow: /*\n"
        )
        self.assertTrue(d.root_allowed(groups, "Googlebot"))  # specific group beats *
        self.assertTrue(d.root_allowed(groups, "GPTBot"))  # Allow wins a tie
        self.assertTrue(d.root_allowed(groups, "ClaudeBot"))  # in either order
        self.assertTrue(d.root_allowed(groups, "Bingbot"))  # /private does not match /
        self.assertTrue(d.root_allowed(groups, "CCBot"))  # /* equals / (a trailing * adds nothing); Allow wins
        self.assertFalse(d.root_allowed(groups, "PerplexityBot"))  # falls back to *
        self.assertTrue(d.root_allowed({}, "anything"))

    def test_blocked_search_crawler_is_an_error_training_crawler_is_not(self):
        robots = ROBOTS + "User-agent: OAI-SearchBot\nDisallow: /\nUser-agent: GPTBot\nDisallow: /\n"
        report = d.cmd_live(argparse.Namespace(url=ORIGIN), healthy_site(**{ORIGIN + "/robots.txt": robots}))
        self.assertEqual(rules(report), ["search-crawler-allowed"])
        self.assertFalse(report.agents["OAI-SearchBot"])
        self.assertFalse(report.agents["GPTBot"])
        self.assertTrue(report.agents["Googlebot"])

    def test_missing_robots(self):
        site = healthy_site()
        del site.pages[ORIGIN + "/robots.txt"]
        report = d.cmd_live(argparse.Namespace(url=ORIGIN), site)
        # RFC 9309: a 4xx robots.txt allows everything, so it is a warning, not an error.
        self.assertEqual((rules(report), rules(report, "warning")), ([], ["robots-present"]))
        self.assertEqual(report.pages, 2)  # falls back to /sitemap.xml

    def test_robots_without_sitemap_line(self):
        report = d.cmd_live(argparse.Namespace(url=ORIGIN), healthy_site(**{ORIGIN + "/robots.txt": "User-agent: *\nAllow: /\n"}))
        self.assertEqual(rules(report, "warning"), ["robots-sitemap"])
        self.assertEqual(report.errors, 0)

    def test_sitemap_on_another_origin(self):
        robots = "User-agent: *\nAllow: /\nSitemap: https://other.com/sitemap.xml\n"
        report = d.cmd_live(argparse.Namespace(url=ORIGIN), healthy_site(**{ORIGIN + "/robots.txt": robots}))
        self.assertEqual(rules(report), ["sitemap-origin"])


class SitemapTests(unittest.TestCase):
    def live(self, **pages):
        return d.cmd_live(argparse.Namespace(url=ORIGIN), healthy_site(**pages))

    def test_healthy_site_is_clean(self):
        report = self.live()
        self.assertEqual(report.findings, [])
        self.assertEqual(report.pages, 2)

    def test_sitemap_missing(self):
        report = self.live(**{ORIGIN + "/sitemap.xml": d.Response(404, {}, "")})
        self.assertEqual(rules(report), ["sitemap-present"])

    def test_sitemap_is_an_app_fallback(self):
        report = self.live(**{ORIGIN + "/sitemap.xml": "﻿<!DOCTYPE html><html><body>app</body></html>"})
        self.assertEqual(rules(report), ["sitemap-present"])

    def test_sitemap_not_xml(self):
        self.assertEqual(rules(self.live(**{ORIGIN + "/sitemap.xml": "<urlset"})), ["sitemap-xml"])

    def test_sitemap_wrong_root(self):
        self.assertEqual(rules(self.live(**{ORIGIN + "/sitemap.xml": "<rss/>"})), ["sitemap-xml"])

    def test_sitemap_empty(self):
        self.assertEqual(rules(self.live(**{ORIGIN + "/sitemap.xml": urlset()})), ["sitemap-empty"])

    def test_bad_urls(self):
        body = urlset("https://other.com/x", "", ORIGIN + "/?q=1", ORIGIN + "/#a", ORIGIN + "/", ORIGIN + "/")
        report = self.live(**{ORIGIN + "/sitemap.xml": body, ORIGIN + "/?q=1": page(ORIGIN + "/?q=1")})
        self.assertEqual(rules(report), ["sitemap-clean-url", "sitemap-duplicate", "sitemap-origin"])
        self.assertEqual(report.pages, 2)  # a query string is a valid sitemap URL

    def test_lastmod_validation(self):
        good = self.live(**{ORIGIN + "/sitemap.xml": urlset(ORIGIN + "/", lastmod="2026-11-19")})
        self.assertEqual(good.findings, [])
        bad = self.live(**{ORIGIN + "/sitemap.xml": urlset(ORIGIN + "/", lastmod="last tuesday")})
        self.assertEqual(rules(bad, "warning"), ["sitemap-lastmod"])

    def test_index_follows_one_level(self):
        report = self.live(**{
            ORIGIN + "/sitemap.xml": index(ORIGIN + "/a.xml", ORIGIN + "/a.xml", "https://other.com/b.xml"),
            ORIGIN + "/a.xml": urlset(ORIGIN + "/", ORIGIN + "/about"),
        })
        self.assertEqual(rules(report), ["sitemap-origin"])
        self.assertEqual(report.pages, 2)

    def test_index_of_an_index_is_an_error(self):
        report = self.live(**{
            ORIGIN + "/sitemap.xml": index(ORIGIN + "/a.xml"),
            ORIGIN + "/a.xml": index(ORIGIN + "/b.xml"),
        })
        self.assertIn("sitemap-depth", rules(report))

    def test_valid_lastmod_forms(self):
        self.assertEqual(d._valid_lastmod("2026-11-19T10:00:00Z").tzinfo, dt.timezone.utc)
        self.assertIsNotNone(d._valid_lastmod("2026-11-19T10:00:00-07:00"))
        self.assertIsNone(d._valid_lastmod("19/11/2026"))


class PageTests(unittest.TestCase):
    def check(self, html=None, resp=None, url=ORIGIN + "/about", home=False):
        report = d.Report(mode="live", target=ORIGIN)
        d.check_page(report, url, resp or d.Response(200, {}, html), ORIGIN, home)
        return report

    def test_clean_page(self):
        self.assertEqual(self.check(page(ORIGIN + "/about")).findings, [])

    def test_redirect_and_missing(self):
        report = self.check(resp=d.Response(307, {"location": "/about/"}, ""))
        self.assertEqual(rules(report), ["page-status"])
        self.assertIn("/about/", report.findings[0].message)
        self.assertNotIn("(to", self.check(resp=d.Response(404, {}, "")).findings[0].message)

    def test_noindex_header_and_meta(self):
        header = self.check(resp=d.Response(200, {"x-robots-tag": "noindex, nofollow"}, page(ORIGIN + "/about")))
        self.assertEqual(rules(header), ["page-indexable"])
        self.assertEqual(rules(self.check(page(ORIGIN + "/about", robots="noindex"))), ["page-indexable"])

    def test_title_and_description(self):
        self.assertEqual(rules(self.check(page(ORIGIN + "/about", title=None))), ["page-title"])
        self.assertEqual(rules(self.check(page(ORIGIN + "/about", title=" "))), ["page-title"])
        self.assertEqual(rules(self.check(page(ORIGIN + "/about", title="t" * 66)), "warning"), ["page-title-length"])
        self.assertEqual(rules(self.check(page(ORIGIN + "/about", description=None))), ["page-description"])
        long_description = self.check(page(ORIGIN + "/about", description="d" * 171))
        self.assertEqual(rules(long_description, "warning"), ["page-description-length"])

    def test_canonical(self):
        self.assertEqual(rules(self.check(page(ORIGIN + "/about", canonical=""))), ["page-canonical"])
        wrong = self.check(page(ORIGIN + "/about", canonical=ORIGIN + "/"))
        self.assertEqual(rules(wrong), ["page-canonical"])
        twice = page(ORIGIN + "/about").replace("</head>", f'<link rel="canonical" href="{ORIGIN}/about"></head>')
        self.assertEqual(rules(self.check(twice)), ["page-canonical"])
        bare_home = self.check(page(ORIGIN, canonical=ORIGIN + "/"), url=ORIGIN)
        self.assertEqual(bare_home.findings, [])

    def test_same_url(self):
        self.assertTrue(d.same_url("https://e.com", "https://e.com/"))
        self.assertTrue(d.same_url("https://e.com/", "https://e.com"))
        self.assertFalse(d.same_url("https://e.com/a", "https://e.com/a/"))

    def test_served_content(self):
        shell = self.check(page(ORIGIN + "/about", body="Loading", h1=0))
        self.assertEqual(rules(shell), ["page-served-content"])
        hidden = page(ORIGIN + "/about", h1=0, body="<noscript>" + WORDS + "</noscript><template>" + WORDS + "</template>")
        self.assertEqual(rules(self.check(hidden)), ["page-served-content"])
        short = self.check(page(ORIGIN + "/about", body="Get a quote in three steps."))
        self.assertEqual((short.errors, rules(short, "warning")), (0, ["page-thin-content"]))
        stray_close = page(ORIGIN + "/about").replace("<p>", "</noscript><p>")
        self.assertEqual(self.check(stray_close).findings, [])

    def test_quality_warnings(self):
        report = self.check(page(ORIGIN + "/about", lang="", h1=2, og=False))
        self.assertEqual(rules(report, "warning"), ["page-h1", "page-lang", "page-open-graph"])
        self.assertEqual(report.errors, 0)

    def test_structured_data(self):
        broken = self.check(page(ORIGIN + "/about", jsonld=("{not json",)))
        self.assertEqual(rules(broken), ["structured-data"])
        untyped = self.check(page(ORIGIN + "/about", jsonld=('{"@context":"https://schema.org"}',)))
        self.assertEqual(rules(untyped), ["structured-data"])
        untyped = self.check(page(ORIGIN + "/about", jsonld=("[]",)))
        self.assertEqual(rules(untyped), ["structured-data"])
        graph = '{"@context":"https://schema.org","@graph":[{"@type":["Organization","Brand"]},{"name":"x"}]}'
        self.assertEqual(self.check(page(ORIGIN + "/", jsonld=(graph, '[{"@type":"WebSite"}]')), url=ORIGIN + "/", home=True).findings, [])
        bare_home = self.check(page(ORIGIN + "/", jsonld=()), url=ORIGIN + "/", home=True)
        self.assertEqual(rules(bare_home, "warning"), ["structured-data-home"])
        self.assertEqual(d._types("text"), [])


class LiveTests(unittest.TestCase):
    def test_soft_404_and_https_redirect(self):
        site = healthy_site(**{
            ORIGIN + d.SOFT_404_PATH: page(ORIGIN + "/"),
            "http://example.com/": d.Response(200, {}, "plain"),
        })
        report = d.cmd_live(argparse.Namespace(url=ORIGIN), site)
        self.assertEqual(rules(report), ["soft-404"])
        self.assertEqual(rules(report, "warning"), ["https-redirect"])

    def test_page_limit(self):
        locs = [f"{ORIGIN}/p{i}" for i in range(d.MAX_PAGES + 1)]
        pages = {loc: page(loc) for loc in locs}
        pages[ORIGIN + "/sitemap.xml"] = urlset(*locs)
        report = d.cmd_live(argparse.Namespace(url=ORIGIN), healthy_site(**pages))
        self.assertEqual(report.pages, d.MAX_PAGES)
        self.assertIn("page-limit", rules(report, "warning"))

    def test_origin_validation(self):
        self.assertEqual(d.normalise_origin("https://Example.com/"), ORIGIN)
        for bad in ("http://example.com", "https://example.com/path", "https://", "https://e.com/?a=1"):
            with self.assertRaises(ValueError):
                d.normalise_origin(bad)


class StaticTests(unittest.TestCase):
    def build(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for rel, text in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return root

    def site(self, **extra):
        files = {
            "robots.txt": ROBOTS,
            "sitemap.xml": urlset(ORIGIN + "/", ORIGIN + "/about", ORIGIN + "/team/", ORIGIN + "/blog/post"),
            "index.html": page(ORIGIN + "/"),
            "about.html": page(ORIGIN + "/about"),
            "team/index.html": page(ORIGIN + "/team/"),
            "blog/post/index.html": page(ORIGIN + "/blog/post"),
            "404.html": "<html><body>missing</body></html>",
        }
        files.update(extra)
        return self.build(files)

    def run_static(self, root):
        return d.cmd_static(argparse.Namespace(dir=str(root), origin=ORIGIN))

    def test_clean_build(self):
        report = self.run_static(self.site())
        self.assertEqual(report.findings, [])
        self.assertEqual(report.pages, 4)

    def test_sitemap_url_without_a_file(self):
        root = self.site(**{"sitemap.xml": urlset(ORIGIN + "/", ORIGIN + "/gone")})
        report = self.run_static(root)
        self.assertEqual(rules(report), ["page-status"])

    def test_page_missing_from_sitemap(self):
        root = self.site(**{"extra.html": page(ORIGIN + "/extra"), "hidden.html": page(ORIGIN + "/hidden", robots="noindex")})
        report = self.run_static(root)
        self.assertEqual([f.url for f in report.findings], ["extra.html"])
        self.assertEqual(rules(report, "warning"), ["sitemap-coverage"])

    def test_path_escape_is_refused(self):
        root = self.site()
        source = d.StaticSource(root / "about.html", ORIGIN)
        self.assertIsNone(source.file_for(ORIGIN + "/../index.html"))
        self.assertEqual(d.StaticSource(root, ORIGIN).get(ORIGIN + "/nope").status, 404)

    def test_not_a_directory(self):
        with self.assertRaises(ValueError):
            d.cmd_static(argparse.Namespace(dir="/definitely/not/here", origin=ORIGIN))


class IndexNowTests(unittest.TestCase):
    KEY = "0123456789abcdef"

    def args(self, **kw):
        base = {"url": ORIGIN, "key": self.KEY, "since_hours": 26.0, "dry_run": False}
        base.update(kw)
        return argparse.Namespace(**base)

    def site(self, lastmod):
        now = dt.datetime.now(dt.timezone.utc)
        return healthy_site(**{
            ORIGIN + f"/{self.KEY}.txt": self.KEY + "\n",
            ORIGIN + "/sitemap.xml": (
                '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                f"<url><loc>{ORIGIN}/</loc><lastmod>{lastmod(now)}</lastmod></url>"
                f"<url><loc>{ORIGIN}/old</loc><lastmod>2020-01-01</lastmod></url>"
                f"<url><loc>{ORIGIN}/undated</loc></url></urlset>"
            ),
        })

    def test_submits_only_recent_urls(self):
        sent = []
        report = d.cmd_indexnow(self.args(), self.site(lambda now: now.isoformat()), post=lambda p: sent.append(p) or 202)
        self.assertEqual(report.errors, 0)
        self.assertEqual(sent[0]["urlList"], [ORIGIN + "/"])
        self.assertEqual(sent[0]["host"], "example.com")
        self.assertEqual(sent[0]["keyLocation"], f"{ORIGIN}/{self.KEY}.txt")

    def test_nothing_recent_or_dry_run_sends_nothing(self):
        post = mock.Mock(side_effect=AssertionError("must not submit"))
        old = self.site(lambda now: "2020-01-01")
        self.assertEqual(d.cmd_indexnow(self.args(), old, post=post).pages, 0)
        recent = self.site(lambda now: now.isoformat())
        self.assertEqual(d.cmd_indexnow(self.args(dry_run=True), recent, post=post).pages, 1)

    def test_rejected_submission(self):
        report = d.cmd_indexnow(self.args(), self.site(lambda now: now.isoformat()), post=lambda p: 403)
        self.assertEqual(rules(report), ["indexnow-submit"])

    def test_key_file_must_hold_the_key(self):
        site = healthy_site()
        self.assertEqual(rules(d.cmd_indexnow(self.args(), site)), ["indexnow-key"])

    def test_key_format(self):
        for bad in ("short", "has space in it", "x" * 129):
            with self.assertRaises(ValueError):
                d.cmd_indexnow(self.args(key=bad), healthy_site())


class ReviewRegressionTests(unittest.TestCase):
    """One test per defect found in independent review of glencoe-tech/.github#28 (e0a60f2)."""

    def live(self, **pages):
        return d.cmd_live(argparse.Namespace(url=ORIGIN), healthy_site(**pages))

    def check(self, html=None, resp=None, url=ORIGIN + "/about"):
        report = d.Report(mode="live", target=ORIGIN)
        d.check_page(report, url, resp or d.Response(200, {}, html), ORIGIN, False)
        return report

    def test_robots_is_applied_to_every_sitemap_url(self):
        report = self.live(**{ORIGIN + "/robots.txt": ROBOTS + "Disallow: /about\n"})
        self.assertEqual(rules(report), ["search-crawler-allowed"])
        self.assertEqual(report.findings[0].url, ORIGIN + "/about")

    def test_robots_wildcards_anchors_and_queries(self):
        groups, _ = d.parse_robots(
            "User-agent: *\nDisallow: /*.pdf$\nDisallow: /search?\nAllow: /fish*\nDisallow: /fish\n"
        )
        everything, _ = d.parse_robots("User-agent: *\nDisallow: /**$\n")
        self.assertFalse(d.path_allowed(everything, "Bingbot", "/"))  # /**$ matches "/" (and every path)
        self.assertTrue(d.path_allowed(groups, "Bingbot", "/"))
        self.assertFalse(d.path_allowed(groups, "Bingbot", "/files/a.pdf"))
        self.assertTrue(d.path_allowed(groups, "Bingbot", "/files/a.pdf?x=1"))  # $ anchors the end
        self.assertFalse(d.path_allowed(groups, "Bingbot", "/search?q=1"))
        self.assertTrue(d.path_allowed(groups, "Bingbot", "/fishing"))  # /fish* equals /fish; Allow wins
        self.assertTrue(d.path_allowed(groups, "Bingbot", "/robots.txt"))
        report = d.Report(mode="live", target=ORIGIN)
        d.check_crawlable(report, groups, ORIGIN + "/search?q=1")
        self.assertEqual(rules(report), ["search-crawler-allowed"])

    def test_group_selection_by_product_token(self):
        groups, _ = d.parse_robots("User-agent: *\nDisallow: /\nUser-agent: googlebot\nAllow: /\n")
        self.assertTrue(d.path_allowed(groups, "Googlebot-News", "/"))  # falls back to googlebot
        self.assertFalse(d.path_allowed(groups, "Bingbot", "/"))

    def test_unreachable_or_html_robots(self):
        down = self.live(**{ORIGIN + "/robots.txt": d.Response(503)})
        self.assertIn("robots-present", rules(down))
        self.assertIn("search-crawler-allowed", rules(down))  # 5xx: the whole site is disallowed
        html = self.live(**{ORIGIN + "/robots.txt": "<!doctype html><html></html>"})
        self.assertEqual(rules(html), [])
        self.assertIn("robots-present", rules(html, "warning"))

    def test_noindex_forms(self):
        self.assertEqual(rules(self.check(page(ORIGIN + "/about", robots="none"))), ["page-indexable"])
        bing = page(ORIGIN + "/about").replace("</head>", '<meta name="bingbot" content="NOINDEX, follow"></head>')
        self.assertEqual(rules(self.check(bing)), ["page-indexable"])
        other = page(ORIGIN + "/about").replace("</head>", '<meta name="gptbot" content="noindex"></head>')
        self.assertEqual(self.check(other).findings, [])
        repeated = d.Response(200, {"X-Robots-Tag": ["noindex", "all"]}, page(ORIGIN + "/about"))
        self.assertEqual(rules(self.check(resp=repeated)), ["page-indexable"])
        scoped = d.Response(200, {"X-Robots-Tag": ["GPTBot: noindex", "max-snippet: 20", "unavailable_after: 2030-01-01"]}, page(ORIGIN + "/about"))
        self.assertEqual(self.check(resp=scoped).findings, [])
        google = d.Response(200, {"X-Robots-Tag": "googlebot: none"}, page(ORIGIN + "/about"))
        self.assertEqual(rules(self.check(resp=google)), ["page-indexable"])

    def test_canonical_resolution(self):
        self.assertEqual(self.check(page(ORIGIN + "/about", canonical="/about")).findings, [])
        self.assertEqual(self.check(page(ORIGIN + "/about", canonical="https://EXAMPLE.com:443/about")).findings, [])
        based = page(ORIGIN + "/about", canonical="about").replace("<head>", '<head><base href="/">')
        self.assertEqual(self.check(based).findings, [])
        self.assertEqual(rules(self.check(page(ORIGIN + "/about", canonical="/about?x=1"))), ["page-canonical"])
        self.assertFalse(d.same_url("https://e.com:8443/a", "https://e.com/a"))
        self.assertEqual(d.url_key("http://E.com:80/a#x"), "http://e.com/a")

    def test_missing_head_end_tag_and_inert_templates(self):
        no_head_end = (f'<html lang="en"><head><title>t</title><meta name="description" content="d">'
                       f'<link rel="canonical" href="{ORIGIN}/about"><body><p>{WORDS}</p></body></html>')
        self.assertEqual(rules(self.check(no_head_end)), [])
        stray_text = f'<html lang="en"><head><title>t</title><meta name="description" content="d"><link rel="canonical" href="{ORIGIN}/about">{WORDS}'
        self.assertEqual(rules(self.check(stray_text)), [])
        hidden_h1 = page(ORIGIN + "/about", h1=0, body="Loading<template><h1>Hidden</h1></template>")
        self.assertEqual(rules(self.check(hidden_h1)), ["page-served-content"])
        template_noindex = page(ORIGIN + "/about").replace("</body>", '<template><meta name="robots" content="noindex"></template></body>')
        self.assertEqual(self.check(template_noindex).findings, [])

    def test_malformed_json_ld_shapes_do_not_crash(self):
        for block in ('{"@type":"Organization","@graph":1}', '{"@type":"Organization","@graph":null}',
                      '{"@type":["Organization",7,null]}', '{"@graph":{"@type":"WebSite"}}'):
            self.assertEqual(self.check(page(ORIGIN + "/about", jsonld=(block,))).findings, [], block)
        self.assertEqual(rules(self.check(page(ORIGIN + "/about", jsonld=('"text"',)))), ["structured-data"])

    def test_w3c_lastmod_grammar(self):
        for good in ("2026", "2026-11", "2026-11-19", "2026-11-19T12:00Z", "2026-11-19T12:00:00.5+01:00"):
            self.assertIsNotNone(d._valid_lastmod(good), good)
        for bad in ("20261119", "2026-W47-4", "2026-11-19T12:00:00", "2026-13-01", "2026-11-19T25:00Z"):
            self.assertIsNone(d._valid_lastmod(bad), bad)
        self.assertEqual(d._valid_lastmod("2026-11-19T12:00-02:00").utcoffset(), dt.timedelta(hours=-2))

    def test_sitemap_size_gzip_and_doctype(self):
        import gzip as gz
        big = d.Response(200, {}, urlset(ORIGIN + "/"), truncated=True)
        self.assertEqual(rules(self.live(**{ORIGIN + "/sitemap.xml": big})), ["sitemap-size"])
        packed = d.Response(200, {}, gz.compress(urlset(ORIGIN + "/", ORIGIN + "/about").encode()))
        self.assertEqual(self.live(**{ORIGIN + "/sitemap.xml": packed}).pages, 2)
        broken = d.Response(200, {}, b"\x1f\x8b\x08\x00broken")
        self.assertEqual(rules(self.live(**{ORIGIN + "/sitemap.xml": broken})), ["sitemap-xml"])
        with mock.patch.object(d, "SITEMAP_LIMIT_BYTES", 10):
            self.assertEqual(rules(self.live(**{ORIGIN + "/sitemap.xml": packed})), ["sitemap-size"])
        doctype = '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaa">]>' + urlset(ORIGIN + "/")
        self.assertEqual(rules(self.live(**{ORIGIN + "/sitemap.xml": doctype})), ["sitemap-xml"])

    def test_large_page_is_flagged_not_mislabelled(self):
        resp = d.Response(200, {}, page(ORIGIN + "/about"), truncated=True)
        self.assertEqual(rules(self.check(resp=resp), "warning"), ["page-size"])
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.html").write_text("x" * 20)
            got = d.StaticSource(Path(tmp), ORIGIN).get(ORIGIN + "/a", limit=10)
            self.assertEqual((got.truncated, len(got.raw)), (True, 10))

    def test_indexnow_batches_every_url(self):
        key = "0123456789abcdef"
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        locs = [f"{ORIGIN}/p{i}" for i in range(d.INDEXNOW_BATCH + 1)]
        site = healthy_site(**{ORIGIN + f"/{key}.txt": key, ORIGIN + "/sitemap.xml": urlset(*locs, lastmod=now)})
        sent = []
        args = argparse.Namespace(url=ORIGIN, key=key, since_hours=26.0, dry_run=False)
        report = d.cmd_indexnow(args, site, post=lambda p: sent.append(len(p["urlList"])) or 200)
        self.assertEqual((sent, report.pages, report.errors), ([d.INDEXNOW_BATCH, 1], d.INDEXNOW_BATCH + 1, 0))
        failing = d.cmd_indexnow(args, site, post=lambda p: 500 if len(p["urlList"]) == 1 else 202)
        self.assertEqual((failing.pages, rules(failing)), (d.INDEXNOW_BATCH, ["indexnow-submit"]))


class DeltaReviewRegressionTests(unittest.TestCase):
    """One test per defect found in the delta review of glencoe-tech/.github#28 (c484f7e)."""

    def live(self, **pages):
        return d.cmd_live(argparse.Namespace(url=ORIGIN), healthy_site(**pages))

    def check(self, html=None, resp=None, url=ORIGIN + "/about"):
        report = d.Report(mode="live", target=ORIGIN)
        d.check_page(report, url, resp or d.Response(200, {}, html), ORIGIN, False)
        return report

    def test_out_of_range_zone_is_invalid_not_a_crash(self):
        self.assertIsNone(d._valid_lastmod("2026-11-19T12:00+99:00"))
        report = self.live(**{ORIGIN + "/sitemap.xml": urlset(ORIGIN + "/", lastmod="2026-11-19T12:00+99:00")})
        self.assertEqual(rules(report, "warning"), ["sitemap-lastmod"])

    def test_robots_redirects_are_followed_and_429_is_unavailable(self):
        moved = self.live(**{
            ORIGIN + "/robots.txt": d.Response(301, {"location": "/real-robots.txt"}),
            ORIGIN + "/real-robots.txt": "User-agent: *\nDisallow: /\n",
        })
        self.assertIn("search-crawler-allowed", rules(moved))
        loop = self.live(**{ORIGIN + "/robots.txt": d.Response(302, {"location": "/robots.txt"})})
        self.assertEqual((rules(loop), rules(loop, "warning")), ([], ["robots-present"]))
        busy = self.live(**{ORIGIN + "/robots.txt": d.Response(429)})
        self.assertIn("robots-present", rules(busy))
        self.assertIn("search-crawler-allowed", rules(busy))

    def test_percent_encoding_is_normalised(self):
        groups, _ = d.parse_robots("User-agent: *\nDisallow: /private\nDisallow: /café\nDisallow: /a%2fb\n")
        self.assertFalse(d.path_allowed(groups, "Bingbot", "/%70rivate"))
        self.assertFalse(d.path_allowed(groups, "Bingbot", "/caf%C3%A9"))
        self.assertFalse(d.path_allowed(groups, "Bingbot", "/caf%c3%a9/menu"))
        self.assertFalse(d.path_allowed(groups, "Bingbot", "/a%2Fb"))  # reserved escapes keep their meaning
        self.assertTrue(d.path_allowed(groups, "Bingbot", "/a/b"))
        self.assertEqual(d.normalise_path("/%7euser/%zz"), "/~user/%zz")

    def test_combined_scopes_in_one_header(self):
        self.assertTrue(d.header_noindex(["bingbot: nofollow, googlebot: noindex"]))
        self.assertTrue(d.header_noindex(["googlebot: nofollow, noindex"]))
        self.assertFalse(d.header_noindex(["googlebot: nofollow, gptbot: noindex"]))
        self.assertFalse(d.header_noindex(["max-snippet: 20, nofollow"]))

    def test_every_title_is_head_content(self):
        second = page(ORIGIN + "/about", h1=0, body="Loading").replace("</title>", "</title><title>" + WORDS + "</title>")
        report = self.check(second)
        self.assertEqual(rules(report), ["page-served-content"])
        svg_only = page(ORIGIN + "/about", title=None, body=WORDS + "<svg><title>Chart</title></svg>")
        self.assertEqual(rules(self.check(svg_only)), ["page-title"])

    def test_malformed_canonical_is_a_finding(self):
        report = self.check(page(ORIGIN + "/about", canonical="https://example.com:bad/about"))
        self.assertEqual(rules(report), ["page-canonical"])
        self.assertIn("not a valid URL", report.findings[0].message)

    def test_corrupt_gzip_is_a_finding(self):
        corrupt = d.Response(200, {}, bytes.fromhex("1f8b080000000000000007"))
        self.assertEqual(rules(self.live(**{ORIGIN + "/sitemap.xml": corrupt})), ["sitemap-xml"])


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(301)
            self.send_header("Location", "/elsewhere")
            self.end_headers()
        elif self.path == "/missing":
            self.send_response(404)
            self.end_headers()
        else:
            body = b"hello"
            self.send_response(200)
            self.send_header("X-Robots-Tag", "all")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        _Handler.posted = json.loads(self.rfile.read(length))
        self.send_response(202 if _Handler.posted.get("key") != "reject" else 422)
        self.end_headers()


class NetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_live_source_does_not_follow_redirects(self):
        source = d.LiveSource(timeout=5)
        ok = source.get(self.base + "/page")
        self.assertEqual((ok.status, ok.body, ok.headers["x-robots-tag"]), (200, "hello", ["all"]))
        moved = source.get(self.base + "/redirect")
        self.assertEqual((moved.status, moved.header("location")), (301, "/elsewhere"))
        self.assertEqual(source.get(self.base + "/missing").status, 404)
        self.assertEqual(source.get("http://127.0.0.1:9/").status, 0)

    def test_post_indexnow(self):
        with mock.patch.object(d, "INDEXNOW_ENDPOINT", self.base + "/indexnow"):
            self.assertEqual(d._post_indexnow({"key": "k", "urlList": []}), 202)
            self.assertEqual(_Handler.posted["key"], "k")
            self.assertEqual(d._post_indexnow({"key": "reject"}), 422)
        with mock.patch.object(d, "INDEXNOW_ENDPOINT", "http://127.0.0.1:9/indexnow"):
            self.assertEqual(d._post_indexnow({}), 0)


class MainTests(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = d.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_static_command_and_json(self):
        root = StaticTests.build(self, {"robots.txt": ROBOTS, "sitemap.xml": urlset(ORIGIN + "/"), "index.html": page(ORIGIN + "/")})
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            code, text, _ = self.run_main(["--json", str(out), "static", str(root), "--origin", ORIGIN])
            data = json.loads(out.read_text())
        self.assertEqual(code, 0)
        self.assertIn("1 pages, 0 errors", text)
        self.assertEqual(data["summary"], {"errors": 0, "warnings": 0})
        self.assertIn("Googlebot=allow", text)

    def test_live_command_reports_errors(self):
        site = healthy_site(**{ORIGIN + "/about": d.Response(307, {"location": "/about/"}, "")})
        with mock.patch.object(d, "LiveSource", return_value=site):
            code, text, _ = self.run_main(["live", ORIGIN])
        self.assertEqual(code, 1)
        self.assertIn("ERROR   page-status", text)

    def test_indexnow_command_uses_live_source(self):
        with mock.patch.object(d, "LiveSource", return_value=healthy_site()):
            code, text, _ = self.run_main(["indexnow", ORIGIN, "--key", "0123456789abcdef", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("indexnow-key", text)

    def test_bad_usage(self):
        code, _, err = self.run_main(["live", "http://example.com"])
        self.assertEqual(code, 2)
        self.assertIn("origin must look like", err)


if __name__ == "__main__":
    unittest.main()

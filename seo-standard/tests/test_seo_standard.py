"""seo-standard: every rule is exercised failing as well as passing, in each mode it applies to,
including the regressions found in independent review (stale check runs, robots precedence and
wildcards, crawler-scoped X-Robots-Tag, MIME-only HTML detection, sampled-page canonicals, foreign
sitemap origins, unreachable sites exiting 2).
Run: python3 -I -m unittest discover -s seo-standard/tests -v
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import seo_standard as seo  # noqa: E402

CANON = "https://example.com/"
SITEMAP = "https://example.com/sitemap.xml"


BODY = b" ".join([b"word"] * 60)


def page(canonical="https://example.com/", extra=b""):
    return (b'<!doctype html><html><head><title>Example</title>'
            b'<link rel="canonical" href="' + canonical.encode() + b'">'
            b'<meta name="description" content="d"><meta property="og:title" content="t">'
            b'<meta property="og:description" content="d"><meta property="og:image" content="i">'
            b'<meta name="viewport" content="width=device-width">' + extra +
            b'<script type="application/ld+json">{"@type": "Organization"}</script></head>'
            b'<body><h1>Example</h1><p>' + BODY + b'</p></body></html>')


HOME = page()
URLSET = (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
          b'<url><loc>https://example.com/</loc></url><url><loc>https://example.com/a</loc></url></urlset>')
ROBOTS = b"User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml\n"


def R(status=200, body=b"", **headers):
    return seo.Response(status, {k.replace("_", "-").lower(): v for k, v in headers.items()}, body)


def live(origin="https://example.com"):
    return {
        ("GET", "http://example.com/"): R(301, location="https://example.com/"),
        ("GET", "https://www.example.com/"): R(308, location="https://example.com/"),
        ("GET", f"{origin}/"): R(200, HOME, content_type="text/html"),
        ("GET", f"{origin}/robots.txt"): R(200, ROBOTS, content_type="text/plain"),
        ("GET", f"{origin}/sitemap.xml"): R(200, URLSET, content_type="application/xml"),
        ("GET", f"{origin}/a"): R(200, page("https://example.com/a"), content_type="text/html"),
        ("GET", f"{origin}{seo.SOFT_404_PATH}"): R(404, b"not found", content_type="text/html"),
    }


class Fake:
    def __init__(self, table):
        self.table, self.calls, self.bodies = dict(table), [], []

    def __call__(self, req, timeout):
        key = (req.get_method(), req.full_url)
        self.calls.append((key, dict(req.header_items())))
        self.bodies.append(req.data)
        if key not in self.table:
            raise urllib.error.URLError(f"no route {key}")
        v = self.table[key]
        if isinstance(v, list):
            v = v.pop(0) if len(v) > 1 else v[0]
        return v


def http(table):
    fake = Fake(table)
    return seo.Http(send=fake, sleep=lambda s: None), fake


def run(table, mode="production", **kw):
    h, _ = http(table)
    return seo.audit(CANON, kw.pop("sitemaps", [SITEMAP]), kw.pop("private", []), mode, h, **kw)


def codes(result, severity=None):
    return {f["code"] for f in result["findings"] if severity in (None, f["severity"])}


def loud(result):
    return [f for f in result["findings"] if f["severity"] != "info"]


class Production(unittest.TestCase):
    def test_a_healthy_site_has_no_findings_above_info(self):
        result = run(live())
        self.assertEqual(loud(result), [])
        self.assertNotIn("unauditable", result)
        self.assertEqual((result["facts"]["sitemap_urls"], result["facts"]["structured_data"]), (2, 1))

    def test_redirects(self):
        t = live()
        t[("GET", "http://example.com/")] = R(200, HOME)
        t[("GET", "https://www.example.com/")] = R(302, location="https://example.com/")
        result = run(t)
        self.assertIn("variant-not-redirected", codes(result, "critical"))
        self.assertIn("redirect-not-permanent", codes(result, "warn"))
        del t[("GET", "https://www.example.com/")]
        self.assertIn("variant-unresolved", codes(run(t), "info"))

    def test_www_canonical_checks_the_apex(self):
        h, fake = http({("GET", "http://www.example.com/"): R(301, location="https://www.example.com/"),
                        ("GET", "https://example.com/"): R(301, location="https://www.example.com/"),
                        ("GET", "https://www.example.com/"): R(404)})
        seo.audit("https://www.example.com/", [SITEMAP], [], "production", h)
        self.assertIn(("GET", "https://example.com/"), [c[0] for c in fake.calls])

    def test_an_unreachable_site_is_not_audited_never_a_critical(self):
        result = run({})
        self.assertIn("unreachable", result["unauditable"])
        self.assertEqual(codes(result, "critical"), set())
        t = live()
        del t[("GET", SITEMAP)]
        self.assertIn("unreachable", run(t)["unauditable"])

    def test_homepage_status_and_bot_challenge(self):
        t = live()
        t[("GET", "https://example.com/")] = R(403, cf_mitigated="challenge")
        self.assertLessEqual({"homepage-status", "bot-challenge"}, codes(run(t)))
        t[("GET", "https://example.com/")] = R(500)
        self.assertNotIn("bot-challenge", codes(run(t)))

    def test_markup(self):
        t = live()
        t[("GET", "https://example.com/")] = R(200, b'<html><head><meta name="robots" content="noindex">'
                                                  b'<link rel="canonical" href="https://other.com/">'
                                                  b'<script type="application/ld+json">{bad</script></head></html>',
                                               x_robots_tag="noindex")
        result = run(t)
        self.assertLessEqual({"homepage-noindex-header", "homepage-noindex-meta", "canonical-mismatch"},
                             codes(result, "critical"))
        self.assertLessEqual({"no-title", "no-meta-description", "open-graph-incomplete", "no-viewport",
                              "structured-data-invalid"}, codes(result, "warn"))
        t[("GET", "https://example.com/")] = R(200, b"<html><title>x</title></html>")
        result = run(t)
        self.assertIn("no-canonical", codes(result, "warn"))
        self.assertIn("no-structured-data", codes(result, "info"))

    def test_x_robots_tag_scoped_to_another_crawler_is_not_a_noindex(self):
        t = live()
        t[("GET", "https://example.com/")] = R(200, HOME, x_robots_tag="googlebot-news: noindex")
        self.assertEqual(loud(run(t)), [])
        t[("GET", "https://example.com/")] = R(200, HOME, x_robots_tag="otherbot: noindex\nnoindex")
        self.assertIn("homepage-noindex-header", codes(run(t), "critical"), "a second header line is unscoped")
        t[("GET", "https://example.com/")] = R(200, HOME, x_robots_tag="unavailable_after: 2030-01-01, googlebot: none")
        self.assertIn("homepage-noindex-header", codes(run(t), "critical"))

    def test_a_relative_canonical_resolves_against_the_production_url(self):
        t = live()
        t[("GET", "https://example.com/")] = R(200, page("/"))
        self.assertNotIn("canonical-mismatch", codes(run(t)))

    def test_robots_availability(self):
        t = live()
        t[("GET", "https://example.com/robots.txt")] = R(404)
        result = run(t)
        self.assertIn("robots-missing", codes(result, "warn"), "a missing robots.txt allows crawling")
        self.assertEqual(codes(result, "critical"), set())
        t[("GET", "https://example.com/robots.txt")] = R(403)
        self.assertIn("robots-unreadable", codes(run(t), "critical"))
        t[("GET", "https://example.com/robots.txt")] = R(200, b"<!doctype html><html>", content_type="text/html")
        self.assertIn("robots-is-html", codes(run(t), "critical"))

    def test_robots_judged_by_content_not_mime(self):
        t = live()
        t[("GET", "https://example.com/robots.txt")] = R(200, ROBOTS, content_type="text/html")
        result = run(t)
        self.assertEqual(codes(result, "critical"), set())
        self.assertIn("robots-content-type", codes(result, "warn"))

    def test_robots_rules(self):
        for body in (b"User-agent: *\nDisallow: /\n", b"User-agent: *\nDisallow: /*\n",
                     b"User-agent: Googlebot\nDisallow: /$\n"):
            t = live()
            t[("GET", "https://example.com/robots.txt")] = R(200, body)
            with self.subTest(body):
                self.assertIn("robots-blocks-search", codes(run(t), "critical"))
        t = live()
        t[("GET", "https://example.com/robots.txt")] = R(200, b"User-agent: *\nDisallow: /a\n" + ROBOTS)
        self.assertIn("sitemap-url-disallowed", codes(run(t), "critical"))
        t[("GET", "https://example.com/robots.txt")] = R(200, b"User-agent: *\nSitemap: https://example.com/o.xml\n")
        self.assertIn("sitemap-not-in-robots", codes(run(t), "warn"))
        t[("GET", "https://example.com/robots.txt")] = R(200, b"User-agent: *\nAllow: /\n")
        self.assertIn("robots-no-sitemap", codes(run(t), "warn"))

    def test_crawler_access_separates_search_from_training(self):
        t = live()
        t[("GET", "https://example.com/robots.txt")] = R(200, ROBOTS + b"\nUser-agent: GPTBot\nDisallow: /\n"
                                                         b"\nUser-agent: PerplexityBot\nDisallow: /\n")
        result = run(t)
        self.assertEqual([f["detail"] for f in result["findings"] if f["code"] == "robots-blocks-ai-search"],
                         ["hidden from AI search: perplexitybot"])
        self.assertEqual(result["facts"]["blocks_training_crawlers"], ["gptbot"])
        self.assertNotIn("robots-blocks-search", codes(result))

    def test_sitemap_defects(self):
        for resp, code in ((R(404), "sitemap-unreachable"),
                           (R(200, b"<!doctype html><html><body>app</body></html>", content_type="text/html"), "sitemap-is-html"),
                           (R(200, b"<urlset", content_type="application/xml"), "sitemap-invalid-xml"),
                           (R(200, b"<rss/>", content_type="application/xml"), "sitemap-not-a-sitemap")):
            t = live()
            t[("GET", SITEMAP)] = resp
            with self.subTest(code):
                self.assertIn(code, codes(run(t), "critical"))
        t = live()
        t[("GET", SITEMAP)] = R(200, b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>')
        self.assertIn("sitemap-empty", codes(run(t), "warn"))

    def test_valid_xml_served_as_html_is_a_warning(self):
        t = live()
        t[("GET", SITEMAP)] = R(200, URLSET, content_type="text/html")
        result = run(t)
        self.assertEqual(codes(result, "critical"), set())
        self.assertIn("sitemap-content-type", codes(result, "warn"))

    def test_index_children_and_sampled_urls(self):
        index = (b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                 b'<sitemap><loc>https://example.com/s1.xml</loc></sitemap>'
                 b'<sitemap><loc>https://example.com/nested.xml</loc></sitemap></sitemapindex>')
        s1 = (b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
              b'<url><loc>https://example.com/ok</loc></url><url><loc>https://example.com/gone</loc></url>'
              b'<url><loc>https://example.com/hidden</loc></url><url><loc>https://example.com/dup</loc></url>'
              b'<url><loc>http://cdn.example.com/x</loc></url></urlset>')
        t = live()
        t.update({("GET", SITEMAP): R(200, index), ("GET", "https://example.com/s1.xml"): R(200, s1),
                  ("GET", "https://example.com/nested.xml"): R(200, index),
                  ("GET", "https://example.com/ok"): R(200, page("https://example.com/ok")),
                  ("GET", "https://example.com/gone"): R(307, location="/gone/"),
                  ("GET", "https://example.com/hidden"): R(200, page("https://example.com/hidden"), x_robots_tag="noindex"),
                  ("GET", "https://example.com/dup"): R(200, page("https://example.com/"))})
        result = run(t)
        self.assertLessEqual({"sitemap-off-host", "sitemap-url-status", "sitemap-url-noindex",
                              "sitemap-url-canonical-elsewhere"}, codes(result, "critical"))
        self.assertIn("sitemap-nested-index", codes(result, "warn"))

    def test_a_foreign_child_sitemap_is_fetched_where_it_really_is(self):
        index = (b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                 b'<sitemap><loc>https://old.invalid/child.xml</loc></sitemap></sitemapindex>')
        t = live()
        t[("GET", SITEMAP)] = R(200, index)
        t[("GET", "https://example.com/child.xml")] = R(200, URLSET)
        t[("GET", "https://old.invalid/child.xml")] = R(404)
        self.assertIn("sitemap-unreachable", codes(run(t), "critical"))

    def test_private_hosts(self):
        t = live()
        t.update({("GET", "https://admin.example.com/"): R(200, b"<html>admin</html>"),
                  ("GET", "https://portal.example.com/"): R(200, b'<meta name="robots" content="noindex">'),
                  ("GET", "https://login.example.com/"): R(302, location="https://x.cloudflareaccess.com/")})
        result = run(t, private=["admin.example.com", "portal.example.com", "login.example.com", "gone.example.com"])
        self.assertEqual([f["detail"] for f in result["findings"] if f["code"] == "private-host-indexable"],
                         ["admin.example.com answers 200 without noindex"])


class Preview(unittest.TestCase):
    BASE = "https://pr-1-site.acme.workers.dev"

    def table(self):
        t = live(self.BASE)
        t[("GET", f"{self.BASE}/")] = R(200, HOME, x_robots_tag="noindex")
        return t

    def test_a_noindex_preview_of_a_healthy_site_passes(self):
        result = run(self.table(), "preview", base=self.BASE)
        self.assertEqual(loud(result), [])
        self.assertIn("homepage-noindex-header", codes(result, "info"))

    def test_previews_are_judged_as_the_production_site(self):
        t = self.table()
        t[("GET", f"{self.BASE}/")] = R(200, page(f"{self.BASE}/"))
        t[("GET", f"{self.BASE}/sitemap.xml")] = R(200, b"<!doctype html><html>", content_type="text/html")
        t[("GET", f"{self.BASE}/robots.txt")] = R(200, b"User-agent: *\nDisallow: /\n")
        result = run(t, "preview", base=self.BASE)
        self.assertEqual(codes(result, "critical"), {"canonical-mismatch", "sitemap-is-html"})
        self.assertIn("robots-blocks-search", codes(result, "info"))

    def test_sampled_pages_must_name_their_own_production_url(self):
        t = self.table()
        t[("GET", f"{self.BASE}/a")] = R(200, page("https://example.com/"))
        self.assertIn("sitemap-url-canonical-elsewhere", codes(run(t, "preview", base=self.BASE), "critical"))
        t[("GET", f"{self.BASE}/a")] = R(200, page("/a"))
        self.assertEqual(loud(run(t, "preview", base=self.BASE)), [])

    def test_preview_sitemap_urls_are_fetched_from_the_preview(self):
        t = self.table()
        t[("GET", f"{self.BASE}/a")] = R(404)
        self.assertIn("sitemap-url-status", codes(run(t, "preview", base=self.BASE), "critical"))

    def test_a_preview_behind_a_login_is_not_audited(self):
        t = self.table()
        t[("GET", f"{self.BASE}/")] = R(302, location="https://acme.cloudflareaccess.com/cdn-cgi/access/login")
        self.assertIn("cannot be audited", run(t, "preview", base=self.BASE)["unauditable"])
        t[("GET", f"{self.BASE}/")] = R(401)
        self.assertIn("cannot be audited", run(t, "preview", base=self.BASE)["unauditable"])

    def test_preview_requests_are_browser_navigations_and_production_ones_are_not(self):
        h, fake = http(self.table())
        seo.audit(CANON, [SITEMAP], [], "preview", h, base=self.BASE)
        self.assertTrue(all(c[1].get("Sec-fetch-mode") == "navigate" for c in fake.calls))
        h, fake = http(live())
        seo.audit(CANON, [SITEMAP], [], "production", h)
        self.assertFalse(any("Sec-fetch-mode" in c[1] for c in fake.calls))


class Artifact(unittest.TestCase):
    def build(self, files):
        d = tempfile.mkdtemp()
        for name, body in files.items():
            p = os.path.join(d, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(body)
        self.addCleanup(shutil.rmtree, d)
        return d

    def test_a_good_static_build(self):
        d = self.build({"index.html": HOME, "robots.txt": ROBOTS, "sitemap.xml": URLSET,
                        "a/index.html": page("https://example.com/a")})
        self.assertEqual(loud(seo.audit(CANON, [SITEMAP], [], "artifact", dist=d)), [])

    def test_missing_files_and_runtime_routes(self):
        d = self.build({"index.html": HOME, "a.html": page("https://example.com/a")})
        result = seo.audit(CANON, [SITEMAP], [], "artifact", dist=d)
        self.assertIn("robots-missing", codes(result, "warn"))
        self.assertIn("sitemap-unreachable", codes(result, "critical"))
        d = self.build({"index.html": HOME, "robots.txt": ROBOTS,
                        "sitemap.xml": URLSET.replace(b"/a<", b"/runtime<")})
        self.assertIn("sitemap-url-not-static", codes(seo.audit(CANON, [SITEMAP], [], "artifact", dist=d), "info"))

    def test_no_homepage(self):
        d = self.build({"robots.txt": ROBOTS})
        self.assertIn("homepage-status", codes(seo.audit(CANON, [SITEMAP], [], "artifact", dist=d)))


class Parsing(unittest.TestCase):
    def test_robots_precedence(self):
        self.assertFalse(seo.allowed("User-agent: *\nDisallow: /\n", "googlebot"))
        self.assertTrue(seo.allowed("User-agent: *\nDisallow: /\n\nUser-agent: Googlebot\nAllow: /\n", "googlebot"))
        self.assertTrue(seo.allowed("User-agent: *\nDisallow: /\nAllow: /$\n", "bingbot", "/"),
                        "the longer Allow wins")
        self.assertFalse(seo.allowed("User-agent: *\nDisallow: /\nAllow: /public\n", "bingbot", "/"),
                         "an Allow for another path does not open the homepage")
        self.assertTrue(seo.allowed("User-agent: *\nDisallow: /p\nAllow: /p\n", "googlebot", "/p"), "Allow wins a tie")
        self.assertTrue(seo.allowed("User-agent: *\nDisallow:\n", "googlebot"))
        self.assertTrue(seo.allowed("junk\nSitemap: https://x/s.xml", "googlebot"))
        self.assertFalse(seo.allowed("User-agent: Googlebot # g\nUser-agent: Bingbot\nDisallow: /\n", "bingbot"))
        self.assertFalse(seo.allowed("User-agent: *\nDisallow: /*.pdf$\n", "googlebot", "/a.pdf"))
        self.assertTrue(seo.allowed("User-agent: *\nDisallow: /*.pdf$\n", "googlebot", "/a.pdf.html"))
        self.assertEqual(seo.robots_sitemaps("sitemap: https://x/a.xml\n"), ["https://x/a.xml"])

    def test_page_and_helpers(self):
        p = seo.Page('<meta name="googlebot" content="NONE"><link rel="alternate" href="x"><meta content="x">')
        self.assertTrue(seo.noindex(p.robots))
        self.assertEqual(p.canonical, "")
        self.assertFalse(seo.noindex("index, follow"))
        self.assertFalse(seo.noindex("noimageindex"))
        self.assertEqual(seo.norm("https://Example.com"), "https://example.com/")
        self.assertEqual(R(200, X_A="1").header("X-A"), "1")
        self.assertIsNone(seo._NoRedirect().redirect_request(None, None, 301, "", {}, "https://x/"))
        self.assertEqual(seo.headers_of([("X-Robots-Tag", "a"), ("x-robots-tag", "b"), ("A", "1")]),
                         {"x-robots-tag": "a\nb", "a": "1"})
        self.assertTrue(seo.looks_html(b"\xef\xbb\xbf  <!DOCTYPE html>"))
        self.assertTrue(seo.looks_html(b"<!-- x --><html><head>"))
        self.assertFalse(seo.looks_html(b"User-agent: *\n"))

    def test_retries_are_bounded(self):
        slept = []
        fake = Fake({("GET", "https://x/"): [R(503), R(503), R(200)]})
        self.assertEqual(seo.Http(send=fake, sleep=slept.append).get("https://x/").status, 200)
        self.assertEqual(slept, [2, 4])
        fake = Fake({("GET", "https://x/"): R(429)})
        self.assertEqual(seo.Http(send=fake, sleep=lambda s: None, attempts=3).get("https://x/").status, 429)
        with self.assertRaises(ConnectionError):
            seo.Http(send=Fake({}), sleep=lambda s: None).get("https://x/")


class DiscoverabilityGaps(unittest.TestCase):
    """The rules ported from the reviewed discoverability check (glencoe-tech/.github#28, closed as
    superseded): served content, soft 404s, #fragment sitemap URLs, robots.txt redirects,
    percent-encoded robots paths, and query strings in sampled robots paths."""

    SHELL = (b'<!doctype html><html><head><title>Shell</title><link rel="canonical" href="https://example.com/">'
             b'<meta name="description" content="d"></head><body><div id="root"></div>'
             b'<noscript>You need to enable JavaScript to run this app and see everything here.</noscript>'
             b'<template><h1>Hidden</h1><p>' + BODY + b'</p></template><script>boot()</script></body></html>')

    def test_a_javascript_shell_is_a_warning_in_every_mode(self):
        t = live()
        t[("GET", "https://example.com/")] = R(200, self.SHELL, content_type="text/html")
        result = run(t)
        shells = [f for f in result["findings"] if f["code"] == "served-content-empty"]
        self.assertEqual([f["severity"] for f in shells], ["warn"])  # homepage once, though sampled too
        self.assertEqual(result["facts"]["served_words"], 0)
        self.assertEqual(codes(run(t), "critical"), set())
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(d, "index.html"), "wb") as fh:
            fh.write(self.SHELL)
        self.assertIn("served-content-empty", codes(seo.audit(CANON, [], mode="artifact", dist=d)))

    def test_short_pages_under_a_heading_and_unclosed_heads_pass(self):
        form = (b'<html><head><title>Quote</title><link rel="canonical" href="https://example.com/a">'
                b'<body><h1>Get a quote</h1><p>Three short steps.</p></body></html>')
        self.assertEqual((seo.Page(form.decode()).h1, seo.Page(form.decode()).words), (1, 6))
        unclosed = "<html><head><title>t</title><meta name=x content=y>" + " ".join(["w"] * 60)
        self.assertEqual(seo.Page(unclosed).words, 60)
        self.assertEqual(seo.Page("<head></head><body></noscript>a b</body>").words, 2)
        t = live()
        t[("GET", "https://example.com/a")] = R(200, form.replace(b"Three short steps.", b"Short."), content_type="text/html")
        self.assertNotIn("served-content-empty", codes(run(t)))

    def test_soft_404(self):
        t = live()
        t[("GET", f"https://example.com{seo.SOFT_404_PATH}")] = R(200, HOME, content_type="text/html")
        self.assertEqual({f["severity"] for f in run(t)["findings"] if f["code"] == "soft-404"}, {"warn"})
        del t[("GET", f"https://example.com{seo.SOFT_404_PATH}")]  # the probe fails: no verdict, no crash
        result = run(t)
        self.assertNotIn("soft-404", codes(result))
        self.assertNotIn("unauditable", result)

    def test_fragment_sitemap_urls(self):
        t = live()
        t[("GET", SITEMAP)] = R(200, URLSET.replace(b"https://example.com/a<", b"https://example.com/#a<"), content_type="application/xml")
        result = run(t)
        self.assertIn("sitemap-url-fragment", codes(result, "warn"))
        self.assertEqual(codes(result, "critical"), set())

    def test_robots_redirects(self):
        t = live()
        t[("GET", "https://example.com/robots.txt")] = R(301, location="/rules/start")
        t[("GET", "https://example.com/rules/start")] = R(302, location="robots.txt")
        t[("GET", "https://example.com/rules/robots.txt")] = R(200, b"User-agent: *\nDisallow: /\n", content_type="text/plain")
        self.assertIn("robots-blocks-search", codes(run(t), "critical"))  # each hop resolves against the last
        t[("GET", "https://example.com/robots.txt")] = R(301, location="/rules,production.txt")
        t[("GET", "https://example.com/rules,production.txt")] = R(200, b"User-agent: *\nDisallow: /\n", content_type="text/plain")
        self.assertIn("robots-blocks-search", codes(run(t), "critical"))  # a comma is part of the URL
        for n in range(seo.ROBOTS_REDIRECTS):  # exactly five redirects still reach the rules
            t[("GET", f"https://example.com/hop{n}")] = R(301, location=f"/hop{n + 1}")
        t[("GET", "https://example.com/robots.txt")] = R(301, location="/hop1")
        t[("GET", f"https://example.com/hop{seo.ROBOTS_REDIRECTS}")] = R(200, b"User-agent: *\nDisallow: /\n", content_type="text/plain")
        self.assertIn("robots-blocks-search", codes(run(t), "critical"))
        t[("GET", "https://example.com/robots.txt")] = R(302, location="/robots.txt")
        loop = run(t)
        self.assertTrue({"robots-redirect-loop", "robots-missing"} <= codes(loop, "warn"))
        self.assertNotIn("robots-blocks-search", codes(loop))
        for target in ("https://[invalid/robots.txt", "https://example.com:bad/r.txt", "mailto:x@example.com"):
            t[("GET", "https://example.com/robots.txt")] = R(302, location=target)
            self.assertIn("robots-redirect-invalid", codes(run(t), "warn"), target)
        t[("GET", "https://example.com/robots.txt")] = R(301, location="https://gone.example.net/robots.txt")
        self.assertIn("robots-unreadable", codes(run(t), "critical"))  # unreachable: treat as disallow-all

    def test_percent_encoded_paths_and_queries(self):
        rules = "User-agent: *\nDisallow: /private\nDisallow: /caf\u00e9\nDisallow: /a%2fb\nDisallow: /search?\n"
        self.assertFalse(seo.allowed(rules, "googlebot", "/%70rivate"))
        self.assertFalse(seo.allowed(rules, "googlebot", "/caf%c3%a9/menu"))
        self.assertFalse(seo.allowed(rules, "googlebot", "/a%2Fb"))  # a reserved escape keeps its meaning
        self.assertTrue(seo.allowed(rules, "googlebot", "/a/b"))
        self.assertEqual(seo.octets("/%7euser/%zz"), "/~user/%zz")
        # RFC 9309 2.2.2: the most specific rule has the most octets, so /* (2) beats / (1).
        self.assertFalse(seo.allowed("User-agent: *\nAllow: /\nDisallow: /*\n", "googlebot", "/x"))
        t = live()
        t[("GET", "https://example.com/robots.txt")] = R(200, ROBOTS + b"Disallow: /search?\n", content_type="text/plain")
        t[("GET", SITEMAP)] = R(200, URLSET.replace(b"https://example.com/a<", b"https://example.com/search?q=1<"), content_type="application/xml")
        t[("GET", "https://example.com/search?q=1")] = R(200, page("https://example.com/search?q=1"), content_type="text/html")
        self.assertIn("sitemap-url-disallowed", codes(run(t), "critical"))


class IndexNow(unittest.TestCase):
    """Only eligible canonical URLs whose content changed since IndexNow last accepted them, only
    after a deploy, with a ledger that records every submission and refusal."""

    KEY = "0123456789abcdef"
    POST = ("POST", seo.INDEXNOW_ENDPOINT)

    def site(self, **extra):
        t = live()
        t[("GET", f"https://example.com/{self.KEY}.txt")] = R(200, self.KEY.encode() + b"\n", content_type="text/plain")
        t.update(extra)
        return t

    def go(self, table, ledger=None, **kw):
        h, fake = http(table)
        new, record = seo.indexnow(CANON, [SITEMAP], self.KEY, ledger, h, "abc123", "2026-11-20T00:00:00Z", **kw)
        return new, record, [json.loads(b) for (k, _), b in zip(fake.calls, fake.bodies) if k == self.POST]

    def baseline(self, table=None):
        new, record, posts = self.go(table or self.site())
        self.assertEqual(posts, [])
        return new

    def test_the_hosted_key_is_verified_first(self):
        t = self.site()
        t[("GET", f"https://example.com/{self.KEY}.txt")] = R(404)
        new, record, posts = self.go(t)
        self.assertEqual((posts, record["submitted"]), ([], 0))
        self.assertIn("without the key", record["notes"][0])
        self.assertEqual(new["log"][-1]["sha"], "abc123")

    def test_unreadable_robots_submits_nothing(self):
        for status in (503, 200):
            t = self.site()
            t[("GET", "https://example.com/robots.txt")] = R(status, HOME if status == 200 else b"")
            _, record, posts = self.go(t, {"version": 1, "host": "example.com", "urls": {}, "log": []})
            self.assertEqual(posts, [])
            self.assertIn("nothing is submitted", record["notes"][0])

    def test_a_first_run_records_a_baseline_and_submits_nothing(self):
        new = self.baseline()
        self.assertEqual(sorted(new["urls"]), ["https://example.com/", "https://example.com/a"])
        self.assertTrue(all(e["accepted"] == e["fingerprint"] for e in new["urls"].values()))
        self.assertIn("baseline", new["log"][-1]["notes"][0])
        t = self.site()
        t[("GET", "https://example.com/robots.txt")] = R(404)  # no robots.txt: everything is allowed
        self.assertEqual(len(self.baseline(t)["urls"]), 2)

    def test_only_changed_urls_are_submitted_once(self):
        ledger = self.baseline()
        t = self.site()
        t[("GET", "https://example.com/a")] = R(200, page("https://example.com/a", b"<meta name='description' content='new'>").replace(b"word", b"text"), content_type="text/html")
        t[self.POST] = R(202)
        new, record, posts = self.go(t, ledger)
        self.assertEqual(posts, [{"host": "example.com", "key": self.KEY, "keyLocation": f"https://example.com/{self.KEY}.txt",
                                  "urlList": ["https://example.com/a"]}])
        self.assertEqual((record["submitted"], record["accepted"], record["status"]), (1, 1, [202]))
        self.assertEqual(new["urls"]["https://example.com/a"]["accepted_at"], "2026-11-20T00:00:00Z")
        again, record2, posts2 = self.go(t, new)
        self.assertEqual((posts2, record2["submitted"]), ([], 0))  # unchanged: never resubmitted
        self.assertIn("no eligible URL changed", record2["notes"][0])

    def test_markup_only_rebuilds_are_not_changes(self):
        ledger = self.baseline()
        t = self.site()
        t[("GET", "https://example.com/a")] = R(200, page("https://example.com/a").replace(b"<h1>", b"<script src='/x.9f3a.js'></script><h1 class='new'>"), content_type="text/html")
        _, record, posts = self.go(t, ledger)
        self.assertEqual(posts, [])

    def test_ineligible_urls_are_never_submitted(self):
        urls = ["https://example.com/", "https://other.example/x", "http://example.com/plain", "https://example.com/#frag",
                "https://example.com/private", "https://example.com/gone", "https://example.com/hidden",
                "https://example.com/scoped", "https://example.com/elsewhere", "https://example.com/googlebot-only"]
        urlset = (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                  + b"".join(b"<url><loc>" + u.encode() + b"</loc></url>" for u in urls) + b"</urlset>")
        t = self.site(**{})
        t[("GET", SITEMAP)] = R(200, urlset, content_type="application/xml")
        t[("GET", "https://example.com/robots.txt")] = R(200, ROBOTS + b"User-agent: bingbot\nDisallow: /private\n", content_type="text/plain")
        t[("GET", "https://example.com/gone")] = R(404)
        t[("GET", "https://example.com/hidden")] = R(200, page("https://example.com/hidden", b'<meta name="robots" content="noindex">'))
        t[("GET", "https://example.com/scoped")] = R(200, page("https://example.com/scoped"), x_robots_tag="bingbot: noindex")
        t[("GET", "https://example.com/elsewhere")] = R(200, page("https://example.com/"))
        t[("GET", "https://example.com/googlebot-only")] = R(200, page("https://example.com/googlebot-only"), x_robots_tag="googlebot: noindex")
        new = self.baseline(t)
        self.assertEqual(sorted(new["urls"]), ["https://example.com/", "https://example.com/googlebot-only"])

    def test_refusals_are_recorded_retried_and_capped(self):
        ledger = self.baseline()
        t = self.site()
        t[("GET", "https://example.com/a")] = R(200, page("https://example.com/a").replace(b"word", b"changed"), content_type="text/html")
        t[self.POST] = R(422)
        state = ledger
        for n in range(seo.INDEXNOW_ATTEMPTS):
            state, record, posts = self.go(t, state)
            self.assertEqual((len(posts), record["failed"], record["status"]), (1, 1, [422]))
        self.assertEqual(state["urls"]["https://example.com/a"]["attempts"], seo.INDEXNOW_ATTEMPTS)
        state, record, posts = self.go(t, state)
        self.assertEqual(posts, [])  # capped until the page changes again
        t[("GET", "https://example.com/a")] = R(200, page("https://example.com/a").replace(b"word", b"again"), content_type="text/html")
        t[self.POST] = [seo.Response(200, {}, b"")]
        state, record, posts = self.go(t, state)
        self.assertEqual((len(posts), record["accepted"]), (1, 1))
        del t[self.POST]  # the endpoint unreachable: recorded as a refusal, never a crash
        t[("GET", "https://example.com/a")] = R(200, page("https://example.com/a").replace(b"word", b"third"), content_type="text/html")
        _, record, _ = self.go(t, state)
        self.assertEqual((record["status"], record["failed"]), ([0], 1))

    def test_batches_page_limits_dry_runs_and_dropped_urls(self):
        ledger = self.baseline()
        ledger["urls"]["https://example.com/old"] = {"fingerprint": "x", "accepted": "x", "attempts": 0}
        for e in ledger["urls"].values():
            e["accepted"] = e["fingerprint"] = "stale"
        t = self.site()
        t[self.POST] = R(200)
        old_batch, old_pages = seo.INDEXNOW_BATCH, seo.INDEXNOW_PAGES
        self.addCleanup(setattr, seo, "INDEXNOW_BATCH", old_batch)
        self.addCleanup(setattr, seo, "INDEXNOW_PAGES", old_pages)
        seo.INDEXNOW_BATCH = 1
        new, record, posts = self.go(t, json.loads(json.dumps(ledger)))
        self.assertEqual((len(posts), record["accepted"]), (2, 2))
        self.assertNotIn("https://example.com/old", new["urls"])
        _, dry, posts = self.go(t, json.loads(json.dumps(ledger)), dry_run=True)
        self.assertEqual((posts, len(dry["would_submit"])), ([], 2))
        seo.INDEXNOW_PAGES = 1
        _, limited, _ = self.go(t, json.loads(json.dumps(ledger)))
        self.assertIn("only the first 1", " ".join(limited["notes"]))

    def test_a_foreign_or_old_ledger_is_a_new_baseline(self):
        for ledger in ({"version": 1, "host": "other.example", "urls": {}, "log": []}, {"version": 0}):
            _, record, posts = self.go(self.site(), ledger)
            self.assertEqual(posts, [])
            self.assertIn("baseline", record["notes"][0])
        log = {"version": 1, "host": "example.com", "urls": {}, "log": [{"n": i} for i in range(seo.LEDGER_LOG)]}
        new, _, _ = self.go(self.site(), log)
        self.assertEqual(len(new["log"]), seo.LEDGER_LOG)

    def test_retry_after_is_honoured(self):
        slept = []
        fake = Fake({("GET", "https://example.com/x"): [R(429, retry_after="7"), R(503, retry_after="soon"), R(200)]})
        h = seo.Http(send=fake, sleep=slept.append)
        self.assertEqual(h.get("https://example.com/x").status, 200)
        self.assertEqual(slept, [7.0, 4])

    def test_fingerprint_is_what_a_reader_sees(self):
        a = seo.fingerprint(seo.Page(page().decode()))
        self.assertEqual(a, seo.fingerprint(seo.Page(page().replace(b"<h1>", b"<script>v2()</script><h1>").decode())))
        self.assertNotEqual(a, seo.fingerprint(seo.Page(page().replace(b"word", b"other").decode())))

    def test_ledger_artifacts(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("ledger.json", json.dumps({"version": 1}))
        api = "https://api.github.com/repos/o/r/actions/artifacts?name=seo-standard-indexnow-ledger&per_page=20"
        listing = json.dumps({"artifacts": [
            {"expired": True, "created_at": "2026-11-21", "archive_download_url": "https://api.github.com/zip/expired"},
            {"expired": False, "created_at": "2026-11-19", "archive_download_url": "https://api.github.com/zip/old"},
            {"expired": False, "created_at": "2026-11-20", "archive_download_url": "https://api.github.com/zip/new"}]}).encode()
        t = {("GET", api): R(200, listing), ("GET", "https://api.github.com/zip/new"): R(302, location="https://blob.example/z"),
             ("GET", "https://blob.example/z"): R(200, buf.getvalue())}
        h, fake = http(t)
        self.assertEqual(seo.fetch_ledger(h, "o/r", "tok"), {"version": 1})
        self.assertEqual(dict(fake.calls[0][1])["Authorization"], "Bearer tok")
        for table in ({("GET", api): R(403)}, {("GET", api): R(200, b'{"artifacts": []}')},
                      {("GET", api): R(200, listing), ("GET", "https://api.github.com/zip/new"): R(410)},
                      {("GET", api): R(200, listing), ("GET", "https://api.github.com/zip/new"): R(200, b"not a zip")}):
            self.assertIsNone(seo.fetch_ledger(http(table)[0], "o/r", "tok"))

    def test_cli(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        out, md, prev = os.path.join(d, "l.json"), os.path.join(d, "s.md"), os.path.join(d, "p.json")
        base = ["indexnow", "--canonical", "https://example.com", "--sitemap", SITEMAP, "--key", self.KEY, "--ledger-out", out]
        with redirect_stderr(io.StringIO()):
            self.assertEqual(seo.main(["indexnow", "--canonical", "http://x", "--key", "k", "--ledger-out", out]), 2)
        with redirect_stdout(io.StringIO()) as o:
            self.assertEqual(seo.main(base, http(self.site())[0]), 0)
        self.assertIn("baseline", o.getvalue())
        self.assertIn("not proof of crawling or indexing", o.getvalue())
        with open(prev, "w") as f:
            f.write("not json")
        self.assertEqual(seo.main(base + ["--ledger-in", prev, "--md", md], http(self.site())[0]), 0)
        shutil.copy(out, prev)
        t = self.site()
        t[self.POST] = R(202)
        t[("GET", "https://example.com/a")] = R(200, page("https://example.com/a").replace(b"word", b"new"), content_type="text/html")
        self.assertEqual(seo.main(base + ["--ledger-in", prev, "--md", md, "--sha", "s"], http(t)[0]), 0)
        with open(md) as f:
            self.assertIn("1 changed URLs submitted, 1 accepted", f.read())
        api = "https://api.github.com/repos/o/r/actions/artifacts?name=seo-standard-indexnow-ledger&per_page=20"
        t2 = self.site()
        t2[("GET", api)] = R(404)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(seo.main(base + ["--repo", "o/r"], http(t2)[0], env={"GITHUB_TOKEN": "t"}), 0)
        t3 = self.site()
        del t3[("GET", f"https://example.com/{self.KEY}.txt")]
        with redirect_stderr(io.StringIO()):
            self.assertEqual(seo.main(base, http(t3)[0]), 2)


class PreviewResolution(unittest.TestCase):
    PAGES = {"id": 1, "started_at": "2026-11-20T10:00:00Z", "conclusion": "success", "status": "completed",
             "output": {"summary": "<tr><td><strong>Preview URL:</strong></td><td>\n<a href='https://a078b749.site.pages.dev'>x</a>"}}
    WORKERS = {"id": 2, "started_at": "2026-11-20T10:00:00Z", "conclusion": "success", "status": "completed",
               "output": {"summary": "Build ID"},
               "details_url": "https://dash.cloudflare.com/acc/workers/services/view/peeps-web/production/previews/seo-sitemap/builds/7a"}
    URL = ("https://api.github.com/repos/o/r/commits/abc/check-runs?"
           "check_name=Workers+Builds%3A+peeps-web&filter=all&per_page=100")

    def test_urls_from_pages_and_workers_builds(self):
        self.assertEqual(seo.preview_url(self.PAGES), "https://a078b749.site.pages.dev")
        self.assertEqual(seo.preview_url(self.WORKERS, "glencoe-delivery"),
                         "https://seo-sitemap-peeps-web.glencoe-delivery.workers.dev")
        self.assertIsNone(seo.preview_url(self.WORKERS))
        self.assertIsNone(seo.preview_url({"output": None}))

    def api(self, *bodies, **headers):
        return http({("GET", self.URL): [R(200, json.dumps(b).encode(), **headers) for b in bodies]})

    def wait(self, h, timeout=60, clock=None):
        return seo.wait_for_check(h, "o/r", "abc", "Workers Builds: peeps-web", "tok", timeout, 1,
                                  clock or iter(range(100)).__next__, lambda s: None)

    def test_waits_until_the_newest_run_completes(self):
        h, fake = self.api({"check_runs": []}, {"check_runs": [{"status": "in_progress", "started_at": "1"}]},
                           {"check_runs": [self.WORKERS]})
        self.assertEqual(self.wait(h), self.WORKERS)
        self.assertEqual(fake.calls[0][1]["Authorization"], "Bearer tok")

    def test_a_rerun_supersedes_an_older_result(self):
        old_fail = {"id": 1, "started_at": "2026-11-20T09:00:00Z", "status": "completed", "conclusion": "failure"}
        h, _ = self.api({"check_runs": [old_fail, self.WORKERS]})
        self.assertEqual(self.wait(h), self.WORKERS, "a newer successful re-run wins over an older failure")
        old_ok = dict(self.WORKERS, started_at="2026-11-20T09:00:00Z")
        running = {"id": 3, "started_at": "2026-11-20T11:00:00Z", "status": "in_progress"}
        h, _ = self.api({"check_runs": [old_ok, running]},
                        {"check_runs": [old_ok, dict(running, status="completed", conclusion="success")]})
        self.assertEqual(self.wait(h)["id"], 3, "a newer running deploy is waited for, not the older success")

    def test_every_page_of_the_listing_is_read(self):
        page2 = self.URL + "&page=2"
        h, _ = http({("GET", self.URL): R(200, json.dumps({"check_runs": [dict(self.WORKERS, started_at="1")]}).encode(),
                                          link=f'<{page2}>; rel="next"'),
                     ("GET", page2): R(200, json.dumps({"check_runs": [dict(self.PAGES, started_at="2")]}).encode())})
        self.assertEqual(self.wait(h)["id"], 1)

    def test_failure_timeout_and_api_errors(self):
        h, _ = self.api({"check_runs": [{"status": "completed", "conclusion": "failure"}]})
        with self.assertRaisesRegex(RuntimeError, "concluded failure"):
            self.wait(h)
        h, _ = self.api({"check_runs": []})
        with self.assertRaisesRegex(RuntimeError, "did not complete"):
            self.wait(h, 15, iter([0, 10, 20]).__next__)
        h, _ = http({})
        h.send = lambda req, timeout: R(403)
        with self.assertRaisesRegex(RuntimeError, "answered 403"):
            self.wait(h)
        h, _ = http({})
        with self.assertRaisesRegex(RuntimeError, "reading check runs failed"):
            self.wait(h)


class Cli(unittest.TestCase):
    def main(self, argv, table=None, env=None):
        out, err = io.StringIO(), io.StringIO()
        h, _ = http(table or {})
        with redirect_stdout(out), redirect_stderr(err):
            code = seo.main(argv, http=h, env=env or {})
        return code, out.getvalue(), err.getvalue()

    def test_audit_exit_codes_reports_and_annotations(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        code, out, _ = self.main(["audit", "--canonical", "https://example.com", "--sitemap", SITEMAP,
                                  "--json", f"{d}/r.json", "--md", f"{d}/r.md", "--github"], live())
        self.assertEqual((code, out), (0, ""))
        with open(f"{d}/r.json") as f:
            self.assertEqual(json.load(f)["facts"]["mode"], "production")
        t = live()
        t[("GET", SITEMAP)] = R(404)
        code, out, _ = self.main(["audit", "--canonical", CANON, "--sitemap", SITEMAP, "--github"], t)
        self.assertEqual(code, 1)
        self.assertIn("::error title=SEO sitemap-unreachable::", out)
        self.assertIn("1 critical", out)
        code, out, _ = self.main(["audit", "--canonical", CANON, "--sitemap", SITEMAP], t)
        self.assertNotIn("::error", out)

    def test_an_unreachable_site_exits_2(self):
        code, out, err = self.main(["audit", "--canonical", CANON, "--sitemap", SITEMAP, "--github"], {})
        self.assertEqual(code, 2)
        self.assertIn("Not audited", out)
        self.assertIn("::error title=SEO not audited::", out)
        self.assertIn("unreachable", err)

    def test_audit_fails_closed_on_bad_arguments(self):
        for argv in (["--canonical", "http://example.com/", "--sitemap", SITEMAP],
                     ["--canonical", CANON],
                     ["--canonical", CANON, "--sitemap", SITEMAP, "--mode", "preview"],
                     ["--canonical", CANON, "--sitemap", SITEMAP, "--mode", "artifact", "--dist", "/nonexistent"]):
            with self.subTest(argv):
                self.assertEqual(self.main(["audit", *argv])[0], 2)

    def test_preview_command(self):
        url = "https://api.github.com/repos/o/r/commits/abc/check-runs?check_name=Cloudflare+Pages&filter=all&per_page=100"
        ok = {("GET", url): R(200, json.dumps({"check_runs": [PreviewResolution.PAGES]}).encode())}
        base = ["preview", "--repo", "o/r", "--sha", "abc", "--check", "Cloudflare Pages"]
        self.assertEqual(self.main(base, ok)[:2], (0, "https://a078b749.site.pages.dev\n"))
        self.assertEqual(self.main(base + ["--wait-only"], ok)[:2], (0, ""))
        nourl = {("GET", url): R(200, json.dumps({"check_runs": [{"status": "completed", "conclusion": "success"}]}).encode())}
        self.assertEqual(self.main(base, nourl)[0], 2)
        bad = {("GET", url): R(200, json.dumps({"check_runs": [{"status": "completed", "conclusion": "failure"}]}).encode())}
        code, _, err = self.main(base, bad)
        self.assertEqual(code, 2)
        self.assertIn("concluded failure", err)


if __name__ == "__main__":
    unittest.main()

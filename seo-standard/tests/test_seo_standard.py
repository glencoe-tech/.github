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
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import seo_standard as seo  # noqa: E402

CANON = "https://example.com/"
SITEMAP = "https://example.com/sitemap.xml"


def page(canonical="https://example.com/", extra=b""):
    return (b'<!doctype html><html><head><title>Example</title>'
            b'<link rel="canonical" href="' + canonical.encode() + b'">'
            b'<meta name="description" content="d"><meta property="og:title" content="t">'
            b'<meta property="og:description" content="d"><meta property="og:image" content="i">'
            b'<meta name="viewport" content="width=device-width">' + extra +
            b'<script type="application/ld+json">{"@type": "Organization"}</script></head></html>')


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
    }


class Fake:
    def __init__(self, table):
        self.table, self.calls = dict(table), []

    def __call__(self, req, timeout):
        key = (req.get_method(), req.full_url)
        self.calls.append((key, dict(req.header_items())))
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

    def test_a_canonical_resolves_against_the_documents_base(self):
        t = live()
        t[("GET", "https://example.com/a")] = R(200, page("a", b'<base href="/">'))
        self.assertEqual(loud(run(t)), [])

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
        for status in (401, 403):
            t[("GET", "https://example.com/robots.txt")] = R(status)
            self.assertEqual((codes(run(t), "critical"), "robots-missing" in codes(run(t), "warn")), (set(), True),
                             f"{status}: crawlers treat a 4xx robots.txt as allow-all")
        for status in (429, 503):
            t[("GET", "https://example.com/robots.txt")] = R(status)
            self.assertIn("robots-unreadable", codes(run(t), "critical"))
        t[("GET", "https://example.com/robots.txt")] = R(200, b"<!doctype html><html>", content_type="text/html")
        self.assertIn("robots-is-html", codes(run(t), "critical"))

    def test_robots_redirects_are_followed(self):
        t = live()
        t[("GET", "https://example.com/robots.txt")] = R(301, location="https://example.com/r/robots.txt")
        t[("GET", "https://example.com/r/robots.txt")] = R(200, ROBOTS)
        self.assertEqual(loud(run(t)), [])
        t[("GET", "https://example.com/robots.txt")] = R(301, location="/robots.txt")
        self.assertIn("robots-unreadable", codes(run(t), "critical"), "a redirect loop never resolves")

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
        t[("GET", "https://example.com/robots.txt")] = R(200, b"User-agent: bingbot\nDisallow: /a\n\n" + ROBOTS)
        self.assertEqual([f["detail"] for f in run(t)["findings"] if f["code"] == "sitemap-url-disallowed"],
                         ["https://example.com/a is listed but robots.txt disallows it for bingbot"])
        t[("GET", "https://example.com/robots.txt")] = R(200, b"User-agent: *\nDisallow: /\n\nUser-agent: googlebot\n"
                                                         b"Disallow:\n\nUser-agent: bingbot\nDisallow:\n")
        self.assertNotIn("robots-blocks-search", codes(run(t)), "a crawler's own empty group overrides *")
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
        self.assertTrue(seo.allowed("User-agent: *\nDisallow: /a\nAllow: /a?ok=1\n", "googlebot", "/a?ok=1"),
                        "the query is part of what is matched")
        self.assertFalse(seo.allowed("User-agent: *\nDisallow: /a\n", "googlebot", "/%61"), "%61 is a")
        self.assertFalse(seo.allowed("User-agent: *\nDisallow: /%7Ex\n", "googlebot", "/~x"))
        self.assertTrue(seo.allowed("User-agent: *\nDisallow: /a/b\n", "googlebot", "/a%2Fb"), "%2F stays reserved")
        self.assertEqual(seo.robots_sitemaps("sitemap: https://x/a.xml\n"), ["https://x/a.xml"])

    def test_page_and_helpers(self):
        self.assertTrue(seo.noindex("noindex nofollow"))
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
        queued = {"id": 4, "started_at": None, "status": "queued"}
        h, _ = self.api({"check_runs": [old_ok, queued]},
                        {"check_runs": [old_ok, dict(queued, status="completed", conclusion="failure")]})
        with self.assertRaisesRegex(RuntimeError, "concluded failure"):
            self.wait(h)

    def test_requests_never_outlast_the_deadline(self):
        h, _ = self.api({"check_runs": [self.WORKERS]})
        h.timeout = 20
        seo.wait_for_check(h, "o/r", "abc", "Workers Builds: peeps-web", "t", 5, 1, iter([0, 1, 2]).__next__, lambda s: None)
        self.assertEqual(h.timeout, 4)

    def test_every_page_of_the_listing_is_read(self):
        page2 = self.URL + "&page=2"
        h, _ = http({("GET", self.URL): R(200, json.dumps({"check_runs": [dict(self.WORKERS, started_at="1")]}).encode(),
                                          link=f'<{page2}>; rel="next"'),
                     ("GET", page2): R(200, json.dumps({"check_runs": [dict(self.PAGES, started_at="2")]}).encode())})
        self.assertEqual(self.wait(h)["id"], 2, "the highest id across every page")

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

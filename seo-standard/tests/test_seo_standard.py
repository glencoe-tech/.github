"""seo-standard: every rule is exercised failing as well as passing, in each mode it applies to.
Run: python3 -I -m unittest discover -s seo-standard/tests -v
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import seo_standard as seo  # noqa: E402

CANON = "https://example.com/"
SITEMAP = "https://example.com/sitemap.xml"
HOME = (b'<!doctype html><html><head><title>Example</title>'
        b'<link rel="canonical" href="https://example.com/">'
        b'<meta name="description" content="d"><meta property="og:title" content="t">'
        b'<meta property="og:description" content="d"><meta property="og:image" content="i">'
        b'<meta name="viewport" content="width=device-width">'
        b'<script type="application/ld+json">{"@type": "Organization"}</script></head></html>')
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
        ("GET", f"{origin}/a"): R(200, HOME, content_type="text/html"),
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

    def test_unreachable_is_a_finding(self):
        self.assertEqual(codes(run({})), {"unreachable"})

    def test_homepage_status_and_bot_challenge(self):
        t = live()
        t[("GET", "https://example.com/")] = R(403, cf_mitigated="challenge")
        self.assertEqual(codes(run(t)) & {"homepage-status", "bot-challenge"}, {"homepage-status", "bot-challenge"})
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

    def test_a_relative_canonical_resolves_against_the_production_url(self):
        t = live()
        t[("GET", "https://example.com/")] = R(200, HOME.replace(b'href="https://example.com/"', b'href="/"'))
        self.assertNotIn("canonical-mismatch", codes(run(t)))

    def test_robots(self):
        for body, status, ctype, code in ((b"", 404, "text/plain", "robots-missing"),
                                          (b"<!doctype html>", 200, "text/html", "robots-is-html"),
                                          (b"User-agent: *\nDisallow: /\n", 200, "text/plain", "robots-blocks-search")):
            t = live()
            t[("GET", "https://example.com/robots.txt")] = R(status, body, content_type=ctype)
            with self.subTest(code):
                self.assertIn(code, codes(run(t), "critical"))
        t = live()
        t[("GET", "https://example.com/robots.txt")] = R(200, b"User-agent: *\nAllow: /\n")
        self.assertIn("robots-no-sitemap", codes(run(t), "warn"))
        t[("GET", "https://example.com/robots.txt")] = R(200, b"User-agent: *\nSitemap: https://example.com/o.xml\n")
        self.assertIn("sitemap-not-in-robots", codes(run(t), "warn"))

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
                           (R(200, b"<html>app</html>", content_type="text/html"), "sitemap-is-html"),
                           (R(200, b"<urlset", content_type="application/xml"), "sitemap-invalid-xml"),
                           (R(200, b"<rss/>", content_type="application/xml"), "sitemap-not-a-sitemap")):
            t = live()
            t[("GET", SITEMAP)] = resp
            with self.subTest(code):
                self.assertIn(code, codes(run(t), "critical"))
        t = live()
        t[("GET", SITEMAP)] = R(200, b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>')
        self.assertIn("sitemap-empty", codes(run(t), "warn"))

    def test_index_children_and_sampled_urls(self):
        index = (b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                 b'<sitemap><loc>https://example.com/s1.xml</loc></sitemap>'
                 b'<sitemap><loc>https://example.com/nested.xml</loc></sitemap></sitemapindex>')
        s1 = (b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
              b'<url><loc>https://example.com/ok</loc></url><url><loc>https://example.com/gone</loc></url>'
              b'<url><loc>https://example.com/hidden</loc></url><url><loc>http://cdn.example.com/x</loc></url></urlset>')
        t = live()
        t.update({("GET", SITEMAP): R(200, index), ("GET", "https://example.com/s1.xml"): R(200, s1),
                  ("GET", "https://example.com/nested.xml"): R(200, index),
                  ("GET", "https://example.com/ok"): R(200, HOME),
                  ("GET", "https://example.com/gone"): R(307, location="/gone/"),
                  ("GET", "https://example.com/hidden"): R(200, HOME, x_robots_tag="noindex")})
        result = run(t)
        self.assertLessEqual({"sitemap-off-host", "sitemap-url-status", "sitemap-url-noindex"}, codes(result, "critical"))
        self.assertIn("sitemap-nested-index", codes(result, "warn"))

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
        t[("GET", f"{self.BASE}/")] = R(200, HOME.replace(b"https://example.com/", f"{self.BASE}/".encode()))
        t[("GET", f"{self.BASE}/sitemap.xml")] = R(200, b"<!doctype html><html>", content_type="text/html")
        t[("GET", f"{self.BASE}/robots.txt")] = R(200, b"User-agent: *\nDisallow: /\n")
        result = run(t, "preview", base=self.BASE)
        self.assertEqual(codes(result, "critical"), {"canonical-mismatch", "sitemap-is-html"})
        self.assertIn("robots-blocks-search", codes(result, "info"))

    def test_preview_requests_are_browser_navigations_and_production_ones_are_not(self):
        h, fake = http(self.table())
        seo.audit(CANON, [SITEMAP], [], "preview", h, base=self.BASE)
        self.assertTrue(all(c[1].get("Sec-fetch-mode") == "navigate" for c in fake.calls))
        h, fake = http(live())
        seo.audit(CANON, [SITEMAP], [], "production", h)
        self.assertFalse(any("Sec-fetch-mode" in c[1] for c in fake.calls))

    def test_preview_sitemap_urls_are_fetched_from_the_preview(self):
        t = self.table()
        del t[("GET", f"{self.BASE}/a")]
        self.assertIn("unreachable", codes(run(t, "preview", base=self.BASE), "critical"))
        t[("GET", f"{self.BASE}/a")] = R(404)
        self.assertIn("sitemap-url-status", codes(run(t, "preview", base=self.BASE), "critical"))


class Artifact(unittest.TestCase):
    def build(self, files):
        d = tempfile.mkdtemp()
        for name, body in files.items():
            p = os.path.join(d, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(body)
        self.addCleanup(lambda: __import__("shutil").rmtree(d))
        return d

    def test_a_good_static_build(self):
        d = self.build({"index.html": HOME, "robots.txt": ROBOTS, "sitemap.xml": URLSET, "a/index.html": HOME})
        self.assertEqual(loud(seo.audit(CANON, [SITEMAP], [], "artifact", dist=d)), [])

    def test_missing_files_and_runtime_routes(self):
        d = self.build({"index.html": HOME, "a.html": HOME})
        result = seo.audit(CANON, [SITEMAP], [], "artifact", dist=d)
        self.assertIn("robots-missing", codes(result, "warn"))
        self.assertIn("sitemap-unreachable", codes(result, "critical"))
        d = self.build({"index.html": HOME, "robots.txt": ROBOTS,
                        "sitemap.xml": URLSET.replace(b"/a<", b"/runtime<")})
        self.assertIn("sitemap-url-not-static", codes(seo.audit(CANON, [SITEMAP], [], "artifact", dist=d), "info"))
        d = self.build({"index.html": HOME, "robots.txt": ROBOTS, "sitemap.xml": URLSET, "a.html": HOME})
        self.assertEqual(loud(seo.audit(CANON, [SITEMAP], [], "artifact", dist=d)), [])

    def test_no_homepage(self):
        d = self.build({"robots.txt": ROBOTS})
        self.assertIn("homepage-status", codes(seo.audit(CANON, [SITEMAP], [], "artifact", dist=d)))


class Parsing(unittest.TestCase):
    def test_robots_groups_and_blocking(self):
        self.assertTrue(seo.blocked("User-agent: *\nDisallow: /\n", "googlebot"))
        self.assertFalse(seo.blocked("User-agent: *\nDisallow: /\n\nUser-agent: Googlebot\nAllow: /\n", "googlebot"))
        self.assertFalse(seo.blocked("User-agent: *\nDisallow: /\nAllow: /public\n", "bingbot"))
        self.assertFalse(seo.blocked("User-agent: *\nDisallow:\n", "googlebot"))
        self.assertFalse(seo.blocked("junk\nSitemap: https://x/s.xml", "googlebot"))
        self.assertTrue(seo.blocked("User-agent: Googlebot # g\nUser-agent: Bingbot\nDisallow: /\n", "bingbot"))
        self.assertEqual(seo.robots_sitemaps("sitemap: https://x/a.xml\n"), ["https://x/a.xml"])

    def test_page_and_helpers(self):
        p = seo.Page('<meta name="googlebot" content="NONE"><link rel="alternate" href="x"><meta content="x">')
        self.assertTrue(seo.noindex(p.robots))
        self.assertEqual(p.canonical, "")
        self.assertFalse(seo.noindex("index, follow"))
        self.assertEqual(seo.norm("https://Example.com"), "https://example.com/")
        self.assertEqual(R(200, X_A="1").header("X-A"), "1")
        self.assertIsNone(seo._NoRedirect().redirect_request(None, None, 301, "", {}, "https://x/"))

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
    PAGES = {"conclusion": "success", "status": "completed", "output": {"summary":
             "<tr><td><strong>Preview URL:</strong></td><td>\n<a href='https://a078b749.site.pages.dev'>x</a>"}}
    WORKERS = {"conclusion": "success", "status": "completed", "output": {"summary": "Build ID"},
               "details_url": "https://dash.cloudflare.com/acc/workers/services/view/peeps-web/production/previews/seo-sitemap/builds/7a"}

    def test_urls_from_pages_and_workers_builds(self):
        self.assertEqual(seo.preview_url(self.PAGES), "https://a078b749.site.pages.dev")
        self.assertEqual(seo.preview_url(self.WORKERS, "glencoe-delivery"),
                         "https://seo-sitemap-peeps-web.glencoe-delivery.workers.dev")
        self.assertIsNone(seo.preview_url(self.WORKERS))
        self.assertIsNone(seo.preview_url({"output": None}))

    def api(self, *bodies):
        url = ("https://api.github.com/repos/o/r/commits/abc/check-runs?check_name=Workers+Builds%3A+peeps-web")
        return http({("GET", url): [R(200, json.dumps(b).encode()) for b in bodies]})

    def test_waits_until_the_check_completes(self):
        h, fake = self.api({"check_runs": []}, {"check_runs": [{"status": "in_progress"}]},
                           {"check_runs": [self.WORKERS]})
        clock = iter(range(100)).__next__
        run = seo.wait_for_check(h, "o/r", "abc", "Workers Builds: peeps-web", "tok", 60, 1, clock, lambda s: None)
        self.assertEqual(run, self.WORKERS)
        self.assertEqual(fake.calls[0][1]["Authorization"], "Bearer tok")

    def test_failure_timeout_and_api_errors(self):
        h, _ = self.api({"check_runs": [{"status": "completed", "conclusion": "failure"}]})
        with self.assertRaisesRegex(RuntimeError, "concluded failure"):
            seo.wait_for_check(h, "o/r", "abc", "Workers Builds: peeps-web", "t", sleep=lambda s: None)
        h, _ = self.api({"check_runs": []})
        clock = iter([0, 10, 20]).__next__
        with self.assertRaisesRegex(RuntimeError, "did not complete"):
            seo.wait_for_check(h, "o/r", "abc", "Workers Builds: peeps-web", "t", 15, 1, clock, lambda s: None)
        h, _ = http({})
        h.send = lambda req, timeout: R(403)
        with self.assertRaisesRegex(RuntimeError, "answered 403"):
            seo.wait_for_check(h, "o/r", "abc", "x", "t")


class Cli(unittest.TestCase):
    def main(self, argv, table=None, env=None):
        out, err = io.StringIO(), io.StringIO()
        h, _ = http(table or {})
        with redirect_stdout(out), redirect_stderr(err):
            code = seo.main(argv, http=h, env=env or {})
        return code, out.getvalue(), err.getvalue()

    def test_audit_exit_codes_reports_and_annotations(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d))
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

    def test_audit_fails_closed_on_bad_arguments(self):
        for argv in (["--canonical", "http://example.com/", "--sitemap", SITEMAP],
                     ["--canonical", CANON],
                     ["--canonical", CANON, "--sitemap", SITEMAP, "--mode", "preview"],
                     ["--canonical", CANON, "--sitemap", SITEMAP, "--mode", "artifact", "--dist", "/nonexistent"]):
            with self.subTest(argv):
                self.assertEqual(self.main(["audit", *argv])[0], 2)

    def test_preview_command(self):
        url = "https://api.github.com/repos/o/r/commits/abc/check-runs?check_name=Cloudflare+Pages"
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

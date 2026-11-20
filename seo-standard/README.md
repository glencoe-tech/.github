# seo-standard

The check behind the Glencoe [Website SEO Standard](https://github.com/glencoe-tech/governance/blob/main/docs/WEBSITE-SEO-STANDARD.md).
It uses one engine for every framework, because it reads what a crawler reads: HTTP responses,
`robots.txt`, sitemaps and the HTML head. It never reads a framework's source.

| Event | What it checks |
|---|---|
| Pull request | Waits for the deploy check to build the preview, then audits **the preview as the production site it will become**. Canonicals and sitemap URLs must name the production host. A `noindex` preview is expected and passes |
| Push to `main` | Waits for the deploy, then audits **the live site** |
| `mode: artifact` | For a site with no preview: audits a static build directory |

A run fails only on **critical** findings, the ones that would get the site indexed wrongly or
not at all. Warnings are annotations. Traffic and indexing delays are never checked here; Search
Console monitoring watches those, in glencoe-infra.

## Adopt it (public production websites only)

Add `.github/workflows/seo.yml`:

```yaml
name: seo
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
  checks: read          # to see the deploy check; nothing else
jobs:
  seo:
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    steps:
      - uses: glencoe-tech/.github/seo-standard@<full commit sha>
        with:
          canonical: https://example.com/
          sitemaps: https://example.com/sitemap.xml
          deploy-check: "Workers Builds: my-worker"   # or "Cloudflare Pages"
          workers-subdomain: glencoe-delivery         # Workers Builds only: the account's workers.dev subdomain
          # private-hosts: app.example.com admin.example.com
```

There is nothing to install. The action runs standard-library Python on the runner's own
`python3`.

Do not adopt it for apps, APIs, admin or login surfaces, or staging. Those follow the
non-indexable policy in the standard: `noindex`, no sitemap, and no Search Console property of
their own.

## Severity

**Critical (the run fails):**
- the homepage is not 200
- `noindex` in production. Meta robots, or an `X-Robots-Tag` that applies to Googlebot or
  Bingbot; a tag scoped to another crawler, e.g. `googlebot-news:`, does not count
- `robots.txt` disallows the homepage for Googlebot or Bingbot, or answers 429 or 5xx, which
  Google treats as disallow-all.
  - Matching follows RFC 9309: a crawler's own group, even an empty one, replaces `*`;
    wildcards and `$` are honoured; the longest rule wins and Allow wins a tie; queries are kept;
    percent-encoding is normalised.
  - Redirects are followed for up to 5 hops.
- `robots.txt` or a sitemap whose body is an HTML page (the SPA fallback)
- a sitemap that is missing, invalid XML, not a sitemap, or lists URLs on another host
- a sampled sitemap URL (up to 20 in production and artifact mode, 5 in preview, spread evenly
  across the whole sitemap with the first and last always included) that is:
  - not a 200 (redirects count)
  - `noindex`
  - disallowed by robots.txt for Googlebot or Bingbot
  - declaring another page as its canonical
- the homepage's `rel=canonical` pointing anywhere but the production URL
- `http://` or the other `www` form not ending at the canonical URL
- a declared private host answering 200 without `noindex`

**Warning (an annotation, never a failure):**
- missing title, description, Open Graph tags, rel=canonical or meta viewport
- invalid JSON-LD
- a temporary (non-301/308) host redirect
- `robots.txt` answering another 4xx (404, 410, 401, 403): crawlers treat that as allow-all,
  but it should exist and name the sitemap
- valid `robots.txt` or sitemap XML served with an HTML content type
- robots naming no sitemap, or not naming the declared one
- an AI search crawler blocked (`OAI-SearchBot`, `PerplexityBot`, `Claude-SearchBot`, …)
- an empty sitemap
- an edge bot challenge
- `served-content-empty`: the homepage or a sampled page serves fewer than 50 words and no `<h1>`
  without JavaScript. `<noscript>` content counts, because a crawler without JavaScript reads it;
  `<script>`, `<style>`, `<template>` and `<svg>` do not, and inline markup does not split words.
  Google renders JavaScript, but most AI-search crawlers do not, so a client-rendered page is empty
  to them. Prerender it. A short page under a server-rendered `<h1>`, such as a form, passes. This
  stays a warning while the sites it flags today are remediated.
- `soft-404` (production): a URL that does not exist answers 200.
- `sitemap-url-fragment`: sitemap URLs with a `#fragment`. Crawlers drop the fragment, so each one
  duplicates the page without it, and they are not sampled.

**Info:** no structured data; which training crawlers (`GPTBot`, `Google-Extended`, `CCBot`, …)
are blocked. Blocking those is a legitimate choice for each client.

## Previews

The deploy check tells the action where the preview is:
- **Cloudflare Pages:** the check run's summary carries the commit's preview URL.
- **Workers Builds:** the check's link names the preview alias and the Worker. `workers-subdomain`
  completes the `https://<alias>-<worker>.<subdomain>.workers.dev` URL.

Preview requests are browser navigations (`Sec-Fetch-Mode: navigate`), because a Workers preview
serves a single-page app's fallback only to those. Production requests are plain crawler
requests.

## Run it locally

```sh
python3 seo-standard/seo_standard.py audit --canonical https://example.com/ --sitemap https://example.com/sitemap.xml
python3 seo-standard/seo_standard.py audit --mode preview --base https://<preview> --canonical https://example.com/ --sitemap https://example.com/sitemap.xml
python3 -m unittest discover -s seo-standard/tests
```

Exit codes: 0 passed, 1 critical findings, 2 could not run. The check fails closed. These all
exit 2, never 1, because a check that verified nothing is not a verdict on the site:
- bad arguments
- a deploy check that failed or never finished, or no preview URL
- an unreachable site
- a preview behind a login (Access)

When a deploy is re-run, the newest check run counts.

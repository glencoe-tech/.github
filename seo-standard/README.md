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

## IndexNow (after a production deploy)

Set `indexnow-key` to tell IndexNow (Bing, Yandex, Seznam, Naver and others) which pages changed.
The site must serve the key at `https://<host>/<key>.txt`. The key is public by design and is not
a credential. In `seo.yml`, add to the snippet above:

```yaml
on:
  # ...pull_request and push as above, plus:
  schedule:
    - cron: "41 5 * * 1"   # weekly: refreshes the ledger between deploys, catches changes made outside a deploy
permissions:
  contents: read
  checks: read
  actions: read          # to read the previous IndexNow ledger
concurrency:             # one ledger writer at a time; pull requests are never queued behind it
  group: seo-${{ github.event_name == 'pull_request' && github.ref || 'production' }}
  cancel-in-progress: false
```

and `indexnow-key: <key>` under `with:`.

- **When:** on the default branch only. On a push, only after this invocation's deploy check
  succeeded for that commit (a step output, never a shared file); a failed or skipped deploy
  submits nothing. On a scheduled run, against what is live, with no deploy to wait for. Pull
  requests and previews never submit.
- **What:** only eligible sitemap URLs. An eligible URL is on the production origin (scheme, host
  and port), has no `#fragment`, is allowed for Bingbot, answers 200, is not `noindex`, and names
  exactly itself as canonical. The canonical is required, and is resolved against `<base href>`.
- **Changed only:** each URL gets a fingerprint of what a reader sees (title, description,
  canonical, served text with inline markup ignored, JSON-LD in canonical form). A URL is submitted
  only when its fingerprint differs from the one IndexNow last accepted.
  - A markup-only or asset-hash rebuild submits nothing.
  - No URL is submitted until one run has completed a baseline. A run where the key, robots.txt,
    a sitemap (child sitemaps included) or the site could not be read records nothing as accepted.
  - A URL missing for a while keeps its accepted fingerprint, so its return unchanged is not a
    change. It is forgotten after 365 days unseen.
- **Limits and retries:**
  - At most 10,000 URLs per request and 1,000 pages per run.
  - 429 and 5xx are retried with backoff, honouring `Retry-After` in seconds or as an HTTP-date.
    When the server asks for more than 60 seconds, the run does not retry early: it records the
    time, counted from when the final response arrived, and later runs defer until then.
  - A refused change is retried at most 5 times, until the page changes again.
- **Records:** the ledger (fingerprints plus the last 200 submissions, with status codes and notes)
  is kept as an artifact for 90 days. It is named `seo-standard-indexnow-ledger-<host>-<hash of the
  calling workflow file>`, so each site and workflow keeps its own. Only artifacts with exactly that
  name from this repository's default-branch runs are read; never a fork's, another branch's or
  another workflow's. The job summary shows each run.
- **When the ledger cannot be used:**
  - GitHub cannot list or serve it (an API error, or a job without `actions: read`): the run
    submits and records nothing and warns, so the last good ledger stays the newest.
  - It expired, or it is corrupt or invalid (including a bad timestamp): the run starts a new
    baseline, records why, and puts a `::warning::` on the run. URLs that changed since IndexNow
    last accepted them are not submitted. The reason stays in the ledger until a baseline
    completes, so a run that ends early (the key not served, say) does not lose the warning.
  - There has never been one: the first baseline, which is planned and silent.
  - The weekly schedule makes expiry unlikely, not impossible. GitHub can drop a scheduled run, and
    it disables schedules in a public repository after 60 days without activity. The warning is
    the backstop.
  - Every page of the artifact listing is read, so a ledger behind newer artifacts from other
    branches is still found.
- **Not proof of indexing:** "accepted" means IndexNow received the URLs, nothing more. Coverage
  comes from Search Console and Bing Webmaster Tools.

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

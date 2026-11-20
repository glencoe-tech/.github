# discoverability-standard

The check that keeps a Glencoe website findable in search engines and in AI-generated answers.
It reads only what the site serves, never its content choices, and has three commands:

| Command | Where it runs | What it reads |
|---|---|---|
| `static DIR --origin URL` | Pull-request CI, after the build | The built files a deploy would serve |
| `live URL` | Against production (by hand or on a schedule) | The site as a crawler sees it, redirects not followed |
| `indexnow URL --key KEY` | After a deploy or on a schedule | The sitemap; submits URLs whose `lastmod` is in the last 26 hours to [IndexNow](https://www.indexnow.org) (Bing, Yandex, Seznam, Naver, Yep) |

## Errors (fail the check)

An error means a crawler would index the wrong page, the wrong URL, or nothing at all.

- `robots-present`: `robots.txt` answers 5xx or not at all, which crawlers read as "the whole site
  is disallowed" (RFC 9309). A 4xx means "everything allowed" and is only a warning.
- `search-crawler-allowed`: `robots.txt` blocks, from any sitemap URL, a crawler that puts the site
  into search results or AI answers: Googlebot, Bingbot, OAI-SearchBot, ChatGPT-User, PerplexityBot, Claude-SearchBot,
  Claude-User. Training crawlers (GPTBot, ClaudeBot, Google-Extended, CCBot, Applebot-Extended)
  are a site-owner policy choice; the check reports them and never fails on them. Rules follow
  RFC 9309 and Google: the crawler's own group (or the longest group token it starts with, else
  `*`), `*` and `$` wildcards, the longest matching rule wins (a trailing `*` adds nothing), Allow
  wins a tie.
- `sitemap-present`, `sitemap-xml`, `sitemap-size`, `sitemap-empty`, `sitemap-depth`: no sitemap,
  an app's HTML fallback served where the sitemap should be, invalid XML or a DOCTYPE, over the
  50 MB protocol limit (gzip sitemaps are read), no URLs, or an index that lists another index.
- `sitemap-origin`, `sitemap-clean-url`, `sitemap-duplicate`: a sitemap URL on another origin,
  with a `#fragment`, or listed twice. Query strings are valid.
- `page-status`: a sitemap URL that does not answer 200 (a redirect counts; list the final URL).
- `page-indexable`: `noindex` or `none` addressed to a search crawler (meta `robots`, `googlebot`,
  `bingbot`...; or any `X-Robots-Tag` value, every repeated header read) on a URL the sitemap asks
  to index. A directive scoped to another crawler (`GPTBot: noindex`) does not count.
- `page-title`, `page-description`: missing.
- `page-canonical`: not exactly one canonical link, or one that is not the sitemap URL.
  The canonical is resolved against `<base>` and the page URL; scheme and host case, the default
  port and an empty path (`https://x.com` = `https://x.com/`) are normalised; path and query must
  match exactly.
- `page-served-content`: fewer than 50 words of text and no `<h1>` in the served HTML, so the
  page is rendered by JavaScript. Google renders JavaScript; most AI-search crawlers do not, so
  such a page is empty to them. Prerender it. (Under 50 words beneath a server-rendered `<h1>`,
  such as a form page, is the `page-thin-content` warning.)
- `structured-data`: a JSON-LD block that does not parse or has no `@type` (malformed `@graph`
  shapes are ignored, never a crash).
- `soft-404` (live): a URL that does not exist answers 200, so every mistyped URL is an
  indexable duplicate of the app shell.
- `indexnow-key`, `indexnow-submit` (indexnow): the key file `https://<host>/<key>.txt` does not
  hold the key, or IndexNow refused a batch (URLs go in batches of 10,000, the protocol limit).

## Warnings (reported, never fail)

Title over 65 or description over 170 characters, a short page (`page-thin-content`), no `lang`,
not exactly one `<h1>`, missing Open Graph tags, no JSON-LD on the home page, invalid `lastmod`, no `Sitemap:` line in
`robots.txt`, plain HTTP not redirecting to HTTPS (live), an indexable built page missing from the
sitemap (static), a page over 10 MB (`page-size`; only its start is read), and more than 500
sitemap URLs (only the first 500 are checked). `lastmod` must be a W3C datetime: `YYYY`,
`YYYY-MM`, `YYYY-MM-DD`, or a date with a time and a zone.

HTML is read as a crawler that does not run JavaScript reads it: `<template>` content is inert, and
the head ends where browsers end it even without `</head>`.

## Use

```yaml
- run: npm run build
- uses: glencoe-tech/.github/discoverability-standard@<full commit sha>
  with:
    dir: dist
    origin: https://glencoetech.com
```

By hand: `python3 discoverability-standard/discoverability.py live https://glencoetech.com`.
`--json FILE` (before the command) also writes the report as JSON. Exit status: 0 no errors,
1 errors, 2 bad usage. Standard-library Python 3.11+.

An IndexNow key is public by design: the site serves it at `/<key>.txt` so IndexNow can prove the
submitter controls the host. It is not a credential; keep one per site.

Tests: `python3 -m unittest discover -s discoverability-standard/tests` (every rule is exercised
failing as well as passing; no test reaches the network).

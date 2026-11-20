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

- `robots-present`: no `robots.txt`.
- `search-crawler-allowed`: `robots.txt` blocks a crawler that puts the site into search results
  or AI answers: Googlebot, Bingbot, OAI-SearchBot, ChatGPT-User, PerplexityBot, Claude-SearchBot,
  Claude-User. Training crawlers (GPTBot, ClaudeBot, Google-Extended, CCBot, Applebot-Extended)
  are a site-owner policy choice; the check reports them and never fails on them.
- `sitemap-present`, `sitemap-xml`, `sitemap-empty`, `sitemap-depth`: no sitemap, an app's HTML
  fallback served where the sitemap should be, invalid XML, or an index that lists another index.
- `sitemap-origin`, `sitemap-clean-url`, `sitemap-duplicate`: a sitemap URL on another origin,
  with a query or `#fragment`, or listed twice.
- `page-status`: a sitemap URL that does not answer 200 (a redirect counts; list the final URL).
- `page-indexable`: `noindex` (meta or `X-Robots-Tag`) on a URL the sitemap asks to index.
- `page-title`, `page-description`: missing.
- `page-canonical`: not exactly one canonical link, or one that is not the sitemap URL.
  `https://x.com` and `https://x.com/` count as the same URL; nothing else is normalised.
- `page-served-content`: fewer than 50 words of text and no `<h1>` in the served HTML, so the
  page is rendered by JavaScript. Google renders JavaScript; most AI-search crawlers do not, so
  such a page is empty to them. Prerender it. (Under 50 words beneath a server-rendered `<h1>`,
  such as a form page, is the `page-thin-content` warning.)
- `structured-data`: a JSON-LD block that does not parse or has no `@type`.
- `soft-404` (live): a URL that does not exist answers 200, so every mistyped URL is an
  indexable duplicate of the app shell.
- `indexnow-key`, `indexnow-submit` (indexnow): the key file `https://<host>/<key>.txt` does not
  hold the key, or IndexNow refused the submission.

## Warnings (reported, never fail)

Title over 65 or description over 170 characters, a short page (`page-thin-content`), no `lang`,
not exactly one `<h1>`, missing Open Graph tags, no JSON-LD on the home page, invalid `lastmod`, no `Sitemap:` line in
`robots.txt`, plain HTTP not redirecting to HTTPS (live), an indexable built page missing from the
sitemap (static), and more than 500 sitemap URLs (only the first 500 are checked).

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

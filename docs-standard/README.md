# docs-standard

The check behind [GEDS-001, the Engineering Documentation Standard](https://github.com/glencoe-tech/governance/blob/main/docs/ENGINEERING-DOCUMENTATION-STANDARD.md).
It checks only what can be decided mechanically. Whether a document is clear, necessary or well
written is a reviewer's call, and no finding here scores it.

```yaml
- uses: actions/checkout@<sha>
  with: { fetch-depth: 0, persist-credentials: false }
- uses: glencoe-tech/.github/docs-standard@<sha>
  with: { mode: warn }
```

Run it locally with `python3 docs-standard/docs_standard.py <repo>` to scan the whole repository,
or add `--base origin/main --body-file pr.md` to check a branch the way CI does.

## Findings

| Code | Kind | Rule |
| --- | --- | --- |
| D101 | objective | A relative link or `#anchor` in Markdown points at nothing. External URLs are not fetched. |
| D102 | objective | A new or changed governed document (ADR, runbook) lacks its fields: ADRs need `Status`, runbooks need `Owner`. |
| D103 | objective | Two decision records in one directory share an id. |
| D104 | objective | A configured high-impact path changed without its mapped documentation, and the change carries no accepted waiver or deferral (see [Documentation impact](#documentation-impact)). |
| D106 | objective | A protected document changed without a new `Version:`, or was deleted. |
| D107 | objective | A recorded exception has expired or runs past 180 days. |
| D108 | objective | A recorded deferral has expired or runs past 30 days. An invalid deferral covers nothing. |
| D109 | objective | A high-impact `paths` or `docs` pattern matches no tracked file: a blind spot or a typo. |
| W201 | advisory | Two or more added lines of commented-out code. |
| W202 | advisory | Change history in a comment (a date plus a verb, "changed by"). |
| W203 | advisory | A comment whose words all appear in the next line of code. |
| W204 | advisory | A new Markdown file named like a task record, or placed outside the documentation locations. |
| W205 | notice | A high-impact rule was satisfied by a waiver or a deferral instead of its documents. It is shown on the PR so a reviewer sees it; it never blocks. |

Files under any `templates/` directory, and any `*template.md`, are always ignored: their links and
fields are placeholders. `ignore` in the configuration adds to that list.

In a pull request, D101 and D102 look only at files the change touched, plus documents that named a
file the change deleted or renamed. Existing debt is not charged to the next person who edits nearby.
A whole-repository scan (no `--base`) reports all of it, for audits.

## Modes

- `warn` (the default and the only rollout mode): every finding is an annotation on the PR and in the job
  summary. The step passes.
- `enforce`: D findings fail the step. W findings never do, in any mode. Moving a repository to
  `enforce` needs CTO approval (GEDS-001 §12).

## Configuration

`.github/docs-standard.json` is optional. Without it the defaults apply, and since there are no
high-impact rules by default, nothing ever requires a documentation change.

```json
{
  "version": 1,
  "doc_paths": ["README.md", "**/README.md", "docs/**"],
  "ignore": ["import/**"],
  "governed": [{ "glob": "docs/adr/*.md", "class": "adr", "fields": ["status"] }],
  "decision_records": [{ "glob": "docs/adr/*.md", "id": "^(\\d{4})-" }],
  "high_impact": [
    { "id": "schema", "paths": ["migrations/**"], "docs": ["docs/data-architecture/**"] },
    { "id": "tenancy", "paths": ["app/db/tenant_filter.py"], "docs": ["docs/SECURITY.md"], "critical": true }
  ],
  "deferrals": [
    {
      "rule": "tenancy", "issue": "glencoe-tech/axentra-core#431", "owner": "@glencoe-tech/platform-team",
      "reason": "the threat model is being rewritten in #431", "expires": "2026-12-15"
    }
  ],
  "uncovered": [
    { "area": "public HTTP API", "reason": "openapi.json is the reference and the GACS-1 contract check covers it" }
  ],
  "protected": ["docs/STANDARD.md"],
  "exceptions": [
    {
      "code": "D101", "path": "docs/legacy/**", "reason": "monorepo-era links",
      "owner": "@glencoe-tech/platform-team", "risk": "readers hit dead links in legacy audits",
      "expires": "2027-03-01", "replacement": "retire docs/legacy in the Q1 clean-up"
    }
  ]
}
```

Exceptions use the shape of the Product and Work Standard's `work/exemptions.json`: scope, reason,
accountable owner, risk, an expiry no more than 180 days out, and a replacement plan. D104, D107, D108
and D109 cannot be excepted. Changing the file goes through the repository's normal review.

`uncovered` records the high-risk areas a repository deliberately leaves unmapped, and why. The check
validates only its shape. Before a repository moves to `enforce`, every unmapped area that can make a
document false is either mapped or listed here (GEDS-001 §12).

## Documentation impact

A high-impact rule fires when one of its `paths` changed and none of its `docs` did. Only then is
anything required, and only these satisfy it:

| Rule | Satisfied by |
| --- | --- |
| Any | A change to one of its mapped `docs` in the same PR (the rule does not fire) |
| Any | An active `deferrals` entry for that rule: an issue, an owner, a reason, an expiry within 30 days. It lives in the configuration, so it is reviewed as a diff and runs out (W205) |
| Not `critical` | `Documentation impact: none — <specific reason>` in the PR description. The reason needs at least four words beyond stock phrases such as "n/a", "no impact" or "internal only" (W205) |

These never satisfy a rule. `Documentation impact: updated …` does not, because the documents a rule
needs are its mapped `docs`, not any Markdown file. If another document is the right one, correct the
mapping. `follow-up` in a description does not either: a follow-up is a deferral and is recorded as
one. A `critical` rule accepts no description waiver of any kind.

Workflow files under `.github/` are not documentation. Only `.md`, `.mdx`, `.rst` and `.adoc` files
under `doc_paths` count.

## Security and cost

- The base SHA and PR description come from `$GITHUB_EVENT_PATH`. They are parsed as data and never
  interpolated into a shell, so a hostile description cannot run anything.
- It needs `contents: read` only. It holds no secrets and makes no network calls, so fork PRs are safe.
- It reads files and `git diff`. On the 19 glencoe-tech repositories a full scan takes 0.26–0.47 s.
- To be safe on pull requests, add `edited` to the `pull_request` types. Then fixing the description
  re-runs the check.

## Tests

`python3 -I -m unittest discover -s docs-standard/tests -v`. It builds real Git repositories in a
temporary directory and exercises every rule both passing and failing. That includes rename handling,
fenced code, template placeholders, generic and specific waivers, critical rules, deferrals (active,
expired, too long, for another rule), unmatched mappings, an expired exception, an unknown base commit
and the script entry point.

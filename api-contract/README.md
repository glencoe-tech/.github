# api-contract

A pinned CI check for an HTTP API contract (OpenAPI 3.1), implementing the repository controls of
the Glencoe API Contract Standard, GACS-1 (draft): §5.A (what qualifies as an OpenAPI 3.1 contract),
§6.3 and §6.4 (generated clients), §7 (publication and versioning), §8 (compatibility of a change),
§14 (exceptions), §17 (repository enforcement) and Appendix A (change rules).

It is a composite action. A caller runs one check per job and references the action by a full
commit SHA.

## Use it

Add a policy file to the default branch first, in its own pull request (see [Policy](#policy)).
The policy is read from the pull request's base, so a pull request that adds the policy and the
workflow together fails with `policy-missing-at-base`. Then add the workflow:

```yaml
# .github/workflows/api-contract.yml
name: api-contract
on:
  pull_request:
  merge_group:
permissions:
  contents: read
jobs:
  api-contract:
    name: api-contract (${{ matrix.check }})
    # A private repository runs on the CI appliance (EQS §1 rule 12). A public repository, or one
    # that accepts pull requests from forks, uses ubuntu-latest instead.
    runs-on: [self-hosted, avenra-ci]
    timeout-minutes: 15
    strategy:
      fail-fast: false
      matrix:
        # Leave out sdk and lockfile when the policy names no generated SDK.
        check: [validate, lint, compatibility, sdk, lockfile]
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          fetch-depth: 0 # the release tags and their history; a shallow clone is refused
      - uses: glencoe-tech/.github/api-contract@<full 40-character commit SHA of glencoe-tech/.github main>
        with:
          check: ${{ matrix.check }}
```

Use a commit on `main` of this repository, never a branch or tag name. To upgrade, change the SHA
in a reviewed pull request.

The first release of a contract is the only time `first-release` is set. Add
`first-release: "true"` under `with:` in the pull request that makes that release. After the first
`contract-v*` tag exists, the check fails with `first-release-with-releases` until the line is
removed again.

Keep `persist-credentials` at its default in `actions/checkout`. The `compatibility` check runs
`git ls-remote origin` to confirm that the clone holds every release tag, and a private
repository needs the job's read-only token for that.

## Checks

Each check reports its own result, under the job name `api-contract (<check>)`. Those names are
the ones to make required.

| Check | GACS-1 | What fails |
|---|---|---|
| `validate` | §5.A.1, §5.A.7 | The document does not parse (including duplicate keys or more than one YAML document). It is not valid against the OpenAPI 3.1 schema (Spectral `oas3-schema`). `openapi` is not `3.1.x`. It uses 3.0 constructs under a 3.1 header (`nullable`, a boolean `exclusiveMinimum` or `exclusiveMaximum`). `jsonSchemaDialect` is not 2020-12. `info.version` is not `MAJOR.MINOR.PATCH`. |
| `lint` | §5.A.2 to §5.A.6 | The [baseline ruleset](rulesets/baseline.spectral.yaml) reports an error: a missing or duplicate `operationId`, an operation with no `security`, no success response, no error response, or an inline body schema. A second pass runs the product's own ruleset (`lint.ruleset`, read from the base) and fails on its errors. The passes are separate, so a product ruleset can add rules and cannot switch off or lower baseline rules. |
| `compatibility` | §7, §8, §14, App. A | The change needs a larger version step than `info.version` takes from the last release, or the release history cannot be trusted (see the reason codes). |
| `sdk` | §6.3 | The generated output is deleted and regenerated with `sdk.generate` (after `sdk.install`, if set), and anything under `sdk.output` then differs from the commit. That means changed, deleted or untracked files. The working tree must be clean beforehand, and an empty output is a failure. |
| `lockfile` | §6.4 | `sdk.lockfile` is not committed. If `sdk.install` is set, the install changes the lockfile. |

### How the version step is measured

1. **The last release comes from the release tags** (§7.4), never from a file field. The tags are
   those that start with the policy's `tag-prefix` (default `contract-v`). Each must be
   `<prefix>MAJOR.MINOR.PATCH`, optionally with a pre-release suffix. The last release is the tag
   with the highest SemVer precedence, so `1.10.0` beats `1.9.0`. If that tag is a pre-release,
   the check fails (§7.6). An older pre-release is ignored.
2. **The history must be trustworthy, or the check fails.** It fails on a shallow clone, a
   malformed tag, local tags that differ from `git ls-remote --tags origin` in either direction
   or point elsewhere, a latest release outside the history of the revision under test, and a
   tag whose own contract declares a different `info.version`.
3. **No release at all fails** (`no-released-baseline`), unless `first-release: "true"` is set
   and no tag exists either locally or on origin.
4. **oasdiff classifies the change.** oasdiff 1.19.1 (`changelog`) runs from an empty directory
   with every `OASDIFF_*` variable removed, so a `.oasdiff.yaml` added by the pull request cannot
   hide findings. The required step is set as follows:
   - ERR (breaking) needs `major`.
   - WARN (risky) and INFO (additive) need `minor`.
   - An unknown level counts as `major` (§8.3).
   - Any other difference in the document needs at least `patch`, because different bytes under
     an existing version fail (§7.2).
   - If the documents differ beyond descriptions, examples and `info`, and oasdiff reports
     nothing, the change counts as `unclassified-change` and needs `major` (§8.3).
   - Equal bytes or digests never count as a pass on their own. The comparison always runs.
5. **The version step must be large enough.** The step from the last release to `info.version`
   must be at least the required step. A version lower than the last release fails.

The [severity overrides](oasdiff/severity-levels.txt) raise oasdiff's default level to ERR where
GACS-1 Appendix A is stricter:

| Appendix A rule (breaking) | oasdiff checks raised to `err` |
|---|---|
| `operation-id-changed` | `api-operation-id-removed` |
| `auth-added-to-public-route` | `api-security-added`, `api-global-security-added` |
| `scopes-tightened` | `api-security-scope-added`, `api-global-security-scope-added` |
| `auth-scheme-changed` | `api-security-component-type-changed`, `api-security-component-removed` |
| `enum-value-removed-request` | `request-body-enum-value-removed` |
| `documented-code-removed` | `response-non-success-status-removed` |

The self-test checks that every id in that file is a check that the pinned oasdiff knows.

## Policy

The policy file is `.github/api-contract.yml` (`policy` input). Every check reads it from the
**base** revision of the pull request (`base-sha`). The product ruleset and the exceptions it
names are read from the base too. When the policy at the head differs, the check adds a notice
and keeps using the base policy. A pull request therefore cannot loosen the gate it is judged by;
its policy change takes effect after it merges. Unknown keys are an error, so a typo cannot
silently disable a rule.

```yaml
version: 1
contract: openapi.yaml           # the OpenAPI 3.1 document, a path in the repository
tag-prefix: contract-v           # optional; release tags are <prefix>MAJOR.MINOR.PATCH
lint:                            # optional
  ruleset: .github/contract.spectral.yaml   # one self-contained Spectral file, read from the base
exceptions:                      # optional; GACS-1 §8.8 and §14
  - id: "api-path-removed-without-deprecation GET /widgets/{widgetId}"
    reason: "Retired with consumers' agreement; see the migration document"
    expires: "2026-12-31"        # quoted; at most 180 days ahead; an expired entry fails
    owner: widgets-team          # optional
sdk:                             # optional; required for the sdk and lockfile checks
  install: npm ci                # optional; proves the lockfile installs unchanged
  generate: npm run generate:sdk
  output: packages/sdk/src/generated
  lockfile: package-lock.json
```

An exception's `id` is the finding exactly as the check prints it: `<oasdiff id> <METHOD> <path>`,
or `unclassified-change`. An excepted finding does not count towards the required step. Each
entry needs `id`, `reason` and `expires`. A missing field, or an expiry more than 180 days away,
fails with `exception-invalid`. An expired entry fails with `exception-expired`, whether or not it
still matches a finding (§14.4). Adding an exception changes the policy, so it takes effect only
after the reviewed pull request that adds it has merged.

`sdk.generate` and `sdk.install` run with `bash` in the repository root, from the base revision's
policy, against the code under test.

## Inputs and outputs

| Input | Default | |
|---|---|---|
| `check` | (required) | `validate`, `lint`, `compatibility`, `sdk` or `lockfile` |
| `first-release` | `"false"` | `"true"` only for the first release (see [Use it](#use-it)) |
| `policy` | `.github/api-contract.yml` | Read at `base-sha` |
| `base-sha` | the pull request's base, the merge queue's base, or the pushed commit | The revision the policy is read from |
| `working-directory` | `.` | The root of the clone |

| Output | |
|---|---|
| `result` | `pass` or `fail` |
| `reasons` | Space-separated reason codes; `ok` on a pass |
| `required-step`, `declared-step` | `compatibility`: `none`, `patch`, `minor` or `major`. `declared-step` is `first-release` on a first release. |
| `baseline` | `compatibility`: the release tag compared against, or `none` |
| `findings` | `compatibility`: a JSON list of the findings that counted |

The step summary shows the same information, and each failure is an `::error` annotation that
names its reason code.

### Reason codes

| Code | Check | Meaning |
|---|---|---|
| `parse-error` | all reading the contract | The contract or the policy does not parse |
| `contract-missing` | validate, lint, compatibility | `contract` does not exist in the revision |
| `not-openapi-3.1`, `openapi-3.0-construct`, `schema-dialect`, `schema-invalid`, `invalid-contract-version` | validate (`invalid-contract-version` also compatibility) | See [Checks](#checks) |
| `lint-baseline`, `lint-extension` | lint | An error from the baseline ruleset, or from the product ruleset |
| `version-step-insufficient` | compatibility | The step in `info.version` is smaller than the change needs |
| `version-regressed` | compatibility | `info.version` is lower than the last release |
| `no-released-baseline` | compatibility | No release tag, and `first-release` is not set |
| `first-release-with-releases` | compatibility | `first-release: "true"` was given, and a release exists |
| `shallow-clone`, `malformed-tag`, `prerelease-baseline`, `tag-not-in-history`, `tags-differ-from-remote`, `tag-missing-contract`, `tag-version-mismatch`, `remote-unreadable` | compatibility | The release history cannot be trusted |
| `exception-invalid`, `exception-expired` | compatibility | See [Policy](#policy) |
| `sdk-not-configured`, `sdk-install-failed`, `sdk-generate-failed`, `sdk-drift`, `sdk-empty`, `dirty-worktree` | sdk (`sdk-not-configured` and `dirty-worktree` also lockfile) | See [Checks](#checks) |
| `lockfile-missing`, `lockfile-install-failed`, `lockfile-drift` | lockfile | See [Checks](#checks) |
| `policy-missing-at-base`, `policy-invalid`, `base-unknown`, `input-invalid` | all | The policy or the inputs cannot be used |
| `tool-error`, `git-error`, `internal-error` | all | A tool did not complete. This is always a failure. |

## What the caller must still do

The action enforces only what runs inside a job. GACS-1 §17 also needs the following, and the
action cannot do it:

- **Make the checks required.** Each `api-contract (<check>)` name must be a required status check
  on the default branch. Branch rules belong to the organization's governance, and this repository
  changes none (§17.4). A check that is not required is advisory, and an advisory check is not the
  control (§17.5).
- **Protect the workflow file.** On `pull_request`, the workflow comes from the pull request
  itself, so the pull request can edit it to drop a check, change the pinned SHA or set
  `first-release`. A required check that never reports blocks the merge. Put a CODEOWNERS entry on
  `.github/workflows/` so that edits to the workflow are reviewed.
- **Protect the contract surface with CODEOWNERS.** Cover the contract path, the policy file, the
  product ruleset and the SDK generator configuration, so that a change to any of them is reviewed
  as a contract change (§8.8, §17.2).
- **Protect the release tags.** A tag ruleset must stop `<prefix>*` tags from being created,
  moved or deleted except by the release process (§7.2, §7.3). The action refuses tags that
  disagree with origin. It cannot tell a legitimate tag on origin from one that was rewritten
  there.
- **Run no path filter** that skips the workflow when the contract does not change (§17.2).

## What it does not guarantee

- **Provider behaviour.** A structural comparison cannot see a change of meaning: a default, an
  ordering, a unit or an authorization rule (§8.7, §10, §11). That is GACS-05/07 work.
- **Consumer compatibility.** The action does not check that consumers build against a release,
  or that they declare the range they support (§6.5, §9). That is GACS-05/07 work.
- **Deployment skew.** It does not check whether the deployed provider and its consumers are
  compatible (§9). That is GACS-05/07 work.
- **Approvals for risky and breaking changes.** It reports risky changes (which need an approval
  and a changelog entry, §8.5) and breaking changes (which need a migration document, a sunset
  date and approval, §8.6), but does not verify those artifacts. It enforces only the version step.
- **Changes oasdiff does not see.** Classification is what oasdiff 1.19.1 reports, plus the
  `unclassified-change` backstop. That backstop applies only when oasdiff reports nothing at all.
- **Other contract kinds.** It covers OpenAPI 3.1 only, not AsyncAPI, Protocol Buffers or GraphQL.
- **Drift of an exported contract** (§5.3), where the document is regenerated from code. That stays
  the product's own check.
- **Remote `$ref`s** are resolved by Spectral and oasdiff at run time, and are not pinned.

## Why a composite action

The tool code that runs must be exactly the code at the SHA the caller pinned.

- **A reusable workflow cannot do this safely.** Inside a reusable workflow, `uses: ./…` and a
  plain `actions/checkout` both resolve to the *caller's* repository, and `github.workflow_sha`
  is the caller's commit. `job.workflow_sha` does name the reusable workflow's own commit
  ([contexts reference, `job` context](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#job-context)).
  Using it would still take a second checkout of this repository, and it is not available on
  GitHub Enterprise Server.
- **A composite action gets its files at the pinned ref.** GitHub downloads the action at the
  `@<sha>` the caller wrote, and every step here runs scripts from `$GITHUB_ACTION_PATH`, which is
  that download
  ([`github.action_path`](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#github-context);
  [composite `runs.steps[*].run`](https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax#runsstepsrun)).
  The rulesets, the oasdiff overrides and the lockfile come from the same download.
- **Other choices stay with the caller.** The caller's job chooses the runner and does the
  checkout.

## Toolchain

Everything is pinned. Nothing is installed globally. It goes to `$RUNNER_TEMP/api-contract-toolchain`
([`bin/install-toolchain.sh`](bin/install-toolchain.sh)).

| Tool | Version | How it is pinned |
|---|---|---|
| oasdiff | 1.19.1 | Release tarball, SHA-256 checked against values recorded from the release's `checksums.txt` (linux amd64/arm64, darwin) |
| Spectral CLI | 6.14.2 | [`toolchain/package-lock.json`](toolchain/package-lock.json), `npm ci --ignore-scripts` (integrity hashes) |
| yaml (the parser for the contract and the policy) | 2.8.4 | Same lockfile |
| Python | 3.12 | `actions/setup-python` (pinned by SHA). The checks use the standard library only. |
| Node.js | 22 | `actions/setup-node` (pinned by SHA). It stays on `PATH` for the rest of the caller's job. |

The runner needs `bash`, `git`, `curl` and `tar`. It also needs network access to github.com
release downloads and registry.npmjs.org.

## Develop

```sh
make -C api-contract test   # installs the pinned toolchain, then runs every self-test case
make -C api-contract lint   # shellcheck + actionlint
python3 api-contract/tests/run_tests.py --list           # the cases
python3 api-contract/tests/run_tests.py -k compat/ --keep # a subset; keep the built histories
```

Each self-test case builds a small git history at test time, in a temporary directory: a bare
`origin`, a clone, release tags and a pull-request branch. It is built from the synthetic
[fixtures](fixtures/). The test runs one check and asserts both the result and the reason code.
Every failure case has a positive control, which is the same history without the defect.
[`.github/workflows/api-contract-selftest.yml`](../.github/workflows/api-contract-selftest.yml)
runs the full set and also drives a sample of cases through the action itself.

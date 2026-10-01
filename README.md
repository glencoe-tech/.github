# glencoe-tech/.github

The organization repository for Glencoe Technology. GitHub reads it for three things: the
organization profile page, the community-health defaults that apply to every repository, and the
shared workflows other repositories call. It holds no product code.

## What lives here

| Path | What it is | Who reads it |
|---|---|---|
| [`profile/README.md`](profile/README.md) | The public organization page at github.com/glencoe-tech | Everyone |
| [`ENGINEERING-QUALITY-STANDARD.md`](ENGINEERING-QUALITY-STANDARD.md) | The quality contract every repository inherits: fail-closed CI, pinned tooling, PRs not pushes, secrets never in repositories, Terraform-only infrastructure | Every contributor and every CI gate |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | The working agreement around the standard: the PR flow, the hard rules, how to bring a repository into contract | Read before a first pull request |
| [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/) | The organization's issue forms, one per work type, and `config.yml` | Anyone opening an issue in a repository without its own forms |
| [`.github/workflows/repo-minimum-gates.yml`](.github/workflows/repo-minimum-gates.yml) | The reusable EQS floor: a full-history secret scan and a build that must succeed | Repositories that call it, pinned by SHA |

## Issue forms

Every repository without its own `.github/ISSUE_TEMPLATE/` uses these forms. There is one form per
work type: Program, Epic, Feature Delivery, Task, Bug, Decision, Research, Qualification, Risk and
KTLO. Blank issues are off. The "Product and Work Standard" link on the new-issue page explains which
type to choose and what each one needs.

The forms are generated from the Product and Work Standard, not edited by hand. A change to a form is
a change to the standard: make it there and regenerate them, so the forms and the board stay in step.
A repository may add a stricter, more specific form of its own. It may not remove or weaken the
fields a work type requires.

## Ownership

Glencoe engineering governance owns this repository. Changes land by pull request and are accepted
by the organization's human acceptance authority. Because the repository is public, nothing private
belongs here: no internal product details, credentials, customer names or security findings.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) first. In short: branch from `main`, open a pull request,
and keep CI green, with every check proving something real. Infrastructure changes go to the owning
Terraform repository, never to a dashboard.

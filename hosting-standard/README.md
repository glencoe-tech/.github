# hosting-standard

The check behind the Glencoe [right-sized hosting standard](https://github.com/glencoe-tech/governance/blob/main/docs/HOSTING-COST-STANDARD.md).
It reads every `wrangler.{jsonc,json,toml}` in a repository and the README's `## Hosting` record, and
fails on:

- `run_worker_first: true` (use a path list such as `["/api/*"]`);
- a missing record, or one that names no tier;
- **T0** with a Worker script (`main`, or Pages `functions/`);
- **T0/T1** on a server runtime (OpenNext, by `main` or dependency);
- **T2** that does not name the requirement needing rendering.

A repository with no Cloudflare config passes. Standard-library Python 3.11+.

```yaml
- uses: glencoe-tech/.github/hosting-standard@<full commit sha>
```

Tests: `python3 -m unittest discover -s hosting-standard/tests` (every rule is exercised failing).

"""Every rule must be shown to fail, not only to pass: python3 -m unittest discover hosting-standard/tests"""
import json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from hosting_standard import check  # noqa: E402

RECORD = "# Site\n\n## Hosting\n\n- **Tier:** {tier}\n- **Requirements that need code:** {need}\n\n## Other\n"


def repo(files):
    root = tempfile.mkdtemp()
    for path, text in files.items():
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text if isinstance(text, str) else json.dumps(text))
    return root


def readme(tier, need="contact form (`/api/contact`)"):
    return RECORD.format(tier=tier, need=need)


class HostingStandard(unittest.TestCase):
    def problems(self, files):
        return check(repo(files))[0]

    def test_passes_a_repository_without_cloudflare(self):
        self.assertEqual(self.problems({"README.md": "# lib\n"}), [])

    def test_passes_a_static_site_declared_t0(self):
        files = {"README.md": readme("T0 (static)"), "wrangler.jsonc": '{\n  // static\n  "assets": {"directory": "out"},\n}'}
        self.assertEqual(self.problems(files), [])

    def test_passes_static_plus_api_declared_t1(self):
        files = {"README.md": readme("T1"), "wrangler.jsonc": {"main": "worker/index.ts", "assets": {"directory": "out", "run_worker_first": ["/api/*"]}}}
        self.assertEqual(self.problems(files), [])

    def test_passes_a_rendered_app_declared_t2(self):
        files = {"README.md": readme("T2", "per-user portal pages"), "wrangler.jsonc": {"main": ".open-next/worker.js"}}
        self.assertEqual(self.problems(files), [])

    def test_fails_run_worker_first_true(self):
        files = {"README.md": readme("T1"), "wrangler.jsonc": {"main": "w.ts", "assets": {"run_worker_first": True}}}
        self.assertIn("run_worker_first is true", " ".join(self.problems(files)))

    def test_fails_without_a_hosting_record(self):
        self.assertIn("no `## Hosting` record", " ".join(self.problems({"README.md": "# x\n", "wrangler.jsonc": {}})))
        self.assertIn("no `## Hosting` record", " ".join(self.problems({"wrangler.jsonc": {}})))

    def test_fails_a_record_without_a_tier(self):
        files = {"README.md": "## Hosting\n\n- **Tier:** static\n", "wrangler.jsonc": {}}
        self.assertIn("names no tier", " ".join(self.problems(files)))

    def test_fails_t0_with_a_worker_script_or_pages_functions(self):
        self.assertIn("tier T0 but a Worker script runs", " ".join(self.problems({"README.md": readme("T0"), "wrangler.jsonc": {"main": "w.ts"}})))
        pages = {"README.md": readme("T0"), "wrangler.toml": 'name = "x"\npages_build_output_dir = "dist"\n', "functions/api/x.ts": "export {}"}
        self.assertIn("tier T0 but a Worker script runs", " ".join(self.problems(pages)))

    def test_fails_t1_on_a_server_runtime(self):
        by_main = {"README.md": readme("T1"), "wrangler.jsonc": {"main": ".open-next/worker.js"}}
        by_dependency = {"README.md": readme("T1"), "wrangler.jsonc": {"main": "w.ts"}, "package.json": {"devDependencies": {"@opennextjs/cloudflare": "1"}}}
        for files in (by_main, by_dependency):
            self.assertIn("server runtime (OpenNext)", " ".join(self.problems(files)))

    def test_fails_t2_without_the_requirement(self):
        files = {"README.md": readme("T2", ""), "wrangler.jsonc": {"main": ".open-next/worker.js"}}
        self.assertIn("must name the requirement", " ".join(self.problems(files)))

    def test_checks_configs_in_subdirectories_and_skips_build_output(self):
        files = {"README.md": readme("T0"), "apps/site/wrangler.jsonc": {"main": "w.ts"}, "node_modules/x/wrangler.jsonc": {"main": "y.ts"}}
        problems = self.problems(files)
        self.assertEqual(len(problems), 1)
        self.assertIn("apps/site/wrangler.jsonc", problems[0])

    def test_reports_an_unreadable_config_instead_of_crashing(self):
        files = {"README.md": readme("T0"), "wrangler.jsonc": "{ not json"}
        self.assertIn("cannot be read", " ".join(self.problems(files)))


if __name__ == "__main__":
    unittest.main()

# The api-contract action's own gates. The same commands run locally and in
# .github/workflows/api-contract-selftest.yml.
TOOLCHAIN ?= $(or $(RUNNER_TEMP),$(CURDIR)/.cache)/api-contract-toolchain

.PHONY: test toolchain lint

toolchain:
	bin/install-toolchain.sh "$(TOOLCHAIN)"

test: toolchain
	python3 tests/run_tests.py --toolchain "$(TOOLCHAIN)"

lint:
	shellcheck bin/install-toolchain.sh
	cd .. && actionlint

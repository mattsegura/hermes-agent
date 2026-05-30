# Hermes kanban test/dev loop runner.
#
# Three tiers, all targeting the kanban dev-loop scope (the same selection the
# CI bar uses). They share one scope so the numbers are directly comparable:
#
#   make test-fast   -> parallel, skips @pytest.mark.slow  (fast inner loop)
#   make test-full   -> parallel, runs EVERYTHING incl. slow (pre-push / CI gate)
#   make test-baseline -> single-process (the historical dev loop; comparison only)
#
# Nothing is deleted or permanently skipped: `slow` tests still run in
# `test-full`. `test-fast` just defers the genuinely-heavy (real-subprocess /
# cross-process / high-volume) tests so the edit→test loop stays quick.
#
# Override the scope on the command line, e.g. run the entire hermes_cli suite:
#   make test-full KANBAN_K= TESTS=tests/hermes_cli/

# Prefer an in-repo venv, else fall back to the shared hermes-agent interpreter.
PY ?= $(shell if [ -x venv/bin/python ]; then echo venv/bin/python; \
              elif [ -x .venv/bin/python ]; then echo .venv/bin/python; \
              else echo $(HOME)/.hermes/hermes-agent/venv/bin/python; fi)
PYTEST := $(PY) -m pytest

TESTS    ?= tests/hermes_cli/
KANBAN_K ?= kanban or launch or reactive or optimizer or amend or steer or ceo or sensor
XDIST    ?= -n auto

# Only emit `-k EXPR` when KANBAN_K is non-empty (so `KANBAN_K=` runs the
# whole TESTS scope).
K_ARG := $(if $(strip $(KANBAN_K)),-k "$(KANBAN_K)",)

# The cross-process board-lock acquire timeout is lowered to 2s under test so a
# contended lock fails fast (the same contention behaviour is asserted) instead
# of hanging for the production 30s. conftest.py sets this per-test too; we
# export it here as belt-and-suspenders for direct `make` invocations.
export HERMES_KANBAN_LOCK_TIMEOUT_SECONDS ?= 2

.PHONY: test-fast test-full test-baseline install-hooks

# Opt-in install of the pre-push gate. We install into the per-repo hooks dir
# (honouring core.hooksPath if the user has set one) rather than editing global
# git config, so nothing fights an existing setup. Bypass a push with
# `git push --no-verify` or `HERMES_SKIP_PREPUSH=1 git push`.
install-hooks:
	@hooks_dir="$$(git config --get core.hooksPath || true)"; \
	if [ -z "$$hooks_dir" ]; then hooks_dir="$$(git rev-parse --git-common-dir)/hooks"; fi; \
	mkdir -p "$$hooks_dir"; \
	cp scripts/hooks/pre-push "$$hooks_dir/pre-push"; \
	chmod +x "$$hooks_dir/pre-push"; \
	echo "installed pre-push hook -> $$hooks_dir/pre-push"

test-fast:
	@unset HERMES_HOME; $(PYTEST) $(TESTS) $(XDIST) -p no:cacheprovider \
		-m "not integration and not slow" $(K_ARG) -q

test-full:
	@unset HERMES_HOME; $(PYTEST) $(TESTS) $(XDIST) -p no:cacheprovider \
		-m "not integration" $(K_ARG) -q

test-baseline:
	@unset HERMES_HOME; $(PYTEST) $(TESTS) -p no:cacheprovider \
		-m "not integration" $(K_ARG) -q

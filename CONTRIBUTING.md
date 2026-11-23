# Contributing

Keep changes small enough to review on their own. Fix behavior and document it before
moving code between modules. For an MCP change, update its schema/description, the
[reference](docs/mcp.md), relevant examples and [changelog](CHANGELOG.md) together.

## Install a development checkout

Use Python 3.12 or 3.13 and Poetry. From a checkout of the branch you intend to change:

```bash
poetry env use python3.12
poetry lock
poetry install --with dev,test -E mcp
poetry run fractrade-hl-mcp --help
```

`poetry.lock` is currently ignored by this repository. Regenerate your local lock after
dependency changes; a stale local lock is not the release's dependency record. The `dev`
and `test` groups supply maintainer tools and test fixtures, while `[mcp]` installs the
optional MCP runtime. Keep those tools out of consumer runtime dependencies. Never commit
`.env`, credentials, local MCP configuration, caches or build outputs.

For the pinned user installation, use the [README instructions](README.md#installation--updates).
A development checkout may contain behavior not present in that release tag.

## Run the existing offline tests

The current test setup loads `.env`, requires account environment variables even for mocked
tests, and includes real exchange tests. **Plain `pytest` is not an offline command.** The
workflow and fixtures remain unchanged. Use this explicit selection from the repository
root for offline verification; its environment overrides apply only to this process:

```bash
poetry run python - <<'PY'
import os

os.environ.update(
    PYTHON_DOTENV_DISABLED="1",
    HYPERLIQUID_PUBLIC_ADDRESS="0x" + "1" * 40,
    HYPERLIQUID_PRIVATE_KEY="0x" + "ab" * 32,
    HYPERLIQUID_ENV="testnet",
)

import requests
import pytest

def blocked(*args, **kwargs):
    raise AssertionError("Offline verification blocked an unexpected HTTP request")

requests.sessions.Session.send = blocked

class OfflineChecks:
    def pytest_runtest_setup(self, item):
        if item.name == "test_the_dotenv_of_the_working_directory_is_found":
            # This test uses its own temporary directory and test-only .env.
            os.environ.pop("PYTHON_DOTENV_DISABLED", None)

    def pytest_runtest_teardown(self, item):
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"

raise SystemExit(pytest.main([
    "-q", "tests/mcp", "tests/test_new_features.py", "tests/test_reliability.py",
    "tests/test_retry.py", "tests/test_optimal_pricing.py", "tests/test_xyz_universe.py",
    "tests/test_wire.py", "tests/test_pnl.py",
    "-k", "not TestOptimalPricingIntegration",
], plugins=[OfflineChecks()]))
PY
```

The dummy key is test data. `testnet` here satisfies existing fixtures; this command does
not run testnet trading. The HTTP block is local to the test process. The selected MCP
stdio tests launch local subprocesses for protocol/configuration checks with their own
isolated environments. Keep any new real-network test out of this selection.

For a focused check, replace the paths above with the relevant mocked test file or node ID,
retaining the environment and HTTP guard. `test_client.py` also contains live reads, and
`TestOptimalPricingIntegration` is live despite sharing a file with unit tests. A test file
without “integration” in its name is not necessarily offline.

## Run a live check only when explicitly intended

The existing live tests use the selected `.env`/environment and can place orders, cancel
orders or close positions. Use the maintainer's designated mainnet test account, review the
chosen test and its cleanup first, and keep unrelated exposure out of its scope. Do not use
the dummy environment above. Configure the library's account variables as shown in
[.env.example](.env.example), with the chosen network set deliberately; never paste a key
into a command or review comment.

Examples of explicit node selection, **not part of offline verification**:

```bash
# Real exchange read; the existing fixture requires configured account credentials.
poetry run pytest -q tests/test_integration.py::test_get_price

# Real BTC orders and cleanup: only for an explicitly authorized trading check.
poetry run pytest -q tests/test_integration.py::test_trading_flow
```

The second test uses 0.001 BTC, cancels BTC orders and closes BTC exposure in cleanup.
Other live tests can have broader cleanup. There is currently no `--live-mainnet` gate or
`live` marker that makes broad collection safe. Do not describe an offline pass as evidence
that real exchange execution was tested.

## Lint, type-check and build

Run from the repository root:

```bash
poetry run ruff check src tests examples
poetry run mypy
git diff --check
poetry build
```

The mypy scope is configured in `pyproject.toml`. `poetry build` produces the sdist and wheel
under `dist/`; it does not publish them. For release verification, install the chosen wheel
with `[mcp]` into clean Python 3.12 and 3.13 environments, run CLI/MCP smoke checks and confirm
imports come from that wheel. Record the commit, Python/dependency versions, artifact and
commands used. A local build alone does not complete that release verification.

## Make a useful change and review

Keep exchange request/signing behavior in the library and MCP account selection, guards
and tool contracts in `src/fractrade_hl_simple/mcp_server`. Reuse existing conversion/error
helpers. A new trading capability needs an explicit decision about account locking, limits,
reduce-only behavior and unknown outcomes; descriptions must match what it actually does.

For behavior fixes, use the existing fake clients or mocked SDK to reproduce a realistic
failure and verify the outcome, including what was and was not submitted. Avoid a new test
for a prose-only edit; instead check links, tool schemas and executable examples. Never
validate a documentation trade example against a live account.

A pull request should explain the concrete problem, resulting behavior, verification and
remaining limits. Include the exact offline selection or explicitly authorized live node
used. For changes involving both connectors, follow the
[two-connector workflow](docs/agentic-trading.md), distinguish source/schema checks from a
live connected walkthrough, and identify the versions actually exercised. Test workflow
and CI changes are separate maintainer decisions, not part of an ordinary documentation fix.

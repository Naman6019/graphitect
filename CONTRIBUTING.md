# Contributing to Graphitect

Thanks for contributing. For bugs and feature ideas, search existing issues
first and include a small reproducible example when possible.

## Local setup

Graphitect supports Python 3.10+ and requires Node.js 18+ for Archify renders.

```bash
git clone https://github.com/Naman6019/graphitect.git
cd graphitect
python -m pip install -e . pytest ruff
node --version
```

## Before opening a pull request

```bash
python -m ruff check graphitect tests
python -m pytest
node graphitect/_vendor/archify/bin/archify.mjs --help
```

Keep changes focused. Add or update tests for behavior changes, preserve the
evidence-first boundary, and never commit API keys, generated reports, or a
target repository's private source code.

## Bundled projects

Graphify and Archify are bundled under their respective license and notice
files. Keep their attribution intact. Update a bundled snapshot deliberately,
with its upstream revision and license impact documented in the pull request.

# Publishing Conduit

Conduit ships as two complementary artifacts:

| Artifact | Registry | Install | Source |
|----------|----------|---------|--------|
| `conduit-lightning` — the server (MCP + REST + core) | PyPI | `pip install conduit-lightning` | `pyproject.toml`, `src/conduit/` |
| `conduit-setup` — the onboarding wizard | npm | `npx conduit-setup` | `cli/` |

> **Naming:** the PyPI *distribution* is `conduit-lightning` (the bare `conduit`
> is already taken on PyPI); the *import* package stays `conduit`. The wizard
> launches the server via `python -m conduit.mcp_server` (or the `conduit-mcp`
> console script). This split is normal — e.g. `pip install scikit-learn` →
> `import sklearn`.

The final `twine upload` / `npm publish` steps need **your** registry tokens and
are effectively permanent (a published version can't be overwritten). Run them
yourself — don't share tokens.

## Prerequisites (one time)

- PyPI API token — https://pypi.org/manage/account/token/
- TestPyPI API token — https://test.pypi.org/manage/account/token/
- npm account logged in — `npm login`
- Build tooling — `pip install -e ".[publish]"` (installs `build` + `twine`)

Put tokens in `~/.pypirc` (or pass at upload time). Never commit tokens.

## 1. Bump the version (every release after the first)

A published version is immutable. Bump **both**, in lockstep:
- `pyproject.toml` → `[project] version`
- `cli/package.json` → `version`

## 2. Build + check the Python package

```bash
rm -rf dist/
python -m build          # -> dist/conduit_lightning-<ver>-py3-none-any.whl + .tar.gz
twine check dist/*       # must report PASSED for both artifacts
```

## 3. Clean-room smoke test (recommended)

Prove the wheel works outside the dev tree:

```bash
python -m venv /tmp/conduit-smoke
/tmp/conduit-smoke/bin/pip install "dist/conduit_lightning-"*.whl
/tmp/conduit-smoke/bin/python -c "import conduit; print('version', conduit.__version__)"
/tmp/conduit-smoke/bin/python -c "from conduit.mcp_server import run; print('conduit-mcp entry OK')"
ls /tmp/conduit-smoke/bin/conduit-*        # conduit-api, conduit-mcp
rm -rf /tmp/conduit-smoke
```

## 4. TestPyPI first

```bash
twine upload --repository testpypi dist/*
# verify it installs from TestPyPI (pulls real deps from PyPI):
pip install --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ conduit-lightning
```

## 5. PyPI

```bash
twine upload dist/*
```

`pip install conduit-lightning` is now live.

## 6. npm wizard

```bash
cd cli
npm publish --access public
```

`npx conduit-setup` is now live.

## 7. After publishing

- Update the README **Quick Start** to offer `pip install conduit-lightning` /
  `npx conduit-setup` alongside the from-source install.
- Tick the roadmap: `- [x] Package for distribution (pip install conduit-lightning)`.
- Tag the release: `git tag v<ver> && git push origin v<ver>`.

(Tell Claude once it's live and the README + roadmap edits can be done for you.)

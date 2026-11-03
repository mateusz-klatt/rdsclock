# Contributing to rdsclock

Thank you for considering a contribution. This project follows a
**lightweight, audit-friendly** workflow.

## Quick Start

```bash
git clone https://github.com/mateusz-klatt/rdsclock.git
cd rdsclock
uv sync --frozen --extra dev --extra plot --no-install-project --no-build
export PYTHONPATH="$PWD/src"
.venv/bin/python -m pytest tests/ -m 'not real_sdr' -q -W error
.venv/bin/ruff check src/ tests/ examples/
.venv/bin/black --check src/ tests/ examples/
```

## Development Workflow

1. **Open an issue first** for non-trivial changes. Discuss approach
   before opening a pull request. This is especially important for
   anything that affects:
   - the RDS decoder pipeline (correctness sensitive),
   - the consensus / anti-spoof logic (security sensitive),
   - the CLI (operator-facing contract),
   - the on-disk file formats (compatibility).

2. **One change per pull request.** Mix concerns only when a
   refactoring is genuinely impossible to split.

3. **Tests are required** for new behaviour. The project keeps a
   100% pass rate on every commit. If your change is not testable
   with a synthetic generator (see `rdsclock.synth`), explain why
   in the PR description.

4. **Documentation** must accompany behaviour changes. Update:
   - `README.md`, `README.pl.md` and `README.fr.md` if the user-facing surface changes,
   - `docs/ARCHITECTURE.md` for internal structure changes,
   - `CHANGELOG.md` under `## [Unreleased]`,
   - `docs/THREAT_MODEL.md` if assumptions change.

   Use the English README as the reference for translations. Keep commands,
   measured results and limitations equivalent across all three versions;
   translate explanatory text and comments, not CLI option names. The linked
   technical guides are maintained in English.

## Code Style

- **Python 3.12** target.
- **Black** (100-column) + **Ruff** for formatting/linting; use the commands
  above. `make lint` performs a compile-only syntax check.
- **Type hints** required on all public functions and dataclasses.
- **Docstrings**: imperative mood, English, terse. Document *why*,
  not *what* (the code already shows what).
- No emoji in source code, no emoji in commit messages.

## Commit Message Format

```
<scope>: <short summary, imperative>

Optional body explaining motivation and trade-offs.
Link issues with: Refs #123 / Fixes #123.
```

Scopes: `dsp`, `rds`, `synth`, `decoder`, `channelizer`, `recon`,
`cli`, `tests`, `docs`, `ci`, `chore`.

## Pull Request Checklist

- [ ] Relevant tests pass; skips and unavailable hardware checks are reported
- [ ] Ruff and Black checks pass
- [ ] Docstrings present on new public API
- [ ] `CHANGELOG.md` updated
- [ ] Documentation updated where applicable
- [ ] Commits are atomic and signed (`git commit -s`)

## Security-Relevant Changes

Changes to consensus logic, anti-spoof checks, or any aspect of the
threat model **must** be accompanied by:

1. An updated `docs/THREAT_MODEL.md`.
2. At least one regression test demonstrating the threat is detected.
3. Sign-off from a second reviewer with security background.

## License

By submitting a contribution you agree that your work is licensed
under the [Apache License 2.0](LICENSE), the project's primary licence.

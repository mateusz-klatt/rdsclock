# Publishing a release

Published versions are immutable history. Use a new version and commit;
do not amend a released commit, move its tag, or delete a working PyPI version.
A tag identifies the source **before** building. A GitHub release is the public
page containing the resulting files; these are separate steps.

## Normal path

1. Update the version in `pyproject.toml`, `src/rdsclock/__init__.py` and the
   `rdsclock` entry in `uv.lock`. Add a changelog entry and reviewed notes at
   `docs/releases/X.Y.Z.md`. Verify EN/PL/FR documentation and interface labels.
2. Run the checks, open a PR and merge after CI passes. The release workflow
   reruns the fast suite, including the three dashboards, without radio access.
3. On the verified merged commit, create and push the version tag:

   ```bash
   git switch master
   git pull --ff-only
   git tag -a vX.Y.Z -m 'rdsclock X.Y.Z'
   git push origin vX.Y.Z
   ```

4. Follow the **publish** Actions run. It validates the tag against all three
   version declarations, builds a wheel and sdist, checks metadata and installs
   the wheel in an isolated environment. The installed package must include
   EN/PL/FR catalogs and web assets and run the synthetic multi-station demo.
5. The workflow prepares a **draft** GitHub release with SHA-256-verified assets,
   publishes those same files to PyPI, checks PyPI's hashes, and only then makes
   the GitHub release public and latest. Do not manually publish an empty
   GitHub release to start this process.
6. Check the public release assets and PyPI version. Post the announcement
   only after both are available. Keep IQ, audio and private correspondence out
   of release assets.

The `pypi` Actions environment needs the existing PyPI token or a configured
trusted publisher. Only the publishing jobs receive their required write
permissions. Releases for the same ref are serialized.

## Recovery

Use **Re-run failed jobs** on the original Actions run. This reuses the exact
build artifact; rebuilding may change archive bytes. Staging accepts an
existing draft and uploads only missing assets. Existing assets must match;
the workflow never uses `--clobber`. A public release is accepted only when
all its expected assets already match.

PyPI publication skips files already present, but staging first checks their
SHA-256 hashes and finalization requires a complete matching set. A mismatch
stops the workflow: investigate and publish a new version rather than replacing
files. If the final check fails because PyPI metadata is not yet visible,
rerun that failed job. If the original artifact has expired, recover the
original distributions and verify their provenance instead of rebuilding and
attempting to overwrite a published version.

Manual workflow dispatch is for an **existing `vX.Y.Z` tag**, for example:

```bash
gh workflow run publish.yml --ref vX.Y.Z
```

Dispatch on `master` or another branch fails before any publication. Prefer
rerunning failed jobs when any assets have already been uploaded.

For 1.1.0, PyPI publication succeeded but GitHub release creation failed because
the release already existed. The original PyPI wheel and sdist were SHA-256
verified and attached to the existing GitHub release. No yank was needed.
Yanking is appropriate for a broken release, not for a missing GitHub attachment;
it discourages installation without deleting the files. PyPI does not allow
reuse of deleted distribution filenames.

References: [PyPI yanking](https://docs.pypi.org/project-management/yanking/),
[PyPI upload troubleshooting](https://docs.pypi.org/publishing/troubleshooting/),
[GitHub releases](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository).

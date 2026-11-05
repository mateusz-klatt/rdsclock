"""Release guards and idempotent staging; never replace published files.

Commands receive only checked values: the canonical repository, the dist
directory and the version read from pyproject.toml, never a raw argument.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

# Releases go to this repository alone, as distributions go to PyPI's rdsclock.
REPOSITORY = "mateusz-klatt/rdsclock"
DIST = Path("dist")
# Both registries are eventually consistent: a new draft or upload can be
# missing from their listings for a while. Tests replace sleep.
sleep = time.sleep
LOOKUP_ATTEMPTS, LOOKUP_DELAY = 6, 5
PYPI_ATTEMPTS, PYPI_DELAY = 10, 15


class IncompleteError(ValueError):
    """Expected files are missing; unlike a mismatch, waiting may resolve it."""


def validate(tag: str, ref_type: str, root: Path = Path(".")) -> str:
    if ref_type != "tag" or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("Select an existing vX.Y.Z tag; branch dispatches cannot publish.")
    version = tag[1:]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    lock = tomllib.loads((root / "uv.lock").read_text())
    locked = next(p["version"] for p in lock["package"] if p["name"] == "rdsclock")
    runtime = re.search(
        r'^__version__ = "([^"]+)"', (root / "src/rdsclock/__init__.py").read_text(), re.M
    )
    if runtime is None or {project, locked, runtime[1]} != {version}:
        raise ValueError("Tag, project, runtime and lock versions must agree.")
    if not (root / f"docs/releases/{version}.md").is_file():
        raise ValueError("Missing reviewed release notes.")
    # The same string as the tag's, but read from the checkout: the commands
    # built from it never carry the command-line argument itself.
    return project


def distributions(directory: Path, version: str) -> dict[str, str]:
    expected = {f"rdsclock-{version}-py3-none-any.whl", f"rdsclock-{version}.tar.gz"}
    files = {p.name: p for p in directory.iterdir() if p.is_file()}
    if set(files) != expected:
        raise ValueError(
            "Distribution directory must contain exactly this version’s wheel and sdist."
        )
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}


def verify_files(expected: dict[str, str], actual: dict[str, str], complete: bool) -> None:
    if set(actual) - set(expected):
        raise ValueError("Unexpected published distribution.")
    if any(expected[name] != digest for name, digest in actual.items()):
        raise ValueError(
            "Published file differs from this build; never overwrite or reuse a version."
        )
    if complete and set(expected) != set(actual):
        raise IncompleteError("Published distributions are incomplete.")


def verify_pypi(version: str, expected: dict[str, str], complete: bool) -> None:
    try:
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/rdsclock/{version}/json", timeout=30
        ) as response:
            info = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        info = {"urls": []}
    actual = {item["filename"]: item["digests"]["sha256"] for item in info["urls"]}
    verify_files(expected, actual, complete)


# Both capture stdout only: on failure, the tool's own message reaches the log.
def gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True)


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def releases(repo: str) -> Iterator[dict]:
    # /releases/tags/{tag} only finds published releases. The authenticated
    # releases list includes drafts, so interrupted staging is discoverable.
    page = 1
    while True:
        items = json.loads(gh("api", f"repos/{repo}/releases?per_page=100&page={page}"))
        yield from items
        if len(items) < 100:
            return
        page += 1


def _empty_draft(item: dict) -> bool:
    return item["draft"] and not item.get("assets")


def release_info(repo: str, tag: str) -> dict | None:
    matches = [item for item in releases(repo) if item["tag_name"] == tag]
    if len(matches) <= 1:
        return matches[0] if matches else None
    # A retried create can leave extra empty drafts. Choosing one silently could
    # publish the wrong page, and deleting is the operator's decision.
    keep = [item for item in matches if not _empty_draft(item)] or matches[:1]
    if len(keep) > 1:
        raise ValueError("Multiple releases use this tag; resolve the ambiguity first.")
    extra = ", ".join(str(item["id"]) for item in matches if item is not keep[0])
    raise ValueError(
        f"Multiple releases use this tag; delete the empty draft release(s) {extra} "
        "and rerun the failed job."
    )


def verify_assets(info: dict, expected: dict[str, str], complete: bool) -> None:
    # GitHub supplies a SHA-256 digest for every newly uploaded release asset.
    actual = {
        item["name"]: (item.get("digest") or "").removeprefix("sha256:") for item in info["assets"]
    }
    verify_files(expected, actual, complete)


def await_release(repo: str, tag: str) -> dict | None:
    # The list may lag behind a create, so poll for a bounded time.
    for attempt in range(LOOKUP_ATTEMPTS):
        if attempt:
            sleep(LOOKUP_DELAY)
        info = release_info(repo, tag)
        if info is not None:
            return info
    return None


def create_draft(repo: str, tag: str) -> dict:
    # Create exactly once: a failed response (e.g. HTTP 502) may still have
    # created the draft, and a second create would duplicate it.
    error = None
    try:
        gh(
            "release",
            "create",
            tag,
            "--repo",
            repo,
            "--verify-tag",
            "--draft",
            "--title",
            tag,
            "--notes-file",
            f"docs/releases/{tag[1:]}.md",
        )
    except subprocess.CalledProcessError as exc:
        error = exc
    info = await_release(repo, tag)
    if info is not None:
        return info
    if error is not None:
        raise error
    raise ValueError(
        "The draft release was created but never appeared in the release list; "
        "check for it and rerun the failed job."
    )


def stage(repo: str, tag: str, directory: Path, expected: dict[str, str]) -> None:
    info = release_info(repo, tag)
    if info is None:
        info = create_draft(repo, tag)
    verify_assets(info, expected, complete=not info["draft"])
    existing = {item["name"] for item in info["assets"]}
    for name in sorted(set(expected) - existing):
        # Deliberately no --clobber. A conflicting asset must stop publication.
        gh("release", "upload", tag, str(directory / name), "--repo", repo)
    verify_assets(release_info(repo, tag), expected, complete=True)


def await_pypi(version: str, expected: dict[str, str]) -> None:
    # PyPI's JSON can lag an upload by minutes. Only missing files are worth
    # waiting for; a mismatch or an unexpected file never resolves itself.
    for attempt in range(PYPI_ATTEMPTS):
        try:
            verify_pypi(version, expected, complete=True)
            return
        except IncompleteError:
            if attempt == PYPI_ATTEMPTS - 1:
                raise
            sleep(PYPI_DELAY)


def semver(tag: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def is_latest(repo: str, tag: str) -> bool:
    # Retrying an older tag must not take "latest" away from a newer release.
    published = (
        semver(item["tag_name"])
        for item in releases(repo)
        if not item["draft"] and not item.get("prerelease")
    )
    own = semver(tag)
    return own >= max((v for v in published if v is not None), default=own)


def finalize(repo: str, tag: str, expected: dict[str, str]) -> None:
    await_pypi(tag[1:], expected)
    info = release_info(repo, tag)
    if info is None:
        raise ValueError("The staged GitHub release is missing.")
    verify_assets(info, expected, complete=True)
    if info["draft"]:
        latest = "--latest" if is_latest(repo, tag) else "--latest=false"
        gh("release", "edit", tag, "--repo", repo, "--draft=false", latest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "pypi", "stage", "finalize"))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--ref-type", default=os.environ.get("GITHUB_REF_TYPE", ""))
    args = parser.parse_args()
    version = validate(args.tag, args.ref_type)
    tag = f"v{version}"
    if args.action == "validate":
        head = git("rev-parse", "--verify", "HEAD")
        tagged = git("rev-parse", "--verify", "--end-of-options", f"{tag}^{{commit}}")
        if head != tagged:
            raise ValueError("Checkout does not match the selected tag.")
        return
    expected = distributions(DIST, version)
    if args.action == "pypi":
        verify_pypi(version, expected, complete=False)
    elif args.action == "stage":
        stage(REPOSITORY, tag, DIST, expected)
    else:
        finalize(REPOSITORY, tag, expected)


if __name__ == "__main__":
    main()

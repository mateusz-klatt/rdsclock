"""Publication boundaries: wrong refs, changed assets and interrupted releases."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import release


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    waited = []
    monkeypatch.setattr(release, "sleep", waited.append)
    return waited


@pytest.fixture
def source(tmp_path):
    (tmp_path / "src/rdsclock").mkdir(parents=True)
    (tmp_path / "docs/releases").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "2.0.0"\n')
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "rdsclock"\nversion = "2.0.0"\n')
    (tmp_path / "src/rdsclock/__init__.py").write_text('__version__ = "2.0.0"\n')
    (tmp_path / "docs/releases/2.0.0.md").write_text("Reviewed notes")
    return tmp_path


@pytest.mark.parametrize(
    "tag,kind", [("master", "branch"), ("v2.0.0", "branch"), ("v2.0", "tag"), ("v2.0.1", "tag")]
)
def test_rejects_branch_dispatch_and_wrong_version(source, tag, kind):
    with pytest.raises(ValueError):
        release.validate(tag, kind, source)


def test_versions_and_reviewed_notes_must_agree(source):
    assert release.validate("v2.0.0", "tag", source) == "2.0.0"
    (source / "uv.lock").write_text('[[package]]\nname = "rdsclock"\nversion = "1.1.0"\n')
    with pytest.raises(ValueError, match="must agree"):
        release.validate("v2.0.0", "tag", source)


def test_old_distributions_cannot_slip_into_upload(tmp_path):
    for name in ("rdsclock-2.0.0-py3-none-any.whl", "rdsclock-2.0.0.tar.gz"):
        (tmp_path / name).write_bytes(b"package")
    assert len(release.distributions(tmp_path, "2.0.0")) == 2
    (tmp_path / "rdsclock-1.0.1.tar.gz").write_bytes(b"stale")
    with pytest.raises(ValueError, match="exactly"):
        release.distributions(tmp_path, "2.0.0")


@pytest.mark.parametrize(
    "actual,complete", [({"wheel": "wrong"}, False), ({}, True), ({"unexpected": "a"}, False)]
)
def test_never_skips_conflicting_or_incomplete_public_files(actual, complete):
    with pytest.raises(ValueError):
        release.verify_files({"wheel": "a"}, actual, complete)


def test_partial_matching_upload_can_be_resumed():
    release.verify_files({"wheel": "a", "sdist": "b"}, {"wheel": "a"}, complete=False)


def _info(draft=True, both=False):
    assets = [{"name": "wheel", "digest": "sha256:a"}]
    if both:
        assets.append({"name": "sdist", "digest": "sha256:b"})
    return {"draft": draft, "assets": assets}


def test_stage_resumes_draft_without_replacing_existing_asset(monkeypatch):
    responses = iter([_info(), _info(both=True)])
    monkeypatch.setattr(release, "release_info", lambda *args: next(responses))
    calls = []
    monkeypatch.setattr(release, "gh", lambda *args: calls.append(args))
    release.stage("owner/repo", "v2.0.0", Path("dist"), {"wheel": "a", "sdist": "b"})
    assert calls == [("release", "upload", "v2.0.0", "dist/sdist", "--repo", "owner/repo")]


def test_new_release_is_a_draft_before_uploading(monkeypatch):
    responses = iter([None, {"draft": True, "assets": []}, _info(both=True)])
    monkeypatch.setattr(release, "release_info", lambda *args: next(responses))
    calls = []
    monkeypatch.setattr(release, "gh", lambda *args: calls.append(args))
    release.stage("owner/repo", "v2.0.0", Path("dist"), {"wheel": "a", "sdist": "b"})
    assert "--draft" in calls[0] and "--verify-tag" in calls[0]
    assert all("--clobber" not in call for call in calls)
    assert [call[1] for call in calls] == ["create", "upload", "upload"]


def test_incomplete_public_release_stops_before_upload(monkeypatch):
    monkeypatch.setattr(release, "release_info", lambda *args: _info(draft=False))
    monkeypatch.setattr(
        release, "gh", lambda *args: pytest.fail("must not mutate a public release")
    )
    with pytest.raises(ValueError, match="incomplete"):
        release.stage("owner/repo", "v2.0.0", Path("dist"), {"wheel": "a", "sdist": "b"})


def test_finalize_requires_matching_pypi_before_publishing(monkeypatch):
    def mismatched(*args, **kwargs):
        raise ValueError("PyPI hash differs")

    monkeypatch.setattr(release, "verify_pypi", mismatched)
    monkeypatch.setattr(release, "gh", lambda *args: pytest.fail("must not publish"))
    with pytest.raises(ValueError, match="PyPI"):
        release.finalize("owner/repo", "v2.0.0", {"wheel": "a"})


def _registry(calls, published=()):
    # Answers the release listing; records every mutating gh call.
    def gh(*args):
        if args[0] == "api":
            return json.dumps(
                [{"tag_name": tag, "draft": False, "prerelease": False} for tag in published]
            )
        calls.append(args)

    return gh


def test_finalize_is_idempotent_when_both_registries_match(monkeypatch):
    calls = []
    monkeypatch.setattr(release, "verify_pypi", lambda *args, **kwargs: calls.append("pypi"))
    monkeypatch.setattr(release, "release_info", lambda *args: _info(both=True))
    monkeypatch.setattr(release, "gh", _registry(calls))
    release.finalize("owner/repo", "v2.0.0", {"wheel": "a", "sdist": "b"})
    assert calls == [
        "pypi",
        ("release", "edit", "v2.0.0", "--repo", "owner/repo", "--draft=false", "--latest"),
    ]
    calls.clear()
    monkeypatch.setattr(release, "release_info", lambda *args: _info(draft=False, both=True))
    release.finalize("owner/repo", "v2.0.0", {"wheel": "a", "sdist": "b"})
    assert calls == ["pypi"]


def test_lookup_finds_drafts_by_listing_and_propagates_api_errors(monkeypatch):
    draft = {**_info(), "tag_name": "v2.0.0"}
    monkeypatch.setattr(release, "gh", lambda *args: json.dumps([draft]))
    assert release.release_info("owner/repo", "v2.0.0") == draft
    assert release.release_info("owner/repo", "v3.0.0") is None

    def forbidden(*args):
        raise subprocess.CalledProcessError(1, "gh", stderr="gh: Forbidden (HTTP 403)")

    monkeypatch.setattr(release, "gh", forbidden)
    with pytest.raises(subprocess.CalledProcessError):
        release.release_info("owner/repo", "v2.0.0")


def test_release_lookup_paginates(monkeypatch):
    draft = {**_info(), "tag_name": "v2.0.0"}
    pages = iter([[{"tag_name": f"v3.0.{n}"} for n in range(100)], [draft]])
    calls = []

    def lookup(*args):
        calls.append(args)
        return json.dumps(next(pages))

    monkeypatch.setattr(release, "gh", lookup)
    assert release.release_info("owner/repo", "v2.0.0") == draft
    assert calls[-1] == ("api", "repos/owner/repo/releases?per_page=100&page=2")


def _main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["release.py", *argv, "--ref-type", "tag"])
    release.main()


def test_validate_action_compares_the_checkout_with_the_tag(source, monkeypatch):
    monkeypatch.chdir(source)
    calls = []

    def git(*args):
        calls.append(args)
        return "abc123"

    monkeypatch.setattr(release, "git", git)
    _main(monkeypatch, "validate", "--tag", "v2.0.0")
    assert calls == [
        ("rev-parse", "--verify", "HEAD"),
        ("rev-parse", "--verify", "--end-of-options", "v2.0.0^{commit}"),
    ]
    monkeypatch.setattr(release, "git", lambda *args: "abc123" if args[-1] == "HEAD" else "def456")
    with pytest.raises(ValueError, match="does not match"):
        _main(monkeypatch, "validate", "--tag", "v2.0.0")


def test_actions_publish_only_the_canonical_repository_and_dist(source, monkeypatch):
    monkeypatch.chdir(source)
    calls = []
    monkeypatch.setattr(
        release, "distributions", lambda directory, version: calls.append(directory) or {"w": "a"}
    )
    monkeypatch.setattr(release, "verify_pypi", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(release, "stage", lambda *args: calls.append(args))
    monkeypatch.setattr(release, "finalize", lambda *args: calls.append(args))
    for action in ("pypi", "stage", "finalize"):
        _main(monkeypatch, action, "--tag", "v2.0.0")
    dist = Path("dist")
    assert calls == [
        dist,
        ("2.0.0", {"w": "a"}),
        dist,
        ("mateusz-klatt/rdsclock", "v2.0.0", dist, {"w": "a"}),
        dist,
        ("mateusz-klatt/rdsclock", "v2.0.0", {"w": "a"}),
    ]
    for option in ("--repo", "--dist"):  # neither is chosen on the command line
        with pytest.raises(SystemExit):
            _main(monkeypatch, "stage", "--tag", "v2.0.0", option, "--clobber")


def _create_then(monkeypatch, create, listings):
    responses = iter(listings)
    monkeypatch.setattr(release, "release_info", lambda *args: next(responses))
    calls = []

    def gh(*args):
        calls.append(args)
        if args[1] == "create":
            create()

    monkeypatch.setattr(release, "gh", gh)
    return calls


def _bad_gateway():
    raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 502")


def test_stage_continues_when_create_fails_but_draft_appears(monkeypatch, sleeps):
    listings = [None, None, None, _info(), _info(both=True)]
    calls = _create_then(monkeypatch, _bad_gateway, listings)
    release.stage("owner/repo", "v2.0.0", Path("dist"), {"wheel": "a", "sdist": "b"})
    assert [call[1] for call in calls] == ["create", "upload"]
    assert sleeps == [release.LOOKUP_DELAY] * 2


def test_stage_waits_for_a_created_draft_to_be_listed(monkeypatch, sleeps):
    calls = _create_then(monkeypatch, lambda: None, [None, None, _info(), _info(both=True)])
    release.stage("owner/repo", "v2.0.0", Path("dist"), {"wheel": "a", "sdist": "b"})
    assert [call[1] for call in calls] == ["create", "upload"]
    assert sleeps == [release.LOOKUP_DELAY]


@pytest.mark.parametrize(
    "create,error", [(_bad_gateway, subprocess.CalledProcessError), (lambda: None, ValueError)]
)
def test_stage_gives_up_when_the_draft_never_appears(monkeypatch, sleeps, create, error):
    calls = _create_then(monkeypatch, create, [None] * (release.LOOKUP_ATTEMPTS + 1))
    with pytest.raises(error) as raised:
        release.stage("owner/repo", "v2.0.0", Path("dist"), {"wheel": "a"})
    if error is ValueError:
        assert "never appeared" in str(raised.value)
    assert [call[1] for call in calls] == ["create"]  # created once, never retried
    assert len(sleeps) == release.LOOKUP_ATTEMPTS - 1


def test_duplicate_empty_drafts_are_named_not_deleted(monkeypatch):
    staged = {**_info(), "id": 7, "tag_name": "v2.0.0"}
    empty = {"id": 9, "tag_name": "v2.0.0", "draft": True, "assets": []}
    calls = []

    def gh(*args):
        calls.append(args)
        return json.dumps([empty, staged])

    monkeypatch.setattr(release, "gh", gh)
    with pytest.raises(ValueError, match=r"delete the empty draft release\(s\) 9 and rerun"):
        release.release_info("owner/repo", "v2.0.0")
    assert all(call[0] == "api" for call in calls)
    monkeypatch.setattr(release, "gh", lambda *args: json.dumps([staged, {**staged, "id": 8}]))
    with pytest.raises(ValueError, match="resolve the ambiguity"):
        release.release_info("owner/repo", "v2.0.0")


def test_finalize_waits_for_pypi_to_list_uploaded_files(monkeypatch, sleeps):
    pypi = iter([{}, {"wheel": "a"}, {"wheel": "a", "sdist": "b"}])
    expected = {"wheel": "a", "sdist": "b"}
    monkeypatch.setattr(
        release,
        "verify_pypi",
        lambda version, expected, complete: release.verify_files(expected, next(pypi), complete),
    )
    monkeypatch.setattr(release, "release_info", lambda *args: _info(both=True))
    calls = []
    monkeypatch.setattr(release, "gh", _registry(calls))
    release.finalize("owner/repo", "v2.0.0", expected)
    assert sleeps == [release.PYPI_DELAY] * 2
    assert calls[-1][-1] == "--latest"


def test_finalize_gives_up_on_pypi_that_stays_incomplete(monkeypatch, sleeps):
    monkeypatch.setattr(
        release,
        "verify_pypi",
        lambda version, expected, complete: release.verify_files(expected, {}, complete),
    )
    monkeypatch.setattr(release, "gh", lambda *args: pytest.fail("must not publish"))
    with pytest.raises(release.IncompleteError):
        release.finalize("owner/repo", "v2.0.0", {"wheel": "a"})
    assert len(sleeps) == release.PYPI_ATTEMPTS - 1


def test_finalize_fails_on_pypi_mismatch_without_waiting(monkeypatch, sleeps):
    attempts = []

    def mismatched(version, expected, complete):
        attempts.append(version)
        release.verify_files(expected, {"wheel": "wrong"}, complete)

    monkeypatch.setattr(release, "verify_pypi", mismatched)
    monkeypatch.setattr(release, "gh", lambda *args: pytest.fail("must not publish"))
    with pytest.raises(ValueError, match="differs") as raised:
        release.finalize("owner/repo", "v2.0.0", {"wheel": "a"})
    assert not isinstance(raised.value, release.IncompleteError)
    assert attempts == ["2.0.0"] and sleeps == []


@pytest.mark.parametrize(
    "published,latest",
    [
        (["v2.0.1", "v1.1.0"], "--latest=false"),
        (["v1.10.0", "v2.0.0-rc1", "v10.0.0-beta"], "--latest"),
        (["v1.1.0", "v1.9.9"], "--latest"),
    ],
)
def test_finalize_marks_latest_only_for_the_highest_version(monkeypatch, published, latest):
    monkeypatch.setattr(release, "verify_pypi", lambda *args, **kwargs: None)
    monkeypatch.setattr(release, "release_info", lambda *args: _info(both=True))
    calls = []
    monkeypatch.setattr(release, "gh", _registry(calls, published))
    release.finalize("owner/repo", "v2.0.0", {"wheel": "a", "sdist": "b"})
    assert calls == [("release", "edit", "v2.0.0", "--repo", "owner/repo", "--draft=false", latest)]


def test_latest_ignores_drafts_and_prereleases(monkeypatch):
    listing = [
        {"tag_name": "v3.0.0", "draft": True, "prerelease": False},
        {"tag_name": "v2.1.0", "draft": False, "prerelease": True},
        {"tag_name": "v1.0.0", "draft": False, "prerelease": False},
    ]
    monkeypatch.setattr(release, "gh", lambda *args: json.dumps(listing))
    assert release.is_latest("owner/repo", "v2.0.0")


def test_finalization_is_serialized_across_tags():
    # is_latest() and the publishing edit must not interleave between releases.
    workflow = (Path(__file__).parents[1] / ".github/workflows/publish.yml").read_text()
    job = workflow.split("\n  github-release:\n", 1)[1]
    assert (
        "group: publish-finalize" in job and "${{" not in job.split("group:", 1)[1].split("\n")[0]
    )

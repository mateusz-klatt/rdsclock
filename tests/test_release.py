"""Publication boundaries: wrong refs, changed assets and interrupted releases."""

import json
import subprocess
from pathlib import Path

import pytest

from tools import release


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


def test_finalize_is_idempotent_when_both_registries_match(monkeypatch):
    calls = []
    monkeypatch.setattr(release, "verify_pypi", lambda *args, **kwargs: calls.append("pypi"))
    monkeypatch.setattr(release, "release_info", lambda *args: _info(both=True))
    monkeypatch.setattr(release, "gh", lambda *args: calls.append(args))
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

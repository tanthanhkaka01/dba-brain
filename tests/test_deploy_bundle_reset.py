"""A deploy bundle is shipped *over* the worker's files, so a leftover is not junk — it is cargo.

The bundle directory is rebuilt on every `build-image`. If a previous build's files survive that
rebuild, they are copied to the worker and written over the live ones, and nothing says so: the
run prints "Bundle ready" either way.

That stopped being hypothetical on 2026-08-22. `assets/` had just been split — the shipped SQL
moved into the package, the operator's task SQL stayed at the tool root — and a build failed with
`FileExistsError` on a `data/` directory carrying a timestamp from an earlier run, after the
delete had apparently succeeded. Had it merged instead of failing, the bundle would have shipped
the *old* `assets/` layout, including a `metrics/` directory that no longer belongs there, on top
of a worker running the new image.

So the reset verifies rather than assumes, and refuses to continue when anything survives.
`dirs_exist_ok=True` would have made the symptom disappear and the hazard permanent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db_ops.control import deploy


def test_a_previous_bundle_does_not_survive_the_reset(tmp_path, monkeypatch) -> None:
    bundle = tmp_path / "db_ops_deploy"
    (bundle / "assets" / "metrics").mkdir(parents=True)
    (bundle / "assets" / "metrics" / "old.sql").write_text("SELECT 1", encoding="utf-8")
    (bundle / "data").mkdir()
    (bundle / "data" / "db_instances.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(deploy, "BUNDLE_DIR", bundle)

    deploy._reset_bundle_dir()

    assert bundle.is_dir(), "the directory itself has to exist for the copy that follows"
    assert list(bundle.iterdir()) == [], "a previous build's files would be shipped to the worker"


def test_it_works_when_there_is_nothing_to_reset(tmp_path, monkeypatch) -> None:
    """The first build on a fresh clone has no bundle directory at all."""
    bundle = tmp_path / "never-built"
    monkeypatch.setattr(deploy, "BUNDLE_DIR", bundle)

    deploy._reset_bundle_dir()

    assert bundle.is_dir() and list(bundle.iterdir()) == []


def test_a_directory_that_cannot_be_emptied_stops_the_build(tmp_path, monkeypatch) -> None:
    """Loudly, and naming what survived — the silent version ships yesterday's files."""
    bundle = tmp_path / "stuck"
    bundle.mkdir()
    (bundle / "assets").mkdir()
    monkeypatch.setattr(deploy, "BUNDLE_DIR", bundle)
    monkeypatch.setattr(deploy.shutil, "rmtree", lambda *a, **k: None)
    monkeypatch.setattr(deploy.time, "sleep", lambda _seconds: None)

    with pytest.raises(SystemExit) as raised:
        deploy._reset_bundle_dir()

    message = str(raised.value)
    assert "assets" in message, "say which file is holding the build up"
    assert "worker" in message, "say what continuing would cost"


def test_the_reset_runs_before_anything_is_copied_into_the_bundle() -> None:
    """Order is the property: clearing after the first copy would delete what was just written."""
    import inspect

    source = inspect.getsource(deploy.build_image)
    assert source.index("_reset_bundle_dir()") < source.index("shutil.copy(")

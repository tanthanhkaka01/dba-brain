"""Every component of db_ops has a doc, and every doc has a component.

`docs/NN_*.md` is the current-state reference — one file per component, numbered to match the
README capability table. The rule is easy to state and easy to break silently: a new package under
`db_ops/` is a new component, and it arrives with code, tests and a CLI long before anyone thinks
about the doc. Nothing fails, so nothing reminds you.

`lib` is why this test exists. It was split out of `common` on 2026-08-15 and grew to 46 modules
and ~6,500 lines — the second-largest shared layer in the tree — while the doc set stayed at 13
files. It was undocumented for two days and the full suite was green the whole time, because no
check connected the two directories.

The reverse direction matters just as much. A doc whose component has been deleted or renamed is
worse than no doc: it reads as current reference and describes something that is not there.
"""

from __future__ import annotations


from pathlib import Path

import pytest

from db_ops.lib.distribution import DOC_FOR_PACKAGE

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_OPS_ROOT = REPO_ROOT / "db_ops"
DOCS_ROOT = REPO_ROOT / "docs"

#: `docs/NN_<slug>.md` — the number orders the set, the slug names the component but does not have
#: to equal its directory name (`db` is documented as `01_runtime_store`, `jobs` as
#: `03_app_command_daemon`), so the mapping is declared rather than inferred from the filename.
#:
#: **Imported, not restated.** It used to be a second copy of the same fourteen rows, and the two
#: were free to disagree — which they would have, silently, the first time a component was renamed
#: in one and not the other. `db_ops/lib/distribution.py` owns it because the export needs the same
#: mapping to decide which docs travel with which packages.
COMPONENT_DOCS: dict[str, str] = dict(DOC_FOR_PACKAGE)


def _present(mapping: dict[str, str]) -> dict[str, str]:
    """The rows whose package is actually on disk.

    The public distribution ships seven of the fourteen components, so in an exported tree the
    other seven are legitimately absent — and this file must keep working there, because it is
    exported too. Filtering by what exists keeps both directions of the rule intact: a present
    package still needs its doc, and a present doc still needs its package. What it stops doing is
    demanding a doc for software that is not in the tree.
    """
    return {
        component: doc for component, doc in mapping.items()
        if (DB_OPS_ROOT / component).is_dir()
    }


def _components() -> set[str]:
    """Every importable package under ``db_ops/`` — the definition of "a component"."""
    return {
        path.name
        for path in DB_OPS_ROOT.iterdir()
        if path.is_dir() and not path.name.startswith("__") and (path / "__init__.py").exists()
    }


def _docs() -> set[str]:
    return {path.name for path in DOCS_ROOT.glob("[0-9][0-9]_*.md")}


def test_every_component_has_a_doc() -> None:
    """A new package under `db_ops/` is a new component, and it needs its own reference page."""
    undocumented = sorted(_components() - set(COMPONENT_DOCS))

    assert not undocumented, (
        f"Component(s) with no docs/NN_*.md: {undocumented}. Add the doc and map it in "
        f"COMPONENT_DOCS. A component's doc is written in the same pass as the component, not "
        f"later — see CLAUDE.md, 'Change an app's behavior -> update its docs/NN_*.md'."
    )


def test_every_mapped_doc_exists() -> None:
    missing = sorted(name for name in _present(COMPONENT_DOCS).values()
                     if not (DOCS_ROOT / name).exists())

    assert not missing, f"COMPONENT_DOCS names docs that are not on disk: {missing}"


def test_every_doc_belongs_to_a_component() -> None:
    """A doc whose component is gone reads as current reference and describes nothing."""
    orphaned = sorted(_docs() - set(COMPONENT_DOCS.values()))

    assert not orphaned, (
        f"docs/NN_*.md with no component behind it: {orphaned}. Either the component was renamed "
        f"or removed and the doc should follow it, or COMPONENT_DOCS is out of date."
    )


def test_every_mapping_names_a_real_component() -> None:
    """Keeps the map from outliving what it maps — the same ratchet the other guards use."""
    stale = sorted(set(_present(COMPONENT_DOCS)) - _components())

    assert not stale, f"COMPONENT_DOCS names package(s) that no longer exist: {stale}"


@pytest.mark.parametrize("component,doc_name", sorted(_present(COMPONENT_DOCS).items()))
def test_the_readme_capability_table_lists_every_component(component: str, doc_name: str) -> None:
    """The README is where someone looks first, so a component missing from it is invisible.

    `lib` was the case in point twice over: it had no doc, *and* the README actively said it was
    not a component — "Thirteen components, and the list is closed... there is no fourteenth", with
    `lib` filed under "carries no ORD, because neither is a component". Adding the doc alone would
    have left the README contradicting it.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert f"](./docs/{doc_name})" in readme, (
        f"{component} is not linked from README.md. Every component needs a row in the capability "
        f"table (the ORD number links to its doc) and a line in the documentation index."
    )


@pytest.mark.parametrize("component,doc_name", sorted(_present(COMPONENT_DOCS).items()))
def test_a_doc_is_not_empty(component: str, doc_name: str) -> None:
    """A mapped file that is a stub satisfies the mapping and documents nothing.

    Deliberately *not* asserting a heading format: 10 of the 14 open with a bare title
    (`# Telegram App`) and 4 with the numbered form (`# 13. Common`). That inconsistency is real
    and is recorded in the project's architecture rules as an observation. Pinning one
    of the two here would be this test inventing a convention the repository does not have.
    """
    body = (DOCS_ROOT / doc_name).read_text(encoding="utf-8").strip()

    assert len(body.splitlines()) > 5, f"{doc_name} is a stub, not a reference page"

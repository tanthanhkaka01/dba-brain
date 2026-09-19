"""A published report writes an address in full, or a scanner built from configuration cannot see it.

Found on 2026-09-16 in the published showcase. A page that shortens an address to its last octets (`"0-248"`) publishes a fragment of a real
address that no identifier check can recognise.

**Every gate passed, and every gate was right.** The page shortens an address at render time, so the
shortened string exists in no configuration file — and every scanner here is built from
configuration. The showcase scrubber learns its terms from `db_instances.json`; `check-identifiers`
searches the same terms; gitleaks looks for secrets. All three answered the question they were
asked. The leak was upstream of all of them, in the renderer.

So the fix is in the renderer and the guard belongs with it. Adding the 31 fragments to the
scrubber's term list — which is what closed it in the showcase that day — does not survive the next
new address, and the next abbreviation would not be found by a reader happening to notice it.

The second reason, which matters on any day nobody is publishing: `100-108` cannot be pasted into a
ping, a connection string or a ticket, and two estates on different /16s produce the same fragment
for different machines.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import pytest

from db_ops.reports.inventory_report import _ip_tag, build_models, build_triage

#: The shape the leak took: two numbers joined by a hyphen, which is what an address looks like
#: after somebody has made it "fit".
FRAGMENT = re.compile(r"\b\d{1,3}-\d{1,3}\b")


def test_an_address_is_written_in_full() -> None:
    assert _ip_tag({"ip": "192.0.2.113"}) == "192.0.2.113"


def test_the_two_octet_form_is_gone() -> None:
    """Stated as its own test because a two-octet tail is the exact shape that shipped."""
    assert "0-113" not in _ip_tag({"ip": "192.0.2.113"})


@pytest.mark.parametrize("address", ["192.0.2.1", "198.51.100.234", "203.0.113.69", "10.0.0.5"])
def test_whatever_goes_in_comes_out_whole(address: str) -> None:
    tag = _ip_tag({"ip": address})

    assert ipaddress.ip_address(tag) == ipaddress.ip_address(address)


def test_every_triage_card_carries_whole_addresses() -> None:
    """At the layer that builds the cards, not at the one function, because the leak was a *call
    site* that had been written before the rule existed — several cards took `m["ip"]` directly and
    several took the fragment, and the page carried both."""
    addresses = ["192.0.2.248", "198.51.100.41", "203.0.113.115"]
    # Through `build_models`, not a hand-written dict: the model shape is large and moves, and a
    # fixture that drifts from it stops exercising the renderer without ever failing.
    _scope, models = build_models({"servers": [
        {"server_id": f"ACME-{ip.replace('.', '-')}", "company_code": "ACME",
         "ip": ip, "databases": []}
        for ip in addresses
    ]})
    supplied = set(addresses)

    cards = build_triage(models)

    tags = [tag for card in cards for tag in (card.get("tags") or [])]
    assert tags, "no triage card produced a tag - has the card shape changed?"
    for tag in tags:
        for fragment in FRAGMENT.findall(str(tag)):
            # A date on a card is fine; an address cut in half is not. The two are told apart by
            # asking whether the fragment is the tail of an address that went in.
            assert not any(address.endswith(fragment.replace("-", ".")) for address in supplied), (
                f"triage tag {tag!r} carries {fragment!r}, the tail of a real address")


def test_no_renderer_slices_an_address_for_display() -> None:
    """The guard that catches the *next* one, at the moment it is written.

    An abbreviation is invisible to every scanner this project has, so it cannot be caught
    downstream - it has to be refused where it is made. `.split(".")` on an address followed by a
    slice is the shape that made `100-108`.
    """
    offenders: list[str] = []
    for path in sorted(Path("db_ops").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'\[["\']ip["\']\]\)?\.split\(["\']\.["\']\)', text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.as_posix()}:{line}")

    assert not offenders, (
        "an address is being split for display, which no scanner built from configuration can "
        f"see: {offenders}")

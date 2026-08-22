"""The three kinds of backup, spelled once.

``full`` / ``diff`` / ``log`` is the vocabulary the whole tool matches on: which file a listing
sorts into, which step a restore applies next, and — most sharply — which backup the retention
rule treats as the anchor of a chain.

It was spelled in three files on 2026-08-15: ``common/backupfiles/``, ``common/restorestep/`` and
``lib/backupfiles_retention.py``, the last one having inlined it deliberately to stay a leaf. That
was the wrong trade. A shared vocabulary with three copies is three chances to disagree, and this
one disagrees silently: spell ``FULL`` differently in the retention rule and it stops recognising
any backup as a full, so the pruner deletes the chain it exists to protect. The engines *also*
have their own names for these — Oracle counts levels, PostgreSQL says ``incr`` — and translating
between those and these is :mod:`db_ops.lib.backup_level`, which is a different question and
stays a different module.

Here rather than in ``common`` so that ``lib`` can reach it: the retention rule is pure and must
not import upward, which is exactly the constraint that produced the third copy.
"""

from __future__ import annotations


FULL = "full"
DIFF = "diff"
LOG = "log"

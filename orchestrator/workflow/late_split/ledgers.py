# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What the external ledgers and the ordered child register read back as.

An obligation this binary cannot type is still owed, and a consumer list it
cannot read is not an empty one, so neither reader answers with a default the
way the scalar readers beside them do. Each hands back the typed entries it
could make sense of *and* the field verbatim whenever those entries are not
the whole of it, and the verbatim copy is what the write puts back -- so a
binary that does not understand an entry cannot delete it.

"Typed" is strict here, and it has to be, because the alternative to
preserving an entry is rewriting it from what was understood. An entry is one
this binary wrote only when it has exactly the three fields it writes, each
holding a value this vocabulary knows: a state it cannot read is not
`pending`, a field it did not put there is not noise to drop, and a target
that is not a usable identifier is not one to re-encode. A consumer is a
positive whole number, because an issue number is what a reclamation sweep
asks GitHub about. Every other shape leaves the ledger opaque and untouched.

The wire spellings of an entry live here beside the reader that parses them;
the encoder that turns a typed obligation back into one is the `state`
owner's, because that is where a write is composed.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from orchestrator.workflow.late_split import formats as _formats
from orchestrator.workflow.late_split import payloads as _payloads
from orchestrator.workflow.late_split.models import (
    MAX_RESOURCE_TARGET,
    LateResource,
    LateResourceKind,
    LateResourceState,
)

KIND_KEY = "kind"
TARGET_KEY = "target"
STATE_KEY = "state"

# Exactly what this binary writes for one obligation. An entry with a field
# more, a field fewer, or a value it cannot type is one it did not write and
# cannot rewrite, so the whole ledger is preserved instead.
_ENTRY_KEYS = frozenset((KIND_KEY, TARGET_KEY, STATE_KEY))

_Ledger = tuple[tuple[Any, ...], Optional[str]]


def read_resources(raw: Any) -> _Ledger:
    """Return the typed obligations, and the ledger verbatim if that is lossy.

    An entry whose kind this binary does not know cannot be reconciled by it,
    but it is still an obligation the remote is owed, so the whole field is
    carried through untouched beside whatever could be typed. The typed view
    is then a projection for reading, never the thing that gets written back.
    """
    if not isinstance(raw, list):
        return ((), _verbatim(raw))
    parsed = tuple(_as_resource(entry) for entry in raw)
    typed = tuple(entry for entry in parsed if entry is not None)
    if len(typed) == len(parsed):
        return (typed, None)
    return (typed, _verbatim(raw))


def read_consumers(raw: Any) -> _Ledger:
    """Return the typed consumers, and the ledger verbatim if that is lossy.

    A consumer ledger that cannot be read is not an empty one: reading it that
    way would let a snapshot be reclaimed as though nobody were waiting on it,
    which is the one mistake the ledger exists to prevent. Only a positive
    whole number is a consumer -- an issue number is what a sweep asks GitHub
    about -- and anything else leaves the ledger opaque rather than dropping
    the entry that was not one.
    """
    if not isinstance(raw, list):
        return ((), _verbatim(raw))
    numbers = tuple(_payloads.as_identity(entry) for entry in raw)
    typed = tuple(number for number in numbers if number is not None)
    if len(typed) == len(numbers):
        return (typed, None)
    return (typed, _verbatim(raw))


def _verbatim(raw: Any) -> Optional[str]:
    """Return one ledger field as stable JSON text, or None when absent.

    Absent is the one value with nothing to preserve. Everything else is kept
    exactly as it was read, sorted so the same ledger produces the same text
    on every pass and a round trip through the record compares equal.
    """
    if raw is None:
        return None
    return json.dumps(raw, sort_keys=True, default=str)


def _as_resource(entry: Any) -> Optional[LateResource]:
    """Return one ledger entry, or None unless it is exactly one of ours.

    Every field this binary writes, and no other: an entry carrying a state it
    cannot type, a key it never wrote, or a target that is not a usable
    identifier is one it must not rewrite, and answering None is what keeps
    the whole ledger verbatim instead.
    """
    if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
        return None
    kind = _payloads.as_member(LateResourceKind, entry[KIND_KEY])
    recorded = _payloads.as_member(LateResourceState, entry[STATE_KEY])
    target = entry[TARGET_KEY]
    if kind is None or recorded is None:
        return None
    if not _formats.is_bounded_text(target, MAX_RESOURCE_TARGET):
        return None
    return LateResource(kind=kind, target=target, resource_state=recorded)


def read_register(raw: Any) -> tuple[int, ...]:
    """Return the ordered child register, or empty unless all of it is ours.

    Ordered and positional, unlike the consumer ledger beside it: entry `i`
    is the child that owns manifest slice `i`, so a value this binary did not
    write is not one to skip past -- skipping would shift every child after it
    onto somebody else's slice. All or nothing is the only safe reading, and
    an empty answer costs a marker lookup rather than a wrong adoption.
    """
    if not isinstance(raw, list):
        return ()
    numbers = tuple(_payloads.as_identity(entry) for entry in raw)
    if any(number is None for number in numbers):
        return ()
    return numbers

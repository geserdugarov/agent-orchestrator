# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow manifest validation."""
from __future__ import annotations

from orchestrator import workflow_messages as _owner

Optional = _owner.Optional
Tuple = _owner.Tuple


def _is_valid_dependency(
    dependency_index: object,
    *,
    child_index: int,
    child_count: int,
) -> bool:
    """Validate type, bounds, and the no-self-edge invariant."""
    if isinstance(dependency_index, bool):
        return False
    if not isinstance(dependency_index, int):
        return False
    if dependency_index < 0 or dependency_index >= child_count:
        return False
    return dependency_index != child_index


def _manifest_child_error(
    child: object, child_index: int, child_count: int,
) -> Optional[str]:
    """Return the first structural error for one split child."""
    text_error = _owner._manifest_child_text_error(child, child_index)
    if text_error is not None:
        return text_error
    dependencies, dependency_error = _owner._manifest_child_dependencies(
        child, child_index,
    )
    if dependency_error is not None:
        return dependency_error
    for dependency_index in dependencies or []:
        if not _owner._is_valid_dependency(
            dependency_index,
            child_index=child_index,
            child_count=child_count,
        ):
            return (
                f"child {child_index} has invalid dependency "
                f"{dependency_index!r}"
            )
    return None


def _manifest_children_error(children: list) -> Optional[str]:
    """Validate every child and then the dependency graph as a whole."""
    for child_index, child in enumerate(children):
        child_error = _owner._manifest_child_error(
            child, child_index, len(children),
        )
        if child_error is not None:
            return child_error
    if _owner._has_dep_cycle(children):
        return "dependency graph has a cycle"
    return None


def _split_manifest_error(manifest: dict) -> Optional[str]:
    """Return the first split-only manifest validation error."""
    children, children_error = _owner._split_manifest_children(manifest)
    if children_error is not None:
        return children_error
    umbrella_error = _owner._manifest_umbrella_error(manifest)
    if umbrella_error is not None:
        return umbrella_error
    return _owner._manifest_children_error(children or [])


def _manifest_validation_error(manifest: dict) -> Optional[str]:
    """Validate the decision and its split-only payload when applicable."""
    decision = manifest.get("decision")
    if decision not in ("single", "split"):
        return "decision must be 'single' or 'split'"
    if decision == "single":
        return None
    return _owner._split_manifest_error(manifest)


def _parse_manifest(
    last_message: str,
) -> Tuple[Optional[dict], Optional[str]]:
    """Parse a fenced `orchestrator-manifest` block.

    Returns `(manifest, error_reason)`:
      * `(dict, None)` -- a valid manifest. `decision` is `"single"` or
        `"split"`; for `"split"`, `children` is non-empty and each entry has
        `title`/`body` and a structurally-valid `depends_on` index list. On
        `"single"` only `decision` is validated -- the optional context
        fields (`rationale`, `affected_files`, `notes`) pass through
        unvalidated and are sanitized where rendered.
      * `(None, error)` -- a fence was present but the payload was invalid.
        `error` is a short human-readable reason (used in the HITL park
        message).
      * `(None, None)` -- no fenced block at all. The caller treats this as
        "agent ended without a manifest" and parks as a question.
    """
    payload, payload_error = _owner._extract_manifest_payload(last_message)
    if payload is None:
        return None, payload_error
    manifest, decode_error = _owner._decode_manifest(payload)
    if manifest is None:
        return None, decode_error
    validation_error = _owner._manifest_validation_error(manifest)
    if validation_error is not None:
        return None, validation_error
    return manifest, None

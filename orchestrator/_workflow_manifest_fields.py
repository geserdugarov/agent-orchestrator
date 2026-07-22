# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow manifest fields."""
from __future__ import annotations

from orchestrator import _workflow_messages_state as _state
from orchestrator import workflow_messages as _owner

Optional = _owner.Optional
Tuple = _owner.Tuple
json = _owner.json
_MANIFEST_RE = _state._MANIFEST_RE
_MAX_CHILDREN = _state._MAX_CHILDREN


def _extract_manifest_payload(
    last_message: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Extract the one final fenced manifest payload from an agent reply."""
    if not last_message:
        return None, None
    # The prompt requires exactly one final fenced block. Accepting the first
    # match would let a quoted sample manifest override the agent's answer.
    matches = list(_MANIFEST_RE.finditer(last_message))
    if not matches:
        return None, None
    if len(matches) > 1:
        return None, (
            f"expected exactly one orchestrator-manifest block, "
            f"found {len(matches)}"
        )
    manifest_match = matches[0]
    if last_message[manifest_match.end():].strip():
        return None, (
            "orchestrator-manifest must be the final block; "
            "found content after the closing fence"
        )
    return manifest_match.group(1), None


def _decode_manifest(
    payload: str,
) -> Tuple[Optional[dict], Optional[str]]:
    """Decode a manifest payload and require a JSON object."""
    try:
        manifest = json.loads(payload)
    except json.JSONDecodeError as error:
        return None, f"invalid JSON: {error.msg}"
    if not isinstance(manifest, dict):
        return None, "manifest is not a JSON object"
    return manifest, None


def _split_manifest_children(
    manifest: dict,
) -> Tuple[Optional[list], Optional[str]]:
    """Return the bounded, non-empty children list for a split decision."""
    children = manifest.get("children")
    if not isinstance(children, list) or not children:
        return None, "split decision requires non-empty children list"
    if len(children) > _MAX_CHILDREN:
        return None, f"too many children ({len(children)} > {_MAX_CHILDREN})"
    return children, None


def _manifest_umbrella_error(manifest: dict) -> Optional[str]:
    """Validate the optional umbrella flag without truthy coercion."""
    umbrella = manifest.get("umbrella")
    if umbrella is not None and not isinstance(umbrella, bool):
        return "umbrella must be a boolean"
    return None


def _is_nonempty_text(text_value: object) -> bool:
    return isinstance(text_value, str) and bool(text_value)


def _manifest_child_text_error(
    child: object, child_index: int,
) -> Optional[str]:
    """Validate one child object and its required text fields."""
    if not isinstance(child, dict):
        return f"child {child_index} is not an object"
    if not _owner._is_nonempty_text(child.get("title")):
        return f"child {child_index} missing title or body"
    if not _owner._is_nonempty_text(child.get("body")):
        return f"child {child_index} missing title or body"
    return None


def _manifest_child_dependencies(
    child: dict, child_index: int,
) -> Tuple[Optional[list], Optional[str]]:
    """Normalize null dependencies and reject every other non-list shape."""
    dependencies = child.get("depends_on")
    if dependencies is None:
        return [], None
    if not isinstance(dependencies, list):
        return None, f"child {child_index} depends_on must be a list"
    return dependencies, None

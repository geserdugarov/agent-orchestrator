# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Typed scheduler submissions and legacy call binding."""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class SubmissionRequest:
    """Caller-facing request for one issue worker."""

    repo_slug: str
    issue_number: int
    fn: Callable[[], None]
    family: bool = False
    cap_exempt: bool = False
    per_repo_cap: int | None = None


@dataclass(frozen=True)
class Submission:
    """Normalized request used by slot reservation and completion release."""

    repo_slug: str
    issue_number: int
    fn: Callable[[], None]
    family: bool
    cap_exempt: bool
    per_repo_cap: int

    @property
    def key(self) -> tuple[str, int]:
        return self.repo_slug, self.issue_number


_LEGACY_SUBMIT_SIGNATURE = inspect.Signature((
    inspect.Parameter("repo_slug", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("issue_number", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("fn", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter(
        "family",
        inspect.Parameter.KEYWORD_ONLY,
        default=False,
    ),
    inspect.Parameter(
        "cap_exempt",
        inspect.Parameter.KEYWORD_ONLY,
        default=False,
    ),
    inspect.Parameter(
        "per_repo_cap",
        inspect.Parameter.KEYWORD_ONLY,
        default=None,
    ),
))
_SUBMIT_METHOD_SIGNATURE = _LEGACY_SUBMIT_SIGNATURE.replace(parameters=(
    inspect.Parameter("self", inspect.Parameter.POSITIONAL_ONLY),
    *_LEGACY_SUBMIT_SIGNATURE.parameters.values(),
))


def bind_submission_request(
    positional_fields: tuple[Any, ...],
    keyword_fields: dict[str, Any],
) -> SubmissionRequest:
    """Bind either a typed request or the historical submit arguments."""
    if positional_fields and isinstance(
        positional_fields[0],
        SubmissionRequest,
    ):
        if len(positional_fields) != 1 or keyword_fields:
            if keyword_fields:
                unexpected_name = next(iter(keyword_fields))
                detail = f"keyword field {unexpected_name!r}"
            else:
                detail = "additional positional fields"
            raise TypeError(f"typed scheduler submission does not accept {detail}")
        return positional_fields[0]
    bound_fields = _LEGACY_SUBMIT_SIGNATURE.bind(
        *positional_fields,
        **keyword_fields,
    )
    bound_fields.apply_defaults()
    return SubmissionRequest(**bound_fields.arguments)


def normalize_submission(
    request: SubmissionRequest,
    default_per_repo_cap: int,
) -> Submission:
    """Normalize integer fields and the optional per-repository override."""
    selected_cap = (
        default_per_repo_cap
        if request.per_repo_cap is None
        else max(1, int(request.per_repo_cap))
    )
    return Submission(
        repo_slug=request.repo_slug,
        issue_number=int(request.issue_number),
        fn=request.fn,
        family=request.family,
        cap_exempt=request.cap_exempt,
        per_repo_cap=selected_cap,
    )

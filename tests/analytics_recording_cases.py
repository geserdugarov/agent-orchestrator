# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Agent stream payload builders for analytics recording tests."""

import json

_TYPE_KEY = 'type'


SKILL_STREAM_INPUT_TOKENS = 1_000


SKILL_STREAM_OUTPUT_TOKENS = 500


_CLAUDE_MODEL = "claude-sonnet-4-6"


_INPUT_TOKENS = "input_tokens"


_OUTPUT_TOKENS = "output_tokens"


def claude_stdout_with_skills(
    *,
    skills: tuple[str, ...],
    offered: tuple[str, ...] = (),
    args_marker: str = "skill-args-must-never-be-stored",
    input_tokens: int = SKILL_STREAM_INPUT_TOKENS,
    output_tokens: int = SKILL_STREAM_OUTPUT_TOKENS,
) -> str:
    """A claude stream-json stdout that both reports usage AND triggers
    `Skill` tool_use blocks.

    Each name in `skills` becomes one `tool_use` block named `"Skill"`
    whose `input` carries the name plus an `args` string we assert never
    reaches the analytics record (Privacy: only the skill name is read).
    The single `assistant` frame also carries a `usage` block so the
    baseline usage/cost record is produced regardless of the skill switch.

    When `offered` is non-empty a `system`/`init` frame carrying that
    `skills` array is prepended -- the dedicated offered-skills source the
    real claude stream exposes, so the extractor populates `available`.
    """
    frames = [
        {
            _TYPE_KEY: "assistant",
            "message": {
                "id": "msg-skill",
                "model": _CLAUDE_MODEL,
                "content": [
                    {
                        _TYPE_KEY: "tool_use",
                        "name": "Skill",
                        "id": f"toolu_{index}",
                        "input": {"skill": name, "args": args_marker},
                    }
                    for index, name in enumerate(skills)
                ],
                "usage": {
                    _INPUT_TOKENS: input_tokens,
                    _OUTPUT_TOKENS: output_tokens,
                },
            },
        },
        {_TYPE_KEY: "result", "num_turns": 1},
    ]
    if offered:
        frames.insert(0, {_TYPE_KEY: "system", "subtype": "init", "skills": list(offered)})
    return "\n".join(json.dumps(frame) for frame in frames)


def codex_command(item_id: str, command: str) -> dict:
    return {
        _TYPE_KEY: "item.completed",
        "item": {
            "id": item_id,
            _TYPE_KEY: "command_execution",
            "command": command,
        },
    }


def codex_stdout_with_skills(
    *,
    read: str | None = None,
    incidental: str | None = None,
    input_tokens: int = SKILL_STREAM_INPUT_TOKENS,
    output_tokens: int = SKILL_STREAM_OUTPUT_TOKENS,
) -> str:
    """A codex exec --json stdout that reports usage and, optionally, a direct
    SKILL.md read (an inferred load) and/or a `git diff` inspection of one (an
    incidental reference).

    `read` / `incidental` are skill names: each becomes one `command_execution`
    item -- a `cat .../SKILL.md` for the load, a `git diff -- .../SKILL.md` for
    the reference -- so the recorder exercises the real `parse_codex_skills`
    classifier end-to-end (no stub).
    """
    frames: list[dict] = []
    if read is not None:
        frames.append(codex_command("read1", f"/bin/bash -lc 'cat skills/{read}/SKILL.md'"))
    if incidental is not None:
        frames.append(codex_command("diff1", f"/bin/bash -lc 'git diff -- .agents/skills/{incidental}/SKILL.md'"))
    frames.append(
        {
            _TYPE_KEY: "turn.completed",
            "usage": {
                _INPUT_TOKENS: input_tokens,
                _OUTPUT_TOKENS: output_tokens,
            },
        }
    )
    return "\n".join(json.dumps(frame) for frame in frames)

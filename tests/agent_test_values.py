# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Command, environment, and protocol values for agent tests."""

import json
import tempfile
from pathlib import Path


_CWD = Path("/tmp/agent-orchestrator-test-cwd-doesnt-matter")
_REAL_CWD = Path(tempfile.gettempdir())
_POPEN_TARGET = "orchestrator.agents.processes.subprocess.Popen"
_OS_ENVIRON_TARGET = "os.environ"
_CODEX = "codex"
_CLAUDE = "claude"
_PROMPT = "p"
_CODEX_EXEC = "exec"
_MODEL_FLAG = "-m"
_CODEX_MODEL = "gpt-5.5"
_CONFIG_FLAG = "-c"
_PYTHON_COMMAND_FLAG = "-c"
_CLAUDE_MODEL_FLAG = "--model"
_CLAUDE_MODEL = "claude-opus-4-7"
_RESUME_FLAG = "--resume"
_PATH_ENV = "PATH"
_SYSTEM_PATH = "/usr/bin"
_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
_ENV_KWARG = "env"
_SUBPROCESS_TIMEOUT_SECONDS = 30
_TERMINATION_GRACE_SECONDS = 0.05
_KILLPG = "killpg"
_AGENT_COMMAND = "agent"
_TYPE_FIELD = "type"
_ASSISTANT_EVENT = "assistant"
_MESSAGE_FIELD = "message"
_CONTENT_FIELD = "content"
_TEXT_FIELD = "text"
_SESSION_ID_FIELD = "session_id"
_RESULT_FIELD = "result"
_MOCK_PID = 12345
_PROCESS_WAIT_SECONDS = 5
_PARTIAL_CLAUDE_OUTPUT = json.dumps(
    {
        _TYPE_FIELD: _ASSISTANT_EVENT,
        _MESSAGE_FIELD: {
            _CONTENT_FIELD: [{_TYPE_FIELD: _TEXT_FIELD, _TEXT_FIELD: "partial work so far"}],
        },
    }
)
_RESULT_BEFORE_KILL = "done before kill"
_CLAUDE_PARTIAL_THEN_RESULT = "\n".join((
    _PARTIAL_CLAUDE_OUTPUT,
    json.dumps({_TYPE_FIELD: _RESULT_FIELD, _RESULT_FIELD: _RESULT_BEFORE_KILL}),
))

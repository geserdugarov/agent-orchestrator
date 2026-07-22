# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Static compatibility inventory for the agent runtime façade."""
from __future__ import annotations

from orchestrator import (
    _agent_claude,
    _agent_claude_messages,
    _agent_codex,
    _agent_environment,
    _agent_models,
    _agent_runner_common,
    _agent_session,
)

AgentResult = _agent_models.AgentResult
CodexResult = _agent_models.CodexResult
AgentRunOptions = _agent_models.AgentRunOptions
AgentRunOptionFields = _agent_models.AgentRunOptionFields
SubprocessResult = _agent_models.SubprocessResult
resolve_agent_run_options = _agent_models.resolve_agent_run_options
parse_session_id = _agent_session.parse_session_id
first_nested_uuid = _agent_session._first_nested_uuid
walk_mapping_for_uuid = _agent_session._walk_mapping_for_uuid
walk_for_uuid = _agent_session._walk_for_uuid
uuid_re = _agent_session._UUID_RE
priority_keys = _agent_session._PRIORITY_KEYS
filter_agent_env = _agent_environment.filter_agent_env
is_secret_shaped = _agent_environment.is_secret_shaped
agent_env_key_allowed = _agent_environment._env_key_allowed
agent_env = _agent_environment.agent_env
forbidden_agent_env = _agent_environment._FORBIDDEN_AGENT_ENV
write_credential_locators = _agent_environment._AGENT_WRITE_CREDENTIAL_LOCATORS
secret_suffixes = _agent_environment._AGENT_SECRET_SUFFIXES
secret_bare_names = _agent_environment._AGENT_SECRET_BARE_NAMES
provider_auth_allowlist = _agent_environment._AGENT_PROVIDER_AUTH_ALLOWLIST
codex_last_message_file = _agent_codex.codex_last_message_file
read_last_message = _agent_codex.read_last_message
build_agent_result = _agent_runner_common.build_agent_result
codex_command = _agent_codex.codex_command
log_agent_spawn = _agent_runner_common.log_agent_spawn
run_codex = _agent_codex.run_codex
decode_claude_event = _agent_claude_messages._decode_claude_event
iter_claude_events = _agent_claude_messages._iter_claude_events
collect_claude_text_blocks = _agent_claude_messages._collect_claude_text_blocks
claude_result_text = _agent_claude_messages._claude_result_text
claude_assistant_text = _agent_claude_messages._claude_assistant_text
collect_claude_message_candidates = (
    _agent_claude_messages._collect_claude_message_candidates
)
claude_last_message = _agent_claude_messages.claude_last_message
claude_command = _agent_claude.claude_command
claude_process_last_message = _agent_claude.claude_process_last_message
run_claude = _agent_claude.run_claude

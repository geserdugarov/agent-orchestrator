# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics sync connection-log tests."""

import unittest


from tests.analytics_sync_execution import (
    sync_capturing_logs as _sync_capturing_logs,
)


from tests.analytics_sync_reload import (
    reload_sync as _reload,
    sync_for_records as _sync_for_records,
)


from tests.analytics_sync_fakes import (
    FakeConnection as _FakeConnection,
)


from tests.analytics_sync_payloads import (
    sample_record as _sample_record,
)


class AnalyticsSyncConnectionLogTest(unittest.TestCase):
    """A successful connect is logged with a redacted URL so an operator
    sees the sync actually reached the database, and credentials never
    land in the operator's log.
    """

    def test_connect_emits_connected_log(self) -> None:
        with _sync_for_records(
            [_sample_record()],
            db_url="postgresql://u:secret@h:5432/db",
        ) as (_, analytics_sync):
            fake = _FakeConnection()
            _, log_lines = _sync_capturing_logs(self, analytics_sync, fake)
        joined = "\n".join(log_lines)
        self.assertIn("connecting to", joined)
        self.assertIn("connection established", joined)
        # The credential half of the URL must never appear; the redacted
        # form keeps the scheme + host + db so the operator can still
        # confirm which endpoint they hit.
        self.assertNotIn("secret", joined)
        self.assertNotIn("u:secret", joined)
        self.assertIn("***@h:5432", joined)

    def test_no_credentials_url_passes_through(self) -> None:
        _, analytics_sync = _reload()
        self.assertEqual(
            analytics_sync._redact_db_url("postgresql://h:5432/db"),
            "postgresql://h:5432/db",
        )

    def test_redact_db_url_strips_user_only(self) -> None:
        _, analytics_sync = _reload()
        self.assertIn(
            "***@h",
            analytics_sync._redact_db_url("postgresql://user@h/db"),
        )

    def test_password_query_param_is_redacted(self) -> None:
        # libpq accepts `postgresql://h/db?user=u&password=secret` --
        # netloc-only redaction would leak the password into the
        # operator's stdout. Both forms must collapse to ***.
        _, analytics_sync = _reload()
        redacted = analytics_sync._redact_db_url("postgresql://h/db?user=u&password=secret&sslmode=require")
        self.assertNotIn("secret", redacted)
        self.assertNotIn("user=u", redacted)
        # Non-credential params survive verbatim so the redacted URL
        # still tells the operator which SSL mode was configured.
        self.assertIn("sslmode=require", redacted)
        self.assertIn("password=", redacted)
        self.assertIn("***", redacted)

    def test_sslpassword_query_param_is_redacted(self) -> None:
        # `sslpassword` decrypts the SSL client key; same threat model
        # as `password` itself.
        _, analytics_sync = _reload()
        redacted = analytics_sync._redact_db_url("postgresql://h/db?sslpassword=ssl-secret")
        self.assertNotIn("ssl-secret", redacted)
        self.assertIn("sslpassword=", redacted)

    def test_query_params_are_case_insensitive(self) -> None:
        # libpq treats parameter names as case-insensitive; uppercase
        # spellings must redact identically so a `?PASSWORD=secret`
        # URL does not slip past the filter.
        _, analytics_sync = _reload()
        redacted = analytics_sync._redact_db_url("postgresql://h/db?PASSWORD=secret")
        self.assertNotIn("secret", redacted)

    def test_connect_log_redacts_query_password(self) -> None:
        # End-to-end regression: a query-string-password URL must not
        # leak the password into the connection log.
        with _sync_for_records(
            [_sample_record()],
            db_url="postgresql://h:5432/db?user=u&password=qs-secret",
        ) as (_, analytics_sync):
            fake = _FakeConnection()
            _, log_lines = _sync_capturing_logs(self, analytics_sync, fake)
        joined = "\n".join(log_lines)
        self.assertNotIn("qs-secret", joined)
        self.assertIn("connection established", joined)

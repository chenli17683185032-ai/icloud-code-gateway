from __future__ import annotations

import pytest

from icloud_gateway.imap_otp import _SHARED_IMAP_POOL


@pytest.fixture(autouse=True)
def reset_shared_imap_pool():
    """Keep the process-wide IMAP connection out of the next test.

    `ImapOtpReader` reuses a module-level pool, so a reader built with the
    default `reuse_connection=True` parks its fake socket there and the next
    test with matching credentials silently scanned the previous test's mailbox.
    """
    _SHARED_IMAP_POOL.close()
    yield
    _SHARED_IMAP_POOL.close()

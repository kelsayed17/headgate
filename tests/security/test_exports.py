# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The seams an auth or a middleware is written against are public, in ``headgate.security``."""

from __future__ import annotations

from headgate import BearerAuth
from headgate.security import CredentialCarrier, CredentialStamper


def test_the_credential_protocols_are_public() -> None:
    """Import the protocols from ``headgate.security``; headgate's own auths implement them."""
    auth = BearerAuth("token")
    assert isinstance(auth, CredentialStamper)
    assert isinstance(auth, CredentialCarrier)

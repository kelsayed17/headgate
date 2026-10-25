# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The README's error-handling sample: an application boundary mapping the ``ClientError`` tree onto a status."""

from http import HTTPStatus

from headgate import ClientConfigurationError, ClientError, ClientTimeoutError


def gateway_status(error: ClientError) -> HTTPStatus:
    """Return the status an API gateway answers with when a call to an upstream failed with *error*."""
    match error:
        case ClientTimeoutError():
            return HTTPStatus.GATEWAY_TIMEOUT  # the upstream was too slow, or the call's deadline passed
        case ClientConfigurationError():
            return HTTPStatus.SERVICE_UNAVAILABLE  # our own setup is wrong; an operator must act
        case _:
            return HTTPStatus.BAD_GATEWAY  # the upstream failed, refused us, or sent something unreadable

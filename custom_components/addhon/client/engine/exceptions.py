"""Native parser engine exceptions.

Defined here so the engine stays self-contained (it does not import its exceptions
from the transport layer or anywhere else). Minimal: the send-path uses two of them.
"""
from __future__ import annotations


class ApiError(Exception):
    """The hOn cloud rejected/did not confirm a command."""


class NoAuthenticationException(Exception):
    """Attempt to use the api without an authenticated session."""

"""Test-harness plugin: makes the upstream ``httpx`` name resolve to ``httpx2`` before any other plugin imports it.

``pytest-httpx`` patches ``httpx.AsyncHTTPTransport``; under ``httpx2`` that class lives at ``httpx2``, so the plugin
only mocks our clients if ``import httpx`` already means httpx2 when the plugin loads. ``alias_httpx()`` raises if
``httpx`` was imported first, hence a ``-p`` plugin (see ``addopts``) that runs before the entry-point plugins.

This is a TEST-ONLY concern. The library never imports ``httpx`` or calls the alias; ``tests/test_import_order.py``
proves that in fresh interpreters where the alias is not installed.
"""

import httpx2

httpx2.alias_httpx()

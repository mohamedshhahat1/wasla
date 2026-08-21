"""Test suite package.

The directories are packages rather than loose files so that two test modules
may share a basename - `tests/unit/test_conversation_projection.py` and its
PostgreSQL-backed counterpart, for instance. Without a package name to
qualify them, pytest's import of the second one collides with the first and
the entire suite fails to collect.
"""

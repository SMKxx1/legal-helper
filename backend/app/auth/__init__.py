"""CLM identity & access control (Phase 0): accounts, sessions, RBAC, audit.

Models live in ``app.auth.models`` (registered on the shared ``app.db.Base``). Password
hashing + session services and the FastAPI auth dependencies land in later P0 tasks.
"""

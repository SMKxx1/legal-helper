"""Auth: argon2id password hashing (``security.py``), bearer-token sessions (``sessions.py``),
and the FastAPI dependency that resolves a request to its signed-in ``User`` (``deps.py``).

The ``User``/``Session`` ORM models live in ``app.models``, alongside ``Review``/``LlmCall`` —
one flat schema module for the whole app (see plan §5).
"""

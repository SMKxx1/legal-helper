"""Dedicated single-replica scheduler worker (P1): the idempotency sweep + the async review-job
claimer under a Postgres advisory lock. The API stays web-only; this process OWNS the schedule.

Run as ``python -m app.worker`` (the container command override for the nda-worker app).
"""

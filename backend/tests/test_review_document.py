"""``GET /api/reviews/{id}/document`` — owner-only presigned download (plan §4.5).

This is the one real access-control boundary this phase adds, so it gets ONE dedicated test: the
owning user is redirected to a presigned URL, and a different signed-in user gets the exact same
404 a nonexistent review would (never a 403, which would confirm the review exists at all).
"""

from __future__ import annotations

import pytest

from app.storage import bucket


@pytest.fixture(autouse=True)
def _fake_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bucket "enabled" with a deterministic fake presigner — no real S3 config or network needed."""
    monkeypatch.setattr(bucket, "enabled", lambda: True)
    monkeypatch.setattr(
        bucket,
        "presigned_get_url",
        lambda key, **kw: f"https://bucket.example.com/{key}?sig=fake",
    )


def _seed_review_with_document(db, user):
    from app.models import Review

    review = Review(
        user_id=user.id,
        filename="Acme_NDA.docx",
        mode="quick",
        status="done",
        doc_object_key=f"users/{user.id}/reviews/seeded/Acme_NDA.docx",
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    return review


def test_owner_gets_redirected_to_presigned_url(client, seed_user, login, db):
    owner = seed_user(username="alice.tan", password="correct horse")
    review = _seed_review_with_document(db, owner)
    token = login(username="alice.tan", password="correct horse")

    resp = client.get(
        f"/api/reviews/{review.id}/document",
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert (
        resp.headers["location"]
        == f"https://bucket.example.com/{review.doc_object_key}?sig=fake"
    )


def test_another_user_gets_404_not_403(client, seed_user, login, db):
    owner = seed_user(username="alice.tan", password="correct horse")
    review = _seed_review_with_document(db, owner)
    seed_user(username="ben.lim", password="a different password")
    other_token = login(username="ben.lim", password="a different password")

    resp = client.get(
        f"/api/reviews/{review.id}/document",
        headers={"Authorization": f"Bearer {other_token}"},
        follow_redirects=False,
    )

    assert resp.status_code == 404


def test_no_stored_document_is_404(client, seed_user, login, db):
    """A review with no ``doc_object_key`` (bucket disabled, or storage failed fail-soft) —
    same 404, no download link to offer."""
    from app.models import Review

    owner = seed_user(username="alice.tan", password="correct horse")
    review = Review(
        user_id=owner.id, filename="No_Doc.docx", mode="quick", status="done"
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    token = login(username="alice.tan", password="correct horse")

    resp = client.get(
        f"/api/reviews/{review.id}/document",
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=False,
    )

    assert resp.status_code == 404

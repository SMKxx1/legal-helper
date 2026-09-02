"""Per-draft-version operations log (app.studio.oplog) — atomic apply/undo/redo over the DB.

Runs against the throwaway per-test SQLite DB (the ``db`` fixture) with drafts seeded via
``conftest_studio.seed_draft_version``. Pins the PLAN §3.6/§3.7 contract:

* every apply logs a ``studio_ops`` row carrying the full reversible op record, with ``seq``
  monotonic per version (and independent across versions);
* the draft blob and the op row commit atomically — any refusal leaves BOTH untouched (no
  orphan rows, no blob re-point);
* content-hash optimistic concurrency: a stale ``view_hash`` refuses with ``studio_stale_view``;
* undo restores the exact prior document (text + formatting, hash-verified) and marks the row
  ``undone``; redo re-applies the oldest undone op and clears the flag; empty stacks refuse typed;
* a new op truncates the redo tail: undone rows are marked ``dead`` (kept for audit, permanently
  un-redoable);
* batches (``apply_batch``) log one row per mapping — individually undoable — but commit
  all-or-nothing;
* blob writes are content-addressed (dedup by raw sha256) and never mutate an existing row's
  bytes, so published versions sharing a blob cannot be corrupted by draft edits.
"""

from __future__ import annotations

import hashlib

import pytest
from conftest_studio import draft_bytes, runs_doc, seed_draft_version, single_para_doc

from app.studio import oplog
from app.studio.docview import content_hash, extract_view
from app.studio.errors import (
    DraftBlobMissingError,
    NothingToRedoError,
    NothingToUndoError,
    OverlappingMappingsError,
    StaleViewError,
    TokenOverlapError,
)
from app.studio.tokenize_ops import OpRecord


def _view_text(db, version_id: str, locator: str = "body/p:0") -> str:
    return extract_view(draft_bytes(db, version_id)).find(locator).text


def _span(text: str, sub: str) -> tuple[int, int]:
    start = text.index(sub)
    return start, start + len(sub)


def test_apply_op_logs_row_and_repoints_blob(db):
    data = single_para_doc("Alpha beta gamma")
    version_id = seed_draft_version(db, data)
    view = extract_view(data)
    start, end = _span(view.segments[0].text, "beta")

    op = oplog.apply_op(
        db,
        version_id,
        locator="body/p:0",
        start=start,
        end=end,
        token_name="b",
        view_hash=view.content_hash,
        created_by="alice",
    )
    assert op.seq == 1
    assert op.created_by == "alice"
    assert op.undone is False and op.dead is False
    record = OpRecord.from_dict(op.op_json)
    assert record.replaced_text == "beta"
    assert record.prior_hash == view.content_hash
    new_bytes = draft_bytes(db, version_id)
    assert content_hash(new_bytes) == record.new_hash
    assert _view_text(db, version_id) == "Alpha {{b}} gamma"


def test_seq_is_monotonic_per_version_and_independent_across_versions(db):
    data = single_para_doc("one two three")
    v1 = seed_draft_version(db, data)
    v2 = seed_draft_version(db, data)
    h = content_hash(data)

    ops_v1 = []
    for token, word in (("a", "one"), ("b", "two"), ("c", "three")):
        text = _view_text(db, v1)
        start, end = _span(text, word)
        ops_v1.append(
            oplog.apply_op(
                db,
                v1,
                locator="body/p:0",
                start=start,
                end=end,
                token_name=token,
                view_hash=content_hash(draft_bytes(db, v1)),
            )
        )
    assert [op.seq for op in ops_v1] == [1, 2, 3]

    op_v2 = oplog.apply_op(
        db, v2, locator="body/p:0", start=0, end=3, token_name="x", view_hash=h
    )
    assert op_v2.seq == 1  # v2's timeline is its own


def test_stale_view_hash_refuses_and_writes_nothing(db):
    data = single_para_doc("Alpha beta")
    version_id = seed_draft_version(db, data)
    blob_id_before = db.get(
        __import__("app.models_v2", fromlist=["TemplateVersion"]).TemplateVersion,
        version_id,
    ).blob_id

    with pytest.raises(StaleViewError):
        oplog.apply_op(
            db,
            version_id,
            locator="body/p:0",
            start=0,
            end=5,
            token_name="a",
            view_hash="0" * 64,
        )
    assert oplog.history(db, version_id) == []
    version = db.get(
        __import__("app.models_v2", fromlist=["TemplateVersion"]).TemplateVersion,
        version_id,
    )
    assert version.blob_id == blob_id_before
    assert draft_bytes(db, version_id) == data


def test_undo_restores_prior_document_and_marks_row(db):
    data = runs_doc(("Alpha beta gamma", {"bold": True}))
    version_id = seed_draft_version(db, data)
    h0 = content_hash(data)
    oplog.apply_op(
        db,
        version_id,
        locator="body/p:0",
        start=6,
        end=10,
        token_name="b",
        view_hash=h0,
    )
    undone = oplog.undo(db, version_id)
    assert undone.seq == 1 and undone.undone is True and undone.dead is False
    assert content_hash(draft_bytes(db, version_id)) == h0
    assert _view_text(db, version_id) == "Alpha beta gamma"


def test_undo_redo_walk_the_timeline_lifo_then_fifo(db):
    data = single_para_doc("Alpha beta gamma delta")
    version_id = seed_draft_version(db, data)
    for token, word in (("a", "Alpha"), ("g", "gamma")):
        text = _view_text(db, version_id)
        start, end = _span(text, word)
        oplog.apply_op(
            db,
            version_id,
            locator="body/p:0",
            start=start,
            end=end,
            token_name=token,
            view_hash=content_hash(draft_bytes(db, version_id)),
        )
    assert _view_text(db, version_id) == "{{a}} beta {{g}} delta"

    assert oplog.undo(db, version_id).seq == 2  # newest first
    assert _view_text(db, version_id) == "{{a}} beta gamma delta"
    assert oplog.undo(db, version_id).seq == 1
    assert content_hash(draft_bytes(db, version_id)) == content_hash(data)

    assert oplog.redo(db, version_id).seq == 1  # oldest first
    assert _view_text(db, version_id) == "{{a}} beta gamma delta"
    assert oplog.redo(db, version_id).seq == 2
    assert _view_text(db, version_id) == "{{a}} beta {{g}} delta"


def test_empty_stacks_refuse_typed(db):
    data = single_para_doc("Alpha beta")
    version_id = seed_draft_version(db, data)
    with pytest.raises(NothingToUndoError) as undo_exc:
        oplog.undo(db, version_id)
    assert undo_exc.value.code == "studio_nothing_to_undo"
    with pytest.raises(NothingToRedoError) as redo_exc:
        oplog.redo(db, version_id)
    assert redo_exc.value.code == "studio_nothing_to_redo"


def test_new_op_truncates_redo_tail_marking_rows_dead(db):
    data = single_para_doc("Alpha beta gamma")
    version_id = seed_draft_version(db, data)
    for token, word in (("a", "Alpha"), ("b", "beta")):
        text = _view_text(db, version_id)
        start, end = _span(text, word)
        oplog.apply_op(
            db,
            version_id,
            locator="body/p:0",
            start=start,
            end=end,
            token_name=token,
            view_hash=content_hash(draft_bytes(db, version_id)),
        )
    oplog.undo(db, version_id)  # seq 2 -> undone

    text = _view_text(db, version_id)
    start, end = _span(text, "gamma")
    op3 = oplog.apply_op(
        db,
        version_id,
        locator="body/p:0",
        start=start,
        end=end,
        token_name="g",
        view_hash=content_hash(draft_bytes(db, version_id)),
    )
    assert op3.seq == 3
    states = [(op.seq, op.undone, op.dead) for op in oplog.history(db, version_id)]
    assert states == [(1, False, False), (2, True, True), (3, False, False)]

    with pytest.raises(NothingToRedoError):  # the dead row is permanently un-redoable
        oplog.redo(db, version_id)
    assert _view_text(db, version_id) == "{{a}} beta {{g}}"


def test_apply_batch_logs_one_row_per_mapping_each_undoable(db):
    data = single_para_doc("Between [COMPANY] and [RECIPIENT] on [DATE].")
    version_id = seed_draft_version(db, data)
    text = extract_view(data).segments[0].text

    def mapping(sub: str, token: str) -> dict:
        start, end = _span(text, sub)
        return {"locator": "body/p:0", "start": start, "end": end, "token_name": token}

    ops = oplog.apply_batch(
        db,
        version_id,
        [
            mapping("[COMPANY]", "company"),
            mapping("[RECIPIENT]", "recipient"),
            mapping("[DATE]", "date"),
        ],
        view_hash=content_hash(data),
        created_by="mapper",
    )
    assert [op.seq for op in ops] == [1, 2, 3]
    assert all(op.created_by == "mapper" for op in ops)
    assert (
        _view_text(db, version_id)
        == "Between {{company}} and {{recipient}} on {{date}}."
    )

    # records chain hash-to-hash in applied order, so each is individually undoable
    records = [OpRecord.from_dict(op.op_json) for op in ops]
    for earlier, later in zip(records, records[1:], strict=False):
        assert earlier.new_hash == later.prior_hash

    oplog.undo(db, version_id)  # undoes only the LAST mapping in applied order
    remaining = _view_text(db, version_id)
    assert remaining.count("{{") == 2
    assert content_hash(draft_bytes(db, version_id)) == records[-1].prior_hash


def test_apply_batch_is_all_or_nothing(db):
    data = single_para_doc("Keep {{locked}} and [FREE] here.")
    version_id = seed_draft_version(db, data)
    text = extract_view(data).segments[0].text
    good_start, good_end = _span(text, "[FREE]")
    bad_start, _ = _span(text, "{{locked}}")

    with pytest.raises(TokenOverlapError):
        oplog.apply_batch(
            db,
            version_id,
            [
                {
                    "locator": "body/p:0",
                    "start": good_start,
                    "end": good_end,
                    "token_name": "ok",
                },
                {
                    "locator": "body/p:0",
                    "start": bad_start,
                    "end": bad_start + 5,
                    "token_name": "bad",
                },
            ],
            view_hash=content_hash(data),
        )
    assert oplog.history(db, version_id) == []  # no orphan rows
    assert draft_bytes(db, version_id) == data  # no blob re-point

    with pytest.raises(OverlappingMappingsError):  # intra-batch overlap: same outcome
        oplog.apply_batch(
            db,
            version_id,
            [
                {
                    "locator": "body/p:0",
                    "start": good_start,
                    "end": good_end,
                    "token_name": "a",
                },
                {
                    "locator": "body/p:0",
                    "start": good_start + 1,
                    "end": good_end + 1,
                    "token_name": "b",
                },
            ],
            view_hash=content_hash(data),
        )
    assert oplog.history(db, version_id) == []


def test_empty_batch_is_a_no_op(db):
    data = single_para_doc("nothing to do")
    version_id = seed_draft_version(db, data)
    assert oplog.apply_batch(db, version_id, [], view_hash=content_hash(data)) == []
    assert oplog.history(db, version_id) == []
    assert draft_bytes(db, version_id) == data


def test_blob_writes_are_content_addressed_and_never_mutated(db):
    from app.models_v2 import DocumentBlob, TemplateVersion

    data = single_para_doc("Alpha beta")
    version_id = seed_draft_version(db, data)
    original_blob_id = db.get(TemplateVersion, version_id).blob_id
    original_sha = hashlib.sha256(data).hexdigest()

    oplog.apply_op(
        db,
        version_id,
        locator="body/p:0",
        start=0,
        end=5,
        token_name="a",
        view_hash=content_hash(data),
    )
    version = db.get(TemplateVersion, version_id)
    assert version.blob_id != original_blob_id  # re-pointed, not rewritten
    original_blob = db.get(DocumentBlob, original_blob_id)
    assert original_blob.bytes == data  # the old row is untouched
    assert original_blob.sha256 == original_sha
    new_blob = db.get(DocumentBlob, version.blob_id)
    assert new_blob.sha256 == hashlib.sha256(new_blob.bytes).hexdigest()

    # storing identical bytes again reuses the existing row (dedupe by raw sha)
    assert oplog._store_blob(db, data).id == original_blob_id


def test_missing_draft_refuses_typed(db):
    from app.models_v2 import TemplateVersion

    with pytest.raises(DraftBlobMissingError) as exc:
        oplog.apply_op(
            db,
            "no-such-version",
            locator="body/p:0",
            start=0,
            end=1,
            token_name="a",
            view_hash="x",
        )
    assert exc.value.code == "studio_draft_blob_missing"

    version_id = seed_draft_version(db, single_para_doc("hi there"))
    db.get(TemplateVersion, version_id).blob_id = None
    db.commit()
    with pytest.raises(DraftBlobMissingError):
        oplog.undo(db, version_id)


def test_history_returns_the_full_audit_trail_oldest_first(db):
    data = single_para_doc("Alpha beta gamma")
    version_id = seed_draft_version(db, data)
    for token, word in (("a", "Alpha"), ("b", "beta")):
        text = _view_text(db, version_id)
        start, end = _span(text, word)
        oplog.apply_op(
            db,
            version_id,
            locator="body/p:0",
            start=start,
            end=end,
            token_name=token,
            view_hash=content_hash(draft_bytes(db, version_id)),
        )
    oplog.undo(db, version_id)
    trail = oplog.history(db, version_id)
    assert [op.seq for op in trail] == [1, 2]
    assert [op.undone for op in trail] == [False, True]
    # rows survive with their op_json intact — a full audit log
    assert all(OpRecord.from_dict(op.op_json).token_name for op in trail)


def test_out_of_band_edit_between_ops_is_refused_on_undo(db):
    """An out-of-band blob swap breaks the hash chain — undo refuses instead of corrupting."""
    from app.models_v2 import TemplateVersion

    data = single_para_doc("Alpha beta")
    version_id = seed_draft_version(db, data)
    oplog.apply_op(
        db,
        version_id,
        locator="body/p:0",
        start=0,
        end=5,
        token_name="a",
        view_hash=content_hash(data),
    )
    # out-of-band: someone re-points the draft at entirely different bytes
    foreign = single_para_doc("Completely different document")
    foreign_blob = oplog._store_blob(db, foreign)
    db.get(TemplateVersion, version_id).blob_id = foreign_blob.id
    db.commit()

    with pytest.raises(StaleViewError):
        oplog.undo(db, version_id)
    assert draft_bytes(db, version_id) == foreign  # nothing was written

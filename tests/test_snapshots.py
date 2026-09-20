"""发布快照测试。

覆盖：
- 创建快照在单事务内锁定当前 watermark，固化 original/effective 文本、
  当时最新更正版本、完整更正链与缺口标记；
- 同一频道相同 idempotency_key 重试（含并发）返回同一快照；
  幂等键按频道隔离；
- 快照发布后新到字幕、追加更正、裁决缺口均不改变既有快照；
- 不存在的频道可生成 watermark=0 的空快照；
- 按 snapshot_id + after/limit 分页，next_after 正确，缺口与字幕统一计数；
- 未知快照返回 404；
- 存储层触发器禁止对三张快照表的 UPDATE/DELETE。
"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import service
from app.db import write_tx


def submit(client, channel, seq, revision, text="t"):
    resp = client.post(
        f"/channels/{channel}/fragments",
        json={"seq": seq, "revision": revision, "text": text},
    )
    assert resp.status_code == 200
    return resp.json()


def correct(client, channel, seq, expected_version, text, reason):
    return client.post(
        f"/channels/{channel}/finalized/{seq}/corrections",
        json={
            "expected_version": expected_version,
            "text": text,
            "reason": reason,
        },
    )


def decide_gap(client, channel, start_seq, end_seq):
    return client.post(
        f"/channels/{channel}/gaps",
        json={"start_seq": start_seq, "end_seq": end_seq},
    )


def make_snapshot(client, channel, key="k1"):
    resp = client.post(
        f"/channels/{channel}/snapshots", json={"idempotency_key": key}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def get_items(client, snapshot_id, **params):
    return client.get(f"/snapshots/{snapshot_id}/items", params=params)


# ---------- 创建与固化内容 ----------


def test_snapshot_freezes_original_effective_corrections_and_gaps(client):
    submit(client, "c1", 1, 1, "第一条")
    submit(client, "c1", 4, 1, "第四条")
    decide_gap(client, "c1", 2, 3)
    correct(client, "c1", 1, 0, "第一条-改", "错字")
    correct(client, "c1", 4, 0, "第四条-改", "错字")
    correct(client, "c1", 1, 1, "第一条-再改", "改口")

    snap = make_snapshot(client, "c1", "release-1")
    assert snap["watermark"] == 4
    assert snap["idempotent"] is False
    assert snap["snapshot_id"]
    assert snap["created_at"]

    body = get_items(client, snap["snapshot_id"]).json()
    assert body["snapshot_id"] == snap["snapshot_id"]
    assert body["channel"] == "c1"
    assert body["watermark"] == 4
    assert body["next_after"] is None
    items = body["items"]
    assert [(i["kind"], i["seq"]) for i in items] == [
        ("subtitle", 1),
        ("gap", 2),
        ("gap", 3),
        ("subtitle", 4),
    ]

    first = items[0]
    assert first["revision"] == 1
    assert first["original_text"] == "第一条"
    assert first["effective_text"] == "第一条-再改"
    assert first["correction_version"] == 2
    assert [(c["version"], c["text"], c["reason"]) for c in first["corrections"]] == [
        (1, "第一条-改", "错字"),
        (2, "第一条-再改", "改口"),
    ]
    assert all(c["created_at"] for c in first["corrections"])

    gap = items[1]
    assert gap["original_text"] is None
    assert gap["effective_text"] is None
    assert gap["correction_version"] is None
    assert gap["corrections"] == []
    assert gap["start_seq"] == 2
    assert gap["end_seq"] == 3

    fourth = items[3]
    assert fourth["original_text"] == "第四条"
    assert fourth["effective_text"] == "第四条-改"
    assert fourth["correction_version"] == 1
    assert [c["version"] for c in fourth["corrections"]] == [1]


def test_uncorrected_subtitle_uses_original_as_effective_version_zero(client):
    submit(client, "c1", 1, 1, "原文")
    snap = make_snapshot(client, "c1")

    item = get_items(client, snap["snapshot_id"]).json()["items"][0]
    assert item["original_text"] == "原文"
    assert item["effective_text"] == "原文"
    assert item["correction_version"] == 0
    assert item["corrections"] == []


def test_unknown_channel_creates_empty_watermark_zero_snapshot(client):
    snap = make_snapshot(client, "ghost", "k")
    assert snap["watermark"] == 0
    assert snap["idempotent"] is False

    body = get_items(client, snap["snapshot_id"]).json()
    assert body["channel"] == "ghost"
    assert body["watermark"] == 0
    assert body["items"] == []
    assert body["next_after"] is None

    # 空快照不创建频道行：频道依旧可正常从头提交
    submit(client, "ghost", 1, 1, "hi")
    # 既有空快照不受影响
    body = get_items(client, snap["snapshot_id"]).json()
    assert body["watermark"] == 0
    assert body["items"] == []


# ---------- 幂等 ----------


def test_idempotency_key_retry_returns_same_snapshot(client):
    submit(client, "c1", 1, 1, "第一条")
    first = make_snapshot(client, "c1", "same-key")
    assert first["idempotent"] is False

    # 水位线继续推进后重试旧键：仍返回同一快照
    submit(client, "c1", 2, 1, "第二条")
    correct(client, "c1", 1, 0, "第一条-改", "r")
    again = make_snapshot(client, "c1", "same-key")
    assert again["idempotent"] is True
    assert again["snapshot_id"] == first["snapshot_id"]
    assert again["watermark"] == 1  # 仍是首次创建时的水位线
    assert again["created_at"] == first["created_at"]

    body = get_items(client, first["snapshot_id"]).json()
    assert body["watermark"] == 1
    item = body["items"][0]
    assert item["effective_text"] == "第一条"
    assert item["correction_version"] == 0


def test_different_keys_create_different_snapshots(client):
    submit(client, "c1", 1, 1, "第一条")
    s1 = make_snapshot(client, "c1", "k-a")
    submit(client, "c1", 2, 1, "第二条")
    s2 = make_snapshot(client, "c1", "k-b")
    assert s1["snapshot_id"] != s2["snapshot_id"]
    assert s1["watermark"] == 1
    assert s2["watermark"] == 2


def test_idempotency_key_is_scoped_per_channel(client):
    submit(client, "a", 1, 1, "a1")
    submit(client, "b", 1, 1, "b1")
    submit(client, "b", 2, 1, "b2")

    sa = make_snapshot(client, "a", "shared-key")
    sb = make_snapshot(client, "b", "shared-key")
    assert sa["snapshot_id"] != sb["snapshot_id"]
    assert sa["watermark"] == 1
    assert sb["watermark"] == 2


def test_concurrent_same_key_creates_exactly_one_snapshot(client):
    submit(client, "c1", 1, 1, "第一条")

    def attempt(_):
        return service.create_snapshot("c1", "race-key")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(20)))

    ids = {r["snapshot_id"] for r in results}
    assert len(ids) == 1
    assert sum(1 for r in results if not r["idempotent"]) == 1
    assert sum(1 for r in results if r["idempotent"]) == 19


def test_snapshot_idempotency_validation_rejects_blank_key(client):
    assert client.post(
        "/channels/c1/snapshots", json={"idempotency_key": "   "}
    ).status_code == 422
    assert client.post(
        "/channels/c1/snapshots", json={"idempotency_key": ""}
    ).status_code == 422


# ---------- 快照不可变 ----------


def test_later_subtitles_corrections_and_gaps_never_change_snapshot(client):
    submit(client, "c1", 1, 1, "第一条")
    submit(client, "c1", 2, 1, "第二条")
    correct(client, "c1", 1, 0, "第一条-v1", "r1")
    snap = make_snapshot(client, "c1", "freeze")

    # 快照之后：追加更正、推进水位线、裁决缺口
    correct(client, "c1", 1, 1, "第一条-v2", "r2")
    submit(client, "c1", 5, 1, "第五条")
    decide_gap(client, "c1", 3, 4)

    body = get_items(client, snap["snapshot_id"]).json()
    assert body["watermark"] == 2
    assert [(i["kind"], i["seq"]) for i in body["items"]] == [
        ("subtitle", 1),
        ("subtitle", 2),
    ]
    first = body["items"][0]
    assert first["effective_text"] == "第一条-v1"
    assert first["correction_version"] == 1
    assert [c["version"] for c in first["corrections"]] == [1]

    # 实时定稿查询反映新状态，与快照互不影响
    live = client.get(
        "/channels/c1/finalized", params={"view": "effective"}
    ).json()
    assert live["watermark"] == 5
    assert live["items"][0]["text"] == "第一条-v2"
    assert live["items"][0]["correction_version"] == 2


def test_pagination_is_stable_across_pages_and_later_mutations(client):
    for seq in range(1, 7):
        submit(client, "c1", seq, 1, f"line{seq}")
    snap = make_snapshot(client, "c1", "paged")

    # 快照后再定稿内容，不能出现在任何分页里
    submit(client, "c1", 7, 1, "line7")

    seen = []
    after = 0
    pages = 0
    while True:
        body = get_items(
            client, snap["snapshot_id"], after=after, limit=2
        ).json()
        pages += 1
        seen.extend(body["items"])
        if body["next_after"] is None:
            break
        after = body["next_after"]

    assert pages == 3
    assert [i["seq"] for i in seen] == [1, 2, 3, 4, 5, 6]
    assert all(i["seq"] <= snap["watermark"] for i in seen)


def test_pagination_next_after_only_when_more_pages(client):
    for seq in range(1, 5):
        submit(client, "c1", seq, 1, f"line{seq}")
    snap = make_snapshot(client, "c1", "paged2")

    # limit 恰好整除总数：最后一页虽取满但无更多数据，next_after 为 null
    body = get_items(client, snap["snapshot_id"], after=0, limit=2).json()
    assert [i["seq"] for i in body["items"]] == [1, 2]
    assert body["next_after"] == 2

    body = get_items(client, snap["snapshot_id"], after=2, limit=2).json()
    assert [i["seq"] for i in body["items"]] == [3, 4]
    assert body["next_after"] is None

    # after 越过快照末尾
    body = get_items(client, snap["snapshot_id"], after=4, limit=2).json()
    assert body["items"] == []
    assert body["next_after"] is None


def test_pagination_counts_gaps_and_subtitles_uniformly(client):
    submit(client, "c1", 1, 1, "one")
    decide_gap(client, "c1", 2, 3)
    submit(client, "c1", 4, 1, "four")
    snap = make_snapshot(client, "c1", "gaps")

    body = get_items(client, snap["snapshot_id"], after=1, limit=2).json()
    assert [(i["kind"], i["seq"]) for i in body["items"]] == [
        ("gap", 2),
        ("gap", 3),
    ]
    assert body["next_after"] == 3


def test_empty_snapshot_pagination(client):
    snap = make_snapshot(client, "ghost", "empty")
    body = get_items(client, snap["snapshot_id"], after=0, limit=10).json()
    assert body["items"] == []
    assert body["next_after"] is None


def test_unknown_snapshot_returns_404(client):
    resp = get_items(client, "deadbeef" * 4)
    assert resp.status_code == 404
    assert resp.json()["error"] == "snapshot_not_found"


def test_snapshot_query_validation(client):
    snap = make_snapshot(client, "c1", "k")
    assert get_items(client, snap["snapshot_id"], after=-1).status_code == 422
    assert get_items(client, snap["snapshot_id"], limit=0).status_code == 422
    assert get_items(client, snap["snapshot_id"], limit=10001).status_code == 422


# ---------- 存储层不可变 ----------


def test_snapshot_tables_reject_update_and_delete_at_storage(conn):
    service.submit_fragment("c1", 1, 1, "原文")
    service.submit_correction("c1", 1, 0, "更正", "原因")
    snap = service.create_snapshot("c1", "immutable")
    sid = snap["snapshot_id"]

    for sql in (
        "UPDATE snapshots SET watermark = 99 WHERE snapshot_id = ?",
        "DELETE FROM snapshots WHERE snapshot_id = ?",
        "UPDATE snapshot_items SET effective_text = 'hacked' WHERE snapshot_id = ?",
        "DELETE FROM snapshot_items WHERE snapshot_id = ?",
        "UPDATE snapshot_corrections SET text = 'hacked' WHERE snapshot_id = ?",
        "DELETE FROM snapshot_corrections WHERE snapshot_id = ?",
    ):
        with pytest.raises(sqlite3.DatabaseError):
            with write_tx() as c:
                c.execute(sql, (sid,))

    # 内容原样
    body = service.get_snapshot_items(sid, after=0, limit=100)
    assert body["items"][0]["effective_text"] == "更正"

"""核心业务：片段接收、水位线推进、永久缺口裁决与追加式更正。

不变式（由单事务保证）：
- finalized 与 gaps 中每个频道的序号恰好是 1..watermark 的连续前缀，
  每个序号要么是定稿字幕，要么是永久缺口标记；无缺口的片段只存在于
  fragments 缓冲区；
- 同一序号只保留最高修订号，定稿后不再接受任何修改；
- 缺口裁决区间起点必须等于当前水位线下一位，且区间内不得存在已缓冲
  片段，校验失败不产生任何写入；
- 重复提交已完整裁决的同一区间幂等返回当前结果，部分重叠一律拒绝。

更正层（corrections）不变式：
- 只允许更正 finalized 中的定稿字幕；缺口序号与未定稿序号一律拒绝且不写入；
- corrections 表只追加：version 在每个 (channel, seq) 内从 1 起单调递增，
  UPDATE/DELETE 被触发器禁止，原始定稿文本永远不被改写；
- expected_version 必须等于当前更正版本（无更正时为 0），否则返回
  version_conflict；“读取当前版本 -> 校验 -> 追加下一版本”在同一个
  BEGIN IMMEDIATE 事务内完成，竞争请求只有一个能成功。
"""

import sqlite3

from .db import read_tx, write_tx


class CorrectionError(Exception):
    """更正 / 审计请求在业务前置条件上被拒绝；HTTP 层映射为 409/404。"""

    def __init__(self, error: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.error = error
        self.message = message
        self.status_code = status_code


def submit_fragment(channel: str, seq: int, revision: int, text: str) -> dict:
    """提交一个字幕片段，返回是否生效、最新水位线及本次新定稿的片段。"""
    with write_tx() as conn:
        conn.execute(
            "INSERT INTO channels (channel, watermark) VALUES (?, 0) "
            "ON CONFLICT(channel) DO NOTHING",
            (channel,),
        )
        watermark = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()["watermark"]

        applied = False
        reason = None
        if seq <= watermark:
            # 已定稿（或已裁决为缺口）的序号不可再修改
            reason = "already_finalized"
        else:
            row = conn.execute(
                "SELECT revision FROM fragments WHERE channel = ? AND seq = ?",
                (channel, seq),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO fragments (channel, seq, revision, text) "
                    "VALUES (?, ?, ?, ?)",
                    (channel, seq, revision, text),
                )
                applied = True
            elif revision > row["revision"]:
                conn.execute(
                    "UPDATE fragments SET revision = ?, text = ? "
                    "WHERE channel = ? AND seq = ?",
                    (revision, text, channel, seq),
                )
                applied = True
            else:
                reason = "stale_revision"

        newly = _consume_contiguous(conn, channel, watermark)
        new_watermark = newly[-1]["seq"] if newly else watermark

        return {
            "channel": channel,
            "seq": seq,
            "applied": applied,
            "reason": reason,
            "watermark": new_watermark,
            "finalized": newly,
        }


def _consume_contiguous(conn, channel: str, watermark: int) -> list:
    """从 watermark+1 起消费连续的缓冲片段：移入定稿表并更新水位线。

    遇到第一个缺失（缺口或未到达）即停止，保证不会越过缺口定稿；
    缺口补齐（片段到达或被裁决为永久缺口）的操作会在一次事务里
    释放整段连续片段。
    """
    newly = []
    seq = watermark + 1
    while True:
        row = conn.execute(
            "SELECT revision, text FROM fragments WHERE channel = ? AND seq = ?",
            (channel, seq),
        ).fetchone()
        if row is None:
            break
        conn.execute(
            "INSERT INTO finalized (channel, seq, revision, text) VALUES (?, ?, ?, ?)",
            (channel, seq, row["revision"], row["text"]),
        )
        conn.execute(
            "DELETE FROM fragments WHERE channel = ? AND seq = ?", (channel, seq)
        )
        newly.append(
            {
                "kind": "subtitle",
                "seq": seq,
                "revision": row["revision"],
                "text": row["text"],
                "start_seq": None,
                "end_seq": None,
            }
        )
        seq += 1
    if newly:
        conn.execute(
            "UPDATE channels SET watermark = ? WHERE channel = ?",
            (newly[-1]["seq"], channel),
        )
    return newly


def decide_gap(channel: str, start_seq: int, end_seq: int) -> dict:
    """裁决 [start_seq, end_seq] 为永久缺口。

    起点必须等于当前水位线下一位，区间内不得存在已缓冲片段。
    成功后在同一事务中记录被跳过序号、推进水位线，并继续释放紧随
    其后的连续缓冲片段。重复提交已完整裁决的同一区间幂等返回；
    与既有裁决部分重叠则拒绝。任何校验失败都不产生写入。
    """
    with write_tx() as conn:
        row = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()
        watermark = row["watermark"] if row else 0

        if start_seq <= watermark:
            return _historical_gap_response(conn, channel, start_seq, end_seq, watermark)

        if start_seq != watermark + 1:
            return _rejected(channel, watermark, "start_not_at_watermark")

        buffered = conn.execute(
            "SELECT seq FROM fragments WHERE channel = ? AND seq BETWEEN ? AND ? "
            "ORDER BY seq LIMIT 1",
            (channel, start_seq, end_seq),
        ).fetchone()
        if buffered is not None:
            # 区间内已有缓冲片段，必须等片段定稿而不能裁决丢失
            return _rejected(channel, watermark, "buffered_fragments")

        # 校验通过：以下写入与水位线推进在同一事务内完成
        conn.execute(
            "INSERT INTO channels (channel, watermark) VALUES (?, ?) "
            "ON CONFLICT(channel) DO UPDATE SET watermark = excluded.watermark",
            (channel, end_seq),
        )
        conn.execute(
            "INSERT INTO gap_decisions (channel, start_seq, end_seq, released_until) "
            "VALUES (?, ?, ?, ?)",
            (channel, start_seq, end_seq, end_seq),
        )
        conn.executemany(
            "INSERT INTO gaps (channel, seq, start_seq, end_seq) VALUES (?, ?, ?, ?)",
            [(channel, seq, start_seq, end_seq) for seq in range(start_seq, end_seq + 1)],
        )

        released = _consume_contiguous(conn, channel, end_seq)
        if released:
            conn.execute(
                "UPDATE gap_decisions SET released_until = ? "
                "WHERE channel = ? AND start_seq = ? AND end_seq = ?",
                (released[-1]["seq"], channel, start_seq, end_seq),
            )
        new_watermark = released[-1]["seq"] if released else end_seq

        return {
            "channel": channel,
            "applied": True,
            "idempotent": False,
            "reason": None,
            "watermark": new_watermark,
            "skipped": {"start_seq": start_seq, "end_seq": end_seq},
            "finalized": released,
        }


def _rejected(channel: str, watermark: int, reason: str) -> dict:
    """构造拒绝响应：不执行任何写入。"""
    return {
        "channel": channel,
        "applied": False,
        "idempotent": False,
        "reason": reason,
        "watermark": watermark,
        "skipped": None,
        "finalized": [],
    }


def _historical_gap_response(
    conn, channel: str, start_seq: int, end_seq: int, watermark: int
) -> dict:
    """处理起点位于水位线之前的提交：完全相同的区间幂等返回，否则拒绝。"""
    decision = conn.execute(
        "SELECT end_seq, released_until FROM gap_decisions "
        "WHERE channel = ? AND start_seq = ? AND end_seq = ?",
        (channel, start_seq, end_seq),
    ).fetchone()
    if decision is not None:
        released = _fetch_finalized(
            conn, channel, after=end_seq, upto=decision["released_until"]
        )
        return {
            "channel": channel,
            "applied": True,
            "idempotent": True,
            "reason": None,
            "watermark": watermark,
            "skipped": {"start_seq": start_seq, "end_seq": end_seq},
            "finalized": released,
        }

    overlap = conn.execute(
        "SELECT 1 FROM gap_decisions "
        "WHERE channel = ? AND start_seq <= ? AND end_seq >= ? LIMIT 1",
        (channel, end_seq, start_seq),
    ).fetchone()
    reason = "gap_overlap" if overlap is not None else "already_finalized"
    return _rejected(channel, watermark, reason)


def _fetch_finalized(conn, channel: str, after: int, upto: int) -> list:
    """读取 (after, upto] 区间内随某次裁决/提交新定稿的字幕片段。"""
    rows = conn.execute(
        "SELECT seq, revision, text FROM finalized "
        "WHERE channel = ? AND seq > ? AND seq <= ? ORDER BY seq",
        (channel, after, upto),
    ).fetchall()
    return [
        {
            "kind": "subtitle",
            "seq": row["seq"],
            "revision": row["revision"],
            "text": row["text"],
            "start_seq": None,
            "end_seq": None,
        }
        for row in rows
    ]


def get_finalized(
    channel: str, after: int = 0, limit: int = 1000, view: str = "original"
) -> dict:
    """查询频道定稿结果与当前水位线，按序混合返回字幕与缺口标记。

    after 用于增量拉取（按序号过滤，字幕与缺口标记统一计数）。
    view=original（默认）只返回不可变的原始定稿；view=effective 合并
    每个序号的最新更正文本并附带 correction_version，原始表不被读取改写。
    """
    with read_tx() as conn:
        row = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()
        watermark = row["watermark"] if row else 0

        if view == "original":
            return {"channel": channel, "watermark": watermark,
                    "items": _fetch_finalized_items(conn, channel, after, limit)}

        # effective：左连接每个序号的最新更正版本，未更正者 version 为 0
        rows = conn.execute(
            """
            SELECT base.seq AS seq, base.kind AS kind, base.revision AS revision,
                   base.text AS original_text, base.start_seq AS start_seq,
                   base.end_seq AS end_seq,
                   COALESCE(c.version, 0) AS correction_version,
                   COALESCE(c.text, base.text) AS effective_text
            FROM (
                SELECT f.seq AS seq, 'subtitle' AS kind, f.revision AS revision,
                       f.text AS text, NULL AS start_seq, NULL AS end_seq, 0 AS ord
                FROM finalized f
                WHERE f.channel = ? AND f.seq > ?
                UNION ALL
                SELECT g.seq AS seq, 'gap' AS kind, NULL AS revision, NULL AS text,
                       g.start_seq AS start_seq, g.end_seq AS end_seq, 1 AS ord
                FROM gaps g
                WHERE g.channel = ? AND g.seq > ?
            ) base
            LEFT JOIN corrections c
                   ON c.channel = ? AND c.seq = base.seq
                  AND c.version = (
                      SELECT MAX(c2.version) FROM corrections c2
                      WHERE c2.channel = ? AND c2.seq = base.seq
                  )
            ORDER BY base.seq, base.ord
            LIMIT ?
            """,
            (channel, after, channel, after, channel, channel, limit),
        ).fetchall()

        items = []
        page_max_version = 0
        for row in rows:
            if row["kind"] == "gap":
                items.append(
                    {
                        "kind": "gap",
                        "seq": row["seq"],
                        "revision": None,
                        "text": None,
                        "start_seq": row["start_seq"],
                        "end_seq": row["end_seq"],
                        "correction_version": None,
                    }
                )
            else:
                cv = row["correction_version"]
                page_max_version = max(page_max_version, cv)
                items.append(
                    {
                        "kind": "subtitle",
                        "seq": row["seq"],
                        "revision": row["revision"],
                        "text": row["effective_text"],
                        "start_seq": None,
                        "end_seq": None,
                        "correction_version": cv,
                    }
                )
        return {
            "channel": channel,
            "watermark": watermark,
            "view": "effective",
            "correction_version": page_max_version,
            "items": items,
        }


def _fetch_finalized_items(conn, channel: str, after: int, limit: int) -> list:
    """读取按序混合的字幕与缺口标记；effective 仅在 original 视图之外复用。"""
    rows = conn.execute(
        """
        SELECT f.seq AS seq, f.revision AS revision, f.text AS text,
               NULL AS start_seq, NULL AS end_seq, 0 AS ord
        FROM finalized f
        WHERE f.channel = ? AND f.seq > ?
        UNION ALL
        SELECT g.seq AS seq, NULL AS revision, NULL AS text,
               g.start_seq AS start_seq, g.end_seq AS end_seq, 1 AS ord
        FROM gaps g
        WHERE g.channel = ? AND g.seq > ?
        ORDER BY seq, ord LIMIT ?
        """,
        (channel, after, channel, after, limit),
    ).fetchall()
    items = []
    for row in rows:
        if row["start_seq"] is None:
            items.append(
                {
                    "kind": "subtitle",
                    "seq": row["seq"],
                    "revision": row["revision"],
                    "text": row["text"],
                    "start_seq": None,
                    "end_seq": None,
                }
            )
        else:
            items.append(
                {
                    "kind": "gap",
                    "seq": row["seq"],
                    "revision": None,
                    "text": None,
                    "start_seq": row["start_seq"],
                    "end_seq": row["end_seq"],
                }
            )
    return items


def submit_correction(
    channel: str, seq: int, expected_version: int, text: str, reason: str
) -> dict:
    """对已定稿字幕原子追加一条更正。

    仅当 (channel, seq) 存在于 finalized（不是缺口、不是未定稿序号），
    且 expected_version 等于当前更正版本（无更正为 0）时才写入；
    任何前置条件不满足都先于写入返回，corrections 表与 finalized 表
    均保持不变。
    """
    with write_tx() as conn:
        finalized = conn.execute(
            "SELECT 1 FROM finalized WHERE channel = ? AND seq = ?",
            (channel, seq),
        ).fetchone()
        if finalized is None:
            gap = conn.execute(
                "SELECT 1 FROM gaps WHERE channel = ? AND seq = ?",
                (channel, seq),
            ).fetchone()
            if gap is not None:
                raise CorrectionError(
                    "seq_is_gap",
                    f"序号 {seq} 已裁决为永久缺口，不能更正",
                )
            raise CorrectionError(
                "not_finalized",
                f"序号 {seq} 尚未定稿，只有已定稿字幕允许更正",
            )

        current = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM corrections "
            "WHERE channel = ? AND seq = ?",
            (channel, seq),
        ).fetchone()["v"]
        if expected_version != current:
            raise CorrectionError(
                "version_conflict",
                f"expected_version={expected_version} 与当前更正版本 {current} 不一致",
            )

        next_version = current + 1
        try:
            row = conn.execute(
                "INSERT INTO corrections (channel, seq, version, text, reason) "
                "VALUES (?, ?, ?, ?, ?) "
                "RETURNING created_at",
                (channel, seq, next_version, text, reason),
            ).fetchone()
        except sqlite3.IntegrityError as exc:  # 极端竞争下的最后防线
            raise CorrectionError(
                "version_conflict",
                f"更正版本 {next_version} 已被并发请求占用",
            ) from exc

        return {
            "channel": channel,
            "seq": seq,
            "correction_version": next_version,
            "text": text,
            "reason": reason,
            "created_at": row["created_at"],
        }


def list_corrections(channel: str, seq: int) -> dict:
    """返回某定稿序号的更正审计记录（按 version 升序）。

    缺口或未定稿序号返回 404——这类序号不存在可审计的更正链。
    """
    with read_tx() as conn:
        finalized = conn.execute(
            "SELECT 1 FROM finalized WHERE channel = ? AND seq = ?",
            (channel, seq),
        ).fetchone()
        if finalized is None:
            raise CorrectionError(
                "not_finalized",
                f"序号 {seq} 不存在已定稿字幕，没有更正审计记录",
                status_code=404,
            )

        rows = conn.execute(
            "SELECT version, text, reason, created_at FROM corrections "
            "WHERE channel = ? AND seq = ? ORDER BY version",
            (channel, seq),
        ).fetchall()
        return {
            "channel": channel,
            "seq": seq,
            "current_version": rows[-1]["version"] if rows else 0,
            "items": [
                {
                    "channel": channel,
                    "seq": seq,
                    "version": row["version"],
                    "text": row["text"],
                    "reason": row["reason"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }

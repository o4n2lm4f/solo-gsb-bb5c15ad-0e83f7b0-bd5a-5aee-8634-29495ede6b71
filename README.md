# Live Subtitle Finalizer — 直播字幕定稿服务

基于 FastAPI + SQLite 的直播字幕定稿服务。客户端按频道提交带**序号 / 修订号 / 文本**的字幕片段，服务将乱序到达的片段放入缓冲区，只从序号 1 起按**连续前缀**推进水位线并生成**不可再修改**的定稿字幕。操作员可对确认永久丢失的序号区间发起**缺口裁决**，服务在同一事务中记录缺口并释放紧随其后的连续缓冲片段。定稿之后如需改字，可通过**追加式更正层**提交不可变的更正版本——原始定稿永远保留，下游可在原始视图与生效视图之间选择。此外，服务支持对频道**当前时刻发布快照**：在单个事务内锁定水位线并固化当时全部定稿、缺口与更正版本，供下游分批拉取同一时刻的稳定字幕；快照发布后，后续字幕、更正或缺口都不会改变它。

## 语义规则

- 片段可乱序到达；同一序号只接受**更高修订号**（更低或相等的修订号被拒绝）。
- 水位线 = 已定稿的最大连续序号（定稿字幕与永久缺口共同构成连续前缀）。遇到缺口即停，**不会越过缺口**定稿。
- 缺口被补齐的提交会在**同一事务**内一次释放整段连续片段。
- 序号 ≤ 当前水位线的提交一律不生效（定稿不可变，已裁决为缺口的序号同样不可再提交）。
- 所有提交 / 裁决在 `BEGIN IMMEDIATE` 事务内串行执行，并发下不会重复定稿或跳过缺口。

### 永久缺口裁决规则

- 区间 `[start_seq, end_seq]`（含两端）的**起点必须等于当前水位线下一位**；
- 区间内**不得存在已缓冲片段**（有缓冲说明片段只是迟到，应等待定稿而不是裁决丢失）；
- 任一校验失败返回 `applied=false` 且**不产生任何写入**；
- 裁决成功：同一事务内记录被跳过序号、推进水位线到区间终点，并继续释放紧随其后的连续缓冲片段；
- 重复提交**已完整裁决的同一区间**幂等返回当前结果（`idempotent=true`）；与既有裁决**部分重叠**则拒绝（`gap_overlap`）。

### 追加式更正规则

- 定稿不可改写；需要修改已定稿字幕时只能**追加**一条更正，原始文本永久保留。
- 仅**已定稿字幕**可被更正；未定稿序号与永久缺口序号一律拒绝（409）且不写入。
- 每个序号的更正版本从 1 起单调递增；提交时必须携带 `expected_version`，无更正时为 `0`，否则须等于当前最新版本。
- 版本不匹配（含并发竞争失败）返回 409 `version_conflict`；校验与追加在同一 `BEGIN IMMEDIATE` 事务内原子完成。
- 定稿查询默认 `view=original`（响应结构不变）；`view=effective` 合并最新更正文本并返回每条字幕的 `correction_version`。
- 更正历史可通过审计接口按版本升序查询。

### 发布快照规则

- 需要让下游分批拉取**同一时刻**的稳定字幕时，对频道创建快照：服务在**单个 `BEGIN IMMEDIATE` 事务**内锁定频道当前水位线，把 `1..watermark` 的定稿字幕与缺口标记、每条字幕的 **original / effective 文本**、快照时最新 `correction_version` 以及各序号的**完整更正链**（含原更正落库时间）全部固化到独立的快照表。
- 快照一经发布即**不可变**：之后新到的字幕、追加的更正或新裁决的缺口只写入工作表，永远不会改变既有快照内容（快照表在存储层用触发器禁止 `UPDATE` / `DELETE`）。
- 创建请求必须携带 `idempotency_key`：同一频道内相同键的**任意重试（含并发请求）返回同一快照**（相同 `snapshot_id`、`watermark` 与首次创建时间），响应中 `idempotent=true`；幂等键按频道隔离，不同频道使用相同键互不影响。
- **不存在的频道也可以创建快照**：固化为 `watermark=0`、条目为空的空快照，且不会因此创建频道。
- 快照内容通过 `snapshot_id` + `after` / `limit` 分页拉取，字幕与缺口标记统一按序号计数；`next_after` 指向下一页，到快照末尾时为 `null`。

## 快速启动（单条命令）

```bash
docker compose up --build
```

服务监听 **8000** 端口（容器内 `uvicorn --host 0.0.0.0 --port 8000`，Compose 映射 `8000:8000`），启动时自动初始化数据库（建表幂等，包括更正层新表与触发器）。

- 交互式 API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health
- 提交片段：`POST /channels/{channel}/fragments`
- 缺口裁决：`POST /channels/{channel}/gaps`
- 定稿查询（`view=original|effective`）：`GET /channels/{channel}/finalized`
- 追加更正：`POST /channels/{channel}/finalized/{seq}/corrections`
- 更正审计（版本升序）：`GET /channels/{channel}/finalized/{seq}/corrections`
- 创建发布快照：`POST /channels/{channel}/snapshots`
- 分页拉取快照内容：`GET /snapshots/{snapshot_id}/items`

## 配置

| 环境变量   | 默认值                 | 说明               |
| ---------- | ---------------------- | ------------------ |
| `DB_PATH`  | `/data/subtitles.db`   | SQLite 数据库文件路径 |

Compose 默认将 `/data` 挂载到命名卷 `subtitles-data`，容器重启数据不丢失。端口映射可在 `docker-compose.yml` 的 `ports` 中调整。

## API

### 提交字幕片段

```bash
curl -X POST http://localhost:8000/channels/news/fragments \
  -H 'Content-Type: application/json' \
  -d '{"seq": 1, "revision": 1, "text": "各位观众晚上好"}'
```

响应（`applied` 表示本次提交是否生效，`watermark` 为处理后的最新水位线，`finalized` 为本次新定稿的片段）：

```json
{
  "channel": "news",
  "seq": 1,
  "applied": true,
  "reason": null,
  "watermark": 1,
  "finalized": [{"seq": 1, "revision": 1, "text": "各位观众晚上好"}]
}
```

`applied=false` 时 `reason` 取值：

- `stale_revision` — 修订号不高于已缓冲的同序号片段；
- `already_finalized` — 该序号已定稿（或已裁决为永久缺口），不可再修改。

### 裁决永久缺口

当确认某些序号永久丢失（例如推流中断），操作员可对缺失区间发起裁决：

```bash
curl -X POST http://localhost:8000/channels/news/gaps \
  -H 'Content-Type: application/json' \
  -d '{"start_seq": 3, "end_seq": 4}'
```

裁决成功的响应（`watermark` 为裁决后的新水位线，`skipped` 为本次跳过区间，`finalized` 为随裁决一并释放的连续缓冲片段）：

```json
{
  "channel": "news",
  "applied": true,
  "idempotent": false,
  "reason": null,
  "watermark": 6,
  "skipped": {"start_seq": 3, "end_seq": 4},
  "finalized": [
    {"kind": "subtitle", "seq": 5, "revision": 1, "text": "第五条字幕", "start_seq": null, "end_seq": null},
    {"kind": "subtitle", "seq": 6, "revision": 1, "text": "第六条字幕", "start_seq": null, "end_seq": null}
  ]
}
```

重复提交**完全相同**的已裁决区间时，幂等返回原裁决结果，`idempotent` 为 `true`。`applied=false` 时 `reason` 取值：

- `start_not_at_watermark` — 区间起点不是当前水位线下一位（不能跳过未裁决的序号）；
- `buffered_fragments` — 区间内存在已缓冲片段，应等待其定稿；
- `already_finalized` — 区间已完全定稿且不匹配任何缺口；
- `gap_overlap` — 与既有缺口裁决部分重叠（完全相同的区间才允许幂等提交）。

请求体校验（如 `start_seq < 1`、`end_seq < start_seq`）返回 HTTP 422。

### 查询频道定稿

定稿查询按序号升序**混合返回字幕与缺口标记**，`kind` 字段供下游区分丢失内容。通过 `view` 参数在原始文本与更正后文本之间选择，**默认 `original`，响应结构与旧版完全一致**：

```bash
curl 'http://localhost:8000/channels/news/finalized?after=0&limit=1000&view=original'
```

```json
{
  "channel": "news",
  "watermark": 6,
  "items": [
    {"kind": "subtitle", "seq": 1, "revision": 1, "text": "第一条字幕", "start_seq": null, "end_seq": null},
    {"kind": "subtitle", "seq": 2, "revision": 1, "text": "第二条字幕", "start_seq": null, "end_seq": null},
    {"kind": "gap",      "seq": 3, "revision": null, "text": null, "start_seq": 3, "end_seq": 4},
    {"kind": "gap",      "seq": 4, "revision": null, "text": null, "start_seq": 3, "end_seq": 4},
    {"kind": "subtitle", "seq": 5, "revision": 1, "text": "第五条字幕", "start_seq": null, "end_seq": null},
    {"kind": "subtitle", "seq": 6, "revision": 1, "text": "第六条字幕", "start_seq": null, "end_seq": null}
  ]
}
```

- `kind=subtitle`：定稿字幕，取 `revision` / `text`；
- `kind=gap`：永久缺口标记，`start_seq` / `end_seq` 标明所属裁决区间（同一区间内每个被跳过序号各占一条标记）；
- `after` 用于增量拉取，对字幕与缺口标记统一按序号计数。

`view=effective` 时，每个字幕条目合并其**最新更正文本**，并附带该序号的当前 `correction_version`（`0` = 未更正；缺口条目为 `null`）；顶层 `correction_version` 是本页返回范围内出现过的最大更正版本。原始定稿不受任何影响：

```bash
curl 'http://localhost:8000/channels/news/finalized?view=effective'
```

```json
{
  "channel": "news",
  "watermark": 6,
  "view": "effective",
  "correction_version": 1,
  "items": [
    {"kind": "subtitle", "seq": 1, "revision": 1, "text": "第一条字幕（已更正）", "start_seq": null, "end_seq": null, "correction_version": 1},
    {"kind": "gap",      "seq": 3, "revision": null, "text": null, "start_seq": 3, "end_seq": 4, "correction_version": null}
  ]
}
```

非法的 `view` 值返回 HTTP 422。

### 追加更正（不可变更正层）

定稿字幕如有错字 / 翻译问题，可对**单个已定稿序号**追加更正。更正不修改任何旧数据，只向 `corrections` 表追加一个新版本：

```bash
curl -X POST http://localhost:8000/channels/news/finalized/1/corrections \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 0, "text": "各位观众晚上好", "reason": "原稿漏字"}'
```

请求字段：

- `expected_version`（≥0，必填）：客户端认为的该序号**当前更正版本**。没有更正时当前版本为 `0`，首个更正必须提交 `0`；此后必须等于最新版本号；
- `text`（非空字符串）：更正后的字幕文本；
- `reason`（非空字符串）：更正原因，随审计记录永久保留。

成功响应（原子追加下一版本，返回该序号新的更正版本）：

```json
{
  "channel": "news",
  "seq": 1,
  "correction_version": 1,
  "text": "各位观众晚上好",
  "reason": "原稿漏字",
  "created_at": "2026-09-20T12:00:00.000Z"
}
```

规则：

- **仅允许更正已定稿字幕**：未定稿序号（尚未进入连续前缀）或已裁决为永久缺口的序号一律拒绝，返回 HTTP 409 且**不产生任何写入**；
- **乐观版本控制**：`expected_version` 与当前版本不一致时返回 HTTP 409 `version_conflict`，客户端应重新拉取生效视图 / 审计记录后携带新版本重试；
- 「读取当前版本 → 校验 → 追加下一版本」在单个 `BEGIN IMMEDIATE` 事务内完成，并发竞争请求中只有一个成功，其余全部得到版本冲突；
- 更正只追加：每个 `(channel, seq)` 的版本号从 1 起严格递增、无空洞；存储层触发器禁止 `UPDATE` / `DELETE`，**原始定稿文本永远不被改写**，随时可用 `view=original` 取回。

409 响应体（更正接口）：

| `error`             | 含义                                                    |
| ------------------- | ------------------------------------------------------- |
| `not_finalized`     | 序号未定稿（频道不存在 / 序号还在缓冲区或水位线之外）   |
| `seq_is_gap`        | 序号已裁决为永久缺口，缺口不可更正                      |
| `version_conflict`  | `expected_version` 与当前更正版本不一致（竞争失败）     |

```json
{"error": "version_conflict", "message": "expected_version=0 与当前更正版本 1 不一致"}
```

### 查询更正审计

按更正版本**升序**返回某定稿序号的全部更正记录（不可变、可追溯）：

```bash
curl http://localhost:8000/channels/news/finalized/1/corrections
```

```json
{
  "channel": "news",
  "seq": 1,
  "current_version": 2,
  "items": [
    {"channel": "news", "seq": 1, "version": 1, "text": "各位观众晚上好", "reason": "原稿漏字", "created_at": "2026-09-20T12:00:00.000Z"},
    {"channel": "news", "seq": 1, "version": 2, "text": "各位观众朋友们晚上好", "reason": "主持人改口", "created_at": "2026-09-20T12:05:00.000Z"}
  ]
}
```

没有更正的定稿序号返回 `current_version=0` 与空 `items`；缺口 / 未定稿 / 不存在的序号返回 HTTP 404。

### 创建发布快照

对频道当前时刻的定稿结果发布不可变快照。快照在**单个事务**内锁定当前水位线，并固化 `1..watermark` 的全部定稿字幕、缺口标记与更正链：

```bash
curl -X POST http://localhost:8000/channels/news/snapshots \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key": "2026-09-20-broadcast-1"}'
```

请求字段：

- `idempotency_key`（非空字符串，≤200 字符，必填）：客户端生成的幂等键。**同一频道**内重复携带相同键的请求返回同一快照；不同频道互不影响。

成功响应（首次创建 `idempotent=false`）：

```json
{
  "snapshot_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
  "channel": "news",
  "idempotency_key": "2026-09-20-broadcast-1",
  "watermark": 6,
  "idempotent": false,
  "created_at": "2026-09-20T12:00:00.000Z"
}
```

规则：

- 相同 `idempotency_key` 的重试（包括并发竞争中的落败请求）得到**完全相同**的响应，`idempotent=true`，`watermark` / `created_at` 均保持首次创建时的值——即使频道水位线已继续推进；
- 不存在的频道也可创建快照，返回 `watermark=0` 的空快照；
- 幂等键为空白字符串或超长时返回 HTTP 422。

### 分页拉取快照内容

按 `snapshot_id` 拉取固化内容，`after` / `limit` 语义与定稿查询一致（字幕与缺口标记统一按序号计数，`limit` 默认 1000、上限 10000）：

```bash
curl 'http://localhost:8000/snapshots/a1b2c3d4e5f60718293a4b5c6d7e8f90/items?after=0&limit=2'
```

```json
{
  "snapshot_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
  "channel": "news",
  "watermark": 6,
  "next_after": 2,
  "items": [
    {
      "kind": "subtitle",
      "seq": 1,
      "revision": 1,
      "original_text": "第一条字幕",
      "effective_text": "第一条字幕（快照时最新更正）",
      "correction_version": 1,
      "corrections": [
        {"version": 1, "text": "第一条字幕（快照时最新更正）", "reason": "原稿漏字", "created_at": "2026-09-20T11:58:00.000Z"}
      ],
      "start_seq": null,
      "end_seq": null
    },
    {
      "kind": "subtitle",
      "seq": 2,
      "revision": 1,
      "original_text": "第二条字幕",
      "effective_text": "第二条字幕",
      "correction_version": 0,
      "corrections": [],
      "start_seq": null,
      "end_seq": null
    }
  ]
}
```

条目字段：

- `kind=subtitle`：`original_text` 为固化的原始定稿文本；`effective_text` 为快照时刻的生效文本（未更正时与原文相同）；`correction_version` 为快照时最新更正版本（`0` = 快照时未更正）；`corrections` 为**快照时刻冻结的该序号全部更正版本**（按 `version` 升序，含原更正的 `created_at`），空数组表示快照时无更正；
- `kind=gap`：缺口标记，文本 / 更正字段为 `null` 或空列表，`start_seq` / `end_seq` 标明所属裁决区间；
- `next_after`：本页取满且后面还有数据时，为下一页应使用的 `after`（本页最后一条的 `seq`）；已到快照末尾（含本页不足 `limit`）时为 `null`。

分页是**稳定**的：对同一快照反复翻页始终得到发布时刻的内容与恒定的 `watermark`，频道后续的字幕、更正与缺口裁决不会影响任何一页。快照不存在时返回 HTTP 404 `snapshot_not_found`；`after<0`、`limit` 越界返回 HTTP 422。

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
pytest                     # 含乱序、修订竞争、缺口裁决、幂等/重叠、更正版本冲突、视图合并、审计、并发事务、快照固化/幂等/分页/不可变等用例
uvicorn app.main:app --reload --port 8000
```

## 实现要点

- **事务化提交 / 裁决 / 更正**：`app/service.py` 中「校验 → 写入」在单个 `BEGIN IMMEDIATE` 事务内完成；缺口裁决与更正的所有校验先于任何写入执行，失败即只读返回，不产生副作用。`finalized` / `gaps` 表主键 `(channel, seq)`、`corrections` 表主键 `(channel, seq, version)` 分别作为防重复定稿 / 裁决 / 版本号重复的最后防线。
- **连续前缀推进**：`_consume_contiguous` 从给定水位线的下一位起逐条消费缓冲区，遇到第一个缺失即停止；片段补齐与缺口裁决都复用该逻辑。
- **缺口存储**：`gap_decisions` 记录每次裁决区间及其当时释放到的序号（用于幂等返回原结果），`gaps` 按被跳过序号逐行落表，定稿查询用 `UNION ALL` 按序合并字幕与缺口标记。
- **追加式更正层**：`corrections(channel, seq, version, text, reason, created_at)` 仅通过 `INSERT` 追加，`(channel, seq)` 外键引用 `finalized`——缺口或未定稿序号在存储层即无法插入；版本号按序号从 1 递增，`expected_version` 乐观并发控制 + `BEGIN IMMEDIATE` 保证竞争下唯一成功；`corrections_no_update` / `corrections_no_delete` 触发器在存储层禁止任何改写与删除，原始定稿保持不可变。生效视图用相关子查询连接每个序号的最新版本（`MAX(version)`）合并文本，审计查询按 `version` 升序返回完整历史。
- **发布快照**：`snapshots(snapshot_id, channel, idempotency_key, watermark, created_at)` 对 `(channel, idempotency_key)` 建唯一约束；创建路径在单个写事务内先按幂等键查重，未命中则读取（经 `BEGIN IMMEDIATE` 锁定的）当前水位线——频道不存在时取 0 且不创建频道行——随后用两条 `INSERT ... SELECT` 一次性把 `1..watermark` 的定稿/缺口固化进 `snapshot_items`（左连接当时 `MAX(version)` 的更正，同时落 `original_text` / `effective_text` / `correction_version`），把当时全部更正版本连同原 `created_at` 复制进 `snapshot_corrections`。快照数据与工作数据物理隔离，三张快照表各配 `*_no_update` / `*_no_delete` 触发器，既有快照不受后续定稿、更正与缺口裁决影响。分页查询走快照表，多取 `limit+1` 条判断是否还有下一页并给出 `next_after`，缺失快照返回 404 `snapshot_not_found`。
- **存储**：`channels`（水位线）、`fragments`（待定稿缓冲）、`finalized`（定稿，不可变）、`gap_decisions` / `gaps`（永久缺口）、`corrections`（追加式更正）、`snapshots` / `snapshot_items` / `snapshot_corrections`（发布快照，不可变）九张表；WAL 模式 + `busy_timeout`。建表语句幂等（`CREATE TABLE/TRIGGER IF NOT EXISTS`），已部署实例重启后自动补建新表与触发器，原有数据与接口行为不受影响。
- **容器化**：`docker compose up --build` 一键启动，数据库落在命名卷 `subtitles-data`；服务固定监听 **8000** 端口。

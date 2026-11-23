# 盘中快照接口前置版本协商（Pre-version Negotiation）实施需求

> 适用接口：`GET /api/v1/intraday/snapshot`
> 目标仓库：`d:\rxlib_api`
> 状态：待实施
> 本文同时给出可直接复制粘贴的完整代码与逐文件改动。
> 建议落位：`d:\rxlib_api\docs\etag-prenegotiation-requirements.md`

---

## 1. 背景与问题

当前 ETag/304 由统一中间件 `app/middleware.py` 在**业务处理完成之后**，对完整响应体计算 MD5 生成：

```
请求进入 → 鉴权/限流 → 全量查询 stock_instant 表（约 1s）
         → pandas 归一化 5000+ 行 → 中间件才算 MD5、决定是否 304
```

因此即使最终返回 **304**，最重的两步——**全量 SQL 查询**与 **DataFrame 归一化**——依然每次都执行。实测重复点击"查询当日名单"：

- 返回 304（无包体），但耗时仍约 **1.0 ~ 1.3s**，与返回 200 全量几乎一致；
- 304 只节省了出口带宽与序列化，**没有节省数据库与 CPU**。

需要把"版本是否变化"的判断**前移到全量查询之前**：版本未变时，只用一条轻量聚合查询即返回 304，跳过全量查库与 pandas 处理。

---

## 2. 目标与非目标

### 目标

1. 重复请求且数据未更新时，服务端只执行一条轻量版本查询（`MAX(tick_time)`），命中即返回 304，**不执行全量查询、不做 pandas 归一化**。
2. 200 响应携带稳定、可复用的 ETag，语义为"`(trade_date, 当日最新 tick)` 版本"。
3. 对客户端透明：现有客户端（已实现 ETag/304 + `If-None-Match`）无需改动即可享受提速。
4. 修复一个既有正确性问题：当前返回体中的 `tick_time` 取自**整月**最新 tick，查询历史日时会返回错误 tick；应改为取**所查询当日**的最新 tick。

### 非目标（本次不做）

- 不做跨用户的服务端共享缓存 / 请求合并（singleflight）。
- 不引入 Redis 缓存版本（先用轻量 SQL，必要时后续再加）。
- 不放开 CDN/共享代理缓存（仍保持 `private`）。
- 不改造除快照外的其他 GET 端点。

---

## 3. 总体设计

### 3.1 版本标识

以 **"交易日 + 该日最新 tick_time"** 作为快照版本：

- 数据写入方每刷新一批快照，`tick_time` 推进；
- 同一交易日内任意股票数据更新 → 当日 `MAX(tick_time)` 变化 → ETag 变化；
- 不同交易日天然不同。

ETag 仍采用弱 ETag 形式（与中间件现有风格一致）：

```
W/"<md5(trade_date|tick_time)>"
```

不对整包 body 算哈希（那正是当前性能问题的来源），改为对版本字符串算哈希，成本 O(1)。

### 3.2 处理流程（改造后）

```
请求进入 → 鉴权 / 限流
   → 解析 day（缺省=今天）
   → discover_instant_table（轻量，INFORMATION_SCHEMA）
   → SELECT MAX(tick_time) WHERE trade_date = day   ← 轻量聚合
   → 用 (day, tick) 生成 ETag
       ├─ 命中 If-None-Match：立即 304（无 body），结束
       └─ 未命中：全量查询 + pandas 归一化 → 200 + ETag
```

### 3.3 代码落点

| 文件 | 改动 |
|---|---|
| `app/sql_queries.py` | 新增按日轻量版本查询 `instant_day_tick` |
| `app/services/intraday_service.py` | 新增版本解析/ETag 工具；重写 `get_snapshot` 支持条件协商；修复 tick 口径 |
| `app/routers/v1.py` | 快照路由读取 `If-None-Match`，命中返回 304，200 响应写入 ETag |
| `app/middleware.py` | 复用响应已有的 `ETag`，不再对该响应重复 body-MD5（其余端点逻辑不变） |

---

## 4. 详细改动（可直接复制）

### 4.1 `app/sql_queries.py`：新增按日版本查询

在现有 `instant_latest_date` 之后新增：

```python
def instant_day_tick(table):
    """轻量版本查询：只取指定交易日的最新 tick_time，不扫全表行。

    依赖 (trade_date, tick_time) 索引（见第 7 节 数据库前提）。
    """
    return ('SELECT MAX(tick_time) AS t FROM '
            + safe_instant_table(table)
            + ' WHERE trade_date=%s')
```

> 说明：保留现有 `instant_latest_date` 不动（`latest_date()` 仍在使用）。
> 本次改造后 `get_snapshot` 不再使用"整月 max(trade_date),max(tick_time)"做日内版本判断。

---

### 4.2 `app/services/intraday_service.py`：版本协商 + 修复 tick 口径

在文件顶部补充导入：

```python
import hashlib
```

新增以下工具函数（建议放在 `today()` 之后、`_normalize()` 之前）：

```python
def _iso_tick(tick):
    """把 DB 返回的 tick 归一化为 ISO 字符串；为空返回 None。"""
    if tick is None or pd.isna(tick):
        return None
    return tick.isoformat() if hasattr(tick, 'isoformat') else str(tick)


def build_version_tag(day, tick) -> str:
    """按 (交易日, 当日最新 tick) 生成弱 ETag，O(1)，不依赖响应体。"""
    raw = '%s|%s' % (day.isoformat(), _iso_tick(tick) or '')
    return 'W/"' + hashlib.md5(raw.encode('utf-8')).hexdigest() + '"'


def parse_if_none_match(header):
    """解析 If-None-Match，支持逗号分隔的多值与通配符 *。"""
    if not header:
        return set()
    return {t.strip() for t in header.split(',') if t.strip()}


def tag_matches(tag, if_none_match):
    """当前版本是否命中客户端条件。"""
    wanted = parse_if_none_match(if_none_match)
    return bool(wanted) and (tag in wanted or '*' in wanted)
```

用下面实现**整体替换**现有 `get_snapshot`（修复 tick 口径并加入协商）：

```python
def get_snapshot(day=None, if_none_match=None):
    """返回盘中快照，支持前置版本协商。

    返回值：
      - 命中条件：(304, {'ETag': tag})（无 body、无全量查询）；
      - 未命中：  (200, data_dict)，data_dict 额外含版本字段。
    业务/数据异常仍抛 envelope.BizError。
    """
    if day is None:
        day = today()

    with datasource.market_connection() as conn:
        tbl = datasource.discover_instant_table(conn, day)
        if tbl is None:
            raise envelope.BizError(404, envelope.DATA_NOT_FOUND,
                                    '当月暂无盘中数据')

        cur = conn.cursor()
        try:
            cur.execute(SQL.instant_day_tick(tbl), (day,))
            tick_row = cur.fetchone()
        finally:
            cur.close()

    day_tick = tick_row[0] if tick_row else None

    if day_tick is None:
        # 区分"今天还没生成"与"历史日期无快照"，口径与现状保持一致。
        if day == today():
            raise envelope.BizError(404, envelope.DATA_NOT_FOUND,
                                    '今日盘中快照尚未生成')
        raise envelope.BizError(404, envelope.DATA_NOT_FOUND,
                                '该日期暂无盘中快照')

    tag = build_version_tag(day, day_tick)

    # 前置协商命中：不执行全量查询、不做 pandas，直接 304。
    if tag_matches(tag, if_none_match):
        return 304, {'ETag': tag}

    with datasource.market_connection() as conn:
        df = pd.read_sql(SQL.instant_snapshot_all(tbl), conn,
                         params=(day,))

    if df.empty:
        raise envelope.BizError(404, envelope.DATA_NOT_FOUND,
                                '该日期暂无盘中快照')

    df = _normalize(df)
    return 200, {
        'trade_date': day.strftime('%Y-%m-%d'),
        'tick_time': _iso_tick(day_tick),
        'rows': v.clean_records(df),
        '_etag': tag,
    }
```

> 备注：
>
> 1. 这里用两次 `with market_connection()`：命中 304 时只发生第一次（轻量）；未命中才第二次连接做全量查询。相比全量查询约 1s，多一次连接建立的开销可忽略。如希望未命中时也只连一次，可将全量查询合并进第一个 `with`，但会让"协商命中"路径仍持有连接，收益不大，当前从简。
> 2. `_normalize`、`v.clean_records` 维持现状不改。
> 3. 返回 dict 中的 `_etag` 仅供路由读取，最终**不会**出现在 JSON 里（路由会弹出该字段）。

---

### 4.3 `app/routers/v1.py`：路由接入条件协商

#### 4.3.1 修改导入

将第 6 行：

```python
from fastapi import APIRouter, Depends, Query
```

改为：

```python
from fastapi import APIRouter, Depends, Query, Request, Response
```

#### 4.3.2 替换快照路由

用下面代码整体替换现有 `intraday_snapshot`（约 109–120 行）：

```python
@router.get('/intraday/snapshot')
@db_endpoint
def intraday_snapshot(
    response: Response,
    request: Request,
    date: str | None = Query(None),
    ctx: ApiKeyContext = Depends(require_api_key),
):
    if not ctx.allow_intraday:
        raise envelope.BizError(403, envelope.FORBIDDEN,
                                '当前 API Key 未开通盘中数据权限')
    day = v.parse_date(date, 'date') if date else None
    if_none_match = request.headers.get('if-none-match')
    status, payload = intraday_service.get_snapshot(day, if_none_match)

    if status == 304:
        # 前置协商命中：304 空体，中间件会原样透传，不做 body-MD5。
        return Response(status_code=304, headers={
            'ETag': payload['ETag'],
            'Cache-Control': 'private, no-cache',
        })

    tag = payload.pop('_etag')
    response.headers['ETag'] = tag
    response.headers['Cache-Control'] = 'private, no-cache'
    return envelope.ok(payload)
```

> FastAPI 会把返回的 `Response(status_code=304, ...)` 作为最终响应，不经过响应模型序列化；`@db_endpoint` 装饰器也能正常透传该返回值。

---

### 4.4 `app/middleware.py`：复用已有 ETag，不重复计算

当前中间件对**所有** `GET /api/v1/` 200 响应都重新用 body-MD5 生成 ETag，会覆盖业务层下发的版本 ETag。需要让它：

1. 304 响应：原样透传（保留业务层写入的 `ETag`）；
2. 200 响应若已有 `ETag`：直接复用，不再重算；
3. 其余 GET 端点（无 ETag）：维持原 body-MD5 行为不变。

将 `_build_etag_response` 方法整体替换为：

```python
    @staticmethod
    def _build_etag_response(request: Request, response, body: bytes):
        if response.status_code == 304:
            # 前置协商已产出 304：原样透传，保留其 ETag / Cache-Control。
            return Response(status_code=304,
                            headers=dict(response.headers))

        if response.status_code != 200:
            return Response(status_code=response.status_code, content=body,
                            headers=dict(response.headers),
                            media_type=response.media_type)
        if not body:
            return Response(status_code=response.status_code,
                            headers=dict(response.headers),
                            media_type=response.media_type)

        if_none = request.headers.get('if-none-match')
        headers = dict(response.headers)

        # 业务层已下发版本 ETag（如盘中快照）：直接复用，不再对 body 算 MD5。
        existing = headers.get('ETag')
        if existing:
            headers.setdefault('Cache-Control', 'private, no-cache')
            if if_none:
                tags = {t.strip() for t in if_none.split(',')}
                if existing in tags or '*' in tags:
                    return Response(status_code=304, headers={
                        'ETag': existing,
                        'Cache-Control': headers['Cache-Control'],
                    })
            return Response(status_code=response.status_code, content=body,
                            headers=headers, media_type=response.media_type)

        # 其余 GET 端点：维持原有"响应体 MD5"行为。
        tag = 'W/"' + hashlib.md5(body).hexdigest() + '"'
        if if_none:
            tags = {t.strip() for t in if_none.split(',')}
            if tag in tags or '*' in tags:
                return Response(status_code=304, headers={
                    'ETag': tag,
                    'Cache-Control': 'private, no-cache',
                })
        headers['ETag'] = tag
        headers['Cache-Control'] = 'private, no-cache'
        return Response(status_code=response.status_code, content=body,
                        headers=headers, media_type=response.media_type)
```

> `hashlib` 已在该文件顶部导入，无需新增。

---

## 5. 正确性契约（服务端必须保证）

1. **内容变 → 版本变**：同一交易日内，只要快照任意一只股票数据变化，写入方推进 `tick_time`，`MAX(tick_time)` 即变化。
2. **版本不变 → 内容不变**：`MAX(tick_time)` 相同，必须保证当日整包内容一致。
3. 若写入方可能在同一秒内多次刷新，`tick_time` 精度必须足够（建议至少秒级且不发生覆盖；更稳妥可引入单调递增批次号，见第 9 节）。
4. ETag 必须是带引号的完整字符串（`W/"..."`），与 `If-None-Match` 逐值精确匹配。
5. 鉴权先于协商：未授权请求不得因协商逻辑而绕过校验（本设计中鉴权依赖在路由内先执行）。

---

## 6. 测试与验收

### 6.1 编译检查

```powershell
cd d:\rxlib_api
..\rxlib\runtime\python312\python.exe -m py_compile `
  app\sql_queries.py `
  app\services\intraday_service.py `
  app\routers\v1.py `
  app\middleware.py
```

### 6.2 功能用例（重启服务后）

| # | 场景 | 预期 |
|---|---|---|
| 1 | 首次请求当日快照（无 If-None-Match） | 200，响应头含新 `ETag`；body 中 `tick_time` 为当日最新 tick |
| 2 | 用例 1 的 ETag 再次请求 | **304，空体**；耗时显著下降（目标：从约 1s 降到 100ms 量级） |
| 3 | 用旧/错误 ETag 请求 | 200 + 当前 ETag + 全量数据 |
| 4 | `If-None-Match: *` | 304 |
| 5 | 多个 ETag 逗号分隔、其中一个匹配 | 304 |
| 6 | 查询历史交易日 | 200，`tick_time` 必须是**该日**最新 tick（验证修复） |
| 7 | 查未来日 / 今日尚未生成 | 404，业务码 40401 |
| 8 | 无盘中权限 Key | 403，业务码 40301 |
| 9 | 盘中写入方刷新后再请求（ETag 为旧值） | 200 + 新 ETag + 新数据 |
| 10 | 其他 GET 端点（如 /universe） | ETag 行为与改造前一致（body-MD5） |

### 6.3 性能对照（关键验收）

连续两次请求同一日快照，记录耗时：

- 第一次 200：约 1s（含全量查询，预期内）；
- 第二次 304：**应明显低于 1s**（仅一次轻量聚合 + 鉴权/网络）。

若第二次仍接近 1s，排查：

- 是否命中改造后的代码路径（确认 304 响应头 `ETag` 是版本哈希而非整包 MD5；两者均为 32 位 hex，可通过"刷新单只数据后 ETag 是否随 tick 立即变化"区分）；
- 轻量查询是否走了索引（`EXPLAIN SELECT MAX(tick_time) ...`）。

---

## 7. 数据库前提

- `stock_instant_YYYYMM` 表建议存在索引 `(trade_date, tick_time)`，使 `MAX(tick_time) WHERE trade_date=?` 走索引极值扫描，避免全表。
- `discover_instant_table` 使用的 `INFORMATION_SCHEMA` 校验保持不变。

若无复合索引、且单日行数很大导致 `MAX` 仍慢，可将 `instant_day_tick` 改写为按 tick 倒序取一条的等价查询（需相应索引）：

```sql
SELECT tick_time AS t FROM <table>
WHERE trade_date=%s
ORDER BY tick_time DESC
LIMIT 1
```

---

## 8. 回滚方案

1. 还原 `app/routers/v1.py` 中快照路由与导入；
2. 还原 `app/middleware.py` 的 `_build_etag_response`；
3. 还原 `app/services/intraday_service.py`（恢复旧 `get_snapshot`，删除新增工具函数）；
4. 删除或保留 `instant_day_tick`（未被引用时保留无副作用）。

回滚后系统恢复为"中间件 body-MD5 协商"，客户端无需改动。

---

## 9. 后续可选增强（本次不含）

1. **单调递增批次号**：以 `snapshot_seq` 替代墙钟 `tick_time` 生成 ETag，抗同秒覆盖/时钟回拨。
2. **服务端共享缓存 + singleflight**：把当日快照结果缓存（Redis/内存），按版本失效；多用户共享，去数据库只取一次——解决"请求条数到达源站"与跨用户压力。
3. **CDN/网关短缓存**：将 `Cache-Control` 由 `private, no-cache` 放宽以允许共享层缓存，并加 `Vary`。
4. **推送（SSE/WebSocket）**：数据更新时主动推送，替代轮询。

---

## 10. 预期收益小结

| 项目 | 改造前 | 改造后（重复请求、数据未变） |
|---|---|---|
| 全量 SQL（约 1s） | 每次执行 | **跳过** |
| pandas 归一化 5000+ 行 | 每次执行 | **跳过** |
| 响应体传输 | 304 已省 | 省 |
| 304 端到端耗时 | 约 1.0~1.3s | 预期降至 100ms 量级 |
| 返回体 tick_time（历史日） | 可能错误（整月 tick） | 正确（当日 tick） |

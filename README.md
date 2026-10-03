# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、规则评分、利益冲突、澄清、废标、投诉重评和授标快照服务。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8209`，数据库默认 `public_procurement.db`。

## 主要接口

使用 `X-User`、`X-Role` 请求头。角色有 `procurement`、`vendor`、`evaluator`、`supervisor`、`auditor`、`public`。

- `GET /health`、`GET /api/state`、`GET /api/tenders/{id}`
- `POST /api/vendors`、`POST /api/tenders`、`POST /api/tenders/publish`
- `POST /api/bids`、`POST /api/bids/withdraw`、`POST /api/bids/disqualify`
- 开标（可恢复的三方见证批次）：
  - `POST /api/open-batches`：截止后由采购员发起批次，锁定本批全部密封投标及承诺哈希，批次处于待核
  - `POST /api/open-batches/{id}/confirm`：监标人（`supervisor`）、审计员（`auditor`）分别核对同一批承诺哈希；两方都通过后在同一事务内统一开标。任一核对不符，整批停在待核（`disputed`），差异逐份落库并记录失败原因，不改变任何投标状态
  - `POST /api/open-batches/{id}/recover`：差异排除后由采购员/监标人把核对不符批次恢复为待核（清空两方确认），重新两方核对
  - `GET /api/open-batches/{id}`：批次详情（批次号、两位确认人、差异明细、失败原因、逐份核对结果）
  - 统一开标写入失败时事务整体回滚到未开标，可对同一批次重试；同一确认人重复提交幂等，已确认部分不重复处理
  - 待核/核对不符/未见证批次的投标不得评分或授标
- `POST /api/conflicts`、`POST /api/evaluations`
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`、`POST /api/complaints/resolve`
- `POST /api/tenders/award`：锁定评分轮次并保存排名快照

项目详情、公开页（`/api/state`、`/api/tenders/{id}`）和审计记录（timeline 的 `open_batch.*`、`tender.opened` 事件）展示同一批次号、采购员/监标人/审计员确认人和失败原因。历史已开标但没有批次号的数据在启动时回填为 `UNWITNESSED-T{n}` 批次，投标见证状态标记为 `unwitnessed`（未见证），同样禁止评分和授标。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖三方见证开标全流程、单方确认不开标、核对幂等、哈希不符整批暂停与恢复重试、写入失败回滚、失败批次禁止评分授标、批次号在公开页/详情/审计一致展示、历史数据未见证回填，以及利益冲突、重复评分、投诉重评和角色权限。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。

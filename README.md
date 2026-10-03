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
- `POST /api/tenders/open-batches`：截止后由**采购员**发起开标批次，快照本批 sealed 投标及承诺哈希，不改变投标状态
- `POST /api/open-batches/confirm`：**监标人**（`supervisor`）和**审计员**（`auditor`）分别核对同一批承诺哈希；两方都通过后在同一事务内统一开标，任一不符整批停在待核并持久化差异（返回 200 且带 `error` 与 `failure_detail`）
- `GET /api/open-batches/{id}`：查询批次号、双方确认人、确认时间和失败原因
- `POST /api/conflicts`、`POST /api/evaluations`
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`、`POST /api/complaints/resolve`
- `POST /api/tenders/award`：锁定评分轮次并保存排名快照

## 开标批次（可恢复）

开标不再是单角色直接逐条改状态，而是一个可恢复、可核对的批次流程：

1. 截止后采购员发起批次，系统把当时全部 `sealed` 投标及其承诺哈希快照进批次（`pending`）。
2. 监标人、审计员分别调用确认接口，各自重新计算承诺哈希并与批次快照比对：
   - 先确认的一方使批次保持 `pending`；两方都通过才在**同一事务**内把整批投标统一改为 `opened`、项目改为 `opened`（`opened`）。
   - 任一投标不符：批次标记 `failed`（核对不符停在待核），记录 `failure_reason` 和逐项 `failure_detail`，**所有**投标保持 `sealed`；失败批次不解决前不能重新发起，也不得进入评分或授标。
   - 确认写入过程中出错时事务整体回滚到未开标；已确认一方的记录保留，重试时该方幂等跳过、不重复处理，由另一方重新核对即可。
   - 同一角色已由某人确认后，其他人不能顶替确认（409）。
3. 项目详情（`GET /api/tenders/{id}`）、公开页（`GET /api/state` 与 `static/index.html`）和审计时间线对同一开标显示**同一批次号、确认人和失败原因**；每条投标带 `open_batch_no` 与 `witness_status`（已见证/待核/核对失败/未见证）。
4. 历史数据库中没有批次号的已开标数据，在服务启动迁移时统一回填为 `unwitnessed` 批次（批次号前缀 `LEGACY-`，见证状态「未见证（历史回填）」），并写入 `open_batch.backfilled` 审计记录。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖三方开标批次、确认幂等不重复处理、承诺哈希不符整批停在待核、写入失败回滚重试、失败批次拦截评分授标、历史数据未见证回填，以及完整开标授标、截止前正文隐藏、利益冲突、重复评分、投诉重评和角色权限。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。

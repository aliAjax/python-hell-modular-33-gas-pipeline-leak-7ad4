# 燃气管线泄漏检测与隔离协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8333`。

- `app.py`：参数、依赖和服务生命周期。
- `src/domain.py`：管段、传感值和来源记录校验。
- `src/rules.py`：泄漏评分、依据聚合与版本差异、阀门顺序、修复、试压、恢复状态机、结论作废。
- `src/repository.py`：SQLite、来源版本与替换、依据快照、乐观版本、离线批次去重、审计哈希链。
- `src/service.py`：角色权限和业务编排。
- `src/offline.py`：离线批次本地账本（连续编号、断点续传、未完成留本地）。
- `src/http_api.py`：JSON 接口与首页。
- `src/audit.py`：可校验的审计事件。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8333
python3 -m unittest discover -s tests -v
```

## 现场依据如何把三段接起来

- **评定依据**：初报（调度）与每条有效来源（巡检/传感器）作为证据聚合成一份依据，取压降、浓度的最大值与异味报告之和，重新评分。每份依据带单调递增的 `basis_revision` 和内容 `digest`，快照落 `basis_snapshots` 表。
- **来源记录**：`(item_id, source_type, external_id)` 标识同一来源，带 `revision` 与 `active`，替换保留历史。
  - 完全重复上报：返回原记录，不重新评定、不升版本、不留新痕迹（HTTP 200，`result.outcome=duplicate`）。
  - 新增或内容替换：生成新依据版本并重新评定。
- **处置动作**：核验时把当前依据版本盖进评定记录；隔离、抢修、试压、恢复必须携带 `expected_basis_revision`（可再带 `expected_basis_digest` 校验内容）。依据版本落后或对不上时返回 `409 basis_conflict`，`details.basis_diff` 给出字段差异和来源证据的新增/删除/替换差异。
- **来源更新的失效语义**：一旦事件已经核验或处置，新增/替换来源后原评定与核验立即失效，状态回到 `reported`（待核验），隔离、抢修、试压结论一并作废：从当前记录移除、写入 `voided_conclusions` 并逐条产生 `conclusion_voided` 审计事件。已恢复或已取消的终态事件拒绝再补登来源。
- **重复事件上报**：同 `(管段, 时间)` 再建事件只返回原记录（`created=false`）。

## 离线批次

`POST /api/items/{id}/source-batches/{batch_id}` 按连续页码 `page_no/total_pages` 上传：

- 服务端按 `(item, batch, page)` 去重，同一页重传只入库一次（`outcome=duplicate`）；页码相同但内容哈希不同返回 `page_content_conflict`。
- 每页原子提交；响应带 `stored_pages` 和 `next_page`，客户端失败后从已入库部分之后继续。
- `src/offline.py` 的 `OfflineLedger` 把未完成页码留在本地原子账本，进程重启可恢复，整批确认后清除；`sync(item_id, batch_id, uploader)` 逐页重试。

## 审计

所有创建、来源新增/替换、批次入库、依据升级、结论作废、处置动作均写入同一事件哈希链（`previous_hash`/`event_hash`），`GET /api/items/{id}/audit` 返回链与 `chain_valid`，结论作废不删除任何历史，链条完整可追溯。

模型不替代 SCADA、管网水力计算或正式应急预案。

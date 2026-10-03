# 燃气管线泄漏检测与隔离协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8333`。

- `app.py`：参数、依赖和服务生命周期。
- `src/domain.py`：管段、传感值和来源记录校验。
- `src/rules.py`：泄漏评分、阀门顺序、修复、试压、恢复状态机、依据聚合与作废。
- `src/repository.py`：SQLite、重复保护、乐观版本、依据版本和审计链。
- `src/service.py`：角色权限、业务编排、依据校验与批次上传。
- `src/http_api.py`：JSON 接口与首页。
- `src/audit.py`：可校验的审计事件。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8333
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`、`POST /api/batches/<id>/pages/<n>`、`GET /api/batches/<id>` 和审计查询。

## 依据、来源与处置的衔接

- **评定依据来自来源记录**：`GET /api/items/<id>` 返回的 `basis` 由该事件的全部来源记录聚合而成（同一指标取观测时间最晚的来源），`assessment` 据此重新评分。来源未覆盖的指标保留初报值。
- **重复上报只返回原记录**：`POST /api/items` 重复创建返回原事件；`POST /api/items/<id>/sources` 重复提交同一来源返回原记录（`status: "duplicate"`），不触发重新评定。
- **来源新增或替换后重新评定**：来源首次录入（`status: "recorded"`）或内容更新（`status: "replaced"`）后，`basis_version` 递增，事件回到 `reported`（待核验），评定依据立即按新来源重算。
- **处置结论一并作废并留痕**：来源更新后，核验、阀门顺序、抢修、试压、恢复结论全部作废（从 payload 移除），并写入 `conclusions_voided` 审计事件；`basis_changed` 事件记录依据版本与依据内容的变化。
- **对外提交携带依据版本**：`isolate`、`repair`、`pressure_test`、`restore` 必须在请求体中携带 `basis_version`。版本落后时返回 `409 basis_version_conflict` 并给出当前依据与差异；版本对不上但提交了依据快照时返回 `409 basis_mismatch` 并给出差异。
- **离线批次连续上传**：`POST /api/batches/<batch_id>/pages/<page_number>` 按连续页码上传来源记录，同一页重传只入库一次（`duplicate: true`）。`GET /api/batches/<batch_id>` 返回已入库页码与下一页码，失败后从已入库部分继续重试，未完成页码留在本地。
- **审计链完整可追溯**：所有依据变更、结论作废、批次入库均通过哈希链写入 `audit_events`，可逐事件校验。

模型不替代 SCADA、管网水力计算或正式应急预案。

# 山火事件指挥与离线人员调度

维护火线、风向、资源和任务区，合并离线现场记录并防止人员重复分配。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8319
```

默认端口为`8319`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：field_commander, incident_commander, logistics, viewer。火线长度、风向变化和离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中。

## 离线设备批次补录

断网时前线把水泵组（pump）、油锯（chainsaw）、运兵车（transport）的到场（arrival）、派单（dispatch）、撤离（evacuation）、移交（handover）记录攒在平板里，回到指挥车后按现场批次号批量上传。

- `POST /api/equipment`：登记设备（field_commander/logistics/incident_commander），`kind`可写中文。
- `POST /api/items/{id}/field-batches`：按`batch_no`接入一批记录（field_commander/logistics）。
  - 同一批次号只认首条；内容完全一致重复上传返回`duplicate`（幂等）。
  - 同号内容不一致时整条留待核对（409），挂起到`GET /api/field-mismatches`，不覆盖首条。
  - 某条派单的设备已被**另一条未结束任务**占用时，响应中说清冲突任务，且**整批不写**（批次记为rejected，冲突解除后可按同号重传）；派入同一任务按幂等跳过。
  - 移交（handover）必须提供`to_item_id`：源任务区释放占用、目标任务区建立占用，整批在单个事务内原子提交。
- `GET /api/items/{id}/field-ledger`：设备占用台账、未释放到场及关闭前阻塞项。
- `GET /api/field-mismatches`：待核对批次（incident_commander/field_commander/logistics）。

任务区关闭前，每台到场/派入的设备都必须有撤离或移交记录，否则`transition`到`closed`被冲突拦截。占用台账、判定规则、请求入口分属`repository.py`、`rules.py`、`http_api.py`，由`service.py`编排；原有的 items 事件与 SHA-256 审计链继续可用。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

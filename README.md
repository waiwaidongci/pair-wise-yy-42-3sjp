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

### 设备占用台账与离线批次补录

山谷火场断网时，前线把水泵组（pump）、油锯（chainsaw）、运兵车（troop_carrier）的
派单/到场/撤离/移交记录攒在平板里，回到指挥车按现场批次号批量补录。

- `POST /api/devices`、`GET /api/devices`：登记/查询设备（field_commander、logistics）。
- `POST /api/items/{id}/tasks`、`POST /api/items/{id}/tasks/{ref}/finish`、
  `GET /api/items/{id}/tasks`：任务（任务区）创建、结束、查询。
- `POST /api/items/{id}/ingest`：离线批次补录，请求体形如
  `{"batch_no":"B-1","entries":[{"kind":"dispatch|arrival|evacuation|handover",
  "device_code":"P1","task_ref":"T-1", ...}]}`。
  - **同号只认首条**：相同`batch_no`+相同内容指纹为幂等回放（`replayed`，不重复写）；
    同号内容不一致时本次内容进入核对队列（`needs_review`），首条记录继续生效，
    可通过`GET /api/reviews`查看待核对条目。
  - **占用冲突整批不写**：设备已被另一条未结束任务占用时，返回409并说清冲突任务，
    整批在一个事务内回滚，不产生任何任务/占用/记录。
  - 撤离/移交后占用释放，设备可重新派单。
- `GET /api/items/{id}/occupations`：设备占用台账；
  `GET /api/items/{id}/device_records`：设备到场/撤离/移交流水；
  `GET /api/items/{id}/batches`、`GET /api/batches`：已接收批次。
- **任务区关闭不变量**：任务区`closed`前，台账中每台被派单的设备都必须已有
  撤离（evacuation）或移交（handover）记录，否则转换返回409并点名缺失设备。

占用台账在`repository.py`，批次幂等/冲突/关闭判定在`rules.py`，请求入口在`http_api.py`；
设备相关事件（device_register、task_create、task_finish、batch_ingest、batch_review）
追加在原有SHA-256审计链上，旧事件与审计校验不受影响。

允许角色：field_commander, incident_commander, logistics, viewer。火线长度、风向变化和离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

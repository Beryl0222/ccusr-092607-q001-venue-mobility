# 分散赛区通勤保障决策台

接收场馆日程、驻地、公共交通时刻、车辆司机资质、安保限制、无障碍与器材条件、代表团申报，
形成可解释的出发方案与备用路径；临时改项、管制、停运只重排真正受影响的行程。

## 目录

- `contracts/domain.schema.json`：基础领域事件契约。
- `contracts/service.schema.json`：保障服务事件（申报、占用、重排、升级、时钟节点、中断）。
- `data/scenario.json`：多场馆、多驻地、班次、安检窗口、车辆与管制的联调场景。
- `data/commands.json`：端到端命令脚本（幂等/核查、抢占、医疗保护、停运、管制、失约、审计）。
- `data/sample.json`：基础契约最小样例。
- `src/venue_mobility/`
  - `model.py`：静态事实与申报模型；`clock.py`：可控时钟；`times.py`：时区时间工具。
  - `planner.py`：候选路径生成、硬约束过滤、成本排序与拒绝理由。
  - `service.py`：事件溯源保障服务（占用、抢占、选择性重排、状态机、恢复、审计）。
  - `api.py`：JSON 命令面；`cli.py`：校验、脚本执行与审计命令。
- `docs/domain.md`：领域对象、事件与规则语义。
- `tests/`：基础契约与服务规则测试。

## 核心规则

- 方案默认取成本最低路径（公共交通优先），其余候选按成本升序作为备用；
  无障碍/医疗必须使用对应资质车辆，器材同行只走有器材容量的专车。
- 确认时车辆、座位、安检名额在同一事务一次性占用；先做改派预演，任一步失败整笔不留痕。
- 争用按优先级抢占：运动员 > 媒体 > 工作人员；医疗与无障碍为保护档，
  任何普通优先级（含其他医疗/无障碍彼此之间）都不能覆盖；已越过出发时刻或已完成的转运不可抢占。
- 同业务编号指纹不变（含相同通知键）为幂等，不重复派车；
  业务编号相同但人员、时间或路线不同进入核查，需人工裁决。
- 停运、管制、改期只重排在途且依据失效的行程；未确认行程只换方案不自动占资源；
  已完成转运随 `COMMITMENT_CLOSED` 保留当时依据。更贵的替代路径进入升级节点，
  宽限期内可人工确认，到期自动生效；无可行路径记为搁浅，条件恢复后自动重新规划。
- 确认、候车、失约、升级全部由可控时钟推进；从事件日志重放即可恢复未结保障义务。

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```

## 命令行

基础契约校验（旧用法保持兼容）：

```bash
PYTHONPATH=src python3 -m venue_mobility.cli validate contracts/domain.schema.json data/sample.json
PYTHONPATH=src python3 -m venue_mobility.cli contracts/domain.schema.json data/sample.json
```

执行场景脚本并持久化事件日志：

```bash
PYTHONPATH=src python3 -m venue_mobility.cli run data/scenario.json data/commands.json --store events.log
```

审计：某人为何走当前路线、资源从何处扣减、变更后释放/替代了哪些安排：

```bash
PYTHONPATH=src python3 -m venue_mobility.cli audit data/scenario.json events.log explain B8
PYTHONPATH=src python3 -m venue_mobility.cli audit data/scenario.json events.log ledger
PYTHONPATH=src python3 -m venue_mobility.cli audit data/scenario.json events.log changes
PYTHONPATH=src python3 -m venue_mobility.cli audit data/scenario.json events.log pending
```

## API

```python
from venue_mobility.api import ServiceHub

hub = ServiceHub.from_files("data/scenario.json", store="events.log")
hub.dispatch({"command": "submit", "params": {...}})
hub.dispatch({"command": "confirm", "params": {"business_ref": "B1"}})
hub.dispatch({"command": "advance", "params": {"minutes": 20}})
hub.dispatch({"command": "explain", "params": {"ref": "B1"}})
```

业务规则被拒绝时返回 `{"error": "service_rule", "message": 中文说明}`，不抛异常到边界。

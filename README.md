# 分散赛区通勤保障决策台

接收多城市分散赛区的场馆日程、驻地、公共交通时刻、车辆司机资质、安保限制、
无障碍与器材同行条件以及代表团申报，形成**可解释**的出发方案与备用路径；
临时变化只重排真正受影响的行程，并通过事件日志回答"为何走这条线、资源从
何处扣减、变更后释放/替代了什么"。

## 核心规则

- **成本择优而非一律专车**：公共交通 → 拼班班车 → 专车，按可行候选的成本分选择。
- **硬门槛高于普通优先级**：运动员/媒体/工作人员有不同角色优先级，但医疗随护、
  轮椅无障碍是硬约束，不能被普通优先级覆盖；安检窗口同理。
- **一次性占用**：车辆、座位、轮椅位、安检时段在同一事件批次内原子占用，
  容量不足整批回滚；重复通知幂等，绝不重复派车。
- **业务编号核查**：同一业务编号而人员/时间/路线不同的通知进入 `REQUEST_QUARANTINED`
  人工核查，不自动派车。
- **增量重排**：停运、道路管制、车辆下线、窗口关闭、临时改项只影响方案被破坏的
  开放行程；已完成转运保留当时依据（`basis_event_ids`），不回溯改写。
- **可控时钟**：确认 → 候车 → 登乘 → 完成；超时失约释放资源；无可行方案则升级，
  升级义务在进程恢复、供给恢复后自动续办。
- **只追加事件**：一切状态由事件重放得到；崩溃后重放日志即可继续未竟保障义务。

## 目录

- `contracts/domain.schema.json`：对象、事件、载荷字段与枚举约定。
- `docs/domain.md`：领域语义、优先规则与事件边界。
- `data/sample.json`：可直接校验的契约联调样例。
- `src/venue_mobility/`
  - `contracts.py`：基础契约校验（不改写输入）。
  - `events.py`：事件信封、可控时钟、乐观版本与 JSONL 事件存储。
  - `model.py`：场馆/驻地/路网/班次/车辆/司机/安检窗口/申报。
  - `planner.py`：候选方案、硬门槛、角色优先级、区间容量原子占用。
  - `service.py`：申报受理、核查、生命周期、增量重排、恢复。
  - `audit.py`：why / ledger / impact 三个审计投影。
  - `scenario.py`：可复现联调场景。
  - `console.py`：管理命令；`cli.py`：原有契约校验入口。
- `tests/`：契约、事件层、规划器、服务生命周期与审计共 54 个测试。

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```

## 契约样例校验

```bash
PYTHONPATH=src python3 -m venue_mobility.cli contracts/domain.schema.json data/sample.json
```

## 联调演示

```bash
PYTHONPATH=src python3 -m venue_mobility.console demo
```

演示依次覆盖：4 份申报择优派车 → 重复通知幂等 → 同业务编号改时间进入核查 →
地铁停运改派班车 → 无障碍安检窗口关闭导致升级、重开后续办 → 人工核查结论 →
候车/登乘/失约释放/到达闭环 → 赛后管制不回溯已完成转运。事件写入
`data/demo-events.jsonl`。

## 审计命令

```bash
# 某人（人员编号/申报号/行程号/业务编号）为何采用当前路线
PYTHONPATH=src python3 -m venue_mobility.console why athlete-03

# 资源扣减台账（可加 --resource window:W-AQU-2）
PYTHONPATH=src python3 -m venue_mobility.console ledger

# 某次变更释放或替代了哪些安排（事件号取自 events 列表）
PYTHONPATH=src python3 -m venue_mobility.console events
PYTHONPATH=src python3 -m venue_mobility.console impact evt-000019
```

## 作为库使用

```python
from datetime import datetime, timedelta, timezone
from venue_mobility import Clock, EventStore, MobilityService, Registry
from venue_mobility.model import TravelRequest, TravelerRole  # 以及 Lodging/Venue/...

clock = Clock(datetime(2026, 9, 27, 6, 0, tzinfo=timezone(timedelta(hours=8))))
service = MobilityService(registry, EventStore("data/events.jsonl"), clock)
service.submit(request)                 # 受理（幂等/核查/择优/原子占用）
clock.advance(timedelta(minutes=30))    # 推进可控时钟
service.tick()                          # 候车/失约/完成/升级重试
# 进程重启后：
service = MobilityService.restore(registry, store, clock)
```

# 领域约定

定义分散赛区日程、交通资源、优先规则和异常改派之间的事件契约与服务规则。

## 聚合与静态事实

聚合对象包括 `venue_schedule`、`travel_request`、`transport_resource`、`journey_commitment`。
静态事实由 `data/scenario.json` 描述：

- `venue_schedule` 对应场次（开赛、最晚抵达、安保分区）与安检窗口（时间窗 + 容量）。
- `travel_request` 对应代表团申报：业务编号、人员、角色（运动员/媒体/工作人员）、
  驻地-场馆方向、同行人数、无障碍、医疗、器材单位、通知幂等键。
- `transport_resource` 是可扣减的资源原子：专车整车、公交座位、安检名额。
- `journey_commitment` 是一段在途保障义务及其当时依据。

所有发生时间必须携带时区，版本号从 1 开始按聚合递增；基础校验不改写调用方输入。

## 事件载荷

基础事件（`contracts/domain.schema.json`）：

- `REQUEST_ACCEPTED`：`traveler_role`, `required_arrival`。
- `RESOURCE_RESERVED`：`resource_ref`, `capacity`。
- `JOURNEY_REPLANNED`：`affected_segments`, `reason`。

服务事件（`contracts/service.schema.json`）补充：

- 申报：`REQUEST_SUBMITTED` / `REQUEST_ESCALATED_REVIEW` / `REQUEST_REJECTED`。
- 方案：`OPTION_PROPOSED`（含备用路径与升级截止时刻）。
- 占用：`RESOURCE_RESERVED` / `RESOURCE_RELEASED`，载荷带每个资源原子的键、单位与剩余容量。
- 状态节点：`WAITING_STARTED` / `WAITING_EXPIRED`（失约）、`DISPATCH_CONFIRMED`、
  `JOURNEY_UPGRADED`、`JOURNEY_STRANDED`、`TRANSFER_COMPLETED` / `COMMITMENT_CLOSED`。
- 中断：`DISRUPTION_DECLARED`（停运/管制/改期）与 `DISRUPTION_CLEARED`（解除）。
- 申报指纹：`REQUEST_SUBMITTED` 的 `request` 字段保存完整申报快照，供审计还原。

## 状态机

```
submitted → planned ⇄ awaiting_upgrade
          → reviewing → planned / rejected
planned → dispatched ⇄ waiting
dispatched → completed → closed
waiting → expired(no_show) → closed
任意在途 → replanned → planned / dispatched / awaiting_upgrade / stranded
stranded →（条件恢复）→ planned
```

## 关键规则

- **方案解释**：每个候选都带拒绝码（资质、分区通行证、无障碍/医疗分级、器材容量、
  管制、停运、角色准备余量、无可用安检窗口），主方案取成本最低，其余为备用路径。
- **一次性占用**：确认把专车整车、公交座位、安检名额作为同一事务；先在模拟台账上
  为所有被挤掉者预演备选，全部可行才真正落事件，失败不留半成品。
- **优先与保护**：同档按运动员 > 媒体 > 工作人员；医疗、无障碍为保护档，
  普通优先级和保护档彼此都不可覆盖；已发车（时钟越过出发时刻）、已完成不可抢占。
- **幂等与核查**：通知键/业务编号+指纹不变视为重复通知，不重复派车；
  同业务编号指纹变化（人员/时间/方向）发 `REQUEST_ESCALATED_REVIEW`，冻结派车待裁决。
- **选择性重排**：只处理状态在 planned/dispatched/waiting/awaiting_upgrade/stranded 且
  当前依据不再可行的行程；已完成依据冻结；未确认行程仅更新提案；已派车行程释放后
  重新预订；更贵替代进入 awaiting_upgrade 宽限节点；搁浅行程在条件恢复时自动回到 planned。
- **时钟与恢复**：确认、候车、失约、升级由 `ControlledClock` 驱动；`tick()` 幂等，
  从 JSONL 事件日志重放投影即可继续所有未结义务，时钟恢复到最后事件时刻。
- **审计**：`explain` 给出当前路线、理由、资源原子与完整时间线（含资源扣减/释放事件）；
  `ledger` 给出资源当前持有与逐笔流水；`changes` 列出每次重排的原因、受影响段、原方案。

相同事件标识的业务幂等由事件溯源投影保证；契约层只定义可稳定交换的基础事实。

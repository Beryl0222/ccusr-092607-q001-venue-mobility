# 领域约定

定义分散赛区日程、交通资源、优先规则和异常改派之间的领域事件契约。

聚合对象包括 `venue_schedule`、`travel_request`、`transport_resource`、`journey_commitment`。所有发生时间都必须携带时区，版本号在同一聚合内从 1 开始递增，基础校验不会改写调用方输入。

## 事件类型

### 日程与供给

- `SCHEDULE_FROZEN`：场馆某日竞赛/训练日程冻结，成为出发倒推的依据。
- `SCHEDULE_CHANGED`：临时改项、训练时段调整。载荷含 `affected_refs`、`effective_from`。
- `SUPPLY_CHANGED`：公共交通停运、道路管制、车辆下线、安检窗口关闭等供给侧变化。载荷含 `change_kind`、`affected_refs`、`effective_from`。
  - `change_kind`：`schedule_change` / `transit_suspension` / `road_restriction` / `vehicle_offline` / `security_window_closed`。

### 申报

- `REQUEST_ACCEPTED`：载荷还需包含 `traveler_role`、`required_arrival`。
- `REQUEST_QUARANTINED`：业务编号相同但人员、时间或路线与既有申报不一致，进入人工核查，不直接派车。载荷含 `business_ref`、`conflict_fields`。

### 资源

- `RESOURCE_RESERVED`：车辆、座位区间或安检时段被一次性原子占用。载荷含 `resource_ref`、`capacity`；同一资源、同一时段的第二份占用必然失败，不存在“两次通知派两辆车”。
- `RESOURCE_RELEASED`：行程取消、被替代或改道不再经过某资源时释放。载荷含 `resource_ref`、`reason`。资源台账由保留与释放事件扣减得出。

### 行程

- `JOURNEY_PLANNED`：首次形成可解释的出发方案。载荷含 `option_id`、`mode`、`reasons`（采用当前路线的逐条理由，含被否方案的原因）。
- `JOURNEY_REPLANNED`：供给或日程变化后重排。载荷含 `affected_segments`、`reason`。只允许作用于尚未完成的行程；已完成转运以当时的 `basis_event_ids` 为准，不回溯改写。
- `JOURNEY_LIFECYCLED`：可控时钟推进的行程节点。载荷含 `node`、`at_time`。
  - `node`：`CONFIRMED`（出发方案确认）→ `WAITING`（进入候车/候检）→ `BOARDED`（登乘）→ `COMPLETED`（到达闭环）；超时未确认或未登乘进入 `NO_SHOW`（失约）；任何可行方案都无法满足硬需求时进入 `ESCALATED`（升级），升级义务在进程恢复后仍然有效。
- `COMMITMENT_CLOSED`：保障义务终结。载荷含 `outcome`（`completed` / `cancelled` / `superseded`）与 `basis_event_ids`（当时依据的事件）。

## 优先规则

运动员、媒体、工作人员适用不同的普通优先级排序，但医疗与无障碍需求是硬门槛：任何无法满足无障碍车辆、随车器材或医疗通行的方案直接不可行，不能被普通优先级覆盖。资源争用时按硬门槛通过后的优先级裁决，胜出者一次性占用车辆、座位与安检时段。

## 边界

相同事件标识的业务幂等、冲突隔离、状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。事件一旦写入只能追加，不能修改；纠偏通过新事件表达。

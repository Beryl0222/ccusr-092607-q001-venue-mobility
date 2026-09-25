# 领域约定

定义分散赛区日程、交通资源、优先规则和异常改派之间的领域事件契约。

聚合对象包括`venue_schedule`、`travel_request`、`transport_resource`、`journey_commitment`。事件类型包括`SCHEDULE_FROZEN`、`REQUEST_ACCEPTED`、`RESOURCE_RESERVED`、`JOURNEY_REPLANNED`、`COMMITMENT_CLOSED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `REQUEST_ACCEPTED`：载荷还需包含 `traveler_role`, `required_arrival`。
- `RESOURCE_RESERVED`：载荷还需包含 `resource_ref`, `capacity`。
- `JOURNEY_REPLANNED`：载荷还需包含 `affected_segments`, `reason`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。

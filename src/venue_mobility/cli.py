"""命令行入口：事件校验、场景脚本执行与审计查询。

用法：

* 校验：     python -m venue_mobility.cli validate <schema.json> <event.json>
* 执行脚本： python -m venue_mobility.cli run <scenario.json> <commands.json> [--store events.log]
* 审计：     python -m venue_mobility.cli audit <scenario.json> <store> <explain|ledger|changes|pending|events|snapshot> [ref]

不带子命令时等价于 ``validate``，保持与旧用法兼容。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .api import ServiceHub
from .contracts import validate_event


def _read_json(path: str) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _validate(schema_path: str, event_path: str) -> int:
    schema = _read_json(schema_path)
    event = _read_json(event_path)
    issues = validate_event(event, schema)
    if not issues:
        print("valid")
        return 0
    for issue in issues:
        print(f"{issue.field}	{issue.code}	{issue.message}")
    return 1


def _run(scenario_path: str, commands_path: str, store: str | None) -> int:
    hub = ServiceHub.from_files(scenario_path, store=store)
    commands = _read_json(commands_path)
    results = hub.run_script(commands)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item.get("ok") or item.get("error") == "service_rule" for item in results) else 1


def _audit(scenario_path: str, store: str, kind: str, ref: str | None) -> int:
    hub = ServiceHub.from_files(scenario_path, store=store)
    service = hub.service
    if kind == "explain":
        if not ref:
            print("explain 需要业务编号或人员编号", file=sys.stderr)
            return 2
        output = service.explain(ref)
    elif kind == "ledger":
        output = service.resource_ledger()
    elif kind == "changes":
        output = {"changes": service.changes()}
    elif kind == "pending":
        output = {"now": service.clock.now.isoformat(), "obligations": service.pending_obligations()}
    elif kind == "events":
        output = {"events": list(service.events)}
    elif kind == "snapshot":
        output = hub.snapshot()
    else:
        print(f"未知审计命令：{kind}", file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "validate":
        if len(args) != 3:
            print("用法: cli.py validate <schema.json> <event.json>", file=sys.stderr)
            return 2
        return _validate(args[1], args[2])
    if args and args[0] == "run":
        if len(args) < 3:
            print("用法: cli.py run <scenario.json> <commands.json> [--store events.log]", file=sys.stderr)
            return 2
        store = None
        if "--store" in args:
            store = args[args.index("--store") + 1]
        return _run(args[1], args[2], store)
    if args and args[0] == "audit":
        if len(args) < 4:
            print("用法: cli.py audit <scenario.json> <store> <kind> [ref]", file=sys.stderr)
            return 2
        return _audit(args[1], args[2], args[3], args[4] if len(args) > 4 else None)
    # 旧用法：schema + event
    if len(args) == 2:
        return _validate(args[0], args[1])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

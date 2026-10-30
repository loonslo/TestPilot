"""Host commands for F1-F6 unified task lifecycle."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

from api_test_agent.model import ModelError
from api_test_agent.tasks.models import CaseRevision, ReviewDecision, Task
from api_test_agent.tasks.store import TaskStore


def register_commands(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("task", help="统一任务与用例资产（F1-F6）")
    parser.add_argument("--root", type=Path, default=Path("runtime/tasks"))
    actions = parser.add_subparsers(dest="task_action", required=True)
    create = actions.add_parser("create", help="从空测试集登记任务，不读取目标源码")
    create.add_argument("task_id")
    create.add_argument("--project")
    create.add_argument("--commit", help="用户指定的完整提交 ID；F2 再核验")
    create.add_argument("--scope", nargs="+", default=[])
    create.add_argument("--environment")
    show = actions.add_parser("show")
    show.add_argument("task_id")
    add = actions.add_parser("import-case", help="导入统一格式的新用例修订，不批准或执行")
    add.add_argument("task_id")
    add.add_argument("file", type=Path)
    configure = actions.add_parser("configure", help="为草稿选择目标；不读取目标")
    configure.add_argument("task_id")
    for flag in ("project", "commit", "repository", "environment"):
        configure.add_argument(f"--{flag}")
    for flag in ("scope", "source-paths", "document-paths"):
        configure.add_argument(f"--{flag}", nargs="+")
    validate = actions.add_parser("validate-target", help="检查目标登记完整性，不访问源码")
    validate.add_argument("task_id")
    review = actions.add_parser("review-state", help="提交/拒绝/暂缓最新修订；不批准执行")
    review.add_argument("task_id")
    review.add_argument("--hash", required=True)
    review.add_argument(
        "--decision", choices=["pending_review", "rejected", "deferred"], required=True
    )
    review.add_argument("--actor", required=True)
    review.add_argument("--reason", required=True)
    state = actions.add_parser("state", help="记录阻塞、终止或重新准备")
    state.add_argument("task_id")
    state.add_argument("status", choices=["blocked", "aborted", "created"])
    state.add_argument("--reason", required=True)
    discover = actions.add_parser("discover", help="离线读取所选快照与资料，发现接口")
    discover.add_argument("task_id")
    discover.add_argument("--materials-root", type=Path)
    material = actions.add_parser("materials", help="导出受字符预算限制的实际模型材料")
    material.add_argument("task_id")
    material.add_argument("--max-chars", type=int, default=40000)
    design = actions.add_parser("design", help="构造用例设计请求或导入生成器响应；不执行测试")
    design.add_argument("task_id")
    generation = design.add_mutually_exclusive_group()
    generation.add_argument("--response", type=Path)
    generation.add_argument("--model-config", type=Path)
    design.add_argument("--max-chars", type=int, default=60000)
    design.add_argument("--operations", nargs="+", help="本批接口 ID；默认前十项，其余保留待设计")
    review = actions.add_parser("review", help="导出完整待审核材料和修订清单")
    review.add_argument("task_id")
    review.add_argument("--execution-config", type=Path)
    recover = actions.add_parser("design-recover", help="核对并补齐中断的草稿导入，不重调模型")
    recover.add_argument("task_id")
    recover.add_argument("attempt_id")
    approve = actions.add_parser("approve", help="宿主记录人工决定并冻结本轮执行集合")
    approve.add_argument("task_id")
    approve.add_argument("decisions", type=Path)
    approve.add_argument("--execution-config", type=Path, required=True)
    verify = actions.add_parser("verify-approval", help="复核冻结批准；不发送请求")
    verify.add_argument("task_id")
    verify.add_argument("approval_id")
    verify.add_argument("--execution-config", type=Path, required=True)
    execute_cmd = actions.add_parser("execute", help="执行冻结的批准清单")
    execute_cmd.add_argument("task_id")
    execute_cmd.add_argument("--approval", help="批准编号；默认读取当前活跃批准")
    execute_cmd.add_argument("--execution-config", type=Path, help="执行配置路径；默认使用快照配置")
    execute_cmd.add_argument("--max-cases", type=int, default=None, help="最多执行用例数")
    execute_cmd.add_argument("--stop-on-failure", action="store_true", help="遇首个失败停止")
    report_cmd = actions.add_parser("report", help="查看或导出当前或最终报告")
    report_cmd.add_argument("task_id")
    report_cmd.add_argument("--execution-id", help="指定执行编号；默认读取最新执行")
    report_cmd.add_argument("--output-dir", type=Path, help="报告导出目录；默认保存在任务目录内")
    parser.set_defaults(task_handler=execute_handler)


def execute_handler(args: argparse.Namespace) -> int:
    store = TaskStore(args.root)
    try:
        if args.task_action in {"approve", "verify-approval"}:
            from api_test_agent.tasks.approval import (
                ExecutionConfig,
                ReviewSubmission,
                approve,
                check_approval,
            )

            config = ExecutionConfig.load(args.execution_config)
            if args.task_action == "approve":
                submission = ReviewSubmission.model_validate_json(args.decisions.read_text("utf-8"))
                print(approve(store, args.task_id, submission, config))
            else:
                snapshot = check_approval(store, args.task_id, args.approval_id, config)
                print(f"approval valid; selected={len(snapshot.cases)}; no requests sent")
        elif args.task_action == "execute":
            from api_test_agent.tasks.approval import ExecutionConfig
            from api_test_agent.tasks.execution import execute_task

            config = ExecutionConfig.load(args.execution_config) if args.execution_config else None
            accounting = execute_task(
                store,
                args.task_id,
                approval_id=args.approval,
                config=config,
                max_cases=args.max_cases,
                stop_on_failure=args.stop_on_failure,
            )
            print(accounting.model_dump_json(indent=2))
        elif args.task_action == "report":
            from api_test_agent.tasks.report import generate_report

            rep, md_path, json_path = generate_report(
                store,
                args.task_id,
                execution_id=args.execution_id,
                output_dir=args.output_dir,
            )
            print(f"report written: md={md_path} json={json_path}")
            print(rep.accounting.model_dump_json(indent=2))
        elif args.task_action == "design-recover":
            from api_test_agent.tasks.design import recover_design

            print(recover_design(store, args.task_id, args.attempt_id))
        elif args.task_action == "design":
            from api_test_agent.tasks.design import design
            from api_test_agent.tasks.design_model import live_generator

            response = args.response.read_text("utf-8") if args.response else None
            generator = live_generator(args.model_config) if args.model_config else None
            print(
                design(
                    store,
                    args.task_id,
                    generator,
                    max_chars=args.max_chars,
                    supplied_response=response,
                    operation_ids=set(args.operations) if args.operations else None,
                )
            )
        elif args.task_action == "review":
            from api_test_agent.tasks.approval import ExecutionConfig
            from api_test_agent.tasks.design import export_review

            config = ExecutionConfig.load(args.execution_config) if args.execution_config else None
            print(export_review(store, args.task_id, config))
        elif args.task_action == "discover":
            from api_test_agent.tasks.discovery import discover

            print(discover(store, args.task_id, args.materials_root))
        elif args.task_action == "materials":
            from api_test_agent.tasks.discovery import Inventory, model_materials

            path = store._folder(args.task_id) / "inventory.json"
            inventory = Inventory.model_validate_json(path.read_text("utf-8"))
            print(model_materials(inventory, args.max_chars))
        elif args.task_action == "create":
            task = Task(
                task_id=args.task_id,
                target_project=args.project,
                target_commit=args.commit,
                scope=tuple(args.scope),
                environment=args.environment,
            )
            print(store.create(task))
        elif args.task_action == "show":
            print(store.load(args.task_id).model_dump_json(indent=2))
            print(
                json.dumps({"review_states": store.review_states(args.task_id)}, ensure_ascii=False)
            )
        elif args.task_action == "configure":
            mapping = {"project": "target_project", "commit": "target_commit"}
            fields = (
                "project",
                "commit",
                "repository",
                "environment",
                "scope",
                "source_paths",
                "document_paths",
            )
            updates = {
                mapping.get(k, k): getattr(args, k) for k in fields if getattr(args, k) is not None
            }
            print(store.configure(args.task_id, **updates).model_dump_json(indent=2))
        elif args.task_action == "validate-target":
            store.load(args.task_id).require_target()
            print("target registration complete; repository identity is not yet verified")
        elif args.task_action == "state":
            print(
                store.transition(args.task_id, args.status, args.reason).model_dump_json(indent=2)
            )
        elif args.task_action == "review-state":
            decision = ReviewDecision(
                task_id=args.task_id,
                revision_hash=args.hash,
                decision=args.decision,
                actor=args.actor,
                at=datetime.now(UTC),
                reason=args.reason,
            )
            print(store.record_review(decision))
        else:
            case = CaseRevision.model_validate_json(args.file.read_text("utf-8"))
            print(store.add_case(args.task_id, case))
            print(f"revision_hash={case.fingerprint()}; review=draft; execution=not_run")
        return 0
    except ModelError as exc:
        print(f"task model error: {exc.kind}", file=sys.stderr)
        return 2
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"task error: {exc}", file=sys.stderr)
        return 2

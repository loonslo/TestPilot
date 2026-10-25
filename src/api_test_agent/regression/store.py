"""运行状态存储：中间结果按 run_id 保存为 JSON 文件，无需工作流引擎和数据库。

目录结构：
    <artifacts>/<run_id>/
        context.json     采集上下文（含 diff 摘要，便于复核）
        analysis.json    模型结构化输出
        prompt.txt       实际发送给模型的提示词（已排除凭据）
        plan.json        回归计划（唯一可被执行的文件）
        run.json         运行记录（真实执行结果）
        report.md        Markdown 报告
        log.txt          关键步骤与中断原因
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def run_dir(artifacts: Path, run_id: str) -> Path:
    path = artifacts / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, model: BaseModel | dict[str, Any]) -> Path:
    data = model.model_dump(mode="json") if isinstance(model, BaseModel) else model
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def append_log(directory: Path, message: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    path = directory / "log.txt"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}Z] {message}\n")


def list_runs(artifacts: Path) -> list[Path]:
    if not artifacts.is_dir():
        return []
    return sorted((p for p in artifacts.iterdir() if p.is_dir()), reverse=True)

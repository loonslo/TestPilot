"""Render the current TestPilot overview and workflow; optional matplotlib only."""

from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("diagram_style", HERE / "regression/make_diagrams.py")
assert spec is not None and spec.loader is not None
style = importlib.util.module_from_spec(spec)
spec.loader.exec_module(style)


def canvas(title: str, subtitle: str, height: float):
    fig, ax = style.plt.subplots(figsize=(18, height))
    fig.patch.set_facecolor("#ffffff")
    ax.set(xlim=(0, 100), ylim=(-4, 100))
    ax.axis("off")
    style.label(ax, 3, 97, title, size=23, color=style.INK, weight="bold")
    style.label(ax, 3, 92.5, subtitle, size=11)
    return fig, ax


def node(ax, x, y, title, lines, color="load", h=13):
    colors = {
        "load": (style.C_LOAD, style.C_LOAD_EDGE),
        "input": (style.C_INPUT, style.C_INPUT_EDGE),
        "analyze": (style.C_ANALYZE, style.C_ANALYZE_EDGE),
        "run": (style.C_RUN, style.C_RUN_EDGE),
        "human": (style.C_HUMAN, style.C_HUMAN_EDGE),
        "report": (style.C_REPORT, style.C_REPORT_EDGE),
    }
    style.box(ax, x, y, 42, h, title, lines, *colors[color], title_size=13, body_size=10)


def save(fig, name):
    fig.savefig(HERE / f"{name}.png", dpi=180, bbox_inches="tight", facecolor="white")
    with (HERE / f"{name}.svg").open("w", encoding="utf-8", newline="\n") as output:
        fig.savefig(output, format="svg", bbox_inches="tight", facecolor="white")
    style.plt.close(fig)
    print(f"Rendered {name}.png and {name}.svg")


def architecture():
    fig, ax = canvas(
        "TestPilot · 最新整体架构",
        "2026-10-25 · 按现有实现绘制 · 一个命令入口，两种测试机制，明确的数据与判定边界",
        12,
    )
    style.box(
        ax,
        29,
        80,
        42,
        9,
        "唯一入口：uv run testpilot",
        [],
        style.C_LOAD,
        style.C_LOAD_EDGE,
        title_size=15,
    )
    style.arrow(ax, (39, 80), (24, 76))
    style.arrow(ax, (61, 80), (74, 76))
    node(
        ax,
        3,
        63,
        "探索输入 · agent run",
        ["公开接口规格、规则、任务", "config.local.toml；模型不读取被测源码"],
        "input",
    )
    node(
        ax,
        55,
        63,
        "回归输入 · plan",
        ["base/head 差异、业务资料、模块映射", "testagent.toml；用例目录与固定冒烟集"],
        "input",
    )
    node(
        ax,
        3,
        44,
        "受限 Agent 循环",
        ["调用前预算 + 模型 + 四个工具", "查规格 / 查操作 / HTTP / 提交用例"],
        "analyze",
    )
    node(
        ax,
        55,
        44,
        "版本影响分析与计划",
        ["离线桩 / 无 AI / 获批后调用 DeepSeek", "编号校验、合并冒烟、计划指纹与覆盖缺口"],
        "analyze",
    )
    node(
        ax,
        3,
        25,
        "本机沙箱与独立判定",
        ["合成数据服务；工具限制访问本机白名单", "用例在参考/目标构建重放；评分资料隔离"],
        "run",
    )
    node(
        ax,
        55,
        25,
        "人工确认与已登记用例执行",
        ["修改计划后 reconcile；run 再检查守卫", "执行当前配置工作区的 pytest；不自动切版本"],
        "human",
    )
    node(
        ax,
        3,
        7,
        "探索证据",
        ["cases.json / trace.jsonl / report.md", "可选 replay.json；状态与缺陷判定分开"],
        "report",
    )
    node(
        ax,
        55,
        7,
        "回归证据",
        ["context / analysis / plan / run / report", "失败、异常、未执行、未覆盖如实区分"],
        "report",
    )
    for x in (24, 76):
        for top, bottom in ((63, 57), (44, 38), (25, 20)):
            style.arrow(ax, (x, top), (x, bottom))
    style.label(
        ax,
        50,
        2.4,
        "共用只读 subjects/FinUnityServer；探索基准固定 55cf875，回归按指定 base/head 采集",
        size=11,
        ha="center",
    )
    style.label(
        ax,
        50,
        -1.2,
        "当前缺口：探索用例 → 回归 pytest 目录的转换与登记尚未实现，不能自动串成闭环",
        size=11,
        ha="center",
        color=style.C_HUMAN_EDGE,
    )
    save(fig, "architecture")


def workflow():
    fig, ax = canvas(
        "TestPilot · 最新运行流程",
        "2026-10-25 · 左：探索循环；右：先计划后执行 · 本次仅使用离线桩与测试替身",
        16,
    )
    style.label(ax, 24, 87.5, "testpilot agent run [--replay]", size=15, ha="center", weight="bold")
    style.label(ax, 76, 87.5, "testpilot plan → run → show", size=15, ha="center", weight="bold")
    left = [
        (
            "1  准备任务与沙箱",
            ["加载公开规格、任务、范围与限额", "创建合成数据场景与受控 HTTP 工具"],
            "input",
        ),
        (
            "2  调用前检查",
            ["本次/每日调用数、时间、token、费用与间隔", "到限则停止；失败尝试同样计数"],
            "load",
        ),
        (
            "3  模型与工具循环",
            ["模型提议；宿主串行执行工具并返回结果", "有限错误重试；重复无进展时停止"],
            "analyze",
        ),
        (
            "4  按原因结束并清理",
            ["最终报告 / 到限 / 无进展 / 错误", "关闭 HTTP、停止场景；记录明确停止原因"],
            "run",
        ),
        (
            "5  保存探索证据",
            ["completed / partial / aborted", "结构化用例、模型轨迹、用量与报告"],
            "report",
        ),
        (
            "6  可选独立重放",
            ["有用例且要求 replay 才进行", "参考通过 + 目标失败 → detects"],
            "report",
        ),
    ]
    right = [
        (
            "1  采集发布差异",
            ["固定 base/head SHA，验证祖先关系", "排除敏感路径；记录资料缺失和截断"],
            "input",
        ),
        (
            "2  分析影响范围",
            ["--stub 离线 / --no-ai / 获批真实模型", "结构化输出校验；失败明确终止"],
            "analyze",
        ),
        (
            "3  生成回归计划",
            ["只接受已登记编号，固定冒烟始终保留", "保存 plan.json、指纹、缺口与待确认项"],
            "load",
        ),
        (
            "4  人工核对与修改",
            ["plan 阶段不执行测试", "修改用例后必须 reconcile 更新指纹"],
            "human",
        ),
        (
            "5  run 复核后执行",
            ["指纹 / 入口 / 版本引用 / 环境版本 / 确认", "不通过则拒绝；通过后逐条跑登记入口"],
            "run",
        ),
        (
            "6  保存回归证据",
            ["真实 passed / failed / error / skipped", "未覆盖和未执行不计入通过；报告可追溯"],
            "report",
        ),
    ]
    ys = [73, 59, 45, 31, 17, 3]
    for x, lane in ((3, left), (55, right)):
        for y, (title, lines, color) in zip(ys, lane, strict=True):
            node(ax, x, y, title, lines, color, h=12)
        for previous, following in zip(ys, ys[1:], strict=False):
            style.arrow(ax, (x + 21, previous), (x + 21, following + 12))
    style.arrow(ax, (45, 51), (45, 65), color=style.C_ANALYZE_EDGE, rad=0.65)
    style.label(
        ax,
        50,
        -1,
        "细节与失败分支见 architecture.md；回归执行不自动 checkout，实际环境须由使用者准备",
        size=11,
        ha="center",
    )
    save(fig, "workflow")


if __name__ == "__main__":
    architecture()
    workflow()

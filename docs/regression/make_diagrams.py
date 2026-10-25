"""生成 testagent 架构图（PNG），可重复执行。

    uv run --with matplotlib python docs/regression/make_diagrams.py

输出：
    docs/architecture.png   模块与数据流
    docs/two-phase.png      两阶段流程、拒绝条件与任务状态
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

HERE = Path(__file__).resolve().parent

CJK_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
]


def pick_font() -> FontProperties:
    for candidate in CJK_CANDIDATES:
        if Path(candidate).is_file():
            return FontProperties(fname=candidate)
    raise SystemExit("找不到中文字体，请修改 CJK_CANDIDATES")


FONT = pick_font()

INK = "#1f2933"
MUTED = "#52606d"
LINE = "#9aa5b1"

C_INPUT, C_INPUT_EDGE = "#eef2f7", "#7b8794"
C_LOAD, C_LOAD_EDGE = "#e8f0fb", "#3f6fa8"
C_COLLECT, C_COLLECT_EDGE = "#e9f5ef", "#2f7d55"
C_ANALYZE, C_ANALYZE_EDGE = "#fdf1e3", "#b26a1b"
C_PLAN, C_PLAN_EDGE = "#eef0fb", "#4b4f9c"
C_HUMAN, C_HUMAN_EDGE = "#fdeef1", "#a53d5c"
C_RUN, C_RUN_EDGE = "#e9f4f7", "#2b7a8c"
C_REPORT, C_REPORT_EDGE = "#f3f0f8", "#6b4c9a"
C_ART, C_ART_EDGE = "#fafbfc", "#c3cbd6"


def box(ax, x, y, w, h, title, lines, face, edge, title_size=11.0, body_size=7.9, dash=False):
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.22,rounding_size=1.0",
            linewidth=1.35,
            facecolor=face,
            edgecolor=edge,
            linestyle="--" if dash else "-",
            zorder=2,
        )
    )
    ax.text(
        x + w / 2,
        y + h - 1.5,
        title,
        ha="center",
        va="top",
        fontproperties=FONT,
        fontsize=title_size,
        color=INK,
        weight="bold",
        zorder=3,
    )
    for index, line in enumerate(lines):
        ax.text(
            x + w / 2,
            y + h - 4.9 - index * 2.5,
            line,
            ha="center",
            va="top",
            fontproperties=FONT,
            fontsize=body_size,
            color=MUTED,
            zorder=3,
        )


def arrow(ax, start, end, color=LINE, width=1.4, rad=0.0):
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle="-|>",
            mutation_scale=13,
            linewidth=width,
            color=color,
            connectionstyle=f"arc3,rad={rad}",
            shrinkA=0.5,
            shrinkB=0.5,
            zorder=1,
        )
    )


def label(ax, x, y, text, size=8.6, color=MUTED, ha="left", va="center", weight="normal"):
    ax.text(
        x,
        y,
        text,
        ha=ha,
        va=va,
        fontproperties=FONT,
        fontsize=size,
        color=color,
        weight=weight,
        zorder=4,
    )


# --------------------------------------------------------------------------- 图一


def architecture() -> Path:
    fig, ax = plt.subplots(figsize=(15.4, 9.4))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    label(ax, 2, 98.4, "testagent V0.1 · 模块与数据流", size=19, color=INK, weight="bold")
    label(
        ax, 2, 95.2, "单进程 Python，按固定顺序调用；每一步的产物落盘为 JSON，可逐条核对", size=9.8
    )

    label(ax, 2, 92.2, "输入（项目资产，Git 管理）", size=10.4, color=INK, weight="bold")
    label(ax, 32, 92.2, "模块（固定顺序，不做自由规划）", size=10.4, color=INK, weight="bold")
    label(ax, 70, 92.2, "产物 artifacts/<run_id>/", size=10.4, color=INK, weight="bold")

    # 左列
    inputs = [
        (80.5, "项目配置 testagent.toml", ["仓库 / 文档 / 套件 / 预算", "模型端点 · 密钥变量名"]),
        (67.0, "项目资产 assets/", ["modules.yaml 模块映射", "cases · smoke 配置"]),
        (53.5, "业务资料 docs/", ["核心流程 · 业务规则", "接口说明（MD / OpenAPI）"]),
        (40.0, "本地 Git 仓库", ["base_sha ── head_sha", "两端固定，不看未提交内容"]),
    ]
    for y0, title, lines in inputs:
        box(ax, 2, y0, 25, 9.6, title, lines, C_INPUT, C_INPUT_EDGE, title_size=9.8, body_size=7.8)
        arrow(ax, (27, y0 + 4.8), (29.2, y0 + 4.8), color=C_INPUT_EDGE, width=1.0)
    ax.plot(
        [29.2, 29.2],
        [inputs[-1][0] + 4.8, 85.3],
        color=C_INPUT_EDGE,
        linewidth=1.0,
        solid_capstyle="round",
        zorder=1,
    )
    arrow(ax, (29.2, 85.3), (32, 85.3), color=C_INPUT_EDGE, width=1.0)

    # 中列
    modules = [
        (
            80.5,
            "project",
            ["加载配置、模块映射、用例目录、冒烟集", "只读；相对路径按配置文件解析"],
            C_LOAD,
            C_LOAD_EDGE,
        ),
        (
            67.0,
            "collect",
            ["git diff + 相关文件 + 业务文档", "排除凭据与运行目录；超预算如实记录"],
            C_COLLECT,
            C_COLLECT_EDGE,
        ),
        (
            53.5,
            "analyze",
            ["1 次模型调用 → 结构化影响", "证据强度 · 代码位置 · 待确认项"],
            C_ANALYZE,
            C_ANALYZE_EDGE,
        ),
        (
            40.0,
            "plan",
            ["只认已登记编号 · 合并固定冒烟集", "缺口单列 · 计划指纹防篡改"],
            C_PLAN,
            C_PLAN_EDGE,
        ),
        (
            21.0,
            "runner",
            ["只跑用例目录登记的 pytest 入口", "异常 / 超时 ≠ 通过；失败不盲目重试"],
            C_RUN,
            C_RUN_EDGE,
        ),
        (
            7.4,
            "report",
            ["版本 · 证据 · 实际结果 · 覆盖缺口", "未执行不写成通过；stub 模式标注"],
            C_REPORT,
            C_REPORT_EDGE,
        ),
    ]
    for y0, title, lines, face, edge in modules:
        box(ax, 32, y0, 34, 9.6, title, lines, face, edge)

    for upper, lower in [(80.5, 76.6), (67.0, 63.1), (53.5, 49.6)]:
        arrow(ax, (49, upper), (49, lower), color=LINE)
    arrow(ax, (49, 30.6), (49, 26.0), color=LINE)
    arrow(ax, (49, 21.0), (49, 17.0), color=LINE)

    # 人工关卡
    ax.add_patch(
        FancyBboxPatch(
            (29, 33.6),
            40,
            3.8,
            boxstyle="round,pad=0.18,rounding_size=0.9",
            linewidth=1.5,
            facecolor=C_HUMAN,
            edgecolor=C_HUMAN_EDGE,
            linestyle="--",
            zorder=2,
        )
    )
    label(
        ax,
        49,
        35.5,
        "两阶段边界：人核对 / 修改 plan.json 后显式确认",
        size=9.4,
        color=C_HUMAN_EDGE,
        ha="center",
        weight="bold",
    )
    arrow(ax, (49, 40.0), (49, 37.4), color=C_HUMAN_EDGE, width=1.5)
    arrow(ax, (49, 33.6), (49, 30.6), color=C_HUMAN_EDGE, width=1.5)

    # 右列
    artifacts = [
        (68.2, "context.json", ["两个 SHA、文件列表、diff"], C_COLLECT_EDGE),
        (54.7, "prompt.txt · analysis.json", ["发送的输入 + 结构化输出"], C_ANALYZE_EDGE),
        (41.2, "plan.json", ["唯一可被执行的文件（含指纹）"], C_PLAN_EDGE),
        (22.2, "run.json", ["真实计数、断言、日志、耗时"], C_RUN_EDGE),
        (8.6, "report.md · log.txt", ["Markdown 报告 + 步骤日志"], C_REPORT_EDGE),
    ]
    for y0, title, lines, edge in artifacts:
        box(ax, 70, y0, 28, 7.4, title, lines, C_ART, C_ART_EDGE, title_size=9.4, body_size=7.6)
        arrow(ax, (66, y0 + 3.7), (70, y0 + 3.7), color=edge, width=1.0)

    # 底部约束
    label(
        ax,
        2,
        3.2,
        "硬约束：模型只建议编号，编号真实性由 plan 校验｜固定冒烟集永远保留｜异常与未执行不计入通过数",
        size=8.6,
    )
    label(
        ax,
        2,
        1.0,
        "凭据不进入配置、提示词与产物｜版本、计划指纹、环境版本任一漂移，即拒绝沿用旧结果",
        size=8.6,
    )

    out = HERE / "architecture.png"
    fig.savefig(out, dpi=190, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


# --------------------------------------------------------------------------- 图二


def two_phase() -> Path:
    fig, ax = plt.subplots(figsize=(15.0, 8.0))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    label(ax, 2, 96.5, "两阶段操作：计划与执行分离", size=19, color=INK, weight="bold")
    label(
        ax,
        2,
        92.8,
        "没有任何自动流程可以跨过人工确认；下列任一情况发生，就拒绝沿用原计划或原结果",
        size=9.8,
    )

    label(
        ax,
        2,
        84.5,
        "阶段一  plan（只读仓库，不执行测试）",
        size=10,
        color=C_COLLECT_EDGE,
        weight="bold",
    )
    label(ax, 74, 84.5, "阶段二  run（执行已确认的计划）", size=10, color=C_RUN_EDGE, weight="bold")

    steps = [
        (2, 16, "collect", ["固定两个 SHA", "diff + 上下文"], C_COLLECT, C_COLLECT_EDGE),
        (21, 16, "analyze", ["1 次模型调用", "结构化影响分析"], C_ANALYZE, C_ANALYZE_EDGE),
        (40, 16, "plan", ["校验用例编号", "冒烟集 + 指纹"], C_PLAN, C_PLAN_EDGE),
        (59, 11, "人 确认", ["查看 / 增删", "必要时 reconcile"], C_HUMAN, C_HUMAN_EDGE),
        (73, 11, "run", ["复核后执行", "只跑登记入口"], C_RUN, C_RUN_EDGE),
        (87, 11, "report", ["证据报告", "未覆盖单列"], C_REPORT, C_REPORT_EDGE),
    ]
    for x, w, title, lines, face, edge in steps:
        box(ax, x, 64, w, 16, title, lines, face, edge, title_size=10.6, body_size=7.8)

    for start_x, end_x in [(18, 21), (37, 40), (56, 59), (70, 73), (84, 87)]:
        arrow(ax, (start_x, 72), (end_x, 72), color=LINE, width=1.5)

    # 拒绝条件
    label(ax, 2, 57.5, "拒绝条件（守卫）", size=10.4, color=INK, weight="bold")
    guards = [
        ("head 提交移动", "计划里的 head_sha ≠ 当前引用 → 重新 plan"),
        ("计划被手工修改", "指纹不匹配 → 先执行 testagent reconcile"),
        ("用例入口漂移", "编号消失或 entry 改名 → 更新目录后重新 plan"),
        ("环境版本未提供", "拒绝执行；或显式 --allow-unconfirmed-env 并标注"),
        ("非交互终端", "未加 --skip-confirm → 退出码 3，不执行任何用例"),
        ("执行中出现异常", "超时 / 启动失败记为 error，不计入通过数"),
    ]
    for index, (title, body) in enumerate(guards):
        y0 = 46.5 - index * 7.0
        ax.add_patch(
            FancyBboxPatch(
                (2, y0),
                56,
                6.0,
                boxstyle="round,pad=0.18,rounding_size=0.8",
                linewidth=1.1,
                facecolor="#fff8f8",
                edgecolor="#c98a92",
                zorder=2,
            )
        )
        label(ax, 4, y0 + 4.1, title, size=9.2, color="#8c2f3f", weight="bold")
        label(ax, 4, y0 + 1.7, body, size=8.2, color=MUTED)

    # 任务状态
    label(ax, 62, 57.5, "任务状态（与测试结果分开记录）", size=10.4, color=INK, weight="bold")
    states = [
        (46.5, "planned", "已生成计划，等待人工确认", C_PLAN, C_PLAN_EDGE),
        (38.2, "running", "执行中", C_RUN, C_RUN_EDGE),
        (29.9, "completed", "已完成（不代表测试全通过）", C_COLLECT, C_COLLECT_EDGE),
        (21.6, "aborted", "执行异常中断", C_HUMAN, C_HUMAN_EDGE),
        (13.3, "incomplete", "未完成（部分用例未执行）", "#fdf6e8", C_ANALYZE_EDGE),
    ]
    for y0, name, desc, face, edge in states:
        ax.add_patch(
            FancyBboxPatch(
                (62, y0),
                36,
                6.4,
                boxstyle="round,pad=0.18,rounding_size=0.8",
                linewidth=1.1,
                facecolor=face,
                edgecolor=edge,
                zorder=2,
            )
        )
        label(ax, 64, y0 + 4.2, name, size=9.4, color=INK, weight="bold")
        label(ax, 64, y0 + 1.8, desc, size=8.2, color=MUTED)

    label(
        ax,
        2,
        3.0,
        "结果只来自真实执行：每条用例单独跑一次，失败与异常都进入报告；没有执行到的用例标为“未执行”。",
        size=8.6,
    )

    out = HERE / "two-phase.png"
    fig.savefig(out, dpi=190, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


def main() -> int:
    print(architecture())
    print(two_phase())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""命令行入口（typer + rich）。

日常只需 `translate` 一个命令：连续全流程（分析→翻译→审校→一致性 QA→报告→回填 EPUB），
中断后再次运行自动续跑。其余 `resume` / `status` 为常用辅助；
细粒度/调试工具收敛到 `tools`：glossary / assemble / qa / report。
"""

from __future__ import annotations

import os
from typing import Optional

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from .config import Config
from .ingest.segmenter import load_document
from .pipeline.runstore import STATUS_DONE, RunStore, slugify

app = typer.Typer(add_completion=False, help="多 Agent 小说翻译系统（多语言 → 中文）")
tools_app = typer.Typer(
    add_completion=False,
    help="高级/调试工具：glossary（术语表）/ assemble（回填）/ qa / report",
)
console = Console()

_CONFIG = {"path": "config.yaml"}


@app.callback()
def _root(
    config: str = typer.Option("config.yaml", "--config", "-c", help="配置文件路径"),
):
    _CONFIG["path"] = config


def _load_config() -> Config:
    return Config.load(_CONFIG["path"])


def _require_input_file(input_path: str) -> None:
    if not os.path.isfile(input_path):
        console.print(f"[red]输入文件不存在：{input_path}[/]")
        raise typer.Exit(1)


def _runstore_for(config: Config, input_path: str) -> RunStore:
    _require_input_file(input_path)
    doc = load_document(input_path, config.source_lang, config.target_lang)
    run_dir = os.path.join(config.state_dir, slugify(doc.title))
    return RunStore(run_dir, create=False)


def _open_glossary(store: RunStore):
    """Open an existing run database and turn identity failures into CLI errors."""
    try:
        return store.open_glossary()
    except Exception as exc:
        console.print(f"[red]术语库状态无效：{exc}[/]")
        raise typer.Exit(1) from exc


def _translate_impl(
    input_path: str,
    *,
    chapter: Optional[int] = None,
    fmt: str = "epub",
    out: Optional[str] = None,
    polish: Optional[bool] = None,
    qa: Optional[bool] = None,
) -> None:
    """translate/resume 共享实现，避免 CLI 参数转发漂移。"""
    from .pipeline.orchestrator import Orchestrator

    _require_input_file(input_path)
    config = _load_config()
    if polish is not None:
        config.pipeline.polish = polish
    orch = Orchestrator(config)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("段"),
        TimeElapsedColumn(),
        console=console,
    ) as prog:
        task = prog.add_task("准备中…", total=None)

        def cb(done: int, total: int, label: str) -> None:
            prog.update(task, completed=done, total=total or None, description=label)

        if chapter is not None:
            store = orch.run(input_path, only_chapter=chapter, progress=cb)
            console.print(f"[green]已翻第 {chapter} 章[/]，状态目录：{store.run_dir}")
            return

        result = orch.run_all(
            input_path,
            progress=cb,
            out_format=fmt,
            out_path=out,
            do_qa=qa,
        )

    s = result["report"]["summary"]
    console.print(
        f"[bold green]完成[/]：{s['chapters_done']}/{s['chapters_total']} 章，"
        f"术语 {s['terms']}，一致性问题 {len(result['qa_issues'])} 项。"
    )
    if s.get("glossary_pending"):
        console.print(
            f"[yellow]仍有 {s['glossary_pending']} 章术语抽取待续跑；"
            "已完成译文不会重翻。[/]"
        )
    if s.get("titles_pending"):
        console.print(
            "[yellow]章节标题仍待按最新术语生成；本次导出使用源标题。"
            "再次续跑会重试标题阶段。[/]"
        )
    console.print(f"译文：[bold]{result['output']}[/]")


# ── translate / resume：连续全流程 ──────────────────────────────────────────
@app.command()
def translate(
    input: str = typer.Argument(..., help="输入文件（.epub / .txt / .md）"),
    chapter: Optional[int] = typer.Option(
        None, "--chapter", help="只翻指定章（调试用，不做收尾）"
    ),
    fmt: str = typer.Option("epub", "--format", help="输出格式：epub | txt"),
    out: Optional[str] = typer.Option(None, "--out", help="输出路径（默认 <源文件名>.zh.<ext>，落在源文件目录）"),
    polish: Optional[bool] = typer.Option(
        None,
        "--polish/--no-polish",
        help="覆盖配置文件中的润色开关",
    ),
    qa: Optional[bool] = typer.Option(
        None,
        "--qa/--no-qa",
        help="覆盖配置文件中的一致性 QA 开关",
    ),
):
    """翻译（连续全流程；可断点续跑）。"""
    _translate_impl(input, chapter=chapter, fmt=fmt, out=out, polish=polish, qa=qa)


@app.command()
def resume(
    input: str = typer.Argument(..., help="输入文件"),
    fmt: str = typer.Option("epub", "--format", help="输出格式：epub | txt"),
    out: Optional[str] = typer.Option(None, "--out", help="输出路径"),
    polish: Optional[bool] = typer.Option(
        None,
        "--polish/--no-polish",
        help="覆盖配置文件中的润色开关",
    ),
    qa: Optional[bool] = typer.Option(
        None,
        "--qa/--no-qa",
        help="覆盖配置文件中的一致性 QA 开关",
    ),
):
    """断点续跑（等价于再次 translate）。"""
    _translate_impl(input, fmt=fmt, out=out, polish=polish, qa=qa)


# ── 查询 / 细粒度命令 ──────────────────────────────────────────────────────
@app.command()
def status(input: str = typer.Argument(..., help="输入文件")):
    """查看各章进度与术语库统计。"""
    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 translate。[/]")
        raise typer.Exit(1)
    m = store.load_manifest()
    console.print(
        f"《{m['title']}》（{m['fmt']}）  {m['source_lang']}→{m['target_lang']}"
    )
    table = Table("", "#", "章节", "翻译", "术语")
    for c in m["chapters"]:
        mark = "✓" if c["status"] == STATUS_DONE else "·"
        table.add_row(
            mark,
            str(c["index"]),
            c["title"],
            c["status"],
            c.get("glossary_status", "legacy"),
        )
    console.print(table)
    g = _open_glossary(store)
    console.print("术语库：", g.stats())
    g.close()


@tools_app.command()
def glossary(
    input: str = typer.Argument(..., help="输入文件"),
    action: str = typer.Argument(
        "list", help="list | conflicts | audit | lock | resolve"
    ),
    arg1: Optional[str] = typer.Argument(None),
    arg2: Optional[str] = typer.Argument(None),
):
    """术语库管理。audit 自动统一译法并改写正文。"""
    from .glossary import resolver
    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 translate。[/]")
        raise typer.Exit(1)
    g = _open_glossary(store)
    try:
        if action == "list":
            table = Table("原文", "译文", "类型", "置信/状态", "锁")
            for t in g.all_terms():
                table.add_row(
                    t.source,
                    t.target,
                    f"{t.type}{'/' + t.gender if t.gender else ''}",
                    f"{t.confidence}{'/' + t.status if t.status != 'ok' else ''}",
                    "🔒" if t.locked else "",
                )
            console.print(table)
        elif action == "conflicts":
            for c in g.open_conflicts():
                console.print(
                    f"  {c['source']}: 现有「{c['existing_target']}」 vs "
                    f"提议「{c['proposed_target']}」（第{c['chapter']}章）"
                )
        elif action == "audit":
            from .agents.glossary_auditor import GlossaryAuditor
            from .llm.base import build_client

            applied = GlossaryAuditor(build_client(config), config).audit(store, g)
            console.print(f"已统一 {len(applied)} 组术语：")
            for u in applied:
                console.print(
                    f"  {u['source']} → [bold]{u['canonical']}[/]"
                    f"（替换 {', '.join(u['variants']) or '—'}）"
                )
        elif action == "lock":
            if arg1 is None:
                console.print("[red]lock 需要提供原文术语。[/]")
                raise typer.Exit(1)
            resolver.lock(g, arg1)
            term = g.get_term(arg1)
            if term is None:
                console.print(f"[red]术语不存在：{arg1}[/]")
                raise typer.Exit(1)
            console.print(f"已锁定 {arg1} → {term.target}")
        elif action == "resolve":
            if arg1 is None or arg2 is None:
                console.print("[red]resolve 需要提供原文术语和目标译名。[/]")
                raise typer.Exit(1)
            resolver.resolve(g, arg1, arg2)
            console.print(f"已裁定并锁定 {arg1} → {arg2}")
        else:
            console.print(f"[red]未知 glossary 子命令：{action}[/]")
            raise typer.Exit(1)
    finally:
        g.close()


@tools_app.command()
def assemble(
    input: str = typer.Argument(..., help="输入文件"),
    out: Optional[str] = typer.Option(None, "--out"),
    fmt: str = typer.Option("epub", "--format", help="epub | txt"),
):
    """回填生成译文文件（默认 EPUB）。"""
    from .assemble.writer import assemble as do_assemble

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 translate。[/]")
        raise typer.Exit(1)
    path = do_assemble(store, input, out_path=out, out_format=fmt)
    console.print(f"已生成译文：[bold]{path}[/]")


@tools_app.command()
def qa(input: str = typer.Argument(..., help="输入文件")):
    """全书跨章一致性扫描。"""
    from .agents.consistency import ConsistencyChecker
    from .llm.base import build_client

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 translate。[/]")
        raise typer.Exit(1)
    g = _open_glossary(store)
    issues = ConsistencyChecker(build_client(config), config).check(store, g)
    g.close()
    console.print(f"一致性问题 {len(issues)} 项：")
    for it in issues:
        console.print(
            f"  [{it.get('type')}] {it.get('detail')}  ({it.get('where', '')})"
        )


@tools_app.command()
def report(input: str = typer.Argument(..., help="输入文件")):
    """生成 QA 报告（漏译/冲突/低置信度汇总）。"""
    from .assemble.report import build_report
    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 translate。[/]")
        raise typer.Exit(1)
    g = _open_glossary(store)
    rep = build_report(store, g)
    g.close()
    store.save_report(rep)
    s = rep["summary"]
    console.print(f"QA 报告已写入 {store.report_path}")
    console.print(
        f"  章节 {s['chapters_done']}/{s['chapters_total']}  术语 {s['terms']}  "
        f"待裁决冲突 {s['open_conflicts']}  审校问题 {s['review_issues']}  "
        f"回译疑点 {s['backtranslation_issues']}"
    )


app.add_typer(tools_app, name="tools")


def main() -> None:
    app()


if __name__ == "__main__":
    main()

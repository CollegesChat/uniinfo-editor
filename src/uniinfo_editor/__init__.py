import itertools
import logging
import re
import shlex
from collections.abc import Iterator
from datetime import datetime, timedelta
from importlib import metadata
from pathlib import Path
from string import Template
from typing import Any, Literal, NamedTuple

import chardet
import click
import pandas as pd
from click import Group
from click_repl import ClickCompleter
from prompt_toolkit import PromptSession
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from viztracer import log_sparse
from wenjuanxing_parser import (
    load_questions_from_yaml,  # FIXME: 加载csv太慢， VizTracer debug
)

# 从同级目录的 models.py 中导入解析器实体（增加了 ChosenOption）
from wenjuanxing_parser.models import (
    IP,
    BasicData,
    ChosenOption,
    QuestionnaireData,
    QuestionnaireResponse,
    UserAnswer,
)
from yaml12 import parse_yaml

# 类型别名定义
type ID = str
type IssueId = list[str] | None
type Changed = Literal['del']  # 移除了 'outdate'


class Stuffs(NamedTuple):
    issue_ids: IssueId
    changed: Changed


# =====================================================================
# ⚙️ 全局业务配置项（可在后续微调）
# =====================================================================
SCHOOL_QNUMS = {
    'v1': 6,  # v1版本问问卷中，学校名字所在的题号
    'v2': 8,  # v2版本问卷中，学校名字所在的题号
}

# 全局插件注册路由表
PLUGIN_COMMANDS: dict[str, click.Command] = {}


def register_plugin(cmd: click.Command) -> click.Command:
    """第三方插件用于注册自己的装饰器"""
    if not isinstance(cmd, click.Command):
        raise TypeError('插件必须提供合法的 click.Command 或 click.Group 实例！')

    callback_name = getattr(cmd.callback, '__name__', None) if cmd.callback else None
    cmd_name = cmd.name or callback_name

    if not cmd_name:
        raise ValueError(
            f'无法为插件 {cmd} 确定有效的命令名称！'
            f'请确保在创建命令时显式指定了 name（例如 @click.command("name")）。'
        )

    PLUGIN_COMMANDS[cmd_name.lower()] = cmd
    return cmd


def setup_logger() -> logging.Logger:
    logger = logging.getLogger('uniinfo')
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    ch = RichHandler(
        rich_tracebacks=True, markup=True, show_time=False, show_path=False
    )
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(logging.Formatter('%(message)s'))

    now_str = datetime.now().strftime('%Y-%m-%d_%H-%M')
    if not Path('logs').exists():
        Path('logs').mkdir()
    filename = f'./logs/uniinfo - {now_str}.log'
    fh = logging.FileHandler(filename, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter('%(levelname)s:  %(message)s'))

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger


def scan_folders(*folders: str) -> dict[str, Path]:
    pattern = {'.csv'}  # 移除了别名关联的 .txt 扫描
    if not folders:
        folders = ('university-information', 'questionnaires')

    gens: list[Iterator[Path]] = [
        Path(folder).rglob('*')
        for folder in folders
        if Path(folder).exists() and Path(folder).is_dir()
    ]

    ret: dict[str, Path] = {}
    for file in itertools.chain(*gens):
        if file.suffix in pattern:
            ret[file.name] = file
    return ret


class UniInfoTUI:
    completer: ClickCompleter
    session: PromptSession
    csv_: Path | None
    df: pd.DataFrame | None  # 统一的 SSOT 数据底座
    data: dict[ID, QuestionnaireResponse]  # 强类型实体映射
    mode: Literal['v1', 'v2']  # 当前问卷版本激活模式
    schemas: dict[str, dict[int, Any]]  # 集中管理各版本的 questions_map
    modified_log: dict[ID, Stuffs]
    alias_log: list[tuple[tuple[str, str], IssueId]]
    encoding: str | None

    def __init__(self) -> None:
        ctx = click.Context(cli_group)
        self.completer = ClickCompleter(cli_group, ctx)
        self.session = PromptSession(completer=self.completer)

        # 内部状态初始化
        self.csv_ = None
        self.df = None
        self.data = {}
        self.mode = 'v1'  # 默认运行在 v1 模式
        self.modified_log = {}
        self.alias_log = []
        self.encoding = None

        # 💡 提示：在这里挂载你解析出来的 YAML/JSON Schema 问卷结构体映射
        # 你可以采用： self.schemas['v1'] = your_parser.load_yaml("v1.yaml")
        self.schemas = {
            'v1': load_questions_from_yaml(parse_yaml(
                Path('/mnt/data/Project/questionnaire/v1.yaml').read_text())  # type: ignore
            ),  # FIXME
            'v2': {},
        }

    def run(self) -> None:
        print("""欢迎使用 University Information Editor CLI。
输入 help 或 ? 查看命令。
输入 exit / Ctrl-D 退出程序，Ctrl-C 开始新的循环。""")
        while True:
            try:
                line = self.session.prompt(f'(editor)[{self.mode}] ')
            except KeyboardInterrupt:
                print('^C')
                continue
            except EOFError:
                print('\n退出程序。')
                break

            if not line.strip():
                continue

            try:
                args = shlex.split(line)
                cli_group.main(args=args, standalone_mode=False, obj=self)
            except SystemExit:
                break
            except click.ClickException as e:
                logger.error(f'输入有误: {e.format_message()}')
            except click.Abort:
                continue
            except ValueError:
                logger.error('语法错误: 请检查闭合引号')
            except Exception as e:
                logger.exception(f'系统内部错误: {e}')

    def _reparse_data(self) -> None:
        """核心辅助：根据当前激活的 mode 和底层 DataFrame 刷新业务解析层对象"""  # FIXME
        if self.df is None:
            self.data = {}
            return

        class LegacyBasicData(BasicData):
            def __repr__(self) -> str:
                return (
                    f'{self.__class__.__name__}('
                    f'answer_date={self.answer_date!r}, '
                    f'num={self.num!r})'
                )

        def meta_extractor(df: pd.DataFrame, idx: Any) -> BasicData | None:
            row = df.loc[idx]
            return LegacyBasicData(  # 4. 移除了括号末尾的硬编码逗号，确保返回纯粹的实体对象，通过 ty check 校验
                answer_date=datetime.fromisoformat(str(row['开始时间'])),
                num=int(row['答题序号']),
                time_used=timedelta(0),
                source='null',
                source_detail='null',
                ip=IP(address='127.0.0.1', location='null'),
            )

        def qnum_extractor(col_name: str) -> int | None:
            # 匹配 Q1 或 Q1:
            match = re.match(r'^[qQ](\d+)', col_name)
            return int(match.group(1)) if match else None

        current_questions_map = self.schemas.get(self.mode, {})
        if not current_questions_map:
            logger.warning(
                f'当前模式 [{self.mode}] 的 Schema 题目映射为空，弱校验展示可能处于不完整状态！'
            )

        try:
            # 借助 wenjuanxing-parser 模型解析矩阵
            q_data = QuestionnaireData.from_dataframe(
                self.df, current_questions_map, meta_extractor, qnum_extractor
            )
            self.data = {}
            # 根据原始 DataFrame 顺序和行索引反向对齐打散到字典
            for idx, response_obj in zip(self.df.index, q_data.data, strict=True):
                qid = str(self.df.loc[idx, '答题序号'])
                self.data[qid] = response_obj
        except Exception as e:
            logger.error(f'刷新问卷星语义层解析失败: {e!r}')

    def _get_school_column(self) -> str | None:
        """根据当前的模式硬编码题号，动态提取 DataFrame 中匹配的列名"""
        if self.df is None:
            return None
        target_qnum = SCHOOL_QNUMS.get(self.mode)
        if target_qnum is None:
            return None

        for col in self.df.columns:
            match = re.match(r'^(\d+)[、\.]', str(col))
            if match and int(match.group(1)) == target_qnum:
                return str(col)
        return None

    def _make_fixes_line(self) -> str:
        issue_ids: set[str] = set()
        for stuff in self.modified_log.values():
            if stuff.issue_ids:
                issue_ids.update(stuff.issue_ids)
        for _, issue_ids_list in self.alias_log:
            if issue_ids_list:
                issue_ids.update(issue_ids_list)
        if not issue_ids:
            return ''
        sorted_ids = sorted(issue_ids, key=lambda x: int(x))
        return 'Fixes ' + ', '.join(f'#{i}' for i in sorted_ids)


def sorted_files_completion(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list:
    from click.shell_completion import CompletionItem

    return [
        CompletionItem(name)
        for name in auto_scan.keys()
        if name.lower().startswith(incomplete.lower())
    ]


def smart_path(p: Path) -> str:
    path = p.resolve()
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


# =====================================================================
# ⚙️ 用 Click 构建统一的根命令组
# =====================================================================
@click.group()
def cli_group() -> None:
    """University Information Editor 命令控制台"""
    pass


@cli_group.command()
@click.argument(
    'file',
    type=Path,
    required=False,
    shell_complete=sorted_files_completion,
    metavar='[data.csv]',
)
@click.pass_obj
def load(tui: UniInfoTUI, file: Path | None) -> None:
    """加载原始问卷数据文件（仅支持 CSV）"""
    if file is None:
        if 'results_desensitized.csv' in auto_scan:
            tui.csv_ = auto_scan['results_desensitized.csv']
        else:
            logger.error('自动加载出错，请确保当前目录下存在 results_desensitized.csv')
            return
    elif file.suffix == '.csv':
        tui.csv_ = file
    else:
        logger.error('参数错误: 只能加载扩展名为 .csv 的问卷数据文件')
        return

    logger.info(f'加载文件: CSV = {smart_path(tui.csv_)}')

    # 嗅探字符编码
    with open(tui.csv_, 'rb') as f:
        chunk = f.read(1000)
        encoding = chardet.detect(chunk)['encoding'] or 'utf-8'
    tui.encoding = encoding
    logger.warning(f'CSV 文件加载中，编码: {encoding}')

    try:
        # 使用 Pandas 接管底层矩阵，并利用内置机制做初步解析层组装
        tui.df = pd.read_csv(tui.csv_, encoding=encoding)
        tui._reparse_data()
        logger.info(f'CSV 文件加载完成，基础数据条目数: {len(tui.df)}')
    except Exception as e:
        logger.error(f'CSV 加载失败: {e!r}')


@cli_group.command()
@click.argument(
    'file',
    type=Path,
    required=False,
    shell_complete=sorted_files_completion,
    metavar='[newData.csv]',
)
@click.pass_obj
def dump(tui: UniInfoTUI, file: Path | None) -> None:
    """导出清理后的结果到 CSV 文件（默认覆写加载文件）"""
    target_csv = file or tui.csv_
    if target_csv is None:
        logger.error('存储路径丢失：未加载任何有效文件且未显式指定目标位置。')
        return
    if target_csv.suffix != '.csv':
        logger.error('仅支持导出为 .csv 格式')
        return
    if tui.df is None:
        logger.error('内存中没有可写的数据底座')
        return

    try:
        # 直接使用 DataFrame 的导出，规避解析器的逆向导出局限
        tui.df.to_csv(target_csv, index=False, encoding=tui.encoding or 'utf-8')
        logger.info(
            f'CSV: 已成功写入 {len(tui.df)} 行完整数据至 -> {smart_path(target_csv)}'
        )
    except Exception as e:
        logger.error(f'导出 CSV 失败: {e!r}')


@cli_group.command()
@click.argument('version', type=click.Choice(['v1', 'v2']), required=True)
@click.pass_obj
def mode(tui: UniInfoTUI, version: Literal['v1', 'v2']) -> None:
    """切换问卷模式分支 (v1 / v2，不清理内存数据，按需再重构语义视图)"""
    tui.mode = version
    logger.info(
        f'已将当前 TUI 工作视图切换至：[bold cyan]{version}[/bold cyan] 版本问卷规则'
    )
    if tui.df is not None:
        logger.info('正在基于新版本的 Schema 题目映射规则刷新缓存数据解析层...')
        tui._reparse_data()


@cli_group.command()
@click.argument('oldname', metavar='oldName')
@click.argument('newname', metavar='newName')
@click.argument('issue_ids', nargs=-1, metavar='[issueId...]')
@click.pass_obj
def alias(
    tui: UniInfoTUI, oldname: str, newname: str, issue_ids: tuple[str, ...]
) -> None:
    """学校统一更名（就地遍历清洗，移除对文本依赖）"""
    if tui.df is None:
        logger.error('基础数据未加载，请执行 load 指令')
        return

    school_col = tui._get_school_column()
    if not school_col:
        logger.error(
            f'在当前 {tui.mode} 模式下未匹配到合法的学校题号（已配置硬编码题号: {SCHOOL_QNUMS.get(tui.mode)}）'
        )
        return

    # 进行全面无死角匹配清洗
    mask = tui.df[school_col].astype(str).str.strip() == oldname.strip()
    match_count = mask.sum()

    if match_count == 0:
        logger.warning(
            f'未在列 [{school_col}] 中抓取到学校名为 "{oldname}" 的作答记录。'
        )
    else:
        tui.df.loc[mask, school_col] = newname
        logger.info(
            f'🎉 成功将列 [{school_col}] 中所有 ({match_count} 处) "{oldname}" 批量变更为 "{newname}"'
        )
        # 联动刷新弱校验语义模型层
        tui._reparse_data()

    # 记录修改链供 generate 指令使用
    tui.alias_log.append(((oldname, newname), list(issue_ids) if issue_ids else None))


@cli_group.command(name='del')
@click.argument('id', metavar='ID')
@click.argument('issue_ids', nargs=-1, metavar='[issueId...]')
@click.pass_obj
def delete_record(tui: UniInfoTUI, id: str, issue_ids: tuple[str, ...]) -> None:
    """删除指定 ID 的数据记录"""
    if id not in tui.data:
        logger.error(f'记录 ID {id} 不存在')
        return

    # 1. 物理移除 DataFrame 的映射行保证 dump 干净
    if tui.df is not None:
        tui.df = tui.df[tui.df['答题序号'].astype(str) != str(id)].reset_index(
            drop=True
        )

    # 2. 移除缓存结构体记录
    del tui.data[id]
    tui.modified_log[id] = Stuffs(list(issue_ids) if issue_ids else None, 'del')
    logger.info(f'物理删除回答记录 {id}，已记录修补依赖 issueIds={list(issue_ids)}')


@cli_group.command()
@click.argument('_id', nargs=-1, required=True, metavar='ID')
@click.pass_obj
def view(tui: UniInfoTUI, _id: tuple[str, ...]) -> None:
    """查看一条或多条数据记录（集成动态弱校验规则状态）"""
    for rid in _id:
        if rid not in tui.data:
            logger.error(f'记录 ID {rid} 在内存缓存中缺席。')
            continue

        resp: QuestionnaireResponse = tui.data[rid]

        table = Table(
            title=f'📋 答卷详情展示面板 (ID: {rid}) [模式: {tui.mode}]',
            show_header=True,
            box=None,
        )
        table.add_column('字段 / 题号', style='bold cyan', justify='right')
        table.add_column('结构化解析数据', style='green')
        table.add_column('弱校验状态', style='bold')
        table.add_column('异常校验日志反馈说明', style='red')

        # 装载元数据层
        if resp.metadata:
            meta = resp.metadata
            table.add_row(
                '提交时间', str(meta.answer_date), '[dim green]SYSTEM[/dim green]', ''
            )
            table.add_row(
                '答题耗时', str(meta.time_used), '[dim green]SYSTEM[/dim green]', ''
            )
            table.add_row(
                '网络 IP',
                f'{meta.ip.address} ({meta.ip.location})',
                '[dim green]SYSTEM[/dim green]',
                '',
            )
            table.add_row(
                '系统来源',
                f'{meta.source} ({meta.source_detail})',
                '[dim green]SYSTEM[/dim green]',
                '',
            )

        # 动态遍历由 parser 结构体反弹出来的有效题号
        for q_num in sorted(resp.answers.keys()):
            ans: UserAnswer = resp.answers[q_num]

            # 精细化解包并翻译各种题型的底层格式
            val_str = ''
            if ans.value is None:
                val_str = '[italic gray](未填入/空白)[/italic gray]'
            elif isinstance(ans.value, list):
                parts = []
                for item in ans.value:
                    if isinstance(
                        item, ChosenOption
                    ):  # 🚀 使用 isinstance 替换 hasattr，精准收窄类型
                        txt = item.text
                        if item.additional_text:
                            txt += f' ({item.additional_text})'
                        parts.append(txt)
                    else:
                        parts.append(str(item))
                val_str = ' ┋ '.join(parts)
            elif isinstance(ans.value, ChosenOption):  # 🚀 使用 isinstance 替换 hasattr
                val_str = ans.value.text
                if ans.value.additional_text:
                    val_str += f' ({ans.value.additional_text})'
            else:
                val_str = str(ans.value)

            # 弱校验结果反馈展示
            status_str = (
                '[bold green]✔ 通过[/bold green]'
                if ans.is_valid
                else '[bold red]❌ 异常[/bold red]'
            )
            err_msg = ans.error_msg or ''

            table.add_row(f'Q{q_num}', val_str, status_str, err_msg)

        Console().print(table)
        print('=' * 60)


@cli_group.command()
@click.option('--git', is_flag=True, help='生成 Fixes 行', metavar='[--git]')
@click.pass_obj
def generate(tui: UniInfoTUI, git: bool) -> None:
    """生成修改日志（Markdown 格式）"""
    DELETED = Template('删除了A${id}${issue_part}')
    ALIASED = Template('添加了新的别名，${old_name} -> ${new_name}${issue_part}')
    ISSUE_PART = Template('，由于${issue_ids}的反馈')
    TEMPLATE = Template("""# 修改日志
以下是此PR的修改记录：
## 删除记录
${deleted}
## 添加别名
${aliased}
${fixes}""")

    def _make_issue_part(issue_ids: list[str] | None) -> str:
        if not issue_ids:
            return ''
        issue_ids_str = ','.join(f' #{i} ' for i in issue_ids)
        return ISSUE_PART.substitute(issue_ids=issue_ids_str)

    deleted: list[str] = []
    aliased: list[str] = []

    for id, stuff in tui.modified_log.items():
        issue_part = _make_issue_part(stuff.issue_ids)
        if stuff.changed == 'del':
            deleted.append(DELETED.substitute(id=id, issue_part=issue_part))

    logger.debug(tui.alias_log)
    for (old_name, new_name), issue_ids in tui.alias_log:
        issue_part = _make_issue_part(issue_ids)
        aliased.append(
            ALIASED.substitute(
                old_name=old_name, new_name=new_name, issue_part=issue_part
            )
        )

    logger.info(
        TEMPLATE.substitute(
            deleted='\n'.join(deleted) if deleted else '无',
            aliased='\n'.join(aliased) if aliased else '无',
            fixes=tui._make_fixes_line() if git else '',
        )
    )


@cli_group.command()
def exit() -> None:
    """退出程序"""
    raise SystemExit


# =====================================================================
# 🧭 为 REPL 沉浸交互定制的 help 与 ? 专属指令
# =====================================================================
@cli_group.command(name='help')
@click.pass_context
def show_help(ctx: click.Context) -> None:
    """查看命令列表与详细帮助"""
    if ctx.parent and isinstance(ctx.parent.command, Group):
        parent_cmd = ctx.parent.command
        click.echo('命令列表:')

        commands_info: list[tuple[str, str]] = []
        max_cmd_len = 0

        for subcommand in sorted(parent_cmd.list_commands(ctx.parent)):
            if subcommand == '?':
                continue

            cmd_obj = parent_cmd.get_command(ctx.parent, subcommand)
            if cmd_obj:
                arg_pieces = []

                if isinstance(cmd_obj, Group):
                    sub_subs = cmd_obj.list_commands(ctx)
                    if sub_subs:
                        arg_pieces.append(f'< {" | ".join(sub_subs)} >')
                else:
                    for param in cmd_obj.params:
                        if isinstance(param, click.Argument):
                            label = param.metavar if param.metavar else param.name
                            if param.metavar and (
                                ' ' in param.metavar
                                or '[' in param.metavar
                                or ']' in param.metavar
                            ):
                                arg_pieces.append(param.metavar)
                            else:
                                if param.nargs == -1:
                                    arg_pieces.append(
                                        f'{label} [{label}...]'
                                        if param.required
                                        else f'[{label}...]'
                                    )
                                else:
                                    arg_pieces.append(
                                        label if param.required else f'[{label}]'
                                    )
                        elif isinstance(param, click.Option):
                            opt_name = param.opts[0] if param.opts else param.name
                            if param.is_flag:
                                arg_pieces.append(f'[{opt_name}]')
                            else:
                                metavar = param.metavar or 'VALUE'
                                arg_pieces.append(f'[{opt_name} {metavar}]')

                cmd_str = f'  {subcommand}'
                if arg_pieces:
                    cmd_str += f' {" ".join(arg_pieces)}'

                desc = cmd_obj.get_short_help_str() or ''
                commands_info.append((cmd_str, desc))
                if len(cmd_str) > max_cmd_len:
                    max_cmd_len = len(cmd_str)

        padding = max(max_cmd_len + 2, 38)
        for cmd_str, desc in commands_info:
            click.echo(f'{cmd_str.ljust(padding)}-- {desc}')


def run() -> None:
    cli = UniInfoTUI()
    cli.run()


logger = setup_logger()
auto_scan = scan_folders()


def load_installed_plugins() -> None:
    """扫描 Python 环境中安装的插件库并注册"""
    discovered_plugins = metadata.entry_points(group='uniinfo.plugins')

    for ep in discovered_plugins:
        try:
            ep.load()
            cmd_name = ep.name.lower()

            if cmd_name not in PLUGIN_COMMANDS:
                logger.error(
                    f'❌ 加载插件失败: 插件 {ep.name} 内部未调用 @register_plugin 绑定 Command！'
                )
                continue

            cli_group.add_command(PLUGIN_COMMANDS[cmd_name])
            logger.info(f'✨ 成功激活插件库指令: [bold green]{cmd_name}[/bold green]')
        except Exception as e:
            logger.error(f'加载插件库 {ep.name} 失败: {e!r}')


load_installed_plugins()

if __name__ == '__main__':
    run()

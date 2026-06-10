import csv
import itertools
import logging
import shlex
from collections.abc import Iterator
from datetime import datetime
from importlib import metadata
from pathlib import Path
from string import Template
from typing import Literal, NamedTuple

import chardet
import click
from click import Group
from click_repl import ClickCompleter
from prompt_toolkit import PromptSession
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

# 类型别名定义
type ID = str
type IssueId = list[str] | None
type Changed = Literal['del', 'outdate']


class Stuffs(NamedTuple):
    issue_ids: IssueId
    changed: Changed


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
    pattern = {'.csv', '.txt'}
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
    alias: Path | None
    data: dict[ID, dict[str, str]]
    alias_data: list[str]
    modified_log: dict[ID, Stuffs]
    alias_log: list[tuple[tuple[str, str], IssueId]]
    encoding: str | None

    def __init__(self) -> None:
        # 手动构建顶层 Context 并喂给 click-repl 补全器
        ctx = click.Context(cli_group)
        self.completer = ClickCompleter(cli_group, ctx)
        self.session = PromptSession(completer=self.completer)

        # 内部状态初始化
        self.csv_ = None
        self.alias = None
        self.data = {}
        self.alias_data = []
        self.modified_log = {}
        self.alias_log = []
        self.encoding = None

    def run(self) -> None:
        print("""欢迎使用 University Information Editor CLI。
输入 help 或 ? 查看命令。
输入 exit / Ctrl-D 退出程序，Ctrl-C 开始新的循环。""")
        while True:
            try:
                line = self.session.prompt('(editor) ')
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
    """[Click 自动化补全] 代替原有复杂的智能文件补全"""
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
    'files',
    nargs=-1,
    type=Path,
    shell_complete=sorted_files_completion,
    metavar='[data1 data2]',
)
@click.pass_obj
def load(tui: UniInfoTUI, files: tuple[Path, ...]) -> None:
    """加载数据文件（默认自动搜寻当前目录.csv和.txt）"""
    files_list = list(files)
    match files_list:
        case []:
            try:
                tui.csv_, tui.alias = (
                    auto_scan['results_desensitized.csv'],
                    auto_scan['alias.txt'],
                )
            except KeyError as e:
                logger.error(
                    f'自动加载出错，请确保当前目录下存在 result_desensitized.csv 和 alias.txt {e!r}'
                )
                return
        case [data, alias] if data.suffix == '.csv' and alias.suffix == '.txt':
            tui.csv_, tui.alias = data, alias
        case [alias, data] if data.suffix == '.csv' and alias.suffix == '.txt':
            tui.csv_, tui.alias = data, alias
        case _:
            logger.error('参数错误: 需要提供 0 或 2 个文件参数')
            return

    if tui.csv_ is None or tui.alias is None:
        return

    logger.info(
        f'加载文件: CSV = {smart_path(tui.csv_)}, Alias = {smart_path(tui.alias)}'
    )

    # 加载 CSV
    with open(tui.csv_, 'rb') as f:
        chunk = f.read(1000)
        encoding = chardet.detect(chunk)['encoding'] or 'utf-8'
    tui.encoding = encoding
    logger.warning(f'CSV 文件加载中，编码: {encoding}')
    with tui.csv_.open(newline='', encoding=encoding, errors='ignore') as f:
        reader = csv.DictReader(f)
        for row in reader:
            tui.data[row['答题序号']] = row
    logger.info(f'CSV 文件加载完成，数据条目数: {len(tui.data)}')

    # 加载 Alias
    with tui.alias.open(encoding='utf-8') as f:
        tui.alias_data = f.read().splitlines()
    logger.info(f'别名文件加载完成，数据条目数: {len(tui.alias_data)}')


@cli_group.command()
@click.argument(
    'files',
    nargs=-1,
    type=Path,
    shell_complete=sorted_files_completion,
    metavar='[newData] [newData]',
)
@click.pass_obj
def dump(tui: UniInfoTUI, files: tuple[Path, ...]) -> None:
    """导出数据文件（默认覆写原始文件）"""
    if len(files) > 2:
        logger.error('参数错误：最多只能提供 2 个文件参数')
        return

    files_list = list(files)

    def dump_csv(data: Path | None = tui.csv_) -> None:
        if data is None or not tui.data:
            return
        with open(data, 'w', newline='', encoding=tui.encoding or 'utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=tui.data[next(iter(tui.data))].keys())
            writer.writeheader()
            writer.writerows(list(tui.data.values()))
        logger.info(f'CSV: 已写入{len(tui.data)}行数据')

    def dump_alias(alias: Path | None = tui.alias) -> None:
        if alias is None:
            return
        with open(alias, 'w', encoding='utf-8') as f:
            f.write('\n'.join(tui.alias_data))
        logger.info(f'Alias: 已写入{len(tui.alias_data)}行数据')

    match files_list:
        case [data] if data.suffix == '.csv':
            dump_csv(data)
        case [data, alias] if data.suffix == '.csv' and alias.suffix == '.txt':
            dump_csv(data)
            dump_alias(alias)
        case [alias, data] if data.suffix == '.csv' and alias.suffix == '.txt':
            dump_csv(data)
            dump_alias(alias)
        case [alias] if alias.suffix == '.txt':
            dump_alias(alias)
        case []:
            dump_csv()
            dump_alias()
        case _:
            logger.error('文件名不正确')
            return


@cli_group.command()
@click.argument('oldname', metavar='oldName')
@click.argument('newname', metavar='newName')
@click.argument('issue_ids', nargs=-1, metavar='[issueId...]')
@click.pass_obj
def alias(
    tui: UniInfoTUI, oldname: str, newname: str, issue_ids: tuple[str, ...]
) -> None:
    """学校更名（记录别名/更名）"""
    tui.alias_data.append(f'{oldname}🚮{newname}')
    tui.alias_log.append(
        ((oldname, newname), list(issue_ids))
        if issue_ids
        else ((oldname, newname), None)
    )
    logger.info(f'添加别名 {oldname} -> {newname}，issueIds={list(issue_ids)}')


@cli_group.command(name='del')
@click.argument('id', metavar='ID')
@click.argument('issue_ids', nargs=-1, metavar='[issueId...]')
@click.pass_obj
def delete_record(tui: UniInfoTUI, id: str, issue_ids: tuple[str, ...]) -> None:
    """删除指定 ID 的数据记录"""
    if id not in tui.data:
        logger.error(f'记录 ID {id} 不存在')
        return
    del tui.data[id]
    tui.modified_log[id] = Stuffs(list(issue_ids) if issue_ids else None, 'del')
    logger.info(f'删除回答 {id}，issueIds={list(issue_ids)}')


@cli_group.command()
@click.argument('_id', nargs=-1, required=True, metavar='ID')
@click.pass_obj
def view(tui: UniInfoTUI, _id: tuple[str, ...]) -> None:
    """查看一条或多条数据记录"""

    def vertical_table(fields: list[str], rows: list[list[str]]) -> None:
        table = Table(show_header=False, box=None)
        table.add_column('字段', style='bold')
        for i, _ in enumerate(rows):
            table.add_column(f'{i}', style='dim')
        for idx, field in enumerate(fields):
            values = [row[idx] if idx < len(row) else '' for row in rows]
            table.add_row(field, *values)
        Console().print(table)

    logger.warning('你可能需要手动调节终端字体大小')
    cols = ['ID'] + [f'Q{i}' for i in range(5, 30)]
    rows: list[list[str]] = []
    for rid in _id:
        if rid not in tui.data:
            logger.error(f'记录 ID {rid} 不存在')
            return
        rows.append([rid, *[tui.data[rid].get(f'Q{i}', '') for i in range(5, 30)]])
    vertical_table(cols, rows)


@cli_group.command()
@click.argument('id', metavar='ID')
@click.argument('issue_ids', nargs=-1, type=int, metavar='[issueId...]')
@click.pass_obj
def outdate(tui: UniInfoTUI, id: str, issue_ids: tuple[int, ...]) -> None:
    """标记记录已过期"""
    if id not in tui.data:
        logger.error(f'记录 ID {id} 不存在')
        return
    for i in range(5, 30):
        tui.data[id]['Q' + str(i)] = '[过时]：' + tui.data[id]['Q' + str(i)]
    # 将 int 类型的 issue_ids 转成 str 以符合 Stuffs 的结构定义
    tui.modified_log[id] = Stuffs(
        [str(x) for x in issue_ids] if issue_ids else None, 'outdate'
    )
    logger.info(f'标记过期 {id}, issueIds={list(issue_ids)}')


@cli_group.command()
@click.option('--git', is_flag=True, help='生成 Fixes 行', metavar='[--git]')
@click.pass_obj
def generate(tui: UniInfoTUI, git: bool) -> None:
    """生成修改日志（Markdown 格式）"""
    DELETED = Template('删除了A${id}${issue_part}')
    OUTDATED = Template('将A${id}标记为过期${issue_part}')
    ALIASED = Template('添加了新的别名，${old_name} -> ${new_name}${issue_part}')
    ISSUE_PART = Template('，由于${issue_ids}的反馈')
    TEMPLATE = Template("""# 修改日志
以下是此PR的修改记录：
## 删除记录
${deleted}
## 标记过时
${outdated}
##添加别名
${aliased}
${fixes}""")

    def _make_issue_part(issue_ids: list[str] | None) -> str:
        if not issue_ids:
            return ''
        issue_ids_str = ','.join(f' #{i} ' for i in issue_ids)
        return ISSUE_PART.substitute(issue_ids=issue_ids_str)

    deleted: list[str] = []
    outdated: list[str] = []
    aliased: list[str] = []

    for id, stuff in tui.modified_log.items():
        issue_part = _make_issue_part(stuff.issue_ids)
        if stuff.changed == 'del':
            deleted.append(DELETED.substitute(id=id, issue_part=issue_part))
        elif stuff.changed == 'outdate':
            outdated.append(OUTDATED.substitute(id=id, issue_part=issue_part))

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
            outdated='\n'.join(outdated) if outdated else '无',
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

        # 1. 动态抓取并解析所有注册进来的命令
        for subcommand in sorted(parent_cmd.list_commands(ctx.parent)):
            if subcommand == '?':
                continue

            cmd_obj = parent_cmd.get_command(ctx.parent, subcommand)
            if cmd_obj:
                arg_pieces = []

                # 🚀 自动化情况 A：如果该命令是一个 Group (如 check)，动态组装其子命令
                if isinstance(cmd_obj, Group):
                    sub_subs = cmd_obj.list_commands(ctx)
                    if sub_subs:
                        arg_pieces.append(f'< {" | ".join(sub_subs)} >')

                # 🚀 自动化情况 B：如果是普通命令，动态提取 Arguments 与具体的 Options
                else:
                    for param in cmd_obj.params:
                        # 解析 Argument
                        if isinstance(param, click.Argument):
                            label = param.metavar if param.metavar else param.name

                            # ✨ 核心改动：如果 metavar 已经被你手动写成了复杂格式（带空格或括号），直接使用
                            if param.metavar and (
                                ' ' in param.metavar
                                or '[' in param.metavar
                                or ']' in param.metavar
                            ):
                                arg_pieces.append(param.metavar)
                            else:
                                # 否则，根据参数规则动态生成高级格式
                                if param.nargs == -1:
                                    if param.required:
                                        # 变长且必填：例如 view ID [ID ...]
                                        arg_pieces.append(f'{label} [{label}...]')
                                    else:
                                        # 变长且选填：例如 [issue_ids ...]
                                        arg_pieces.append(f'[{label}...]')
                                else:
                                    # 单个参数
                                    arg_pieces.append(
                                        label if param.required else f'[{label}]'
                                    )

                        # 解析 Option (拒绝硬编码，动态抓取类似 --git 的具体名称)
                        elif isinstance(param, click.Option):
                            opt_name = param.opts[0] if param.opts else param.name
                            if param.is_flag:
                                arg_pieces.append(f'[{opt_name}]')
                            else:
                                metavar = param.metavar or 'VALUE'
                                arg_pieces.append(f'[{opt_name} {metavar}]')

                # 2. 拼接指令与动态生成的参数部分
                cmd_str = f'  {subcommand}'
                if arg_pieces:
                    cmd_str += f' {" ".join(arg_pieces)}'

                desc = cmd_obj.get_short_help_str() or ''
                commands_info.append((cmd_str, desc))

                if len(cmd_str) > max_cmd_len:
                    max_cmd_len = len(cmd_str)

        # 3. 动态计算填充间距实现完美对齐
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

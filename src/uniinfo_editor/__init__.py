import argparse  # 能跑就别动，留着吧（其实最好是用click-repl
import csv
import itertools
import logging
import shlex
from collections.abc import Callable, Iterator
from datetime import datetime
from importlib import metadata
from pathlib import Path
from string import Template
from typing import Literal, NamedTuple

import chardet
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

PLUGIN_COMMANDS: dict[str, tuple[Callable, argparse.ArgumentParser, str]] = {}


def register_plugin(parser: argparse.ArgumentParser):
    """第三方插件用于注册自己和 ArgumentParser 的装饰器"""
    if not isinstance(parser, argparse.ArgumentParser):
        raise TypeError('插件必须提供合法的 argparse.ArgumentParser 实例！')

    def decorator(func: Callable):
        cmd_name = parser.prog.lower()
        desc = parser.description or func.__doc__ or '[第三方插件] 未提供描述'

        # 写入主程序路由表
        PLUGIN_COMMANDS[cmd_name] = (func, parser, desc)
        return func

    return decorator


def setup_logger() -> logging.Logger:
    logger = logging.getLogger('uniinfo')
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # === 终端输出，用 RichHandler ===
    ch = RichHandler(
        rich_tracebacks=True, markup=True, show_time=False, show_path=False
    )
    ch.setLevel(logging.DEBUG)  # 控制台显示级别
    ch.setFormatter(logging.Formatter('%(message)s'))  # Rich 要用 message

    # === 文件日志输出，用普通 Formatter ===
    now_str = datetime.now().strftime('%Y-%m-%d_%H-%M')
    if not Path('logs').exists():
        Path('logs').mkdir()
    filename = f'./logs/uniinfo - {now_str}.log'
    fh = logging.FileHandler(filename, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter('%(levelname)s:  %(message)s'))

    # === 绑定 handler ===
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
            # later same-name files will be overridden by last encountered
            ret[file.name] = file
    return ret


class CommandCompleter(Completer):
    def __init__(
        self,
        commands: list[str],
        file_names: list[str] | None = None,
        native_parsers: dict = None,
    ):
        self.commands = commands
        self.file_names = file_names or []
        self.native_parsers = native_parsers or {}  # 存下原生解析器

    def update_files(self, files: list[str]):
        self.file_names = files

    def _traverse_argparse(
        self,
        parser: argparse.ArgumentParser,
        established_args: list[str],
        last_word: str,
    ) -> list[str]:
        """[自动化核心] 递归扒开 argparse 的 subparsers 和 options"""
        current_parser = parser

        # 1. 根据用户已经敲完的参数，顺着子解析器树一路摸下去
        for arg in established_args:
            if current_parser._subparsers:
                for action in current_parser._subparsers._actions:
                    if (
                        isinstance(action, argparse._SubParsersAction)
                        and arg in action.choices
                    ):
                        current_parser = action.choices[arg]
                        break

        candidates = []
        # 2. 收集当前层级所有合法的子命令 (例如 joke, school, typo)
        if current_parser._subparsers:
            for action in current_parser._subparsers._actions:
                if isinstance(action, argparse._SubParsersAction):
                    candidates.extend(action.choices.keys())

        # 3. 收集当前层级所有合法的参数选项 (例如 -t, --threshold)
        for action in current_parser._actions:
            if action.option_strings:
                candidates.extend(action.option_strings)

        # 4. 过滤出匹配当前正在输入的词
        return [c for c in candidates if c.startswith(last_word)]

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lower()

        # 1) 空输入或仅空白 -> 🔥 用 set 去重并排序
        if text == '' or text.isspace():
            all_root_cmds = sorted(
                list(set(self.commands) | set(PLUGIN_COMMANDS.keys()))
            )
            for cmd in all_root_cmds:
                yield Completion(cmd, start_position=0)
            return

        ends_with_space = text.endswith(' ')

        try:
            parts = shlex.split(text)
        except ValueError:
            return

        # 2) 仍在输入第一个单词 -> 🔥 同样用 set 去重并排序
        if ' ' not in text:
            prefix = text
            all_root_cmds = sorted(
                list(set(self.commands) | set(PLUGIN_COMMANDS.keys()))
            )
            for cmd in all_root_cmds:
                if cmd.lower().startswith(prefix):
                    yield Completion(cmd, start_position=-len(prefix))
            return
        cmd = parts[0] if parts else ''
        used_files = parts[1:] if len(parts) > 1 else []

        # 3) 已输入命令且进入参数补全阶段

        # 保留你原本完美的 load/dump 文件补全逻辑
        if cmd in ('load', 'dump'):
            if len(used_files) >= 2:
                return
            last_word = ''
            if not ends_with_space:
                last_word = document.get_word_before_cursor(WORD=True) or ''
            for fname in self.file_names:
                if fname in used_files:
                    continue
                if last_word == '' or fname.startswith(last_word):
                    yield Completion(fname, start_position=-len(last_word))
            return

        parser = None
        if cmd in PLUGIN_COMMANDS:
            _, parser, _ = PLUGIN_COMMANDS[cmd]
        elif cmd in self.native_parsers:
            parser = self.native_parsers[cmd]

        if parser:
            established_args = parts[1:] if ends_with_space else parts[1:-1]
            last_word = '' if ends_with_space else (document.get_word_before_cursor(WORD=True) or '')

            suggestions = self._traverse_argparse(parser, established_args, last_word)
            for s in suggestions:
                yield Completion(s, start_position=-len(last_word))


def smart_path(p: Path) -> str:
    path = p.resolve()
    try:
        # 尝试相对于当前工作目录
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        # 无法相对当前目录，返回绝对路径
        return str(path)


type ID = str
type IssueId = list[str] | None
type Changed = Literal['del', 'outdate']
commands = [
    ('load [data1 data2]', '加载数据文件（默认自动搜寻当前目录.csv和.txt）'),
    ('dump [newData] [newData]', '导出数据文件（默认覆写）'),
    ('alias oldName newName [issueId...]', '学校更名（记录别名/更名）'),
    ('del ID [issueId...]', '删除记录'),
    ('outdate ID [issueId...]', '标记过期'),
    ('view ID [ID ...]', '查看记录'),
    ('exit', '退出程序'),
    ('generate', '生成修改日志（Markdown格式）'),
]


class Stuffs(NamedTuple):
    issue_ids: IssueId
    changed: Changed


class UniInfoTUI:
    def __init__(self):
        self.native_parsers = {
            'generate': argparse.ArgumentParser(prog='generate', add_help=False)
        }
        self.native_parsers['generate'].add_argument(
            '--git', action='store_true', help='生成 Fixes 行'
        )
        base_cmd_names = [cmd.split()[0] for cmd, _ in commands]
        self.commands = base_cmd_names + list(PLUGIN_COMMANDS.keys()) + ['?']
        self.completer = CommandCompleter(
            self.commands, list(auto_scan.keys()), native_parsers=self.native_parsers
        )
        self.session = PromptSession(completer=self.completer)
        # state variables
        self._csv: Path = None
        self._alias: Path = None
        self.data: dict[ID, dict[str, str]] = {}
        self.alias_data: list[str] = None
        self.modified_log: dict[ID, Stuffs] = {}
        self.alias_log: list[tuple[tuple[str, str], IssueId]] = []
        self.encoding: str = None

    def run(self):
        print(
            """欢迎使用 University Information Editor CLI。
输入 help 或 ? 查看命令。
输入 exit / Ctrl-D 退出程序，Ctrl-C 开始新的循环。"""
        )
        while True:
            self.completer.update_files(list(auto_scan.keys()))
            try:
                line = self.session.prompt('(editor) ')
            except KeyboardInterrupt:
                # Ctrl-C — 中断当前输入，继续循环
                print('^C')
                continue
            except EOFError:
                print('\n退出程序。')
                break
            if not line.strip():
                continue
            lines = line.splitlines()
            for line in lines:
                line = line.strip()
                i = line.find(' ')
                if i == -1:
                    cmd, arg_str = line, ''
                else:
                    cmd, arg_str = line[:i], line[i + 1 :].strip()

                match cmd.lower():
                    case 'load':
                        self.do_load(arg_str)
                    case 'dump':
                        self.do_dump(arg_str)
                    case 'alias':
                        self.do_alias(arg_str)
                    case 'del':
                        self.do_del(arg_str)
                    case 'outdate':
                        self.do_outdate(arg_str)
                    case 'generate':
                        self.do_generate(arg_str)
                    case 'exit':
                        return
                    case 'view':
                        self.do_view(arg_str)
                    case 'help' | '?' | '？':
                        self.do_help()
                    case _ as custom_cmd:
                        if custom_cmd in PLUGIN_COMMANDS:
                            func, parser, _ = PLUGIN_COMMANDS[custom_cmd]

                            # 主程序替插件代劳解析
                            parsed_args = self.safe_parse(parser, arg_str)
                            if parsed_args is None:
                                continue  # 解析失败（例如敲错参数），直接拦截，开启下一轮 TUI 循环

                            # 传入解析好的 Namespace
                            func(self, parsed_args)
                        else:
                            logger.warning(f'未知命令: {cmd}')

    def do_help(self):
        print('命令列表:')

        all_commands = list(commands)

        # 顺应新的字典结构取出描述
        for cmd_name, (_, _, desc) in PLUGIN_COMMANDS.items():
            all_commands.append((cmd_name, desc))

        width = max(len(cmd) for cmd, _ in all_commands) + 2

        for cmd, desc in all_commands:
            print(f'  {cmd.ljust(width)}-- {desc}')

    def do_load(self, arg: str):
        def load_csv():
            with open(self._csv, 'rb') as f:
                chunk = f.read(1000)
                encoding = chardet.detect(chunk)['encoding'] or 'utf-8'
            self.encoding = encoding
            logger.warning(f'CSV 文件加载中，编码: {encoding}')
            with self._csv.open(newline='', encoding=encoding, errors='ignore') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    self.data[row['答题序号']] = row
            logger.info(f'CSV 文件加载完成，数据条目数: {len(self.data)}')

        def load_alias():
            with self._alias.open(encoding='utf-8') as f:
                self.alias_data = f.read().splitlines()
            logger.info(f'别名文件加载完成，数据条目数: {len(self.alias_data)}')

        parser = argparse.ArgumentParser(
            prog='load',
            description='加载一个或多个数据文件',
            add_help=False,
        )
        parser.add_argument(
            'files', nargs='*', type=Path, help='要加载的文件（支持多个）'
        )
        parsed = self.safe_parse(parser, arg)
        files: list[Path] = parsed.files
        match files:
            case []:
                try:
                    self._csv, self._alias = (
                        auto_scan['results_desensitized.csv'],
                        auto_scan['alias.txt'],
                    )
                except KeyError as e:
                    logger.error(
                        f'自动加载出错，请确保当前目录下存在 result_desensitized.csv 和 alias.txt {e!r}'
                    )
                    return

            case [data, alias] if data.suffix == '.csv' and alias.suffix == '.txt':
                self._csv, self._alias = data, alias

            case [alias, data] if data.suffix == '.csv' and alias.suffix == '.txt':
                self._csv, self._alias = data, alias
            case _:
                logger.error('参数错误: 需要提供 0 或 2 个文件参数')
                return
        logger.info(
            f'加载文件: CSV = {smart_path(self._csv)}, Alias = {smart_path(self._alias)}'
        )
        load_csv()
        load_alias()

    # ---- dump: 支持多个目标文件（位置参数） ----
    def do_dump(self, arg: str):
        def dump_csv(data: Path = self._csv):
            with open(data, 'w', newline='', encoding=self.encoding) as f:
                writer = csv.DictWriter(
                    f, fieldnames=self.data[next(iter(self.data))].keys()
                )
                writer.writeheader()  # 写表头
                writer.writerows(list(self.data.values()))  # 写多行字典
            logger.info(f'CSV: 已写入{len(self.data)}行数据')

        def dump_alias(alias: Path = self._alias):
            with open(alias, 'w', encoding='utf-8') as f:
                f.write('\n'.join(self.alias_data))
            logger.info(f'Alias: 已写入{len(self.alias_data)}行数据')

        parser = argparse.ArgumentParser(
            prog='dump',
            description='导出一个或多个数据文件',
            add_help=False,
        )
        parser.add_argument(
            'files', nargs='*', type=Path, help='要导出的文件（支持多个）'
        )
        parsed = self.safe_parse(parser, arg)
        if len(parsed.files) > 2:
            logger.error('参数错误：最多只能提供 2 个文件参数')
            return
        files: list[Path] = parsed.files
        match files:
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
                logger.debug(files)
                logger.error('文件名不正确')
                return

    # ---- alias: oldname newname [issueId...] ----
    def do_alias(self, arg: str):
        parser = argparse.ArgumentParser(
            prog='alias',
            description='记录学校更名',
            add_help=False,
        )
        parser.add_argument('oldname', help='原名')
        parser.add_argument('newname', help='新名')
        parser.add_argument('issueIds', nargs='*', help='可选的 issueId(s)')
        parsed = self.safe_parse(parser, arg)
        if not parsed:
            return
        self.alias_data.append(f'{parsed.oldname}🚮{parsed.newname}')
        self.alias_log.append(
            ((parsed.oldname, parsed.newname), parsed.issueIds)
            if parsed.issueIds
            else ((parsed.oldname, parsed.newname), None)
        )
        logger.info(
            f'添加别名 {parsed.oldname} -> {parsed.newname}，issueIds={parsed.issueIds}'
        )

    def do_del(self, arg: str):
        parser = argparse.ArgumentParser(
            prog='del',
            add_help=False,
        )
        parser.add_argument('id', help='记录 ID')
        parser.add_argument('issueIds', nargs='*', type=str, help='可选的 issueId(s)')
        parsed = self.safe_parse(parser, arg)
        if not parsed:
            return
        if parsed.id not in self.data:
            logger.error(f'记录 ID {parsed.id} 不存在')
            return
        del self.data[parsed.id]
        self.modified_log[parsed.id] = Stuffs(parsed.issueIds, 'del')
        logger.info(f'删除回答 {parsed.id}，issueIds={parsed.issueIds}')

    def do_view(self, arg: str):
        def vertical_table(fields: list[str], rows: list[list[str]]):
            table = Table(show_header=False, box=None)

            # 第一列是字段名
            table.add_column('字段', style='bold')

            # 添加每一行作为列
            for i, _ in enumerate(rows):
                table.add_column(f'{i}', style='dim')

            # 每一字段对应每列的值
            for idx, field in enumerate(fields):
                values = [row[idx] if idx < len(row) else '' for row in rows]
                table.add_row(field, *values)

            Console().print(table)

        parser = argparse.ArgumentParser(
            prog='view',
            add_help=False,
        )
        parser.add_argument('ids', nargs='+', help='记录 ID(s)')
        parsed = self.safe_parse(parser, arg)

        if not parsed:
            return
        logger.warning('你可能需要手动调节终端字体大小')
        cols = ['ID'] + [f'{i}' for i in range(5, 30)]
        rows = []
        for id in parsed.ids:
            if id not in self.data:
                logger.error(f'记录 ID {id} 不存在')
                return

            rows.append([id, *[self.data[id].get(f'Q{i}', '') for i in range(5, 30)]])
        vertical_table(cols, rows)

    def do_outdate(self, arg: str):
        parser = argparse.ArgumentParser(
            prog='outdate',
            add_help=False,
        )
        parser.add_argument('id', help='记录 ID')
        parser.add_argument('issueIds', nargs='*', type=int, help='可选的 issueId(s)')
        parsed = self.safe_parse(parser, arg)
        if not parsed:
            return
        if parsed.id not in self.data:
            logger.error(f'记录 ID {parsed.id} 不存在')
            return
        for i in range(5, 30):
            self.data[parsed.id]['Q' + str(i)] = (
                '[过时]：' + self.data[parsed.id]['Q' + str(i)]
            )
        self.modified_log[parsed.id] = Stuffs(parsed.issueIds, 'outdate')
        logger.info(f'标记过期 {parsed.id}, issueIds={parsed.issueIds}')

    @staticmethod
    def safe_parse(parser: argparse.ArgumentParser, arg_str: str):
        try:
            return parser.parse_args(shlex.split(arg_str))
        except SystemExit:
            pass

    def do_generate(self, arg: str):
        parser = argparse.ArgumentParser(
            prog='generate',
            add_help=False,
        )
        parser.add_argument('--git', action='store_true', help='生成 Fixes 行')
        parsed = self.safe_parse(parser, arg)

        # 基本条目模板（不包含 "由于...的反馈" 部分）
        DELETED = Template('删除了A${id}${issue_part}')
        OUTDATED = Template('将A${id}标记为过期${issue_part}')
        ALIASED = Template('添加了新的别名，${old_name} -> ${new_name}${issue_part}')

        # issue 部分的模板，按需加入
        ISSUE_PART = Template('，由于${issue_ids}的反馈')

        # 最终日志模板
        TEMPLATE = Template("""# 修改日志
以下是此PR的修改记录：
## 删除记录
${deleted}
## 标记过时
${outdated}
## 添加别名
${aliased}
${fixes}""")

        def _make_issue_part(issue_ids):
            """根据 issue_ids 列表返回要插入的字符串（空或 '，由于...的反馈'）"""
            if not issue_ids:
                return ''
            issue_ids_str = ','.join(f' #{i} ' for i in issue_ids)
            return ISSUE_PART.substitute(issue_ids=issue_ids_str)

        deleted = []
        outdated = []
        aliased = []

        for id, stuff in self.modified_log.items():
            issue_part = _make_issue_part(stuff.issue_ids)
            if stuff.changed == 'del':
                deleted.append(DELETED.substitute(id=id, issue_part=issue_part))
            elif stuff.changed == 'outdate':
                outdated.append(OUTDATED.substitute(id=id, issue_part=issue_part))

        logger.debug(self.alias_log)
        for (old_name, new_name), issue_ids in self.alias_log:
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
                fixes=self._make_fixes_line() if parsed.git else '',
            )
        )

    def _make_fixes_line(self) -> str:
        """从所有操作记录中收集 issue_ids，生成 Fixes 行"""
        issue_ids = set()
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


def run():
    cli = UniInfoTUI()
    cli.run()


logger = setup_logger()
auto_scan = scan_folders()


def load_installed_plugins():
    """扫描 Python 环境中安装的插件库并注册"""
    discovered_plugins = metadata.entry_points(group='uniinfo.plugins')

    for ep in discovered_plugins:
        try:
            # 加载插件入口函数（这会触发插件代码被执行，从而让装饰器生效）
            plugin_func = ep.load()
            cmd_name = ep.name.lower()

            # 🚨 核心约束：检查装饰器有没有把它塞进主程序的全局路由表里
            if cmd_name not in PLUGIN_COMMANDS:
                logger.error(
                    f'❌ 加载插件失败: 插件 {ep.name} 内部未调用 @register_plugin 绑定 Parser！'
                )
                continue

            logger.info(f'✨ 成功激活插件库指令: [bold green]{cmd_name}[/bold green]')
        except Exception as e:
            logger.error(f'加载插件库 {ep.name} 失败: {e!r}')


load_installed_plugins()
if __name__ == '__main__':
    run()

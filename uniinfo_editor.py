import argparse
import itertools
import logging
import shlex
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion


class ColorFormatter(logging.Formatter):
    def format(self, record):
        RESET = '\033[0m'
        COLORS = {
            logging.DEBUG: '\033[36m',  # 青色
            logging.INFO: '\033[32m',  # 绿色
            logging.WARNING: '\033[33m',  # 黄色
            logging.ERROR: '\033[31m',  # 红色
        }
        color = COLORS.get(record.levelno, RESET)
        message = super().format(record)
        return f'{color}{message}{RESET}'


def setup_logger() -> None:
    global logger
    logger = logging.getLogger('uniinfo')
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    formatter = ColorFormatter('[%(levelname)s] %(message)s')

    ch = logging.StreamHandler()
    now_str = datetime.now().strftime('%Y-%m-%d_%H-%M')
    filename = f'uniinfo - {now_str}.log'
    fh = logging.FileHandler(filename)

    ch.setFormatter(formatter)
    fh.setFormatter(formatter)
    logger.addHandler(ch)
    logger.addHandler(fh)


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
    def __init__(self, commands: list[str], file_names: list[str] = None):
        self.commands = commands
        self.file_names = file_names or []

    def update_files(self, files: list[str]):
        self.file_names = files

    def get_completions(self, document, complete_event):
        # 保留原始 text（不要 strip，需检测末尾空格）
        text = document.text_before_cursor

        # 1) 空输入或仅空白 -> 列出所有命令
        if text == '' or text.isspace():
            for cmd in self.commands:
                # start_position=0 表示从当前位置插入完整命令
                yield Completion(cmd, start_position=0)
            return

        # 检查是否以空格结尾（用于判断用户是否已开始新参数）
        ends_with_space = text.endswith(' ')

        # 尝试 shell 风格切分（处理引号）
        try:
            parts = shlex.split(text)
        except ValueError:
            # unmatched quotes 等解析错误时，不返回补全
            return

        # 2) 仍在输入第一个单词（命令）
        if ' ' not in text:
            prefix = text
            for cmd in self.commands:
                if cmd.startswith(prefix):
                    yield Completion(cmd, start_position=-len(prefix))
            return

        # 3) 已输入命令且进入参数补全阶段
        cmd = parts[0] if parts else ''
        # 已输入的参数（文件名）列表
        used_files = parts[1:] if len(parts) > 1 else []

        # 如果已经输入 2 个或以上参数（达到上限），不再提供文件补全
        if len(used_files) >= 2:
            return

        # 确定当前正在输入的词（若以空格结尾则表示正在新建参数，last_word 为空）
        last_word = ''
        if not ends_with_space:
            # WORD=True 允许把连字符等也作为词的一部分，按需可改为 False
            last_word = document.get_word_before_cursor(WORD=True) or ''

        # 只有 load/dump 支持文件名补全
        if cmd in ('load', 'dump'):
            for fname in self.file_names:
                if fname in used_files:
                    continue  # 排除已输入的文件
                # 如果没有部分前缀（last_word == ''），就显示所有剩余文件
                if last_word == '' or fname.startswith(last_word):
                    yield Completion(fname, start_position=-len(last_word))


class UniInfoCLI:
    def __init__(self):
        self.commands = ['load', 'dump', 'alias', 'del', 'outdate', 'exit', 'help', '?']
        self.completer = CommandCompleter(self.commands, list(auto_scan.keys()))
        self.session = PromptSession(completer=self.completer)
        # state variables
        self._csv: Path | None = None
        self._txt: Path | None = None
        self._alias: Path | None = None

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

            cmd, *args = line.split()
            arg_str = ' '.join(args)

            match cmd:
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
                case 'exit':
                    print('退出程序。')
                    break
                case 'help' | '?' | '？':
                    self.do_help()
                case _:
                    print(f'未知命令: {cmd}')

    def do_help(self):
        print('命令列表:')
        commands = [
            ('load [$data...]', '加载数据文件（默认自动搜寻当前目录.csv和.txt）'),
            ('dump [$newData...]', '导出数据文件（默认覆写）'),
            ('alias $oldName $newName [$issueId...]', '学校更名（记录别名/更名）'),
            ('del $ID [$issueId...]', '删除记录'),
            ('outdate $ID [$issueId...]', '标记过期'),
            ('exit', '退出程序'),
        ]

        width = max(len(cmd) for cmd, _ in commands) + 2
        for cmd, desc in commands:
            print(f'  {cmd.ljust(width)}-- {desc}')

    def do_load(self, arg: str):
        parser = argparse.ArgumentParser(
            prog='load', description='加载一个或多个数据文件', add_help=False
        )
        parser.add_argument('files', nargs='*', help='要加载的文件（支持多个）')
        parsed = self.safe_parse(parser, arg)
        if parsed is None or len(parsed.files) > 2:
            parser.error('最多只能指定两个文件或自动扫描。')

        files = parsed.files or (
            list(auto_scan.keys()) if auto_scan else ['default.csv']
        )
        print(f'加载文件: {files}')
        logger.info(f'load {files}')
        # 实际加载逻辑（示例：记录第一个被加载的文件）
        if files:
            self._csv = Path(files[0])

    # ---- dump: 支持多个目标文件（位置参数） ----
    def do_dump(self, arg: str):
        parser = argparse.ArgumentParser(
            prog='dump', description='导出一个或多个数据文件', add_help=False
        )
        parser.add_argument('files', nargs='*', help='要导出的文件（支持多个）')
        parsed = self.safe_parse(parser, arg)
        if parsed is None or len(parsed.files) > 2:
            parser.error('最多只能指定两个文件或自动扫描。')

        if parsed.files:
            targets = parsed.files
        else:
            # 若有当前加载文件则覆盖之，否则使用默认名
            targets = [str(self._csv)] if self._csv else ['default_out.csv']

        print(f'导出文件: {targets}')
        logger.info(f'dump {targets}')
        # 实际导出逻辑

    # ---- alias: oldname newname [issueId...] ----
    def do_alias(self, arg: str):
        parser = argparse.ArgumentParser(
            prog='alias', description='记录学校更名', add_help=False
        )
        parser.add_argument('oldname', help='原名')
        parser.add_argument('newname', help='新名')
        parser.add_argument('issueIds', nargs='*', type=int, help='可选的 issueId(s)')
        parsed = self.safe_parse(parser, arg)
        if parsed is None:
            return

        print(
            f"将学校 '{parsed.oldname}' 更名为 '{parsed.newname}', issueIds={parsed.issueIds}"
        )
        logger.info(f'alias {parsed.oldname} -> {parsed.newname} ({parsed.issueIds})')
        # 实现 alias 逻辑（例如写入 CSV 或内存结构）

    # ---- del: ID [issueId...] ----
    def do_del(self, arg: str):
        parser = argparse.ArgumentParser(
            prog='del', description='删除记录', add_help=False
        )
        parser.add_argument('id', help='记录 ID')
        parser.add_argument('issueIds', nargs='*', type=int, help='可选的 issueId(s)')
        parsed = self.safe_parse(parser, arg)
        if parsed is None:
            return

        print(f'删除 ID={parsed.id}, issueIds={parsed.issueIds}')
        logger.info(f'del {parsed.id} ({parsed.issueIds})')
        # 删除逻辑

    # ---- outdate: ID [issueId...] ----
    def do_outdate(self, arg: str):
        parser = argparse.ArgumentParser(
            prog='outdate', description='标记记录过期', add_help=False
        )
        parser.add_argument('id', help='记录 ID')
        parser.add_argument('issueIds', nargs='*', type=int, help='可选的 issueId(s)')
        parsed = self.safe_parse(parser, arg)
        if parsed is None:
            return

        print(f'标记过期 ID={parsed.id}, issueIds={parsed.issueIds}')
        logger.info(f'outdate {parsed.id} ({parsed.issueIds})')

    @staticmethod
    def safe_parse(parser, arg_str: str):
        try:
            return parser.parse_args(shlex.split(arg_str))
        except SystemExit:
            return None


def run():
    global auto_scan
    setup_logger()
    auto_scan = scan_folders()
    cli = UniInfoCLI()
    cli.run()


if __name__ == '__main__':
    run()

# uniinfo-editor

UniInfo Editor 是一个命令行工具，用于管理和编辑大学问卷信息，支持加载 CSV/文本数据、记录学校别名、删除或标记记录过期，并生成修改日志。

## 功能概览

| 命令                                 | 功能说明                                               |
| ------------------------------------ | ------------------------------------------------------ |
| `load [data1 data2]`                 | 加载数据文件（默认自动搜寻当前目录 .csv 和 .txt 文件） |
| `dump [newData] [newData]`           | 导出数据文件（默认覆写）                               |
| `alias oldName newName [issueId...]` | 学校更名（记录别名/更名）                              |
| `del ID [issueId...]`                | 删除记录                                               |
| `outdate ID [issueId...]`            | 标记记录过期                                           |
| `exit`                               | 退出程序                                               |
| `generate`                           | 生成修改日志（Markdown 格式）                          |

## 安装

使用 pip 安装最新版：

```bash
pip install git+https://github.com/kaixinol/uniinfo-editor.git
```

## 运行

安装完成后，通过以下命令启动 CLI：

```bash
uniinfo_editor
```

---

## 协议

MIT License

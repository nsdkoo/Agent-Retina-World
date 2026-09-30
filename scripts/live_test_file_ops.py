"""文件操作实战测试：真跑一遍，把每一步的真实结果打出来。

沙箱在 `data/sandbox/`：用 patch 把「桌面 / 下载 / 文档」指到沙箱子目录，
全程不碰真实用户目录。分五段：

  A 文件系统层（列目录 / 归档三阶段 / 撤销 / 移动 / 复制 / 重命名 / 新建 / 回收站删除）
  B 文件内容层（读 / 写 / 改 / 内容搜索 / 通配匹配 / 覆盖后撤销还原）
  C Agent 端到端（一句话拆多步，走完整 controller + 权限 + 轨迹）
  D 安全边界（白名单拦截 + 额外根目录开关）
  E 汇总

跑法：.venv/Scripts/python.exe scripts/live_test_file_ops.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from screen_agent.agent import build_agent  # noqa: E402
from screen_agent.tools import files, shell  # noqa: E402
from screen_agent.tools.registry_setup import build_default_registry  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def ok(label: str, condition: bool, extra: str = "") -> None:
    print(f"   {'✓' if condition else '✗'} {label}" + (f"  [{extra}]" if extra else ""))
    (PASSED if condition else FAILED).append(label)


def head(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def show(label: str, result) -> None:  # noqa: ANN001
    print(f"\n▶ {label}")
    print(f"  success = {result.success}")
    for line in (result.message or "").split("\n"):
        print(f"  │ {line}")
    if result.options:
        print(f"  └ 选项：{result.options}")


def main() -> int:
    sandbox = ROOT / "data" / "sandbox"
    if sandbox.exists():
        shutil.rmtree(sandbox)
    for name in ("Desktop", "Downloads", "Documents"):
        (sandbox / name).mkdir(parents=True)

    desktop, downloads = sandbox / "Desktop", sandbox / "Downloads"
    (desktop / "素材").mkdir()

    seeds: dict[str, str] = {
        "季度报告.pdf": "2026 年第三季度数据汇总\n营收环比增长 12%\n人员净增 3 人",
        "笔记.md": "# 待办\n- 整理面试题库\n- 复盘上周项目",
        "脚本.py": "import os\n\nTARGET = 'data'\nprint(os.path.exists(TARGET))",
        "截图1.png": "\x89PNG\r\n\x1a\nbinary",
        "截图2.png": "\x89PNG\r\n\x1a\nbinary",
        "素材包.zip": "PK\x03\x04binary",
        "背景音乐.mp3": "ID3binary",
    }
    for name, body in seeds.items():
        (desktop / name).write_text(body, encoding="utf-8")
    (desktop / "素材" / "图标.png").write_text("PNG", encoding="utf-8")

    print(f"沙箱：{sandbox}")
    print(f"预置：桌面 {len(seeds)} 个文件 + 1 个子目录（子目录内 1 个文件）")

    patchers = [
        patch.object(files, "_user_dir", side_effect=lambda name: sandbox / name),
        patch.object(files, "_journal_path", lambda: sandbox / "_journal.json"),
        patch.object(files, "_pending_path", lambda: sandbox / "_pending.json"),
    ]
    for item in patchers:
        item.start()

    try:
        # ---------------- A 文件系统层 ----------------
        head("A 文件系统层")

        result = files.list_dir("桌面")
        show("A1 列出桌面内容", result)
        ok("列目录有内容且带文件/目录标记", result.success and "季度报告.pdf" in result.message)

        result = files.find_files("报告", base=str(desktop))
        show("A2 模糊找文件「报告」", result)
        ok("模糊查找命中", result.success)

        result = files.organize_dir(path="桌面")
        show("A3 归档第一步：问怎么整理", result)
        ok("给出整理选项", bool(result.options) and "按类型归档" in result.options)
        ok("统计跳过子目录（应为 7）", result.detail.get("count") == 7, f"count={result.detail.get('count')}")

        result = files.organize_dir(mode="type")
        show("A4 归档第二步：出「源 → 目标」清单", result)
        ok("出清单并等确认", bool(result.options) and "确认归档" in result.options)
        ok("预览阶段不动文件", not (desktop / "图片").exists())

        result = files.organize_dir(mode="type", apply=True)
        show("A5 归档第三步：确认后执行", result)
        ok("图片进了 图片/", (desktop / "图片" / "截图1.png").exists())
        ok("文档进了 文档/", (desktop / "文档" / "季度报告.pdf").exists())
        ok("代码进了 代码/", (desktop / "代码" / "脚本.py").exists())
        ok("子目录原样不动", (desktop / "素材" / "图标.png").exists())

        result = files.undo_last()
        show("A6 撤销归档", result)
        ok("文件回到桌面", (desktop / "季度报告.pdf").exists())
        ok("归档产生的空目录被清掉", not (desktop / "图片").exists())

        result = files.move_path("桌面/季度报告.pdf", "下载")
        show("A7 移动文件到下载", result)
        ok("文件到位", (downloads / "季度报告.pdf").exists())

        (desktop / "季度报告.pdf").write_text("第二份", encoding="utf-8")
        result = files.move_path("桌面/季度报告.pdf", "下载")
        show("A8 再移一个同名文件（验证不覆盖）", result)
        ok("重名自动加 _1", (downloads / "季度报告_1.pdf").exists())
        ok("原文件没被覆盖", (downloads / "季度报告.pdf").read_text(encoding="utf-8").startswith("2026"))

        result = files.copy_path("桌面/笔记.md", "下载")
        show("A9 复制文件", result)
        ok("复制件到位且源文件保留",
           (downloads / "笔记.md").exists() and (desktop / "笔记.md").exists())

        result = files.rename_path("桌面/笔记.md", "面试待办")
        show("A10 重命名（只给主名，后缀要保住）", result)
        ok("后缀自动保留", (desktop / "面试待办.md").exists())

        result = files.make_dir("项目资料")
        show("A11 新建文件夹", result)
        ok("默认落在桌面", (desktop / "项目资料").is_dir())

        victim = desktop / "回收站测试.txt"
        victim.write_text("这个文件会进回收站，可还原", encoding="utf-8")
        result = files.delete_path(str(victim))
        show("A12 删除（走回收站）", result)
        ok("文件从原位置消失", not victim.exists())
        ok("提示可还原", "回收站" in result.message)

        # ---------------- B 文件内容层 ----------------
        head("B 文件内容层（对标 Codex 的 read / write / edit / grep / glob）")

        result = files.read_file("下载/季度报告.pdf")
        show("B1 读文件内容", result)
        ok("读出带行号的内容", result.success and "1|" in result.message)

        binary = desktop / "真二进制.bin"
        binary.write_bytes(b"\xff\xfe\x00\x01\x80\x81")
        result = files.read_file(str(binary))
        show("B2 读二进制文件（应被拒绝）", result)
        ok("二进制被拒且给出人话", (not result.success) and "二进制" in result.message)

        result = files.write_file("桌面/会议记录.md", "2026-09-30 周会\n议题：Agent 架构对齐")
        show("B3 写新文件", result)
        ok("文件创建成功", (desktop / "会议记录.md").exists())

        result = files.write_file("桌面/会议记录.md", "\n结论：先落地权限策略", mode="append")
        show("B4 追加内容", result)
        ok("内容追加而非覆盖", "议题" in (desktop / "会议记录.md").read_text(encoding="utf-8"))

        result = files.edit_file("桌面/会议记录.md", "先落地权限策略", "先落地权限策略与事件流")
        show("B5 精确替换一处", result)
        ok("替换生效", "事件流" in (desktop / "会议记录.md").read_text(encoding="utf-8"))

        result = files.edit_file("桌面/会议记录.md", "不存在的句子", "x")
        show("B6 替换不存在的文本（应拒绝）", result)
        ok("找不到就不动", not result.success)

        result = files.grep_files("TARGET", path="桌面", suffix=".py")
        show("B7 按内容搜索（限定 .py）", result)
        ok("搜到脚本里的 TARGET", result.success and "脚本.py" in result.message)

        result = files.glob_files("**/*.png", path="桌面")
        show("B8 通配符匹配", result)
        ok("匹配到图片", result.success and ".png" in result.message)

        before = (desktop / "面试待办.md").read_text(encoding="utf-8")
        files.write_file("桌面/面试待办.md", "整份内容都被换掉了")
        result = files.undo_last()
        show("B9 覆盖写入后撤销（用备份还原）", result)
        after = (desktop / "面试待办.md").read_text(encoding="utf-8")
        ok("内容还原成覆盖前", after == before)

        # ---------------- C Agent 端到端 ----------------
        head("C Agent 端到端（一句话拆多步，走完整 controller）")
        agent = build_agent(sandbox)
        goal = "新建文件夹 归档区 然后 把 桌面/会议记录.md 移动到 归档区"
        print(f"\n▶ 指令：{goal}")
        result = agent.run(goal)
        print(f"  success = {result.success}")
        for line in (result.message or "").split("\n"):
            print(f"  │ {line}")
        ok("任务走完了", agent.state is not None and agent.state.state.value == "finished")
        ok("第一步真的建了目录", (desktop / "归档区").is_dir())
        ok("第二步真的移动了文件", (desktop / "归档区" / "会议记录.md").exists())
        ok("轨迹落库可查", agent.trajectory.load(result.detail.get("task_id", "")) is not None)

        print("\n▶ 上面这个任务的完整事件流（审计轨迹）")
        for event in agent.stream.history():
            print(f"  · [{event.type.value}] {event.brief()}")

        # ---------------- D 安全边界 ----------------
        head("D 安全边界")
        outside = sandbox.parent / "sandbox_outside"
        outside.mkdir(exist_ok=True)
        result = files.write_file(str(outside / "越界.txt"), "不该写进来")
        show("D1 白名单之外的写入（应被拒）", result)
        ok("越界写入被拦", not result.success and "为了安全" in result.message)

        files.set_extra_roots([outside])
        result = files.write_file(str(outside / "放行.txt"), "这次是显式放开的")
        show("D2 显式放开额外根目录后", result)
        ok("放开后写入成功", result.success and (outside / "放行.txt").exists())

        # ---------------- D3 命令执行 ----------------
        head("D3 命令执行（受限 shell，对标 Codex 的 exec_command）")
        files.set_extra_roots([sandbox])

        result = shell.run_command("dir /b", workdir=str(desktop))
        show("D3 只读命令 `dir /b`", result)
        ok("只读命令正常执行", result.success and "面试待办.md" in result.message)

        result = shell.run_command("rm -rf /", workdir=str(desktop))
        show("D4 致命命令 `rm -rf /`（直接拒，不会执行）", result)
        ok("致命命令根本没跑", (not result.success) and "不能执行" in result.message)

        result = shell.run_command('python -c "print(6*7)"', workdir=str(desktop), timeout=20)
        show("D5 需确认类命令（单步直调会跑，多步任务里由 policy 拦）", result)
        ok("命令真的跑出结果", result.success and "42" in result.message)

        result = shell.run_command(
            'python -c "import time; time.sleep(4)"', workdir=str(desktop), timeout=1
        )
        show("D6 超时命令（1 秒后停掉）", result)
        ok("超时被兜住", (not result.success) and "超过" in result.message)
        files.set_extra_roots([])

        # ---------------- E 汇总 ----------------
        head("E 汇总")
        print(f"工具总数：{len(build_default_registry().list_tools())}")
        print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
        if FAILED:
            print("失败项：")
            for item in FAILED:
                print(f"  ✗ {item}")
        print(f"\n沙箱保留在：{sandbox}")
    finally:
        for item in patchers:
            item.stop()

    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""
cleanup_helper.py —— 卸载收尾清理器（windowed、无控制台、无 .bat）
=================================================================
uninstaller.exe 在退出前通过 Popen 启动本程序（与本进程同为 GUI 子系统，
不受"windowed 父进程派生控制台子进程『假执行』"问题影响），由本进程在
延迟后以纯 Python shutil/os 删除仍在占用自身的 uninstaller.exe 及整个
安装目录，从而彻底规避历史方案中"cmd /c start 临时 .bat + 脚本内自删"
产生的黑窗与「找不到批处理文件」弹窗。

使用：cleanup_helper.exe <安装目录绝对路径>

设计要点：
  · --windowed 打包：无控制台，任务栏绝不出现黑色命令行窗口；
  · 不调用任何 cmd / powershell / bat，仅用 shutil.rmtree（纯 Win32 文件 API）；
  · 首轮延迟 1.2s：等主卸载进程完全退出、释放对自身 exe 的句柄；
  · 最多 4 轮重试：覆盖个别文件短暂占用（如杀毒扫描）的边角情况。
"""
import os
import shutil
import sys
import time


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    if not target or not os.path.isdir(target):
        return 99
    # 等待主卸载进程完全退出、释放自身 exe 句柄后再动手
    time.sleep(1.2)
    for _ in range(4):
        try:
            shutil.rmtree(target, ignore_errors=False)
        except OSError:
            time.sleep(0.8)
            continue
        if not os.path.isdir(target):
            return 0
        time.sleep(0.6)
    return 1


if __name__ == "__main__":
    sys.exit(main())

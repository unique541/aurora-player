# -*- coding: utf-8 -*-
"""
uninstaller.py —— Aurora Player 卸载引导程序
=========================================
由 PyInstaller 打包为 uninstaller.exe，安装时随 setup.exe 复制到安装目录，
系统的「卸载」入口（控制面板→程序和功能 / 设置→应用）通过 UninstallString
指向本程序。

流程：确认卸载 → 进行中 → 完成。
实际执行：
  1. 结束正在运行的播放器进程
  2. 删除安装目录
  3. 删除桌面快捷方式 与 开始菜单（所有应用）快捷方式
  4. 清理 HKCU 文件关联（仅清理仍指向本播放器的扩展名，不误删其它应用）
  5. 删除 HKCU 系统卸载注册项
  6. 完成提示，并在退出后引导删除自身目录
"""
import ctypes
import os
import shutil
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from tkinter import ttk, messagebox

APP_NAME = "Aurora Player"
APP_DIR_NAME = "AuroraPlayer"
EXE_NAME = "AuroraPlayer.exe"
PROG_ID = "AuroraPlayer.Audio"
VIDEO_PROG_ID = "AuroraPlayer.Video"
AUDIO_EXTS = (".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".opus")
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv",
              ".wmv", ".m4v", ".mpg", ".mpeg", ".ts", ".3gp", ".ogv")
SHORTCUT_NAME = "Aurora Player.lnk"
START_MENU_GROUP = "AuroraPlayer"
INSTALL_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_DIR_NAME)
UNINSTALL_KEY = (r"Software\Microsoft\Windows\CurrentVersion"
                 r"\Uninstall\AuroraPlayer")


def get_folder(csidl):
    """按 CSIDL 取系统特殊文件夹路径（用户级，免管理员权限）。"""
    buf = ctypes.create_unicode_buffer(1024)
    ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return buf.value


def get_desktop():
    return get_folder(0x0010)  # CSIDL_DESKTOPDIRECTORY


def get_start_menu():
    return get_folder(0x0002)  # CSIDL_PROGRAMS


def reg_delete_tree(root, path):
    """递归删除注册表键及其全部子键。

    标准库 winreg 只有 DeleteKey，无 DeleteTree（那是 pywin32 的 API），
    故自行递归实现：先删光子键，再删自身。
    """
    import winreg

    def _del(p):
        try:
            k = winreg.OpenKey(root, p, 0,
                               winreg.KEY_READ | winreg.KEY_WRITE)
        except OSError:
            return
        try:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _del(p + "\\" + sub)
        finally:
            winreg.CloseKey(k)
        try:
            winreg.DeleteKey(root, p)
        except OSError:
            pass

    _del(path)


def remove_file_assoc():
    """清理 HKCU 文件关联：仅删除默认值仍指向本播放器的扩展名。

    返回实际清理的扩展名列表。其它应用的关联（默认值不是本 ProgID）保持不动。
    """
    removed = []
    try:
        import winreg
    except Exception:
        return removed
    base = r"Software\Classes"
    prog_ids = (PROG_ID, VIDEO_PROG_ID)
    try:
        for ext in AUDIO_EXTS + VIDEO_EXTS:
            key = base + "\\" + ext
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0,
                                    winreg.KEY_QUERY_VALUE
                                    | winreg.KEY_SET_VALUE) as k:
                    val = None
                    try:
                        val, _ = winreg.QueryValueEx(k, "")
                    except FileNotFoundError:
                        pass
                    if val in prog_ids:
                        winreg.DeleteValue(k, "")
                        removed.append(ext)
            except OSError:
                pass  # 键不存在，跳过
            # 清理 OpenWithProgids 中本播放器的 ProgID（其它应用不动）
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    base + "\\" + ext + "\\OpenWithProgids",
                                    0, winreg.KEY_SET_VALUE
                                    | winreg.KEY_QUERY_VALUE) as k:
                    for pid in prog_ids:
                        try:
                            winreg.DeleteValue(k, pid)
                        except FileNotFoundError:
                            pass
            except OSError:
                pass
        # 清理本播放器的 ProgID 本体（音频 + 视频）
        for pid in prog_ids:
            reg_delete_tree(winreg.HKEY_CURRENT_USER, base + "\\" + pid)
    except Exception:
        pass
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except Exception:
        pass
    return removed


def remove_uninstall_entry():
    try:
        import winreg
    except Exception:
        return False
    try:
        reg_delete_tree(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        return True
    except OSError:
        return False


def remove_shortcuts(paths):
    for p in paths:
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


def remove_install_dir():
    """删除安装目录（保留正在运行的自身 exe，稍后由清理进程收尾）。"""
    self_path = os.path.abspath(sys.argv[0])
    if not os.path.isdir(INSTALL_DIR):
        return
    for _ in range(2):  # 两轮清扫，处理占用释放后的残留
        try:
            for name in os.listdir(INSTALL_DIR):
                p = os.path.join(INSTALL_DIR, name)
                if os.path.abspath(p).lower() == self_path.lower():
                    continue
                try:
                    if os.path.isdir(p) and not os.path.islink(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        os.remove(p)
                except OSError:
                    continue
            break
        except OSError:
            break


def schedule_self_cleanup():
    """退出后由内嵌的 windowed 清理器（无控制台、无 .bat）删除自身及安装目录。

    · 旧方案写入临时 .bat 并由 cmd /c start 执行、脚本内 `del "%%~f0"` 自删：
      cmd 逐行读取时文件已消失，弹「找不到批处理文件」且黑窗残留任务栏。
    · 中间方案改为 spawn PowerShell（-EncodedCommand，-WindowStyle Hidden +
      CREATE_NO_WINDOW）：但实测 PyInstaller --windowed（无控制台）进程里
      spawn 的控制台型子进程（powershell/cmd）会"假执行"，延时自删实际失效。
    · 现方案：spawn 与本进程同为 GUI 子系统的内嵌清理器
      aurora_cleanup_helper.exe（--windowed 打包，从 _MEIPASS 释放到 %TEMP%
      后以 CREATE_NO_WINDOW|DETACHED 启动）。windowed→windowed 的后台进程
      在父进程退出后仍可靠运行（纯 Python shutil 删除），任务栏无黑窗、无
      .bat、无「找不到批处理文件」。
    """
    tmp_helper = os.path.join(os.environ.get(
        "TEMP", os.path.expanduser("~")), "aurora_cleanup_helper.exe")
    try:
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
            os.path.abspath(sys.argv[0]))
        src = os.path.join(base, "aurora_cleanup_helper.exe")
        if os.path.isfile(src) and \
                os.path.abspath(src).lower() != os.path.abspath(tmp_helper).lower():
            shutil.copy2(src, tmp_helper)
        if not os.path.isfile(tmp_helper):
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x00000008
        subprocess.Popen([tmp_helper, INSTALL_DIR],
                         creationflags=flags,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception:
        pass


def run_uninstall(update):
    """执行卸载全流程。update(msg) 可为 None。"""
    if update:
        update("正在结束播放器进程 ...")
    try:
        subprocess.run(["taskkill", "/f", "/im", EXE_NAME],
                       capture_output=True, timeout=30,
                       creationflags=getattr(subprocess,
                                             "CREATE_NO_WINDOW", 0))
    except Exception:
        pass

    if update:
        update("正在删除桌面快捷方式 ...")
    remove_shortcuts([os.path.join(get_desktop(), SHORTCUT_NAME),
                      os.path.join(os.path.expandvars(r"%PUBLIC%\Desktop"),
                                   SHORTCUT_NAME)])

    if update:
        update("正在删除开始菜单快捷方式 ...")
    sm_dir = get_start_menu()
    remove_shortcuts([os.path.join(sm_dir, START_MENU_GROUP, SHORTCUT_NAME),
                      os.path.join(sm_dir, SHORTCUT_NAME)])
    try:
        shutil.rmtree(os.path.join(sm_dir, START_MENU_GROUP),
                      ignore_errors=True)
    except OSError:
        pass

    if update:
        update("正在清理文件关联（仅清理本播放器的关联）...")
    removed = remove_file_assoc()

    if update:
        update("正在删除系统卸载注册项 ...")
    remove_uninstall_entry()

    if update:
        update("正在删除安装目录 ...")
    remove_install_dir()
    return removed


class UninstallerUI:
    """tkinter 卸载引导界面：确认 → 进行中 → 完成/失败。"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("卸载 %s" % APP_NAME)
        self.root.resizable(False, False)
        self.root.configure(bg="#f4f6fb")
        w, h = 480, 320
        self.root.geometry("%dx%d+%d+%d" % (
            w, h,
            (self.root.winfo_screenwidth() - w) // 2,
            (self.root.winfo_screenheight() - h) // 2))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._done = False

        # ---- 确认页 ----
        self.confirm = tk.Frame(self.root, bg="#f4f6fb")
        title = tk.Label(self.confirm, text="卸载 %s" % APP_NAME,
                         font=("Microsoft YaHei UI", 20, "bold"),
                         bg="#f4f6fb", fg="#c53030")
        title.pack(pady=(26, 10))
        desc = ("将删除以下内容：\n"
                "  · %s 安装目录（%s）\n"
                "  · 桌面与开始菜单快捷方式\n"
                "  · 音频文件关联（mp3/flac/wav/ogg/m4a/aac/opus）与"
                "视频文件关联（mp4/mkv/avi/mov/webm/flv 等），仅本播放器的\n"
                "  · 系统卸载记录"
                % (APP_NAME, INSTALL_DIR))
        tk.Label(self.confirm, text=desc, justify="left",
                 font=("Microsoft YaHei UI", 10), bg="#f4f6fb", fg="#2d3748",
                 anchor="w").pack(padx=34, fill="x", pady=(0, 16))
        bf = tk.Frame(self.confirm, bg="#f4f6fb")
        bf.pack(pady=(2, 0))
        tk.Button(bf, text="取消", width=12, command=self._on_cancel,
                  relief="flat", bg="#a0aec0", fg="white",
                  font=("Microsoft YaHei UI", 10, "bold"),
                  activebackground="#8996a8").pack(side="left", padx=8)
        tk.Button(bf, text="确认卸载", width=12, command=self._start,
                  relief="flat", bg="#c53030", fg="white",
                  font=("Microsoft YaHei UI", 10, "bold"),
                  activebackground="#9b2c2c").pack(side="left", padx=8)

        # ---- 进行中/结果页 ----
        self.progress = tk.Frame(self.root, bg="#f4f6fb")
        self.res_title = tk.Label(
            self.progress, text="正在卸载 ...",
            font=("Microsoft YaHei UI", 18, "bold"),
            bg="#f4f6fb", fg="#2d3748")
        self.res_title.pack(pady=(30, 14))
        self.status = tk.Label(self.progress, text="",
                               font=("Microsoft YaHei UI", 10),
                               bg="#f4f6fb", fg="#4a5568")
        self.status.pack(pady=(0, 16))
        self.bar = ttk.Progressbar(self.progress, mode="indeterminate",
                                   length=380)
        self.bar.pack(pady=(0, 4))
        self.bar.start(12)
        self.done_btn = tk.Button(
            self.progress, text="完成", width=12, state="disabled",
            command=self._on_done, relief="flat", bg="#2b6cb0", fg="white",
            font=("Microsoft YaHei UI", 10, "bold"),
            activebackground="#234e8f")
        self.done_btn.pack(pady=(14, 0))

    def _on_cancel(self):
        self._done = False
        self.root.destroy()

    def _on_close(self):
        if not self._done:
            self._on_cancel()
        else:
            self._on_done()

    def _start(self):
        self.confirm.pack_forget()
        self.progress.pack(fill="both", expand=True)

        def work():
            try:
                removed = run_uninstall(self._status)
                self._ui(self._finish_ok, removed)
            except Exception as exc:
                self._ui(self._finish_err, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _ui(self, fn, *args):
        self.root.after(0, fn, *args)

    def _status(self, msg):
        self.root.after(0, lambda m=msg: self.status.configure(text=m))

    def _finish_ok(self, removed):
        self.bar.stop()
        self._done = True
        self.res_title.configure(text="卸载完成", fg="#2f855a")
        note = ""
        if removed:
            note = "已清理文件关联：" + " / ".join(removed) + "\n"
        self.status.configure(
            text="已删除安装目录、桌面与开始菜单快捷方式。\n%s"
                 "您可以从系统中卸载本应用了。" % note)
        self.done_btn.configure(state="normal", text="完成")

    def _finish_err(self, msg):
        self.bar.stop()
        self._done = False
        self.res_title.configure(text="卸载失败", fg="#c53030")
        self.status.configure(text="发生错误：%s" % msg)
        self.done_btn.configure(state="normal", text="关闭")

    def _on_done(self):
        self.root.destroy()
        schedule_self_cleanup()

    def run(self):
        self.confirm.pack(fill="both", expand=True)
        self.root.mainloop()
        return self._done


def run_silent():
    """静默卸载（不弹确认框与完成框），供系统/自动化调用。"""
    removed = run_uninstall(None)
    schedule_self_cleanup()
    return 0 if removed is not None else 1


def main():
    # 支持静默模式（环境变量或 --silent 参数），用于自动化与系统脚本
    silent = os.environ.get("AURORA_UNINSTALL_SILENT") == "1" \
        or "--silent" in sys.argv[1:]
    if silent:
        sys.exit(run_silent())
    ui = UninstallerUI()
    ok = ui.run()
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
installer.py —— Aurora Player 安装程序
========================================
本脚本由 PyInstaller 打包为 setup.exe（--onefile --windowed），
并把 AuroraPlayer.exe 与 uninstaller.exe 作为内置资源一并打入（--add-binary）。

双击 setup.exe 后：
  0. 先弹出确认框「是否安装 Aurora Player？」——选「是」才开始正式安装，
     选「否」立即退出、不做任何安装动作（无写盘/建快捷方式/注册表操作）；
  1. 将 AuroraPlayer.exe、uninstaller.exe 复制/覆盖到 %LOCALAPPDATA%\AuroraPlayer
  2. 在桌面创建「Aurora Player」快捷方式
  3. 在开始菜单「所有应用」创建 AuroraPlayer\Aurora Player 快捷方式
  4. 注册系统卸载信息（HKCU\...\\Uninstall\\AuroraPlayer），
     使「设置→应用」与「控制面板→程序和功能」中出现卸载入口，
     卸载会弹出 GUI 引导（uninstaller.exe）
  5. 可选：注册常见音频/视频文件关联（双击媒体文件即可用 Aurora Player 播放）
  6. 全程有 tkinter 图形界面提示（进行中 / 完成），完成可选立即启动
"""
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import tkinter as tk
from tkinter import ttk, messagebox

APP_NAME = "Aurora Player"
APP_VERSION = "2.0.0"
PUBLISHER = "Marvis"
EXE_NAME = "AuroraPlayer.exe"
UNINSTALLER_EXE = "uninstaller.exe"
APP_DIR_NAME = "AuroraPlayer"
START_MENU_GROUP = "AuroraPlayer"
# 文件关联：ProgID 与常见音频/视频扩展名（HKCU 用户级，免管理员权限）
PROG_ID = "AuroraPlayer.Audio"
VIDEO_PROG_ID = "AuroraPlayer.Video"
AUDIO_EXTS = (".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".opus")
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv",
              ".wmv", ".m4v", ".mpg", ".mpeg")
SHORTCUT_NAME = "Aurora Player.lnk"
INSTALL_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_DIR_NAME)
UNINSTALL_KEY = (r"Software\Microsoft\Windows\CurrentVersion"
                 r"\Uninstall\AuroraPlayer")


def resource(exe_name):
    """定位 PyInstaller 解包后的内置资源。"""
    base = getattr(sys, "_MEIPASS",
                   os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, exe_name)


def get_folder(csidl):
    """用 Windows API 获取系统特殊文件夹真实路径，避免编码/重定向问题。"""
    buf = ctypes.create_unicode_buffer(1024)
    ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return buf.value


def get_desktop():
    return get_folder(0x0010)  # CSIDL_DESKTOPDIRECTORY


def get_start_menu():
    return get_folder(0x0002)  # CSIDL_PROGRAMS


def _shortcut_vbs(lnk_path, target, working_dir):
    """生成创建指定路径快捷方式的 VBScript（由 wscript.exe GUI 宿主执行）。

    VBS 字符串转义：双引号写成 ""；文件以 UTF-16LE(BOM) 落盘，中文/空格
    路径均无损，且绕开 PowerShell 5.1 管道输出乱码问题。
    """
    def s(x):
        return '"' + str(x).replace('"', '""') + '"'

    return (
        "Set ws = CreateObject(\"WScript.Shell\")\r\n"
        "Set lnk = ws.CreateShortcut(" + s(lnk_path) + ")\r\n"
        "lnk.TargetPath = " + s(target) + "\r\n"
        "lnk.WorkingDirectory = " + s(working_dir) + "\r\n"
        "lnk.IconLocation = " + s(target + ",0") + "\r\n"
        "lnk.Description = " + s(APP_NAME) + "\r\n"
        "lnk.Save()\r\n"
    )


def _run_wscript_create_shortcut(lnk_path, target, working_dir):
    """用 wscript.exe（GUI 子系统宿主、无控制台、任务栏不出现黑窗）创建快捷方式。

    为何不用 PowerShell：实测 PyInstaller --windowed（无控制台）进程里
    spawn 的控制台型子进程（powershell/cmd）会"假执行"——进程能起来、返回
    码为 0，但脚本命令根本不生效。而 wscript.exe 与 PowerShell 不同，它
    本身是 GUI 子系统程序，作为 windowed 父进程的子进程可正常执行 .vbs，
    因此创建快捷方式改用 wscript + 临时 .vbs（用后立即删除）。
    """
    vbs_path = ""
    try:
        fd, vbs_path = tempfile.mkstemp(prefix="aurora_lnk_", suffix=".vbs")
        with os.fdopen(fd, "w", encoding="utf-16", newline="") as f:
            f.write(_shortcut_vbs(lnk_path, target, working_dir))
        subprocess.run(
            ["wscript.exe", vbs_path],
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    finally:
        if vbs_path and os.path.exists(vbs_path):
            try:
                os.remove(vbs_path)
            except OSError:
                pass
    return os.path.isfile(lnk_path)


def make_shortcut_at(lnk_path, target, working_dir):
    """在指定路径创建快捷方式，返回 (ok, lnk_path)。"""
    try:
        os.makedirs(os.path.dirname(lnk_path), exist_ok=True)
    except OSError:
        pass
    return (_run_wscript_create_shortcut(lnk_path, target, working_dir),
            lnk_path)


def make_desktop_shortcut(target, working_dir):
    """创建桌面快捷方式。"""
    return make_shortcut_at(os.path.join(get_desktop(), SHORTCUT_NAME),
                            target, working_dir)


def make_start_menu_shortcut(target, working_dir):
    """创建开始菜单「所有应用」下的快捷方式（Programs\AuroraPlayer\）。"""
    return make_shortcut_at(
        os.path.join(get_start_menu(), START_MENU_GROUP, SHORTCUT_NAME),
        target, working_dir)


def register_file_assoc(install_dir):
    """在 HKCU\Software\Classes 写入文件关联（免管理员权限）。

    为常见音频/视频扩展名关联本播放器，使双击媒体文件即可由
    Aurora Player 播放：音频走 AuroraPlayer.Audio，视频走 AuroraPlayer.Video。
    """
    try:
        import winreg
    except Exception:
        return False
    exe = os.path.join(install_dir, EXE_NAME)
    base = r"Software\Classes"

    def _write_progid(progid, exts):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              base + "\\" + progid) as k:
            winreg.SetValue(k, "", winreg.REG_SZ, APP_NAME)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              base + "\\" + progid + "\\DefaultIcon") as k:
            winreg.SetValue(k, "", winreg.REG_SZ, '"%s",0' % exe)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              base + "\\" + progid
                              + "\\shell\\open\\command") as k:
            winreg.SetValue(k, "", winreg.REG_SZ, '"%s" "%%1"' % exe)
        for ext in exts:
            ekey = base + "\\" + ext
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ekey, 0,
                                    winreg.KEY_SET_VALUE
                                    | winreg.KEY_QUERY_VALUE) as k:
                    winreg.SetValue(k, "", winreg.REG_SZ, progid)
            except OSError:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, ekey) as k:
                    winreg.SetValue(k, "", winreg.REG_SZ, progid)
            _add_open_with(ext, progid)
            _reset_user_choice(ext)

    def _add_open_with(ext, progid):
        """把 ProgID 写入 OpenWithProgids，使本播放器出现在「打开方式」列表。"""
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                                  base + "\\" + ext
                                  + "\\OpenWithProgids") as k:
                try:
                    winreg.QueryValueEx(k, progid)
                except FileNotFoundError:
                    winreg.SetValueEx(k, progid, 0, winreg.REG_SZ, "")
        except OSError:
            pass

    def _reset_user_choice(ext):
        """Win10/11 的默认程序由 FileExts\\ext\\UserChoice 的 Hash 校验值决定，
        仅写 Software\\Classes 默认值不会改变实际打开方式；UserChoice 的 Hash
        算法随系统版本变化且会过期，无法可靠伪造，删除该缓存键后资源管理器
        回退到 Software\\Classes 默认值（即本播放器）。
        """
        try:
            uckey = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                     r"\FileExts" + "\\" + ext)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, uckey, 0,
                                winreg.KEY_READ | winreg.KEY_WRITE) as k:
                try:
                    winreg.DeleteKey(k, "UserChoice")
                except FileNotFoundError:
                    pass
        except OSError:
            pass

    try:
        _write_progid(PROG_ID, AUDIO_EXTS)
        _write_progid(VIDEO_PROG_ID, VIDEO_EXTS)
        try:
            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
        except Exception:
            pass
        return True
    except Exception:
        return False


def register_uninstall_entry(install_dir):
    """注册系统卸载信息（HKCU），使出现在「设置→应用」与「程序和功能」。

    UninstallString 指向 uninstaller.exe，触发后弹出 GUI 卸载引导。
    """
    try:
        import winreg
    except Exception:
        return False
    exe = os.path.join(install_dir, EXE_NAME)
    uninst = os.path.join(install_dir, UNINSTALLER_EXE)
    size_kb = 0
    for p in (exe, uninst):
        try:
            size_kb += os.path.getsize(p)
        except OSError:
            pass
    size_kb //= 1024
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ,
                              APP_VERSION)
            winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
            winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ,
                              install_dir)
            winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ,
                              '"%s",0' % exe)
            winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ,
                              '"%s"' % uninst)
            winreg.SetValueEx(k, "QuietUninstallString", 0, winreg.REG_SZ,
                              '"%s" --silent' % uninst)
            winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD,
                              size_kb)
            winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
        return True
    except Exception:
        return False


def verify_installed():
    """快速自检：主程序 + 卸载程序 + 桌面/开始菜单快捷方式。"""
    checks = []
    if not os.path.isfile(os.path.join(INSTALL_DIR, EXE_NAME)):
        checks.append("主程序文件缺失")
    if not os.path.isfile(os.path.join(INSTALL_DIR, UNINSTALLER_EXE)):
        checks.append("卸载程序缺失")
    if not os.path.isfile(os.path.join(get_desktop(), SHORTCUT_NAME)):
        checks.append("桌面快捷方式不可用")
    if not os.path.isfile(os.path.join(
            get_start_menu(), START_MENU_GROUP, SHORTCUT_NAME)):
        checks.append("开始菜单快捷方式不可用")
    if checks:
        return False, "；".join(checks)
    return True, ""


def uninstall_registered():
    """判断系统卸载注册表项是否已写入（用于日志汇报）。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0,
                            winreg.KEY_QUERY_VALUE):
            return True
    except Exception:
        return False


def do_install(update, done, fail, register_assoc):
    """后台线程执行安装。回调 update/done/fail；返回成功与否。"""
    try:
        if update:
            update("正在准备安装目录 ...")
        os.makedirs(INSTALL_DIR, exist_ok=True)
        src = resource(EXE_NAME)
        if not os.path.isfile(src):
            raise RuntimeError("未找到要安装的主程序文件：%s" % src)
        dst = os.path.join(INSTALL_DIR, EXE_NAME)
        if update:
            update("正在复制主程序（约 90MB，请稍候）...")
        shutil.copy2(src, dst)

        usrc = resource(UNINSTALLER_EXE)
        if not os.path.isfile(usrc):
            raise RuntimeError("未找到内置的卸载程序：%s" % usrc)
        udst = os.path.join(INSTALL_DIR, UNINSTALLER_EXE)
        if update:
            update("正在部署卸载引导程序 ...")
        shutil.copy2(usrc, udst)

        if update:
            update("正在创建桌面快捷方式 ...")
        ok, lnk = make_desktop_shortcut(dst, INSTALL_DIR)
        if not ok:
            raise RuntimeError("桌面快捷方式创建失败")

        if update:
            update("正在创建开始菜单快捷方式 ...")
        sm_ok, sm_lnk = make_start_menu_shortcut(dst, INSTALL_DIR)
        if not sm_ok:
            raise RuntimeError("开始菜单快捷方式创建失败")

        reg_assoc = False
        if register_assoc:
            if update:
                update("正在注册文件关联（设为音频默认播放器）...")
            reg_assoc = register_file_assoc(INSTALL_DIR)
            if not reg_assoc and update:
                update("文件关联注册失败，已跳过（不影响使用）...")

        if update:
            update("正在注册系统卸载入口 ...")
        reg_unin = register_uninstall_entry(INSTALL_DIR)
        if not reg_unin:
            raise RuntimeError("系统卸载入口注册失败")

        v_ok, v_msg = verify_installed()
        if not v_ok:
            raise RuntimeError("安装自检未通过：%s" % v_msg)
        unin_reg = "1" if uninstall_registered() else "0"
        try:
            with open(os.path.join(INSTALL_DIR, "install_log.txt"),
                      "w", encoding="utf-8", errors="replace") as f:
                f.write("installed_ok=1\nversion=%s\ninstall_dir=%s\n"
                        "desktop_shortcut=%s\nstart_menu_shortcut=%s\n"
                        "uninstaller=1\nuninstall_registered=%s\n"
                        "file_assoc_registered=%s\n"
                        "video_assoc_registered=%s\n"
                        % (APP_VERSION, INSTALL_DIR, lnk, sm_lnk,
                           unin_reg, "1" if reg_assoc else "0",
                           "1" if reg_assoc else "0"))
        except OSError:
            pass
        assoc_note = ""
        if register_assoc:
            if reg_assoc:
                assoc_note = ("已将常见音频与视频文件关联到本播放器\n"
                              "（音频：mp3/flac/wav/ogg/m4a/aac/opus；\n"
                              " 视频：mp4/mkv/avi/mov/webm/flv 等），\n"
                              "双击媒体文件即可播放。\n")
            else:
                assoc_note = "文件关联未注册（可稍后在系统设置中手动指定）。\n"
        if done:
            done("安装完成！\n\n"
                 "安装目录：%s\n"
                 "桌面/开始菜单快捷方式：已创建\n"
                 "系统卸载入口：%s\n"
                 "（可在「设置→应用」或「控制面板→程序和功能」中卸载）\n\n"
                 "%s\n"
                 "您可以双击桌面上的「Aurora Player」图标直接启动，"
                 "或直接双击音频/视频文件播放。"
                 % (INSTALL_DIR,
                    "已注册（Uninstall\\AuroraPlayer）"
                    if unin_reg == "1" else "注册失败", assoc_note))
        return True
    except Exception as exc:
        try:
            os.makedirs(INSTALL_DIR, exist_ok=True)
            with open(os.path.join(INSTALL_DIR, "install_log.txt"),
                      "w", encoding="utf-8", errors="replace") as f:
                f.write("installed_ok=0\nerror=%s\n%s\n"
                        % (exc, traceback.format_exc()))
        except OSError:
            pass
        if fail:
            fail(str(exc))
        return False


class InstallerUI:
    def __init__(self, silent=False):
        self.silent = silent
        self.root = None
        if silent:
            return
        self.root = tk.Tk()
        self.root.title("安装 %s" % APP_NAME)
        self.root.resizable(False, False)
        self.root.configure(bg="#f4f6fb")
        w, h = 460, 330
        self.root.geometry("%dx%d+%d+%d" % (
            w, h,
            (self.root.winfo_screenwidth() - w) // 2,
            (self.root.winfo_screenheight() - h) // 2))

        title = tk.Label(self.root, text=APP_NAME,
                         font=("Microsoft YaHei UI", 20, "bold"),
                         bg="#f4f6fb", fg="#2b6cb0")
        title.pack(pady=(24, 4))
        sub = tk.Label(self.root, text="音频与视频播放器 · 正在为您安装",
                       font=("Microsoft YaHei UI", 10),
                       bg="#f4f6fb", fg="#718096")
        sub.pack(pady=(0, 14))

        self.assoc_var = tk.BooleanVar(value=True)
        self.assoc_chk = tk.Checkbutton(
            self.root,
            text="设为常见音频/视频文件的默认播放器\n"
                 "（音频：mp3/flac/wav/ogg/m4a/aac/opus\n"
                 " 视频：mp4/mkv/avi/mov/webm/flv）",
            variable=self.assoc_var, bg="#f4f6fb", fg="#2d3748",
            activebackground="#f4f6fb", activeforeground="#2d3748",
            font=("Microsoft YaHei UI", 9), anchor="w", justify="left")
        self.assoc_chk.pack(padx=24, fill="x", pady=(0, 2))
        tip = tk.Label(self.root, text="（将注册到「设置→应用」卸载列表，"
                                       "并创建开始菜单快捷方式）",
                       font=("Microsoft YaHei UI", 8), bg="#f4f6fb",
                       fg="#a0aec0", anchor="w")
        tip.pack(padx=28, fill="x", pady=(0, 8))

        self.status = tk.Label(self.root, text="正在初始化安装 ...",
                               font=("Microsoft YaHei UI", 10),
                               bg="#f4f6fb", fg="#2d3748")
        self.status.pack(pady=(0, 10))

        self.bar = ttk.Progressbar(self.root, mode="indeterminate", length=360)
        self.bar.pack(pady=(0, 8))
        self.bar.start(12)

        self.btn_frame = tk.Frame(self.root, bg="#f4f6fb")
        self.btn_frame.pack(pady=(6, 0))
        self.close_btn = tk.Button(self.btn_frame, text="关闭", width=12,
                                   command=self.root.destroy,
                                   relief="flat", bg="#a0aec0", fg="white",
                                   font=("Microsoft YaHei UI", 10, "bold"),
                                   activebackground="#8996a8")
        self.close_btn.pack(side="left", padx=8)
        self.launch_btn = tk.Button(self.btn_frame, text="启动播放器", width=12,
                                    state="disabled", command=self._launch,
                                    relief="flat", bg="#2b6cb0", fg="white",
                                    font=("Microsoft YaHei UI", 10, "bold"),
                                    activebackground="#234e8f")
        self.launch_btn.pack(side="left", padx=8)

        self._done = False
        self._target = os.path.join(INSTALL_DIR, EXE_NAME)

    def _launch(self):
        try:
            subprocess.Popen([self._target], cwd=INSTALL_DIR)
        except Exception:
            pass
        if self.root:
            self.root.destroy()

    def _ui(self, fn, *args):
        if self.silent or not self.root:
            try:
                if args:
                    fn(*args)
                else:
                    fn()
            except Exception:
                pass
            return
        self.root.after(0, fn, *args)

    def run(self):
        def _start():
            do_install(
                lambda m: self._ui(self._set_status, m),
                lambda m: self._ui(self._finish_ok, m),
                lambda m: self._ui(self._finish_err, m),
                self.assoc_var.get() if self.root else True)
        if self.silent:
            return do_install(lambda m: None, lambda m: None,
                              lambda m: None, True)
        threading.Thread(target=_start, daemon=True).start()
        self.root.mainloop()
        return self._done

    def _set_status(self, msg):
        self.status.configure(text=msg)

    def _finish_ok(self, msg):
        self.bar.stop()
        self._done = True
        self.status.configure(text="安装成功", fg="#2f855a")
        messagebox.showinfo("安装完成", msg, parent=self.root)
        self.launch_btn.configure(state="normal")

    def _finish_err(self, msg):
        self.bar.stop()
        self.status.configure(text="安装失败", fg="#c53030")
        messagebox.showerror("安装失败", msg, parent=self.root)


def confirm_install():
    """安装前的显式确认询问「是否安装 AuroraPlayer？」。

    在**任何**安装动作（写盘、建快捷方式、注册表操作）之前调用：
    · 返回 True  → 继续开始正式安装流程；
    · 返回 False → 调用方应直接退出进程，零安装副作用。
    用独立 withdrawn 的 Tk 根承载 messagebox，避免与安装向导窗口叠加。
    """
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        return messagebox.askyesno(
            "安装 Aurora Player",
            "是否安装 Aurora Player？\n\n"
            "点击「是」将执行以下操作：\n"
            "  · 复制主程序与卸载程序到安装目录\n"
            "    （%s）\n"
            "  · 创建桌面与开始菜单快捷方式\n"
            "  · 注册系统卸载入口（可随时在「设置→应用」中卸载）\n"
            "  · 注册常见音频/视频文件关联（双击即可播放）\n\n"
            "点击「否」将退出安装，不进行任何操作。"
            % INSTALL_DIR)
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def main():
    silent = os.environ.get("AURORA_INSTALL_SILENT") == "1" \
        or "--silent" in sys.argv[1:]
    if not silent:
        # 双击 setup.exe：先确认是否安装；「否」直接退出，零副作用
        if not confirm_install():
            sys.exit(0)
    ui = InstallerUI(silent=silent)
    ok = ui.run()
    if not ok:
        sys.exit(2)


if __name__ == "__main__":
    main()

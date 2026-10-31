# -*- coding: utf-8 -*-
"""
player_core.py —— 音频播放引擎核心
=====================================
提供三层解码后端，按可用性自动降级（engine_factory 自动选择）：

  1. FfmpegEngine : 调用 ffmpeg 解码为 PCM 管道 + sounddevice 播放
                    （支持格式最多：mp3/flac/wav/ogg/m4a/aac/wma/ape/opus/aiff 等）
  2. PygameEngine : pygame.mixer 播放（mp3/ogg/wav/mid 等基础格式，无 ffmpeg 时降级）
  3. QtEngine     : QtMultimedia 播放（PySide6 自带，常见格式兜底）

所有引擎对外暴露一致的接口（信号 + 方法），主窗口可无差别调用。
"""
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal

# ---------------------------------------------------------------------------
# 惰性依赖加载：numpy / sounddevice / QtMultimedia 都是重量级模块，
# 模块级直接 import 会显著拖慢启动（numpy ~0.3s、QtMultimedia ~0.5s）。
# 统一改为「首次真正使用时才 import」，失败则返回 None / False 优雅降级。
# ---------------------------------------------------------------------------
_np = None            # None=未尝试；False=导入失败；模块=成功
_sd = None
_QMP = None
_QAO = None
_QTM_AVAILABLE = False


def _get_np():
    """返回 numpy 模块（惰性导入）；不可用时返回 None。"""
    global _np
    if _np is None:
        try:
            import numpy as _np
        except Exception:
            _np = False
    return _np if _np is not False else None


def _get_sd():
    """返回 sounddevice 模块（惰性导入）；不可用时返回 None。"""
    global _sd
    if _sd is None:
        try:
            import sounddevice as _sd
        except Exception:
            _sd = False
    return _sd if _sd is not False else None


def _get_qtm():
    """惰性探测 QtMultimedia 是否可用，返回 (QMediaPlayer, QAudioOutput)。"""
    global _QMP, _QAO, _QTM_AVAILABLE
    if _QMP is None:
        try:
            from PySide6.QtMultimedia import (QMediaPlayer as _QMP,
                                              QAudioOutput as _QAO)
            _QTM_AVAILABLE = True
        except Exception:
            _QMP = _QAO = False
    if _QMP is False:
        return None, None
    return _QMP, _QAO


# ---------------------------------------------------------------------------
# 通用媒体扩展名表（导入歌单按此扫描；实际能否播放取决于所选后端）
# ---------------------------------------------------------------------------
AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".mp4",
    ".wma", ".ape", ".opus", ".aiff", ".aif", ".aifc", ".mp2", ".mp1",
    ".amr", ".mka", ".ac3", ".dts", ".wv", ".tta", ".caf", ".mid",
    ".midi", ".mod", ".s3m", ".xm", ".it", ".mpc", ".m4b", ".webm",
}

# 视频扩展名（QtMultimedia 可播；Windows 下 Qt6 内置 FFmpeg 后端）
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv",
    ".wmv", ".m4v", ".mpg", ".mpeg", ".ts", ".3gp", ".ogv",
}

# 音乐软件专属加密音频扩展名（导入后由 unlock_formats 自动解密播放；
# 注意：mflac/mgg 属于 QMCv2，解密模块明确不支持，故不纳入可导入列表）
CRYPTO_AUDIO_EXTENSIONS = {
    ".ncm", ".qmc0", ".qmc2", ".qmc3", ".qmcflac", ".qmcogg",
    ".kgm", ".kgma", ".kgg", ".kwm",
}

# 可导入播放列表的完整媒体扩展名 = 音频 + 视频 + 专属加密音频
PLAYABLE_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS \
    | CRYPTO_AUDIO_EXTENSIONS

# pygame 后端实际支持的基础格式
_PYGAME_EXTS = {".mp3", ".ogg", ".oga", ".wav", ".mid", ".midi",
                ".mod", ".s3m", ".xm", ".it"}

# QtMultimedia 后端典型的可播格式
_QT_EXTS = {".mp3", ".flac", ".wav", ".ogg", ".oga", ".m4a", ".aac",
            ".mp4", ".webm", ".wma", ".aiff", ".aif", ".amr", ".mid",
            ".midi", ".m4b"}


def _win_nowindow():
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def find_executable(name):
    """查找可执行文件：优先 PyInstaller 内置资源（sys._MEIPASS），
    其次 PATH，最后常见固定安装位置。"""
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        p = os.path.join(meipass, name)
        if os.path.isfile(p):
            return os.path.normpath(p)
    found = shutil.which(name)
    if found:
        return os.path.normpath(found)
    if os.name == "nt":
        for base in (r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin",
                     r"C:\Program Files (x86)\ffmpeg\bin", r"D:\ffmpeg\bin",
                     r"D:\Program Files\ffmpeg\bin"):
            p = os.path.join(base, name)
            if os.path.isfile(p):
                return p
    # 兜底：imageio-ffmpeg 自带二进制（文件名带平台前缀，无法被 which 命中）
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg
            p = imageio_ffmpeg.get_ffmpeg_exe()
            if p and os.path.isfile(p):
                return os.path.normpath(p)
        except Exception:
            pass
    return None


def scan_media_files(folder, exts=None):
    """递归扫描文件夹内的媒体文件（默认音频+视频），返回排序后的绝对路径。"""
    if exts is None:
        exts = PLAYABLE_EXTENSIONS
    result = []
    folder = os.path.normpath(folder)
    if not os.path.isdir(folder):
        return result
    for root, _dirs, files in os.walk(folder):
        for fn in files:
            if os.path.splitext(fn)[1].lower() in exts:
                result.append(os.path.join(root, fn))
    return sorted(result)


def scan_audio_files(folder):
    """递归扫描文件夹内的所有音频文件（兼容旧调用）。"""
    return scan_media_files(folder, AUDIO_EXTENSIONS)


# ---------------------------------------------------------------------------
# 公共引擎抽象
# ---------------------------------------------------------------------------
class AudioEngine(QObject):
    """所有音频引擎的公共抽象基类（信号接口约定）。"""

    positionChanged = Signal(int)      # 当前播放位置（毫秒）
    durationChanged = Signal(int)      # 时长（毫秒）
    playbackFinished = Signal()        # 一曲自然播放结束
    playbackError = Signal(str)        # 播放出错

    def __init__(self, parent=None):
        super().__init__(parent)
        self._path = None
        self._duration_ms = 0
        self._volume = 0.8
        self._playing = False
        self._paused = False

    # ---- 子类必须实现 ----
    @property
    def backend_name(self):  # pragma: no cover - 抽象
        raise NotImplementedError

    @property
    def supported_exts(self):  # pragma: no cover - 抽象
        raise NotImplementedError

    def open_file(self, path):  # pragma: no cover
        raise NotImplementedError

    def play_stream(self, from_ms=0):  # pragma: no cover
        raise NotImplementedError

    def pause_stream(self):  # pragma: no cover
        raise NotImplementedError

    def resume_stream(self):  # pragma: no cover
        raise NotImplementedError

    def stop_stream(self):  # pragma: no cover
        raise NotImplementedError

    def seek_to(self, ms):  # pragma: no cover
        raise NotImplementedError

    def position_ms(self):  # pragma: no cover
        raise NotImplementedError

    # ---- 共用状态 ----
    @property
    def is_playing(self):
        """True = 当前有活动曲目（播放中或暂停）。"""
        return self._playing

    @property
    def is_paused(self):
        return self._paused

    @property
    def current_path(self):
        return self._path

    def duration_ms(self):
        return self._duration_ms

    def set_volume(self, value):
        self._volume = max(0.0, min(1.0, float(value)))

    def supports(self, path):
        if self.supported_exts is None:  # ffmpeg 后端支持一切
            return True
        return os.path.splitext(path)[1].lower() in self.supported_exts


# ---------------------------------------------------------------------------
# 后端 1：ffmpeg + sounddevice（格式支持最广）
# ---------------------------------------------------------------------------
class FfmpegEngine(AudioEngine):
    """ffmpeg 解码为 s16le PCM 管道，sounddevice 实时播放。

    解码参数固定为 44100Hz / 双声道 / 16bit；seek 通过 -ss 重启解码进程实现。
    """

    SAMPLE_RATE = 44100
    CHANNELS = 2
    FRAME_BYTES = 2 * 2  # 16bit * 2ch

    def __init__(self, ffmpeg_path, parent=None):
        super().__init__(parent)
        self._ff = ffmpeg_path
        self._ffprobe = find_executable("ffprobe")
        self._proc = None
        self._stream = None
        self._thread = None
        self._stop_ev = None
        self._resume_ev = None
        self._played_bytes = 0
        self._lock = threading.Lock()
        self._manual_stop = False

    @property
    def backend_name(self):
        return "ffmpeg + sounddevice"

    @property
    def supported_exts(self):
        return None  # 任何 ffmpeg 可解码的格式

    def _probe_duration(self, path):
        if self._ffprobe:
            try:
                r = subprocess.run(
                    [self._ffprobe, "-v", "error", "-show_entries",
                     "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", path],
                    capture_output=True, timeout=20,
                    creationflags=_win_nowindow(),
                )
                # 注意：不能用 text=True 解码 stdout/stderr。ffmpeg/ffprobe 对含
                # 中文 LYRICS 等元数据的文件会在 stderr/stdout 输出 UTF-8 中文，
                # text=True 用 Windows locale(GBK) 解码会抛异常，导致时长探测
                # 静默失败返回 0（进度条总时长显示 0:00）。统一 bytes 读取后
                # 显式 utf-8 解码并容错。
                line = (r.stdout or b"").decode("utf-8", "replace").strip().splitlines()
                sec = float(line[0]) if line else 0.0
                if sec > 0:
                    return int(sec * 1000)
            except Exception:
                pass
        try:  # 回退：解析 ffmpeg -i 的 Duration 行（bytes 解码，规避中文元数据编码问题）
            r = subprocess.run(
                [self._ff, "-hide_banner", "-i", path],
                capture_output=True, timeout=20,
                creationflags=_win_nowindow(),
            )
            err = (r.stderr or b"").decode("utf-8", "replace")
            m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err)
            if m:
                h, mi, s = m.groups()
                return int((int(h) * 3600 + int(mi) * 60 + float(s)) * 1000)
        except Exception:
            pass
        return 0

    def _probe_stream_info(self, path):
        """探测音频文件的实际声道数与采样率（播放引擎跟随文件配置）。

        解析 ffmpeg -i 输出的 Stream 行（如 "Audio: flac, 44100 Hz, stereo,
        s16"）；统一 bytes 读取 + utf-8 容错解码，规避内嵌中文 LYRICS 元
        数据的编码问题。返回 (channels, samplerate)；探测失败返回 (None, None)。
        """
        try:
            r = subprocess.run(
                [self._ff, "-hide_banner", "-i", path],
                capture_output=True, timeout=20,
                creationflags=_win_nowindow(),
            )
            err = (r.stderr or b"").decode("utf-8", "replace")
            m = re.search(
                r"Audio:\s*[^,]+,\s*(\d+)\s*Hz,\s*([a-z0-9.]+)", err)
            if not m:
                return None, None
            sr_file = int(m.group(1))
            ch_desc = m.group(2).lower()
            ch_map = {
                "mono": 1, "stereo": 2, "2.1": 3, "3.0": 3, "3.0(back)": 3,
                "quad": 4, "4.0": 4, "5.0": 5, "5.0(side)": 5, "5.1": 6,
                "6.1": 7, "7.1": 8,
            }
            ch_file = ch_map.get(ch_desc)
            if ch_file is None:
                m2 = re.search(r"(\d+)\s*channels", err)
                ch_file = int(m2.group(1)) if m2 else None
            return ch_file, sr_file
        except Exception:
            return None, None

    def open_file(self, path):
        self._manual_stop = True
        self._teardown()
        self._manual_stop = False
        self._path = path
        self._duration_ms = self._probe_duration(path)
        self.durationChanged.emit(self._duration_ms)

    def play_stream(self, from_ms=0):
        if not self._path:
            return
        self._restart(from_ms)
        self._playing = True
        self._paused = False

    def _pick_output_config(self, with_retry=False, file_ch=None, file_sr=None):
        """确定输出 (channels, samplerate)。

        优先跟随音频文件的实际声道数与采样率（用户要求：播放引擎根据
        播放的音乐调整输出配置），避免 ffmpeg 无谓重采样造成音质损失或
        异常听感；仅当设备不接受文件采样率时才回退设备默认
        (44100/48000、≤2 声道)。声道上限 2，多声道文件统一由 ffmpeg 下混。
        """
        try:
            sd = _sd
            dev = sd.default.device[1]
            info = sd.query_devices(dev) if dev not in (None, -1, "") else None
            max_ch = int(info["max_output_channels"]) if info else 0
            native = float(info["default_samplerate"]) if info else 0.0
        except Exception:
            max_ch, native = 0, 0.0
        dev_ch = max_ch if max_ch >= 1 else 2
        dev_sr = int(native) if int(native) in (44100, 48000) else 44100

        # 声道：优先文件实际声道数（上限 2，避免 WASAPI 多声道交错混乱）；
        # 采样率：优先文件实际采样率，越界或设备不接受时回退设备默认。
        ch = file_ch if file_ch and 1 <= file_ch <= 2 else dev_ch
        if ch > 2:
            ch = 2
        if with_retry:
            ch = 1
        sr = file_sr if file_sr and 8000 <= file_sr <= 192000 else dev_sr
        try:
            sd.check_output_settings(samplerate=sr, channels=ch, dtype="int16")
        except Exception:
            sr = dev_sr
            try:
                sd.check_output_settings(
                    samplerate=sr, channels=ch, dtype="int16")
            except Exception:
                sr = 44100
        return max(1, ch), sr

    def _restart(self, from_ms):
        self._manual_stop = True
        self._teardown()
        self._manual_stop = False
        if not _get_sd():
            # sounddevice 不可用（典型：打包版缺 numpy），降级提示而非崩溃
            self._playing = False
            self._paused = False
            self.playbackError.emit(
                "ffmpeg 后端所需 sounddevice 不可用（可能缺失 numpy），"
                "无法播放此文件。请安装 numpy/sounddevice，或改用 pygame / "
                "QtMultimedia 后端。")
            return
        sd = _get_sd()
        self._retried_cfg = False
        from_ms = max(0, int(from_ms))
        file_ch, file_sr = self._probe_stream_info(self._path)
        ch, sr = self._pick_output_config(file_ch=file_ch, file_sr=file_sr)
        with self._lock:
            self._played_bytes = int(from_ms / 1000 * sr * ch * 2)
        cmd = [
            self._ff, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-ss", "%.3f" % (from_ms / 1000.0),
            "-i", self._path, "-vn",
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", str(ch), "-ar", str(sr), "-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=_win_nowindow(),
            )
            self._stream = sd.OutputStream(
                samplerate=sr, channels=ch, dtype="int16")
            self._stream.start()
        except Exception as exc:  # 打开输出设备失败：转中文友好错误，不抛未捕获异常
            self._playing = False
            self._paused = False
            self.playbackError.emit(
                "无法播放该音频（声道/格式不支持）：%s" % (exc,))
            return
        self._ch, self._sr = ch, sr
        self._stop_ev = threading.Event()
        self._resume_ev = threading.Event()
        self._resume_ev.set()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _write_chunk(self, raw):
        """向输出设备写一块 PCM；write 失败(声道/采样率不匹配)时
        用最保守配置(单声道 44100/48000)重建一次再试。

        sounddevice 新版只接受 numpy 数组（bytes 会报 dtype mismatch），
        统一先转为 int16 数组再写。
        """
        npmod = _get_np()
        if npmod is None:
            raise RuntimeError("numpy 不可用，无法播放该音频")
        arr = npmod.frombuffer(raw, dtype=npmod.int16)
        # 关键修复：sounddevice 新版要求写入数组 shape 与流声道数严格匹配。
        # 之前直接传 1D 数组到多声道流会抛 "number of channels must match"，
        # 触发下方降级重建逻辑：ffmpeg 仍输出 N 声道 PCM，却按单声道流播放，
        # 导致播放速度减半、声道交错错乱（声音变慢变怪）。这里显式 reshape。
        ch = getattr(self, "_ch", self.CHANNELS)
        if arr.size % ch == 0:
            arr = arr.reshape(-1, ch)
        try:
            self._stream.write(arr)
            return
        except Exception:
            pass
        if self._retried_cfg:
            raise RuntimeError(
                "无法播放该音频（声道/采样率与输出设备不匹配）")
        self._retried_cfg = True
        stream = self._stream
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
        last_err = None
        for ch, sr in ((1, 44100), (1, 48000)):
            try:
                self._stream = _get_sd().OutputStream(
                    samplerate=sr, channels=ch, dtype="int16")
                self._stream.start()
                # 降级流为单声道，直接写 1D 数组即可（sounddevice 接受 1D 写 1ch）；
                # 不可再 reshape，否则 2 声道交错数据会被当作单声道连续帧，速率翻倍。
                self._stream.write(
                    npmod.frombuffer(raw, dtype=npmod.int16))
                # 降级成功：同步更新解码/进度换算使用的声道数与采样率，
                # 否则 _read_loop / position_ms 仍按旧 ch/sr 计算而错乱。
                self._ch, self._sr = ch, sr
                return
            except Exception as e:
                last_err = e
        self._stream = stream
        raise RuntimeError(
            "无法播放该音频（声道/格式不支持）：%s" % (last_err,))

    def _read_loop(self):
        proc = self._proc
        stream = self._stream
        ch, sr = self._ch, self._sr
        frame_bytes = ch * 2
        read_size = sr * frame_bytes // 4  # 每 0.25s 一块
        try:
            while not self._stop_ev.is_set():
                self._resume_ev.wait()
                if self._stop_ev.is_set():
                    break
                chunk = proc.stdout.read(read_size)
                if not chunk:
                    break
                raw = chunk
                npmod = _get_np()
                if self._volume < 1.0 and npmod is not None:
                    arr = npmod.frombuffer(chunk, dtype=npmod.int16).astype(npmod.float32)
                    arr *= self._volume
                    npmod.clip(arr, -32768, 32767, out=arr)
                    raw = arr.astype(npmod.int16).tobytes()
                self._write_chunk(raw)
                with self._lock:
                    self._played_bytes += len(raw)
        except Exception as exc:  # noqa: BLE001
            self.playbackError.emit(str(exc))
        finally:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            for _call in (stream.stop, stream.close):
                try:
                    _call()
                except Exception:
                    pass
            if not self._manual_stop:
                self._playing = False
                self._paused = False
                self.playbackFinished.emit()

    def pause_stream(self):
        if not (self._playing and not self._paused):
            return
        self._paused = True
        if self._resume_ev is not None:
            self._resume_ev.clear()

    def resume_stream(self):
        if not self._paused:
            return
        self._paused = False
        if self._resume_ev is not None:
            self._resume_ev.set()

    def stop_stream(self):
        self._manual_stop = True
        self._teardown()
        self._playing = False
        self._paused = False

    def seek_to(self, ms):
        if self._path:
            self._restart(ms)
            self._playing = True
            self._paused = False

    def position_ms(self):
        with self._lock:
            b = self._played_bytes
        ch = getattr(self, "_ch", self.CHANNELS)
        sr = getattr(self, "_sr", self.SAMPLE_RATE)
        return int(b / (ch * 2) / sr * 1000)

    def _teardown(self):
        if self._stop_ev is not None:
            self._stop_ev.set()
        if self._resume_ev is not None:
            self._resume_ev.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    self._proc.terminate()
            except Exception:
                pass
            self._proc = None
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._stop_ev = None
        self._resume_ev = None


# ---------------------------------------------------------------------------
# 后端 2：pygame（无 ffmpeg 时的优雅降级）
# ---------------------------------------------------------------------------
class PygameEngine(AudioEngine):
    def __init__(self, parent=None):
        super().__init__(parent)
        import pygame
        self._pygame = pygame
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=44100)
        except Exception:
            pass
        self._offset_ms = 0.0
        self._start_mono = None
        self._played_since = time.monotonic()

    @property
    def backend_name(self):
        return "pygame"

    @property
    def supported_exts(self):
        return _PYGAME_EXTS

    def _probe_duration(self, path):
        py = self._pygame
        try:
            snd = py.mixer.Sound(path)
            d = int(snd.get_length() * 1000)
            del snd
            return d
        except Exception:
            return 0

    def open_file(self, path):
        self.stop_stream()
        self._path = path
        self._duration_ms = self._probe_duration(path)
        self.durationChanged.emit(self._duration_ms)

    def play_stream(self, from_ms=0):
        if not self._path:
            return
        py = self._pygame
        try:
            py.mixer.music.load(self._path)
        except Exception as exc:  # noqa: BLE001
            self.playbackError.emit("pygame 后端无法解码该文件：%s" % exc)
            self._playing = False
            self._paused = False
            return
        py.mixer.music.play(start=max(0, from_ms) / 1000.0)
        self._offset_ms = float(from_ms)
        self._start_mono = time.monotonic() * 1000
        self._playing = True
        self._paused = False
        self._played_since = time.monotonic()

    def pause_stream(self):
        # 加固：即使 _playing 标记异常（如异步终止后未复位），也保存当前
        # 位置并进入暂停态，保证 pause->resume 不会因 offset 丢失而归零。
        # 注意：必须在标记 _paused 之前读取 position_ms()，否则会因暂停态
        # 直接返回旧 offset，导致实时播放位置丢失、恢复后从 0 断点。
        if self._paused:
            return
        self._offset_ms = self.position_ms()
        self._start_mono = None
        self._paused = True
        if getattr(self, "_pygame", None) is not None:
            try:
                self._pygame.mixer.music.pause()
            except Exception:
                pass

    def resume_stream(self):
        if not self._paused:
            return
        self._paused = False
        self._start_mono = time.monotonic() * 1000
        try:
            self._pygame.mixer.music.unpause()
        except Exception:
            pass

    def stop_stream(self):
        try:
            self._pygame.mixer.music.stop()
        except Exception:
            pass
        self._playing = False
        self._paused = False

    def seek_to(self, ms):
        if self._path:
            self.play_stream(max(0, int(ms)))

    def position_ms(self):
        if self._paused or self._start_mono is None:
            return int(self._offset_ms)
        if self._playing:
            return int(self._offset_ms + (time.monotonic() * 1000 - self._start_mono))
        return int(self._offset_ms or 0)

    def set_volume(self, value):
        super().set_volume(value)
        try:
            self._pygame.mixer.music.set_volume(value)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 后端 3：QtMultimedia（PySide6 自带，最后兜底；同时负责视频播放）
# ---------------------------------------------------------------------------
class QtEngine(AudioEngine):
    """QtMultimedia 引擎：音频兜底后端 + 视频播放引擎。

    视频文件（VIDEO_EXTENSIONS）由本引擎通过 QMediaPlayer 播放：
    · 调用 set_video_output(QVideoWidget) 绑定画面渲染控件；
    · 播放视频时 QMediaPlayer 同步输出音轨与画面，进度/暂停/拖动与音频一致。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        QMP, QAO = _get_qtm()
        if QMP is None:
            raise RuntimeError("QtMultimedia 不可用")
        self._audio_out = QAO(self)
        self._audio_out.setVolume(self._volume)
        self._player = QMP(self)
        self._player.setAudioOutput(self._audio_out)
        self._manual_stop = False
        self._video_output = None
        self._is_video = False
        self._player.positionChanged.connect(self.positionChanged.emit)
        self._player.durationChanged.connect(self._on_duration)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(self._on_error)
        from PySide6.QtCore import QUrl
        self._qurl = QUrl

    @property
    def backend_name(self):
        return "QtMultimedia"

    @property
    def supported_exts(self):
        return _QT_EXTS

    def set_video_output(self, widget):
        """绑定 QVideoWidget：播放视频时把画面渲染到该控件（None 取消绑定）。"""
        self._video_output = widget
        if widget is not None:
            self._player.setVideoOutput(widget)

    def is_video_file(self):
        return self._is_video

    def _on_duration(self, ms):
        self._duration_ms = ms
        self.durationChanged.emit(ms)

    def _on_error(self, err, msg):
        if err in (getattr(_get_qtm()[0], "Error.ResourceError", None),
                   getattr(_get_qtm()[0], "Error.FormatError", None),
                   getattr(_get_qtm()[0], "Error.AccessDeniedError", None),
                   getattr(_get_qtm()[0], "Error.NetworkError", None)):
            self._playing = False
            self._paused = False
            self.playbackError.emit("无法播放该媒体文件（%s）：%s"
                                    % (self._is_video and "视频" or "音频", msg))

    def _on_status(self, st):
        QMP = _get_qtm()[0]
        if QMP is not None and st == QMP.MediaStatus.EndOfMedia \
                and not self._manual_stop:
            self._playing = False
            self._paused = False
            self.playbackFinished.emit()

    def open_file(self, path):
        self._manual_stop = True
        self._player.stop()
        self._manual_stop = False
        self._path = path
        self._is_video = os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS
        self._player.setSource(self._qurl.fromLocalFile(path))

    def play_stream(self, from_ms=0):
        if not self._path:
            return
        self._player.setPosition(max(0, int(from_ms)))
        self._player.play()
        self._playing = True
        self._paused = False

    def pause_stream(self):
        self._player.pause()
        self._paused = True

    def resume_stream(self):
        self._player.play()
        self._paused = False

    def stop_stream(self):
        self._manual_stop = True
        self._player.stop()
        self._manual_stop = False
        self._playing = False
        self._paused = False

    def seek_to(self, ms):
        self._player.setPosition(max(0, int(ms)))

    def position_ms(self):
        return int(self._player.position())

    def set_volume(self, value):
        super().set_volume(value)
        self._audio_out.setVolume(value)


# ---------------------------------------------------------------------------
# 引擎工厂：自动选择第一个可用后端
# ---------------------------------------------------------------------------
def create_engine(parent=None):
    """
    返回 (engine, backend_label)：
      1. ffmpeg 存在且 sounddevice 可导入 -> FfmpegEngine（最全格式）
      2. pygame 可导入 -> PygameEngine
      3. QtMultimedia 可用 -> QtEngine
      4. 无任何后端 -> (None, "")
    """
    ff = find_executable("ffmpeg")
    sd_ok = _get_sd() is not None
    if ff and sd_ok:
        try:
            eng = FfmpegEngine(ff, parent)
            return eng, "ffmpeg + sounddevice"
        except Exception:
            pass
    try:
        import pygame  # noqa: F401
        return PygameEngine(parent), "pygame"
    except Exception:
        pass
    if _get_qtm()[0] is not None:
        try:
            return QtEngine(parent), "QtMultimedia"
        except Exception:
            pass
    return None, ""

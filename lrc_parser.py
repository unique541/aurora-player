# -*- coding: utf-8 -*-
"""
lrc_parser.py —— 歌词解析与加载（.lrc 文件 + 音频文件内嵌歌词）
=====================================================
支持标签格式：[mm:ss.xxx]，每行允许携带多个时间戳；自动排序；
自动在音频同目录查找同名 .lrc 文件。
新增：自动读取音频文件内嵌歌词（音乐与歌词一体场景）：
  * MP3/MP2        -> ID3 USLT（及 TXXX:LYRICS 兜底）
  * FLAC/OGG/OPUS  -> LYRICS / lyrics / UNSYNCEDLYRICS / SYNCEDLYRICS 等 Vorbis 注释
  * M4A/MP4        -> iTunes 歌词字段 \xa9lyr
内嵌歌词若含 LRC 时间戳则解析为时间轴歌词（可滚动），否则作为纯文本静态展示。
"""
import os
import re

_LRC_TAG = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]")


def _frac_ms(fragment):
    """把 1~3 位的毫秒碎片标准化为毫秒。"""
    if not fragment:
        return 0
    if len(fragment) == 1:
        return int(fragment) * 100
    if len(fragment) == 2:
        return int(fragment) * 10
    return int(fragment)


def parse_lrc(text):
    """
    解析 LRC 文本，返回按时间升序的 [(time_ms, content), ...] 列表。
    形如 [ti:...] / [ar:...] 的元信息行会被过滤。
    """
    lines = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        tags = _LRC_TAG.findall(raw)
        if not tags:
            continue
        content = _LRC_TAG.sub("", raw).strip()
        for minute, second, frac in tags:
            t = int(minute) * 60000 + int(second) * 1000 + _frac_ms(frac)
            lines.append((t, content))
    lines.sort(key=lambda x: x[0])
    return lines


def find_lrc_file(audio_path):
    """在音频同目录查找同名 .lrc 文件，找不到返回 None。"""
    base = os.path.splitext(audio_path)[0]
    for ext in (".lrc", ".LRC"):
        p = base + ext
        if os.path.isfile(p):
            return p
    return None


def load_lyrics(audio_path):
    """
    为音频文件加载本地同名 .lrc 歌词。
    返回 (lrc_file_path_or_None, parsed_lines_or_empty_list)。
    仅查本地同名 .lrc；未找到返回 (None, [])。
    """
    p = find_lrc_file(audio_path)
    if not p:
        return None, []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return p, []
    return p, parse_lrc(text)


def _first_nonempty(tags, keys):
    """从标签对象中按 keys 依次取首个非空字段，多值用换行拼接成一个字符串。"""
    for key in keys:
        try:
            vals = tags.get(key) or []
        except Exception:
            continue
        if isinstance(vals, str):
            vals = [vals]
        parts = []
        for v in vals:
            if v is None:
                continue
            s = str(v).rstrip("\x00").strip()
            if s:
                parts.append(s)
        if parts:
            return "\n".join(parts)
    return None


def load_embedded_lyrics(audio_path):
    """
    用 mutagen 读取音频文件内嵌歌词。

    返回 (desc, lines, timed)：
      desc : 描述字符串（如 "内嵌歌词 · USLT"）；无内嵌歌词时返回 None
      lines: timed=True  -> [(ms, text), ...] 按时间升序，供歌词面板滚动
             timed=False -> [text, ...] 纯文本行，静态展示
    """
    try:
        from mutagen import File
    except Exception:
        return None, [], True

    try:
        meta = File(audio_path, easy=False)
    except Exception:
        meta = None
    if meta is None or getattr(meta, "tags", None) is None:
        return None, [], True

    low = os.path.splitext(audio_path)[1].lower()
    text, desc = None, None

    try:
        if low in (".mp3", ".mp2"):
            # ID3v2：优先 USLT 帧（含语言字段），取其首个非空文本
            try:
                frames = [f for f in meta.tags.getall("USLT")
                          if getattr(f, "text", None)]
            except Exception:
                frames = []
            if frames:
                # 排序：默认语言（XXX 靠后）、非空描述靠前，最终取最优帧
                frames.sort(
                    key=lambda f: (
                        getattr(f, "lang", "") == "XXX",
                        not bool(getattr(f, "desc", "")),
                        getattr(f, "desc", "") == ""),
                    reverse=False)
                try:
                    text = (frames[0].text or "").rstrip("\x00").strip()
                    desc = "内嵌歌词 · USLT"
                except Exception:
                    text = None
            if not text:
                text = _first_nonempty(
                    meta.tags, ("TXXX:LYRICS", "TXXX:lyrics", "USLT", "SLT"))
                if text:
                    desc = "内嵌歌词 · ID3"

        elif low in (".flac", ".ogg", ".oga", ".opus", ".wav",
                     ".mpc", ".ape", ".wv", ".aiff", ".aif"):
            # Vorbis 注释 / FLAC：常见字段
            text = _first_nonempty(
                meta.tags, ("lyrics", "LYRICS", "unsyncedlyrics",
                            "UNSYNCEDLYRICS", "syncedlyrics",
                            "SYNCEDLYRICS", "Lyrics"))
            if text:
                desc = "内嵌歌词 · LYRICS"

        elif low in (".m4a", ".mp4", ".m4b", ".m4v"):
            text = _first_nonempty(meta.tags, ("\xa9lyr", "\xa9ly"))
            if text:
                desc = "内嵌歌词 · \xa9lyr"

        else:
            # 其它格式：尽力尝试常见键
            text = _first_nonempty(
                meta.tags, ("lyrics", "LYRICS", "\xa9lyr",
                            "unsyncedlyrics", "SYNCEDLYRICS"))
            if text:
                desc = "内嵌歌词"
    except Exception:
        text, desc = None, None

    if not text:
        return None, [], True

    # 含 LRC 时间戳 -> 时间轴歌词
    timed_lines = parse_lrc(text)
    if timed_lines:
        return desc or "内嵌歌词", timed_lines, True

    # 无时间戳 -> 纯文本静态（过滤 [xx:xx] 元信息行）
    plain = [ln.strip() for ln in text.splitlines() if ln.strip()]
    plain = [ln for ln in plain
             if not re.match(r"^\[[a-zA-Z\u4e00-\u9fff]+:", ln)]
    if not plain:
        plain = [text]
    return desc or "内嵌歌词", plain, False

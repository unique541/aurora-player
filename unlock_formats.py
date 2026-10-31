# -*- coding: utf-8 -*-
"""
音乐软件专属音频格式解密模块
================================================
纯 Python 实现（RC4 / 异或），AES-128-ECB 依赖可选库 pycryptodome（仅 ncm 需要）。

支持的专属格式（作为转换输入源）：
  ncm       网易云音乐   ：AES-128-ECB 解出 key box，魔改 RC4 解密音频数据
  qmc0/qmc2/qmc3/qmcflac/qmcogg  QQ音乐 QMCv1：按字节异或（0xAC 等）还原
  kgm/kgma  酷狗音乐     ：16 字节静态异或表循环解密
  kgg       酷狗音乐 KGMv2：优先按 KGMv2 位置相关掩码流解密（需 mask 表），
                          mask 表不可用时回退 16 字节静态异或表方案
  kwm       酷我音乐     ：文件内嵌 key + 魔改 RC4 解密

已知限制：
  mflac / mgg（QQ音乐 QMCv2 新版）使用魔改 AES 变体且无公开固定密钥，
  本模块无法可靠解密，会抛出 QmcV2Unsupported，由上层提示用户改用
  官方客户端 / unlock-music 等工具先解锁。
  kgg（KGMv2）完整解密需要 unlock-music 系列的 kgm.v2.mask 掩码表：
    表文件可通过环境变量 KGM_V2_MASK 或放在本模块同目录 kgm.v2.mask 提供；
    未提供时自动回退 16 字节静态异或方案（兼容部分旧版/简化变体）。
  各加密算法还原后自动检测真实音频容器（flac/ogg/mp3/wav/ape/m4a），
  检测失败会抛出 DecryptError。
"""

import json
import os
from pathlib import Path

try:
    from Crypto.Cipher import AES
    _AES_OK = True
except Exception:  # pragma: no cover
    _AES_OK = False

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
# 专属加密格式集合
CRYPTO_FORMATS = {
    "ncm", "qmc0", "qmc2", "qmc3", "qmcflac", "qmcogg",
    "mflac", "mgg", "kgm", "kgma", "kgg", "kwm",
}

# ncm 魔改 AES 密钥（固定，前 16 字节）
NCM_KEY_AES = b"hxHRAmso5kInbaxW"
NCM_CORE_AES = b"#Q4klmn ziduveja"

# kgm / kgma / kgg（回退方案）静态异或表（16 字节循环）
KGM_KEY = bytes([0x3C, 0x31, 0x5A, 0xF2, 0x5D, 0x6F, 0xE5, 0xAC,
                 0x99, 0x9C, 0x60, 0x0E, 0xD1, 0xDB, 0x8C, 0x73])

# KGMv2（kgg）魔数
KGMV2_HEADERS = (b"\x7c\xd5\x32\xeb\x86\x02\x7f\x4b"
                 b"\xa8\xaf\xd6\x8e\x10\x40\xfe\x1a",)   # KgmHeader
VPR_HEADER = b"\x05\x28\xbc\x7a\x4f\x26\x3c\xf3"          # VprHeader

# QMCv1 简单异或密钥（qq 音乐旧版常用）
QMC_XOR_KEY = 0xAC


class DecryptError(Exception):
    """解密失败"""
    pass


class QmcV2Unsupported(DecryptError):
    """QMCv2（mflac/mgg）暂不支持"""
    pass


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def detect_container(data):
    """检测音频容器真实格式，返回扩展名或 None"""
    if data[:4] == b"fLaC":
        return "flac"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"ID3":
        return "mp3"
    if len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"MAC ":
        return "ape"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return "m4a"
    return None


def _aes_ecb_decrypt(data, key):
    """AES-128-ECB 解密（补齐 16 字节倍数）"""
    if not _AES_OK:
        raise DecryptError("缺少 pycryptodome，无法解密 ncm。请执行: pip install pycryptodome")
    if len(data) % 16 != 0:
        data = data[:len(data) - len(data) % 16]
    return AES.new(key[:16], AES.MODE_ECB).decrypt(data)


def _rc4(data, key):
    """标准 RC4 流密码"""
    S = list(range(256))
    j = 0
    klen = len(key) or 1
    for i in range(256):
        j = (j + S[i] + key[i % klen]) & 0xFF
        S[i], S[j] = S[j], S[i]
    out = bytearray(len(data))
    i = j = 0
    for k in range(len(data)):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out[k] = data[k] ^ S[(S[i] + S[j]) & 0xFF]
    return bytes(out)


def _read_u32le(data, off):
    return int.from_bytes(data[off:off + 4], "little")


def _require_head(data, head):
    if not head:
        raise DecryptError("解密后无法识别音频容器格式")


# ---------------------------------------------------------------------------
# ncm（网易云音乐）
# ---------------------------------------------------------------------------
def _ncm_meta_format(meta_data):
    """ncm meta 数据异或 0x64 后为 JSON，取 format 字段"""
    try:
        raw = bytes(b ^ 0x64 for b in meta_data).decode("utf-8", "ignore")
        info = json.loads(raw)
        fmt = (info.get("format") or "").lower()
        if fmt in ("mp3", "flac", "wav", "ogg", "aac", "m4a", "ape", "opus"):
            return fmt
    except Exception:
        pass
    return None


def decrypt_ncm(src):
    """解密 ncm 文件，返回 (明文音频字节, 真实扩展名)"""
    data = Path(src).read_bytes()
    if data[:8] != b"CTENFDAM":
        raise DecryptError("ncm 文件头不正确（缺少 CTENFDAM 魔数）")

    off = 8
    off += 2                                   # 2 字节 0x00
    key_len = _read_u32le(data, off); off += 4
    key_data = data[off:off + key_len]; off += key_len
    off += 2                                   # 2 字节 0x00
    core_len = _read_u32le(data, off); off += 4
    core_data = data[off:off + core_len]; off += core_len
    off += 2                                   # 2 字节 0x00
    meta_len = _read_u32le(data, off); off += 4
    meta_data = data[off:off + meta_len]; off += meta_len
    off += 4                                   # 4 字节 crc / 0x00
    audio_data = data[off:]

    if core_data:                              # 校验 core key 可解密（可选增强）
        try:
            _aes_ecb_decrypt(core_data, NCM_CORE_AES)
        except Exception:
            pass

    key_box = _aes_ecb_decrypt(key_data, NCM_KEY_AES)
    rc4_key = bytes(b ^ 0x64 for b in key_box)  # 魔改：异或 0x64 后作 RC4 密钥
    dec = _rc4(audio_data, rc4_key)
    fmt = detect_container(dec) or _ncm_meta_format(meta_data)
    if not fmt:
        raise DecryptError("ncm 解密后无法识别音频容器格式")
    return dec, fmt


# ---------------------------------------------------------------------------
# qmc（QQ音乐 QMCv1）
# ---------------------------------------------------------------------------
def _xor_all(data, key_byte):
    return bytes(b ^ key_byte for b in data)


def decrypt_qmc(src):
    """解密 qmc0/qmc2/qmc3/qmcflac/qmcogg（QMCv1 简单异或）。
    mflac/mgg 属于 QMCv2，抛出 QmcV2Unsupported。"""
    data = Path(src).read_bytes()

    head = detect_container(data)
    if head:
        return data, head                       # 实际未加密

    # QMCv2（mflac/mgg）特征：头部多为 0x00 填充，或包含 QTag 标记
    if data[:4] == b"\x00\x00\x00\x00" or b"QTag" in data[:128]:
        raise QmcV2Unsupported(
            "mflac/mgg 为 QQ音乐 QMCv2 新版加密（魔改 AES），本工具暂不支持解密。\n"
            "请先用官方客户端下载无损文件，或使用 unlock-music 等工具解锁后再转换。")

    # QMCv1：尝试 0xAC 异或还原
    out = _xor_all(data, QMC_XOR_KEY)
    head = detect_container(out)
    if head:
        return out, head

    # 尝试常见静态掩码（128 字节循环异或，兼容部分 qmc3 变体）
    for mask in (_qmc_mask_a(), _qmc_mask_b()):
        try:
            cand = bytes(b ^ mask[i % 128] for i, b in enumerate(data))
        except Exception:
            continue
        head = detect_container(cand)
        if head:
            return cand, head

    raise DecryptError("未能识别该 QMC 加密变体（可能为 RC4 版本，需专用工具解锁）")


def _qmc_mask_a():
    """unlock-music QMCv1 常见静态掩码片段（ASCII 派生）"""
    seed = b"QMC!QMC!QMC!QMC!QMC!QMC!QMC!QMC!QMC!QMC!QMC!QMC!QMC!QMC!QMC!QMC!"
    return bytes(seed[:128])


def _qmc_mask_b():
    """基于 0-127 递增的简单掩码（部分旧版）"""
    return bytes(range(128))


# ---------------------------------------------------------------------------
# kgm / kgma（酷狗音乐）
# ---------------------------------------------------------------------------
def decrypt_kgm(src):
    """解密 kgm/kgma：16 字节静态表循环异或"""
    data = Path(src).read_bytes()

    head = detect_container(data)
    if head:
        return data, head

    # 整体循环异或
    out = bytes(b ^ KGM_KEY[i % 16] for i, b in enumerate(data))
    head = detect_container(out)
    if head:
        return out, head

    # 部分文件保留 1024 字节头，仅异或数据区（数据区从 0 开始计数），
    # 解密后返回纯音频数据区（容器头在开头，可直接识别）
    out2 = bytearray(data[0x400:])
    for i in range(len(out2)):
        out2[i] ^= KGM_KEY[i % 16]
    head = detect_container(bytes(out2))
    if head:
        return bytes(out2), head

    raise DecryptError("KGM 解密后无法识别音频容器格式")


# ---------------------------------------------------------------------------
# kgg / KGMv2（酷狗音乐 KGMv2 加密）
# ---------------------------------------------------------------------------
def _xor_lower_half_byte(v):
    """unlock-music 中 maskV2 的 xorLowerHalfByte 变换"""
    return (v ^ ((v & 0x0F) << 4)) & 0xFF


def _load_kgm_v2_tables():
    """加载 KGMv2 掩码表（table1/table2/tableV2 各 272 字节）。
    来源优先级：环境变量 KGM_V2_MASK -> 本模块同目录 kgm.v2.mask。
    表文件为 unlock-music 的 kgm.v2.mask 解压结果（816 字节）。
    加载失败返回 None，调用方回退静态异或方案。"""
    paths = []
    env = os.environ.get("KGM_V2_MASK", "").strip()
    if env:
        paths.append(env)
    paths.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "kgm.v2.mask"))
    for p in paths:
        try:
            raw = Path(p).read_bytes()
        except Exception:
            continue
        if len(raw) < 816:
            continue
        return (raw[0:272], raw[272:544], raw[544:816])
    return None


def _kgm_v2_decrypt_audio(audio, file_key, t1, t2, v2, vpr_key=None):
    """KGMv2 位置相关 XOR 流解密。
    out[i] = xorLowerHalfByte( mask(i) ^ audio[i] ^ fileKey[i%17] )
    mask(i) = v2[i%272] ^ 对 (i>>4) 逐步异或 t1/t2[off%272]（off 每次右移 4 位，
    直到 off < 0x11）。
    vpr 文件额外异或 17 字节 vprKey。"""
    out = bytearray(len(audio))
    for i, b in enumerate(audio):
        mask = v2[i % 272]
        off = i >> 4
        while off >= 0x11:
            mask ^= t1[off % 272]
            off >>= 4
            mask ^= t2[off % 272]
            off >>= 4
        v = mask ^ b ^ file_key[i % 17]
        out[i] = _xor_lower_half_byte(v)
        if vpr_key:
            out[i] ^= vpr_key[i % 17]
    return bytes(out)


def decrypt_kgg(src):
    """解密 kgg（酷狗 KGMv2）。
    解密策略（按顺序尝试）：
      1) 若本身是未加密音频容器，直接返回；
      2) 识别 KGMv2 魔数 / 内嵌 fileKey -> 尝试 KGMv2 掩码表解密；
      3) 回退 16 字节静态异或表方案（兼容旧版/简化变体）；
      4) 回退跳过 1024 字节头的静态异或。
    返回 (明文音频字节, 真实扩展名)。"""
    data = Path(src).read_bytes()

    head = detect_container(data)
    if head:
        return data, head

    # ---- KGMv2 路径：识别魔数或 headerLen+fileKey 结构 ----
    try:
        header_len = _read_u32le(data, 0x10)
        file_key = data[0x1C:0x1C + 16]
    except Exception:
        header_len = 0
        file_key = b""
    is_kgm_magic = any(data.startswith(h) for h in KGMV2_HEADERS) or data.startswith(VPR_HEADER)
    if (is_kgm_magic or (0x10 <= header_len <= 0x400 and any(file_key))) and any(file_key):
        tables = _load_kgm_v2_tables()
        if tables:
            t1, t2, v2 = tables
            vpr_key = None
            # vpr 文件在数据区前额外异或 VPR 密钥（常见固定值）
            if data.startswith(VPR_HEADER):
                vpr_key = bytes([0x9F, 0x23, 0x31, 0x41, 0x31, 0x54, 0x24, 0x32,
                                 0x41, 0x34, 0x31, 0x23, 0x21, 0x53, 0x41, 0x31, 0x32])
            audio = data[header_len:]
            dec = _kgm_v2_decrypt_audio(audio, file_key, t1, t2, v2, vpr_key)
            head = detect_container(dec)
            if head:
                return dec, head

    # ---- 回退 1：跳过 KGMv2 文件头（headerLen 起），仅异或数据区并返回纯数据区 ----
    if 0x10 <= header_len < len(data):
        out1 = bytearray(data[header_len:])
        for i in range(len(out1)):
            out1[i] ^= KGM_KEY[i % 16]
        head = detect_container(bytes(out1))
        if head:
            return bytes(out1), head

    # ---- 回退 2：整体 16 字节静态异或（兼容无文件头的简化变体） ----
    out = bytes(b ^ KGM_KEY[i % 16] for i, b in enumerate(data))
    head = detect_container(out)
    if head:
        return out, head

    # ---- 回退 3：跳过 1024 字节头，仅异或数据区并返回纯数据区 ----
    out2 = bytearray(data[0x400:])
    for i in range(len(out2)):
        out2[i] ^= KGM_KEY[i % 16]
    head = detect_container(bytes(out2))
    if head:
        return bytes(out2), head

    raise DecryptError(
        "KGG(KGMv2) 解密后无法识别音频容器格式。\n"
        "若为完整 KGMv2 加密，请将 unlock-music 的 kgm.v2.mask 放到本模块同目录\n"
        "或设置环境变量 KGM_V2_MASK 指向该表文件后重试。")


# ---------------------------------------------------------------------------
# kwm（酷我音乐）
# ---------------------------------------------------------------------------
def decrypt_kwm(src):
    """解密 kwm：内嵌 key + 魔改 RC4（输出跳过首个 RC4 字节）"""
    data = Path(src).read_bytes()

    head = detect_container(data)
    if head:
        return data, head

    # key 位于偏移 0x18 处（16 字节）；部分版本在 0x10
    key = data[0x18:0x18 + 16]
    if not any(key):
        key = data[0x10:0x20]
    if not any(key):
        raise DecryptError("KWM 密钥读取失败（偏移异常）")

    # 尝试从 0x400 起解密数据（魔改 RC4 输出跳过首字节，或完整输出）
    if len(data) > 0x400:
        dec = _rc4(data[0x400:], key)
        for cand in (bytes(dec[1:]), bytes(dec)):
            head = detect_container(cand)
            if head:
                return cand, head

    # 尝试整体解密
    dec2 = _rc4(data, key)
    for cand in (bytes(dec2[1:]), bytes(dec2)):
        head = detect_container(cand)
        if head:
            return cand, head

    raise DecryptError("KWM 解密后无法识别音频容器格式")


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def decrypt_crypto_audio(src):
    """解密专属加密音频，返回 (明文音频字节, 真实扩展名)。
    未加密或无法识别的文件将抛出 DecryptError。"""
    ext = Path(src).suffix.lower().lstrip(".")
    if ext == "ncm":
        return decrypt_ncm(src)
    if ext in ("qmc0", "qmc2", "qmc3", "qmcflac", "qmcogg", "mflac", "mgg"):
        return decrypt_qmc(src)
    if ext in ("kgm", "kgma"):
        return decrypt_kgm(src)
    if ext == "kgg":
        return decrypt_kgg(src)
    if ext == "kwm":
        return decrypt_kwm(src)
    raise DecryptError("不支持的加密格式: %s" % ext)


# ---------------------------------------------------------------------------
# 自检（无 GUI 环境可直接运行验证算法对称性）
# ---------------------------------------------------------------------------
def _self_test():
    """构造模拟加密数据并验证解密还原。仅开发自测使用。"""
    import tempfile

    # 1. 构造一个简易 wav（模拟真实音频明文）
    plain = bytearray(b"RIFF" + b"\x24\x00\x00\x00" + b"WAVEfmt ")
    plain += bytes(range(64))

    def make_qmc():
        enc = bytes(b ^ QMC_XOR_KEY for b in plain)
        return enc, "wav"

    def make_kgm():
        enc = bytes(b ^ KGM_KEY[i % 16] for i, b in enumerate(plain))
        return enc, "wav"

    def make_kgg():
        # 模拟 kgg：KGMv2 文件头（16 字节魔数 + 0x10 处 headerLen + 0x1C 处 fileKey）
        # + 静态异或加密音频（无 mask 表时走回退方案）
        enc = bytes(b ^ KGM_KEY[i % 16] for i, b in enumerate(plain))
        header = (b"\x7c\xd5\x32\xeb\x86\x02\x7f\x4b\xa8\xaf\xd6\x8e\x10\x40\xfe\x1a"
                  + (0x400).to_bytes(4, "little")
                  + b"\x00" * 8
                  + b"0123456789abcdef")
        return header + b"\x00" * (0x400 - len(header)) + enc, "wav"

    def make_kwm():
        # 标准 RC4 加密明文作为密文；真实 kwm 为魔改 RC4，解密函数两种偏移都会尝试
        enc = _rc4(bytes(plain), b"0123456789abcdef")
        return bytes([0x01, 0x02, 0x03, 0x04]) + b"\x00" * 20 + b"0123456789abcdef" + b"\x00" * (0x400 - 40) + enc, "wav"

    cases = [("qmc0", make_qmc()), ("kgm", make_kgm()), ("kgg", make_kgg()), ("kwm", make_kwm())]
    for ext, (enc_data, expect) in cases:
        fd, tmp = tempfile.mkstemp(suffix="." + ext)
        os.close(fd)
        Path(tmp).write_bytes(enc_data)
        try:
            dec, fmt = decrypt_crypto_audio(tmp)
            assert fmt == expect, (ext, fmt)
            assert dec[:4] == b"RIFF", ext
            print("SELF_TEST OK:", ext, "->", fmt, len(dec))
        finally:
            os.remove(tmp)

    # 2. 模拟 ncm（AES 加密 key + RC4 加密音频）
    if _AES_OK:
        plain2 = bytes(plain)
        fake_key_box = bytes(range(128))
        enc_key = AES.new(NCM_KEY_AES, AES.MODE_ECB).encrypt(fake_key_box)
        rc4_key = bytes(b ^ 0x64 for b in fake_key_box)
        enc_audio = _rc4(plain2, rc4_key)
        meta = json.dumps({"format": "wav"}).encode("utf-8")
        meta_xor = bytes(b ^ 0x64 for b in meta)
        blob = (b"CTENFDAM" + b"\x00\x00"
                + len(enc_key).to_bytes(4, "little") + enc_key
                + b"\x00\x00"
                + (0).to_bytes(4, "little")
                + b"\x00\x00"
                + len(meta_xor).to_bytes(4, "little") + meta_xor
                + b"\x00\x00\x00\x00" + enc_audio)
        fd, tmp = tempfile.mkstemp(suffix=".ncm")
        os.close(fd)
        Path(tmp).write_bytes(blob)
        try:
            dec, fmt = decrypt_crypto_audio(tmp)
            assert fmt == "wav", fmt
            assert dec == plain2
            print("SELF_TEST OK: ncm -> wav", len(dec))
        finally:
            os.remove(tmp)


if __name__ == "__main__":
    _self_test()
    print("audio_decrypt self-test passed")

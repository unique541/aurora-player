# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[('build_res/ffmpeg.exe', '.'), ('C:/Users/1107/AppData/Local/Programs/Python/Python314/Lib/site-packages/PySide6/plugins/multimedia/*.dll', 'PySide6/plugins/multimedia')],
    datas=[('build_res/app_icon.ico', 'build_res')],
    hiddenimports=['numpy', 'sounddevice', 'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AuroraPlayer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['build_res/app_icon.ico'],
)

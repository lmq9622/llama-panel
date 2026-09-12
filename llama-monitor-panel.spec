# -*- mode: python ; coding: utf-8 -*-
# 单文件打包：把 panel.html 与 feeder.py 一并打包进 exe，
# 否则运行时 resource() 找不到文件，页面会返回 500。

import os

# llama 图标（assets/llama.ico）；嵌进 exe 之后，快捷方式直接用 exe 自带图标
ICON = os.path.join(SPECPATH, 'assets', 'llama.ico')

a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=[],
    datas=[('panel.html', '.'), ('feeder.py', '.')],
    hiddenimports=[],
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
    name='llama-monitor-panel',
    icon=ICON,
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
)

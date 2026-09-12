# -*- mode: python ; coding: utf-8 -*-
# 远端自动安装器：把 feeder.py 与 remote/llama-proxy-v2.py 一并打进 exe，
# 安装向导只需调用 deploy.exe 即可完成上传、写配置、重启服务。
# console=False 很关键：安装过程中不能弹出任何黑色控制台窗口。

import os

a = Analysis(
    ['deploy.py'],
    pathex=[],
    binaries=[],
    datas=[('feeder.py', '.'), (os.path.join('remote', 'llama-proxy-v2.py'), '.')],
    hiddenimports=['paramiko', 'bcrypt', 'cryptography', 'nacl'],
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
    name='deploy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    # True：万一真崩了也不弹模态错误框，否则安装向导会一直卡在进度页；
    # 崩溃原因一律看日志文件（默认写到系统临时目录的 llama-panel-deploy.log）。
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# -*- mode: python ; coding: utf-8 -*-
# 可移植 spec：所有依赖路径都相对本 spec 所在目录（即仓库根），
# 不再写死本机绝对路径，便于 GitHub Actions(Windows) 直接构建。

import os

ROOT = os.path.dirname(os.path.abspath(SPEC))
PTOOLS = os.path.join(ROOT, "tools", "platform-tools")

a = Analysis(
    [os.path.join(ROOT, "dtbo_tuner.py")],
    pathex=[],
    binaries=[
        (os.path.join(PTOOLS, "adb.exe"), "fastboot"),
        (os.path.join(PTOOLS, "AdbWinApi.dll"), "fastboot"),
        (os.path.join(PTOOLS, "AdbWinUsbApi.dll"), "fastboot"),
        (os.path.join(PTOOLS, "fastboot.exe"), "fastboot"),
        (os.path.join(PTOOLS, "libwinpthread-1.dll"), "fastboot"),
    ],
    datas=[],
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
    [],
    exclude_binaries=True,
    name='DTBO-Tuner',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DTBO-Tuner',
)

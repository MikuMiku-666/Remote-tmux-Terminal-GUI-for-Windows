# -*- mode: python ; coding: utf-8 -*-

base_client = 'base_client.py'

a = Analysis(
    ['client.py'],
    pathex=['.'],
    binaries=[],
    datas=[(base_client, 'windows_client')],
    hiddenimports=[
        'base64', 'ctypes', 'dataclasses', 'hashlib', 'json', 'queue',
        'socket', 'struct', 'subprocess', 'tempfile', 'threading',
        'tkinter', 'tkinter.messagebox', 'tkinter.simpledialog', 'tkinter.ttk',
        'traceback', 'urllib.error', 'urllib.parse', 'urllib.request',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='RemoteTmuxTerminal-v31',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)

# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

# Collect svgpathtools fully — it uses pkg_resources to find its own data files.
svg_datas, svg_binaries, svg_hiddenimports = collect_all('svgpathtools')

a = Analysis(
    ['launcher.py'],
    pathex=['src'],
    binaries=[
        ('libHeliosDacAPI.dylib', '.'),
        ('libusb-1.0.0.dylib', '.'),
    ] + svg_binaries,
    datas=[
        ('Bigfoot2.svg', '.'),
        ('aedyn_logo_BH.svg', '.'),
        ('death_star.svg', '.'),
        ('death_star_2.svg', '.'),
    ] + svg_datas,
    hiddenimports=svg_hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FALCON',
    debug=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch='arm64',
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='FALCON',
)

app = BUNDLE(
    coll,
    name='FALCON.app',
    icon=None,
    bundle_identifier='com.aedyn.falcon',
    info_plist={
        'NSHighResolutionCapable': True,
        'NSPrincipalClass': 'NSApplication',
        'CFBundleShortVersionString': '1.4.3',
        'CFBundleName': 'FALCON',
    },
)

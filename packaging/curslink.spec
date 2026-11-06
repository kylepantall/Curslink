# PyInstaller spec for the packaged Curslink app (see .github/workflows/build.yml).
#
# One folder, not one file: a one-file build unpacks itself on every launch,
# and the switch animation helper is a second launch of the same executable.
#
#   pip install ".[gaze]" pyinstaller
#   pyinstaller packaging/curslink.spec
#
# Output: dist/curslink/, run as dist/curslink/curslink[.exe].
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

# MediaPipe loads its graphs and native libraries from package data that
# static analysis can't see.
for package in ("mediapipe",):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h

# pynput picks its backend (win32, darwin, xorg) at runtime, and Curslink
# imports the gaze and input modules only when a config asks for them.
hiddenimports += collect_submodules("pynput")
hiddenimports += collect_submodules("curslink")

a = Analysis(
    [SPECPATH + "/../curslink/__main__.py"],
    pathex=[SPECPATH + "/.."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # Not matplotlib: MediaPipe imports it on load.
    excludes=["pytest"],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="curslink",
    console=True,
    # Arm or Intel, whichever the runner is.
    target_arch=None,
)

coll = COLLECT(exe, a.binaries, a.datas, name="curslink")


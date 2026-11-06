# PyInstaller spec for the packaged Curslink app (see .github/workflows/build.yml).
#
# One folder, not one file: a one-file build unpacks itself on every launch,
# and the switch animation helper is a second launch of the same executable.
#
#   pip install ".[gaze]" pyinstaller
#   pyinstaller packaging/curslink.spec
#
# Output: dist/curslink/, run as dist/curslink/curslink[.exe]. On Windows it
# also holds curslinkw.exe, the same program without a console window, which
# the Start menu and start-at-sign-in use to open the app.
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

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
# zeroconf (LAN discovery) is partly compiled and imported lazily.
hiddenimports += collect_submodules("zeroconf") + ["ifaddr"]
# macOS: pyobjc for the window glass, the Dock icon and camera names.
if sys.platform == "darwin":
    hiddenimports += ["objc", "AppKit", "Foundation", "AVFoundation"]
# The app's icons (curslink/assets). PyInstaller's own hooks bring Qt along,
# with its platform, image format and SVG icon plugins.
datas += collect_data_files("curslink", includes=["assets/*.svg", "assets/*.png", "assets/navigation/*.svg", "assets/illustrations/**/*.svg"])

icon = SPECPATH + "/icons/" + ("curslink.icns" if sys.platform == "darwin" else "curslink.ico")

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
    icon=icon,
    # Arm or Intel, whichever the runner is.
    target_arch=None,
)
exes = [exe]
if sys.platform == "win32":
    exes.append(EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="curslinkw",
        console=False,
        icon=icon,
    ))

coll = COLLECT(*exes, a.binaries, a.datas, name="curslink")

# macOS: also dist/Curslink.app, a real app bundle with the executable inside
# it, which the .pkg installs. macOS ties Accessibility, Input Monitoring,
# Camera and Local Network to the app that runs, so the executable has to be
# the bundle's own: a launcher script in an unsigned bundle that starts a
# binary elsewhere gets its permissions recorded against the wrong thing.
# packaging/make-installer.sh replaces the Info.plist and signs the bundle.
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Curslink.app",
        icon=icon,
        bundle_identifier="io.github.orimovo.curslink.app",
    )


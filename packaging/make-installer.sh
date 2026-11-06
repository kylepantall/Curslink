#!/usr/bin/env bash
# Builds the installer for this platform from the PyInstaller output in
# dist/curslink (see .github/workflows/build.yml).
#
#   packaging/make-installer.sh VERSION PLATFORM OUTDIR
#
# Windows: curslink-VERSION-windows-x64-setup.exe (Inno Setup, per user)
# macOS:   curslink-VERSION-macos-arm64.pkg       (/usr/local/lib/curslink, curslink on PATH,
#                                                  /Applications/Curslink.app opens the app)
# Linux:   curslink-VERSION-linux-x64.deb         (/opt/curslink, curslink on PATH,
#                                                  a menu entry and icons)
#
# Each installs an `install-kind` file beside the executable, which tells
# `curslink update` how to install the next version (curslink/update.py).
set -euo pipefail

version=$1 platform=$2 out=$3
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/.." && pwd)
app="$root/dist/curslink"
[ -x "$app/curslink" ] || [ -f "$app/curslink.exe" ] || { echo "no build in $app" >&2; exit 1; }
mkdir -p "$out"
out=$(cd "$out" && pwd)
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
name="curslink-$version-$platform"
# 1.2.3-rc.1 -> 1.2.3, for fields that only take numbers.
numeric=${version%%[-+]*}

case "$platform" in
  windows-*)
    cp -R "$app" "$stage/curslink"
    printf 'windows-setup\n' > "$stage/curslink/install-kind"
    iscc="/c/Program Files (x86)/Inno Setup 6/ISCC.exe"
    if [ ! -f "$iscc" ]; then
      # Not on every runner image; Chocolatey is.
      choco install innosetup --no-progress -y > /dev/null
    fi
    # MSYS would turn /DName=... into a path; Inno takes -D too.
    "$iscc" -Q \
      "-DAppVersion=$version" "-DAppNumVersion=$numeric" \
      "-DSourceDir=$(cygpath -w "$stage/curslink")" "-DOutputDir=$(cygpath -w "$out")" \
      "$(cygpath -w "$here/windows/curslink.iss")"
    ;;

  macos-*)
    mkdir -p "$stage/root/usr/local/lib" "$stage/root/usr/local/bin" "$stage/scripts"
    cp -R "$app" "$stage/root/usr/local/lib/curslink"
    printf 'macos-pkg\n' > "$stage/root/usr/local/lib/curslink/install-kind"
    ln -s ../lib/curslink/curslink "$stage/root/usr/local/bin/curslink"
    # Curslink.app: a small launcher for Finder, Launchpad and Spotlight that
    # opens the app from the same install (so updates replace one copy).
    bundle="$stage/root/Applications/Curslink.app/Contents"
    mkdir -p "$bundle/MacOS" "$bundle/Resources"
    cp "$here/macos/launcher" "$bundle/MacOS/Curslink"
    chmod +x "$bundle/MacOS/Curslink"
    cp "$here/icons/curslink.icns" "$bundle/Resources/curslink.icns"
    sed -e "s/@VERSION@/$numeric/g" "$here/macos/Info.plist" > "$bundle/Info.plist"
    cp "$here/macos/preinstall" "$stage/scripts/preinstall"
    chmod +x "$stage/scripts/preinstall"
    pkgbuild \
      --root "$stage/root" \
      --install-location / \
      --scripts "$stage/scripts" \
      --identifier io.github.kylepantall.curslink \
      --version "$numeric" \
      "$stage/component.pkg"
    # A product archive gets the standard installer window and a title.
    cat > "$stage/distribution.xml" <<XML
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
  <title>Curslink $version</title>
  <options customize="never" require-scripts="false" hostArchitectures="arm64"/>
  <domains enable_localSystem="true"/>
  <choices-outline><line choice="default"/></choices-outline>
  <choice id="default" title="Curslink"><pkg-ref id="io.github.kylepantall.curslink"/></choice>
  <pkg-ref id="io.github.kylepantall.curslink" version="$numeric">component.pkg</pkg-ref>
</installer-gui-script>
XML
    productbuild --distribution "$stage/distribution.xml" --package-path "$stage" "$out/$name.pkg"
    ;;

  linux-*)
    arch=${platform#linux-}
    [ "$arch" = x64 ] && arch=amd64
    pkg="$stage/pkg"
    mkdir -p "$pkg/DEBIAN" "$pkg/opt" "$pkg/usr/bin" "$pkg/usr/share/applications"
    cp -R "$app" "$pkg/opt/curslink"
    printf 'deb\n' > "$pkg/opt/curslink/install-kind"
    ln -s /opt/curslink/curslink "$pkg/usr/bin/curslink"
    # The app in the desktop's menu, with its icon at every hicolor size.
    cp "$here/linux/curslink.desktop" "$pkg/usr/share/applications/curslink.desktop"
    for png in "$here"/icons/png/curslink-*.png; do
      size=${png##*-}; size=${size%.png}
      mkdir -p "$pkg/usr/share/icons/hicolor/${size}x${size}/apps"
      cp "$png" "$pkg/usr/share/icons/hicolor/${size}x${size}/apps/curslink.png"
    done
    # Debian sorts ~ before everything, so 1.2.3~rc.1 < 1.2.3.
    cat > "$pkg/DEBIAN/control" <<CONTROL
Package: curslink
Version: ${version//-/\~}
Architecture: $arch
Maintainer: Curslink <https://github.com/kylepantall/Curslink>
Section: utils
Priority: optional
Installed-Size: $(du -sk "$pkg/opt" | cut -f1)
Recommends: libegl1, libgles2, libxcb-cursor0, libxkbcommon-x11-0
Homepage: https://github.com/kylepantall/Curslink
Description: Look at a device and your mouse and keyboard follow
 Gaze and edge based mouse and keyboard switching between computers.
CONTROL
    # Refresh the menu and icon caches so the entry shows up straight away.
    cat > "$pkg/DEBIAN/postinst" <<'SH'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
SH
    chmod 755 "$pkg/DEBIAN/postinst"
    dpkg-deb --root-owner-group --build "$pkg" "$out/$name.deb"
    ;;

  *) echo "unknown platform $platform" >&2; exit 1 ;;
esac
ls -l "$out"

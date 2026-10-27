#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
for c in python curl git; do command -v "$c" >/dev/null || { echo "Puuttuva ohjelma: $c"; exit 1; }; done
# Use upstream's established real-device HID implementation (not guessed writes).
HID_COMMIT="5c690c1ea1bea18cc557da4df4bcb773a1b71086"
if [ ! -d vendor/upstream/.git ]; then
  mkdir -p vendor
  git clone https://github.com/OthrineTM/pelta-control vendor/upstream
fi
git -C vendor/upstream fetch origin "$HID_COMMIT"
git -C vendor/upstream checkout --detach "$HID_COMMIT"
test "$(git -C vendor/upstream rev-parse HEAD)" = "$HID_COMMIT" || { echo "HID-versio ei täsmää"; exit 1; }
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install 'PySide6>=6.6,<7' 'pyinstaller>=6,<7'
# Upstream uses standard Linux hidraw (read upstream README for permissions).
python -m PyInstaller --noconfirm --clean --windowed --name PeltaControl --onedir pelta/main.py
python -m PyInstaller --noconfirm --clean --console --name PeltaBackend --onedir --paths vendor/upstream vendor_cli.py
APPDIR="$PWD/AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps" "$APPDIR/usr/share/licenses/pelta-control-upstream"
cp -a dist/PeltaControl dist/PeltaBackend "$APPDIR/usr/bin/"
cp vendor/upstream/LICENSE "$APPDIR/usr/share/licenses/pelta-control-upstream/LICENSE"
cat > "$APPDIR/AppRun" <<'APP'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/PeltaControl/PeltaControl" "$@"
APP
chmod +x "$APPDIR/AppRun"
cp assets/pelta-control.desktop "$APPDIR/pelta-control.desktop"
cp assets/pelta-control.png "$APPDIR/pelta-control.png"
cp assets/pelta-control.desktop "$APPDIR/usr/share/applications/"
cp assets/pelta-control.png "$APPDIR/usr/share/icons/hicolor/256x256/apps/"
if [ ! -f appimagetool.AppImage ]; then
 curl -fL --retry 3 -o appimagetool.AppImage https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
fi
chmod +x appimagetool.AppImage
ARCH=x86_64 ./appimagetool.AppImage --appimage-extract-and-run "$APPDIR" "$PWD/Pelta-Control-0.5.3-x86_64.AppImage"
echo "Valmis: $PWD/Pelta-Control-0.5.3-x86_64.AppImage"

# Generate the same checksum manifest that the in-app updater accepts.
(cd "$PWD" && sha256sum Pelta-Control-0.5.3-x86_64.AppImage > SHA256SUMS)
echo "SHA256SUMS luotu."

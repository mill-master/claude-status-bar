#!/bin/bash
# Builds claude-status-bar_<version>_all.deb into linux/build/.
# Build machine needs: dpkg-deb, python3 with Pillow, and the DejaVu Sans font
# (all present on a stock Ubuntu runner: python3-pil, fonts-dejavu-core).
set -euo pipefail
cd "$(dirname "$0")/.."

PKG=claude-status-bar
# The version's home is build.sh's Info.plist block; everything else derives from it.
VERSION="$(sed -n 's|.*CFBundleShortVersionString</key><string>\([0-9.]*\)</string>.*|\1|p' build.sh | head -1)"
[[ -n "$VERSION" ]] || { echo "package.sh: could not read version from build.sh" >&2; exit 1; }
# Deliberately NOT read from git config: a public .deb must not harvest whatever personal
# identity happens to be configured on the build machine. Override with MAINTAINER=... .
MAINTAINER="${MAINTAINER:-mill-master <mill-master@users.noreply.github.com>}"

STAGE="linux/build/deb"
SHARE="$STAGE/usr/share/$PKG"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$STAGE/usr/bin" "$SHARE/app" "$SHARE/hooks" "$STAGE/usr/share/doc/$PKG"

python3 linux/gen-assets.py --repo . --out "$SHARE/assets"
install -m 644 linux/app/core.py linux/app/main.py "$SHARE/app/"
printf 'VERSION = "%s"\n' "$VERSION" > "$SHARE/app/_version.py"
install -m 644 hooks/update.js hooks/lifecycle.js hooks/install.js hooks/uninstall.js "$SHARE/hooks/"
install -m 644 LICENSE "$STAGE/usr/share/doc/$PKG/copyright"

cat > "$STAGE/usr/bin/$PKG" <<EOF
#!/bin/sh
exec /usr/bin/python3 /usr/share/$PKG/app/main.py "\$@"
EOF
chmod 755 "$STAGE/usr/bin/$PKG"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Depends: python3, python3-gi, python3-pil, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1, nodejs
Recommends: gir1.2-notify-0.7, gir1.2-gst-plugins-base-1.0, gstreamer1.0-plugins-good
Maintainer: $MAINTAINER
Homepage: https://github.com/mill-master/claude-status-bar
Description: Claude Code status icon for the system tray
 Shows Claude Code's live status in the top bar / system tray: an animated
 Claude icon while it thinks or runs a tool, an amber dot when a session is
 awaiting your permission, and the elapsed time of the current turn.
 .
 The app is launched by Claude Code hooks when a session starts and quits
 itself when none is running. Launch it once after installing to wire up
 the hooks; after that there is nothing to manage.
EOF

# Normalize modes (the build umask must not leak group-writable entries into the package).
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -exec chmod 644 {} +
chmod 755 "$STAGE/usr/bin/$PKG"

dpkg-deb --build --root-owner-group "$STAGE" "linux/build/${PKG}_${VERSION}_all.deb" >/dev/null
echo "Built linux/build/${PKG}_${VERSION}_all.deb"

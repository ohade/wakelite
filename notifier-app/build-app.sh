#!/bin/bash
# Build WakeLiteNotify.app — the notification poster whose click opens the
# WakeLite dashboard. See WakeLiteNotify.swift for why a bundle is required.
#
# Idempotent: safe to re-run after editing the Swift source.
set -euo pipefail

cd "$(dirname "$0")"

APP_DIR="${WAKELITE_NOTIFY_APP:-$HOME/Applications/WakeLiteNotify.app}"
CONTENTS="$APP_DIR/Contents"
MACOS="$CONTENTS/MacOS"

echo "Building WakeLiteNotify..."
rm -rf "$APP_DIR"
mkdir -p "$MACOS"

swiftc -O -o "$MACOS/WakeLiteNotify" WakeLiteNotify.swift \
  -framework AppKit -framework UserNotifications

cat > "$CONTENTS/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.wakelite.notify</string>
    <key>CFBundleName</key>
    <string>WakeLite</string>
    <key>CFBundleDisplayName</key>
    <string>WakeLite</string>
    <key>CFBundleExecutable</key>
    <string>WakeLiteNotify</string>
    <key>CFBundleIconFile</key>
    <string>WakeLite</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>LSUIElement</key>
    <true/>
</dict>
</plist>
PLIST

# The notification shows CFBundleName and this icon, so the alert reads as
# WakeLite rather than as an anonymous helper.
if [ -f ../wakelite/logo.png ]; then
  mkdir -p "$CONTENTS/Resources"
  ICONSET="$(mktemp -d)/WakeLite.iconset"
  mkdir -p "$ICONSET"
  for size in 16 32 128 256 512; do
    sips -z $size $size ../wakelite/logo.png --out "$ICONSET/icon_${size}x${size}.png" >/dev/null 2>&1 || true
    sips -z $((size*2)) $((size*2)) ../wakelite/logo.png --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null 2>&1 || true
  done
  iconutil -c icns "$ICONSET" -o "$CONTENTS/Resources/WakeLite.icns" 2>/dev/null || true
fi

# Ad-hoc signature, matching the ClaudeCallout.app pattern on this machine.
codesign --force --deep -s - "$APP_DIR"

# Register with LaunchServices so macOS can relaunch it on a notification click.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP_DIR"

echo "Built: $APP_DIR"
echo
echo "First run needs a permission grant. Launch it once and click Allow:"
echo "  open \"$APP_DIR\""
echo "Then verify:"
echo "  \"$MACOS/WakeLiteNotify\" --title WakeLite --message 'click me' --url 'http://127.0.0.1:17341/ui#incidents'; echo \$?"

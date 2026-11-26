#!/bin/bash
# Build "Signal Chain Generator.app" and a .dmg for a release, on a Mac.
#
#     bash scripts/build-mac-app.sh 0.2.0
#
# The app is a small launcher: a copy of uv plus a script that runs that exact
# PyPI release (see packaging/macos/launcher.sh). It's signed and notarized
# when these are set (the release workflow sets them):
#
#   SIGN_IDENTITY   "Developer ID Application: Name (TEAMID)" in the keychain
#   ASC_KEY_PATH    App Store Connect API key (.p8), with ASC_KEY_ID and
#                   ASC_ISSUER_ID, for notarization

set -euo pipefail

VERSION="${1:?usage: build-mac-app.sh VERSION}"
UV_VERSION="${UV_VERSION:-0.11.32}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/packaging/macos"
OUT="$ROOT/dist/macos"
APP="$OUT/Signal Chain Generator.app"
DMG="$OUT/Signal-Chain-Generator-$VERSION.dmg"

rm -rf "$OUT"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# uv for both kinds of Mac, joined into one universal binary.
work="$(mktemp -d)"
for arch in aarch64 x86_64; do
    base="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$arch-apple-darwin.tar.gz"
    curl -fsSL -o "$work/uv.tar.gz" "$base"
    expected="$(curl -fsSL "$base.sha256" | awk '{print $1}')"
    actual="$(shasum -a 256 "$work/uv.tar.gz" | awk '{print $1}')"
    [ "$expected" = "$actual" ] || { echo "uv download checksum mismatch ($arch)" >&2; exit 1; }
    tar -xzf "$work/uv.tar.gz" -C "$work"
    mv "$work/uv-$arch-apple-darwin/uv" "$work/uv-$arch"
done
lipo -create -output "$APP/Contents/MacOS/uv" "$work/uv-aarch64" "$work/uv-x86_64"
rm -rf "$work"

clang -O2 -arch arm64 -arch x86_64 -mmacosx-version-min=12.0 \
    -o "$APP/Contents/MacOS/Signal Chain Generator" "$SRC/launcher.c"
cp "$SRC/launcher.sh" "$APP/Contents/Resources/launcher.sh"
chmod +x "$APP/Contents/Resources/launcher.sh"
printf '%s' "$VERSION" > "$APP/Contents/Resources/VERSION"
sed "s/__VERSION__/$VERSION/g" "$SRC/Info.plist" > "$APP/Contents/Info.plist"
cp "$ROOT/LICENSE" "$ROOT/NOTICE" "$APP/Contents/Resources/"

if [ -n "${SIGN_IDENTITY:-}" ]; then
    sign() { codesign --force --timestamp --options runtime --sign "$SIGN_IDENTITY" "$@"; }
    sign "$APP/Contents/MacOS/uv"
    sign "$APP/Contents/MacOS/Signal Chain Generator"
    sign "$APP"
    codesign --verify --strict --verbose=2 "$APP"
fi

# A disk image with the app and a shortcut to Applications to drag it onto.
stage="$(mktemp -d)"
cp -R "$APP" "$stage/"
ln -s /Applications "$stage/Applications"
hdiutil create -quiet -volname "Signal Chain Generator" -srcfolder "$stage" -format UDZO "$DMG"
rm -rf "$stage"

if [ -n "${SIGN_IDENTITY:-}" ]; then
    codesign --force --timestamp --sign "$SIGN_IDENTITY" "$DMG"
fi
if [ -n "${ASC_KEY_PATH:-}" ]; then
    xcrun notarytool submit "$DMG" --key "$ASC_KEY_PATH" --key-id "$ASC_KEY_ID" \
        --issuer "$ASC_ISSUER_ID" --wait --timeout 30m
    xcrun stapler staple "$DMG"
    spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG"
fi

echo "Built: $DMG"

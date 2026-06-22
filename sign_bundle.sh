#!/usr/bin/env bash
# Post-build code signing for FALCON.app on macOS 26+
# macOS 26 stamps com.apple.provenance on every file written to disk.
# codesign accepts this attribute on Mach-O binaries when signing them
# individually, but rejects it on resource files (SVGs, .py, zip, etc.)
# when computing the bundle's CodeResources seal.
# Fix: strip xattrs from the whole bundle, then sign.
set -euo pipefail

APP="dist/FALCON.app"

if [ ! -d "$APP" ]; then
    echo "ERROR: $APP not found. Run 'pyinstaller FALCON.spec' first." >&2
    exit 1
fi

echo "Clearing extended attributes from $APP ..."
xattr -cr "$APP"

echo "Signing bundle ..."
codesign --force --sign - --timestamp=none "$APP"

echo "Verifying ..."
codesign --verify "$APP"
echo "Done — $APP is signed and verified."

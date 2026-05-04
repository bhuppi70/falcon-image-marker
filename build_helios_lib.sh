#!/usr/bin/env bash
# Builds libHeliosDacAPI.dylib (macOS arm64) and its libusb dependency
# from source and places them in the project root.
# Requirements: clang++, make, curl, git
set -e

BUILD=/tmp/helios_build
mkdir -p "$BUILD"

echo "==> Downloading libusb 1.0.27 source..."
curl -fsSL https://github.com/libusb/libusb/releases/download/v1.0.27/libusb-1.0.27.tar.bz2 \
    -o "$BUILD/libusb.tar.bz2"
tar -xjf "$BUILD/libusb.tar.bz2" -C "$BUILD"

echo "==> Building libusb for arm64..."
cd "$BUILD/libusb-1.0.27"
./configure --prefix="$BUILD/libusb_arm64" --disable-dependency-tracking --quiet
make -j4
make install

echo "==> Cloning Helios DAC SDK..."
if [ ! -d "$BUILD/helios_dac" ]; then
    git clone --depth=1 https://github.com/Grix/helios_dac.git "$BUILD/helios_dac"
fi

SDK="$BUILD/helios_dac/sdk/cpp"
LIBUSB="$BUILD/libusb_arm64"
OUT="$(cd "$(dirname "$0")"; pwd)"

echo "==> Building libHeliosDacAPI.dylib..."
clang++ -std=c++14 -shared -fPIC \
    -I "$SDK" \
    -I "$LIBUSB/include/libusb-1.0" \
    -o "$BUILD/libHeliosDacAPI.dylib" \
    "$SDK/shared_library/HeliosDacAPI.cpp" \
    "$SDK/HeliosDac.cpp" \
    "$SDK/idn/idn.cpp" \
    "$SDK/idn/idnServerList.cpp" \
    "$SDK/idn/plt-posix.cpp" \
    -L "$LIBUSB/lib" -lusb-1.0 -lpthread

echo "==> Patching dylib rpath for libusb..."
install_name_tool -change \
    "$LIBUSB/lib/libusb-1.0.0.dylib" \
    "@loader_path/libusb-1.0.0.dylib" \
    "$BUILD/libHeliosDacAPI.dylib"

echo "==> Copying libraries to project root..."
cp "$BUILD/libHeliosDacAPI.dylib" "$OUT/"
cp "$LIBUSB/lib/libusb-1.0.0.dylib" "$OUT/"

echo "Done. Libraries installed to $OUT/"

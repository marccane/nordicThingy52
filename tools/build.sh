#!/bin/sh
# Fetches Nordic's Thingy:52 firmware into thingy-fw/ (first run only) and applies
# patches/thingy-fw.patch. Then builds the secure bootloader (signed with keys/private.pem)
# and the application, and writes to out/:
#   thingy_sd_bl.hex        SoftDevice + bootloader, flashed once over SWD
#   thingy_app_v<N>.zip     signed OTA package for the application
#
# Usage: tools/build.sh [app-version]   (default 1; must not be lower than the version on the device)
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
FW=$ROOT/thingy-fw
OUT=$ROOT/out
VERSION=${1:-1}
SD_HEX=$FW/external/sdk13/components/softdevice/s132/hex/s132_nrf52_4.0.2_softdevice.hex
TOOLCHAIN="GNU_INSTALL_ROOT=/usr GNU_VERSION=$(arm-none-eabi-gcc -dumpversion)"

PATCH=$ROOT/patches/thingy-fw.patch
UPSTREAM=https://github.com/NordicSemiconductor/Nordic-Thingy52-FW.git
UPSTREAM_COMMIT=6ada0a9aed640141ab9f9e91b5f9f5ec0ed237e7 # v2.2.0, last commit before archiving
MICRO_ECC=https://github.com/kmackay/micro-ecc.git
MICRO_ECC_COMMIT=541b3a78026420a3e369c4c9281c396b5e531113

mkdir -p "$OUT"

# Nordic's firmware tree is not kept in this repo: fetch it at the pinned commit and apply our changes.
if [ ! -d "$FW" ]; then
    git clone --quiet "$UPSTREAM" "$FW"
    git -C "$FW" checkout --quiet --detach "$UPSTREAM_COMMIT"
    git -C "$FW" apply "$PATCH"
    git clone --quiet "$MICRO_ECC" "$FW/external/sdk13/external/micro-ecc/micro-ecc"
    git -C "$FW/external/sdk13/external/micro-ecc/micro-ecc" checkout --quiet --detach "$MICRO_ECC_COMMIT"
elif [ "$(git -C "$FW" diff | grep -v '^index ')" != "$(grep -v '^index ' "$PATCH")" ]; then
    echo "warning: thingy-fw/ differs from patches/thingy-fw.patch; to record your changes run:" >&2
    echo "         git -C thingy-fw diff > patches/thingy-fw.patch" >&2
fi

if [ ! -e "$FW/sdk_components" ]; then
    # Creates the SDK symlinks. Its own micro-ecc build hardcodes the GCC 4.9 path and fails; ours follows.
    (cd "$FW" && sh setup_sdk.sh) || true
fi
if [ ! -f "$FW/external/sdk13/external/micro-ecc/nrf52_armgcc/armgcc/micro_ecc_lib_nrf52.a" ]; then
    make -C "$FW/external/sdk13/external/micro-ecc/nrf52_armgcc/armgcc" $TOOLCHAIN
fi

# Bootloader, with the public half of our signing key.
"$ROOT/.venv/bin/python" "$ROOT/tools/pubkey_c.py" "$ROOT/keys/private.pem" > "$FW/project/bootloader_secure/dfu_public_key.c"
rm -rf "$FW/project/bootloader_secure/pca20020/armgcc/_build"
make -C "$FW/project/bootloader_secure/pca20020/armgcc" -j8 $TOOLCHAIN
mergehex -m "$SD_HEX" "$FW/project/bootloader_secure/pca20020/armgcc/_build/nrf52832_xxaa_s132.hex" -o "$OUT/thingy_sd_bl.hex"

# Application. The version also becomes the patch number of the FW Version characteristic.
rm -rf "$FW/project/pca20020_s132/armgcc/_build"
make -C "$FW/project/pca20020_s132/armgcc" -j8 $TOOLCHAIN THINGY_FW_VERSION_PATCH="$VERSION"
cp "$FW/project/pca20020_s132/armgcc/_build/nrf52832_xxaa_s132.hex" "$OUT/thingy_app_v$VERSION.hex"
"$ROOT/.venv/bin/python" "$ROOT/tools/mkpkg.py" --key "$ROOT/keys/private.pem" --app-version "$VERSION" \
    "$OUT/thingy_app_v$VERSION.hex" "$OUT/thingy_app_v$VERSION.zip"

ls -l "$OUT"

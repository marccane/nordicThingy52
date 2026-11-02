#!/usr/bin/env python3
"""Build a signed DFU package (nrfutil-style zip) for the nRF5 SDK 13 secure bootloader.

The init packet follows thingy-fw/project/dfu_req_handling/dfu-cc.proto. The bootloader verifies
the ECDSA-P256 signature over the serialized InitCommand, and expects the firmware SHA-256 and
the signature halves (r, s) in little-endian byte order.

Usage: mkpkg.py --key keys/private.pem --app-version N app.hex out.zip
"""
import argparse
import hashlib
import json
import zipfile

from ecdsa import SigningKey
from ecdsa.util import sigencode_string
from intelhex import IntelHex

HW_VERSION = 52
SD_REQ_S132_4_0_2 = 0x98

# dfu-cc.proto enum values
OP_CODE_INIT = 1
FW_TYPE_APPLICATION = 0
HASH_TYPE_SHA256 = 3
SIGNATURE_TYPE_ECDSA_P256_SHA256 = 0


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def field_varint(number: int, value: int) -> bytes:
    return varint(number << 3 | 0) + varint(value)


def field_bytes(number: int, value: bytes) -> bytes:
    return varint(number << 3 | 2) + varint(len(value)) + value


def init_command(fw_version: int, firmware: bytes) -> bytes:
    fw_hash = hashlib.sha256(firmware).digest()[::-1]
    return b"".join([
        field_varint(1, fw_version),
        field_varint(2, HW_VERSION),
        field_bytes(3, varint(SD_REQ_S132_4_0_2)),  # packed repeated sd_req
        field_varint(4, FW_TYPE_APPLICATION),
        field_varint(5, 0),  # sd_size
        field_varint(6, 0),  # bl_size
        field_varint(7, len(firmware)),  # app_size
        field_bytes(8, field_varint(1, HASH_TYPE_SHA256) + field_bytes(2, fw_hash)),
        field_varint(9, 0),  # is_debug
    ])


def signed_packet(init: bytes, key: SigningKey) -> bytes:
    signature = key.sign(init, hashfunc=hashlib.sha256, sigencode=sigencode_string)
    signature = signature[31::-1] + signature[63:31:-1]
    # The bootloader hashes everything after the init field's one-byte length, so init must come
    # last in Command and stay shorter than 128 bytes.
    assert len(init) < 128
    command = field_varint(1, OP_CODE_INIT) + field_bytes(2, init)
    signed = b"".join([
        field_bytes(1, command),
        field_varint(2, SIGNATURE_TYPE_ECDSA_P256_SHA256),
        field_bytes(3, signature),
    ])
    return field_bytes(2, signed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--key", required=True, help="PEM private key")
    parser.add_argument("--app-version", type=int, required=True)
    parser.add_argument("hex")
    parser.add_argument("zip")
    args = parser.parse_args()

    firmware = IntelHex(args.hex).tobinstr()
    with open(args.key) as f:
        key = SigningKey.from_pem(f.read())
    init = init_command(args.app_version, firmware)
    packet = signed_packet(init, key)

    manifest = {"manifest": {"application": {"bin_file": "application.bin", "dat_file": "application.dat"}}}
    with zipfile.ZipFile(args.zip, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=4))
        z.writestr("application.bin", firmware)
        z.writestr("application.dat", packet)
    print(f"{args.zip}: app {len(firmware)} bytes, version {args.app_version}, init packet {len(packet)} bytes")


if __name__ == "__main__":
    main()

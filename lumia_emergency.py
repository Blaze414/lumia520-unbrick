#!/usr/bin/env python3
"""Lumia 520 (MSM8227) emergency-mode recovery for macOS.

Stage 1: upload the Nokia V1 emergency programmer (.hex) to the boot ROM over
         DMSS download protocol (05c6:9008, "QHSUSB_DLOAD") and start it.
Stage 2: talk to the programmer ("QHSUSB_ARMPRG") over the streaming protocol
         and write MBR, GPT, SBL1/2/3, UEFI, TZ, RPM and WINSECAPP from the FFU.

The packet formats, load address (0x2A000000), partition id (0x21) and the
write list are ported from WPinternals (QualcommDownload.cs, QualcommFlasher.cs,
LumiaUnlockBootloaderViewModel.cs). Stage 3 (full FFU flash) still needs
Windows: thor2 -mode uefiflash, or WPinternals.

Setup (once, in this folder):
  brew install libusb
  python3 -m venv .venv && .venv/bin/pip install pyusb
Then run every command as: .venv/bin/python lumia_emergency.py ...

Usage:
  lumia_emergency.py probe                      # read-only: ping, version, root key hash
  lumia_emergency.py info  --hex X.hex --ffu Y.ffu   # parse files, print the write plan
  lumia_emergency.py flash --hex X.hex --ffu Y.ffu --yes
  lumia_emergency.py selftest
"""
import argparse
import hashlib
import struct
import sys
import time

VID, PID = 0x05C6, 0x9008
LOAD_ADDR = 0x2A000000
SECTOR = 0x200
WINSECAPP_LIMIT = 0x1E7FE00  # programmer bounds-check bug, see WPinternals

# ---------------------------------------------------------------- HDLC framing

def crc16(data):
    """CRC-16/X.25 as used by Qualcomm HDLC framing."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return ~crc & 0xFFFF


def frame(payload):
    body = bytes(payload) + struct.pack("<H", crc16(payload))
    out = bytearray(b"\x7e")
    for b in body:
        if b in (0x7D, 0x7E):
            out += bytes((0x7D, b ^ 0x20))
        else:
            out.append(b)
    out.append(0x7E)
    return bytes(out)


def unframe(buf):
    """Return payload of the first complete frame in buf, or None if incomplete."""
    i = 0
    while i < len(buf) and buf[i] == 0x7E:
        i += 1
    end = buf.find(b"\x7e", i)
    if end < 0:
        return None
    raw = bytearray()
    j = i
    while j < end:
        b = buf[j]
        if b == 0x7D:
            j += 1
            b = buf[j] ^ 0x20
        raw.append(b)
        j += 1
    if len(raw) < 3:
        raise IOError(f"short frame: {bytes(raw).hex()}")
    payload, crc = bytes(raw[:-2]), struct.unpack("<H", raw[-2:])[0]
    if crc16(payload) != crc:
        raise IOError(f"bad CRC in reply: {bytes(raw).hex()}")
    return payload

# ---------------------------------------------------------------- USB

class Phone:
    def __init__(self, wait_s=0, want=None):
        """Open 05c6:9008. With `want`, wait until its product string is one of `want`."""
        import usb.core
        import usb.util
        self.usb = usb
        deadline = time.time() + wait_s
        while True:
            dev = usb.core.find(idVendor=VID, idProduct=PID)
            product = None
            if dev is not None:
                try:
                    product = usb.util.get_string(dev, dev.iProduct) or ""
                except Exception:  # noqa: BLE001 - device may be mid re-enumeration
                    product = None
            if product is not None and (want is None or product in want):
                break
            if time.time() >= deadline:
                raise IOError(f"no 05c6:9008 device{' in ' + '/'.join(want) + ' mode' if want else ''} found")
            time.sleep(0.5)
        self.dev, self.product = dev, product
        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass  # already configured
        intf = dev.get_active_configuration()[(0, 0)]
        usb.util.claim_interface(dev, intf.bInterfaceNumber)
        d = usb.util.ENDPOINT_IN
        self.ep_in = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == d)
        self.ep_out = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) != d)
        self.intf = intf

    def close(self):
        self.usb.util.dispose_resources(self.dev)

    def command(self, payload, expect, encode=True, timeout_ms=3000):
        """Send one command, return decoded reply; raise if it doesn't start with `expect`."""
        self.ep_out.write(frame(payload) if encode else bytes(payload), timeout_ms)
        buf = bytearray()
        deadline = time.time() + timeout_ms / 1000
        while True:
            try:
                buf += bytes(self.ep_in.read(0x4000, timeout_ms))
            except self.usb.core.USBTimeoutError:
                raise IOError(f"no reply to command {payload[:1].hex()}") from None
            reply = unframe(bytes(buf))
            if reply is not None:
                break
            if time.time() > deadline:
                raise IOError(f"incomplete reply to {payload[:1].hex()}: {bytes(buf).hex()}")
        if not reply.startswith(expect):
            raise IOError(f"command {payload[:1].hex()}: expected {expect.hex()}, got {reply[:16].hex()}")
        return reply

# ---------------------------------------------------------------- DMSS (stage 1)

def dmss_alive(p):
    p.command(b"\x06", b"\x02")


def dmss_version(p):
    return p.command(b"\x0c", b"\x0d")[2:].decode("latin-1")


def dmss_rkh(p):
    return p.command(b"\x18", b"\x18\x01\x00")[3:3 + 32]


def dmss_upload(p, addr, data):
    for off in range(0, len(data), 0x100):
        chunk = data[off:off + 0x100]
        p.command(b"\x0f" + struct.pack(">IH", addr + off, len(chunk)) + chunk, b"\x02")


def dmss_go(p, addr):
    p.command(b"\x05" + struct.pack(">I", addr), b"\x02")

# ---------------------------------------------------------------- streaming (stage 2)

HELLO = b"\x01" + b"QCOM fast download protocol host" + b"\x02\x02\x01"


def flash_write(p, start, data, label):
    total = len(data)
    for off in range(0, total, 0x400):
        chunk = data[off:off + 0x400]
        addr = struct.pack("<I", start + off)
        p.command(b"\x07" + addr + chunk, b"\x08" + addr, encode=False)
        if off % 0x40000 == 0 or off + 0x400 >= total:
            print(f"\r  {label:<10} {min(off + 0x400, total):>9}/{total} bytes", end="", flush=True)
    print()

# ---------------------------------------------------------------- files

def parse_hex(path):
    """Intel HEX -> (base address, contiguous bytes)."""
    upper, chunks = 0, []
    with open(path) as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            if line[0] != ":":
                raise ValueError(f"{path}:{n}: not an Intel HEX line")
            rec = bytes.fromhex(line[1:])
            if len(rec) != rec[0] + 5 or sum(rec) & 0xFF:
                raise ValueError(f"{path}:{n}: bad length or checksum")
            kind, addr, data = rec[3], (rec[1] << 8) | rec[2], rec[4:-1]
            if kind == 0:
                chunks.append((upper + addr, data))
            elif kind == 4:
                upper = struct.unpack(">H", data)[0] << 16
            elif kind == 2:
                upper = struct.unpack(">H", data)[0] << 4
            elif kind == 1:
                break
    if not chunks:
        raise ValueError(f"{path}: no data records")
    chunks.sort()
    base, out = chunks[0][0], bytearray()
    for a, d in chunks:
        if a != base + len(out):
            raise ValueError(f"{path}: gap at 0x{a:08x}; non-contiguous programmer not supported")
        out += d
    return base, bytes(out)


def root_key_hash(binary):
    """SHA-256 of the last DER certificate in the first chain found (same rule as WPinternals)."""
    certs, last = [], 0
    for i in range(len(binary) - 6):
        if binary[i:i + 2] == b"\x30\x82" and binary[i + 4:i + 6] == b"\x30\x82":
            size = struct.unpack(">H", binary[i + 2:i + 4])[0] + 4
            if last and last != i:
                break
            certs.append(binary[i:i + size])
            last = i + size
    return hashlib.sha256(certs[-1]).digest() if certs else None


class FFU:
    def __init__(self, path):
        self.f = open(path, "rb")
        sh = self.f.read(0x20)
        if sh[4:16] != b"SignedImage ":
            raise ValueError(f"{path}: not an FFU")
        self.chunk = struct.unpack_from("<I", sh, 0x10)[0] * 1024
        sec_size, cat, hashes = struct.unpack_from("<I", sh, 0)[0], *struct.unpack_from("<II", sh, 0x18)
        pos = self._up(sec_size + cat + hashes)
        self.f.seek(pos)
        ih = self.f.read(0x1C)
        if ih[4:16] != b"ImageFlash  ":
            raise ValueError(f"{path}: bad image header")
        pos += self._up(struct.unpack_from("<I", ih, 0)[0] + struct.unpack_from("<I", ih, 0x10)[0])
        self.f.seek(pos)
        st = self.f.read(248)
        self.platform = st[0x0C:0x0C + 192].split(b"\0")[0].decode("ascii", "replace").strip()
        count, wlen, vlen = struct.unpack_from("<I", st, 208)[0], struct.unpack_from("<I", st, 212)[0], struct.unpack_from("<I", st, 220)[0]
        store_len = self._up(248 + wlen + vlen)
        self.f.seek(pos)
        store = self.f.read(store_len)
        self.payload = pos + store_len
        self.map, off, ffu_idx = {}, 248 + vlen, 0
        for _ in range(count):
            locs, n = struct.unpack_from("<II", store, off)
            for j in range(locs):
                method, idx = struct.unpack_from("<II", store, off + 8 + j * 8)
                if method == 0:  # 2 = from end of disk (backup GPT); ignored like WPinternals
                    for k in range(n):
                        self.map[idx + k] = ffu_idx + k
            off += 8 + locs * 8
            ffu_idx += n
        self.f.seek(0, 2)
        if self.f.tell() != self.payload + ffu_idx * self.chunk:
            raise ValueError(f"{path}: size {self.f.tell()} != expected {self.payload + ffu_idx * self.chunk}; truncated or corrupt download")
        self.partitions = parse_gpt(self.sectors(1, 0x21))

    def _up(self, n):
        return -(-n // self.chunk) * self.chunk

    def sectors(self, first, count):
        out, per = bytearray(), self.chunk // SECTOR
        for s in range(first, first + count):
            c = self.map.get(s // per)
            if c is None:
                out += bytes(SECTOR)
            else:
                self.f.seek(self.payload + c * self.chunk + (s % per) * SECTOR)
                data = self.f.read(SECTOR)
                if len(data) != SECTOR:
                    raise ValueError(f"short read at sector {s}")
                out += data
        return bytes(out)

    def partition(self, name):
        first, last = self.partitions[name]
        return self.sectors(first, last - first + 1)


def parse_gpt(buf):
    """buf starts at LBA 1 (GPT header). Returns {name: (first_lba, last_lba)}."""
    if buf[:8] != b"EFI PART":
        raise ValueError("FFU has no GPT at sector 1")
    entries_lba, n, size = struct.unpack_from("<QII", buf, 0x48)
    base = (entries_lba - 1) * SECTOR
    parts = {}
    for i in range(n):
        e = buf[base + i * size: base + (i + 1) * size]
        if len(e) < 128 or e[:16] == bytes(16):
            continue
        first, last = struct.unpack_from("<QQ", e, 32)
        name = e[56:128].decode("utf-16-le").split("\0")[0]
        parts[name] = (first, last)
    return parts


def build_plan(ffu):
    """(label, byte offset, data) in WPinternals' order."""
    p = ffu.partitions
    for bad in ("HACK", "IS_UNLOCKED_SBL3"):
        if bad in p:
            raise ValueError(f"FFU GPT has a {bad} partition; use a stock ROM")
    for need in ("SBL1", "SBL2", "SBL3", "UEFI", "TZ", "RPM", "WINSECAPP"):
        if need not in p:
            raise ValueError(f"FFU GPT has no {need} partition")
        # WPinternals swaps these into place and rewrites the GPT; we only support the stock layout.
        if "BACKUP_" + need in p and p[need][0] > p["BACKUP_" + need][0]:
            raise ValueError(f"{need} sits after BACKUP_{need}; GPT would need rewriting, not supported")
        if p[need][1] >= 0xF400:
            raise ValueError(f"{need} ends past sector 0xF400, out of the V1 programmer's range")
    off = lambda n: p[n][0] * SECTOR
    plan = [("MBR", 0, ffu.sectors(0, 1)),
            ("GPT", 0x200, ffu.sectors(1, 0x21)[:0x41FF])]  # last byte skipped: programmer bounds bug
    for n in ("SBL2", "SBL3", "UEFI"):
        plan.append((n, off(n), ffu.partition(n)))
    plan.append(("SBL1", off("SBL1"), ffu.partition("SBL1")[:-SECTOR]))
    for n in ("TZ", "RPM"):
        plan.append((n, off(n), ffu.partition(n)))
    win = ffu.partition("WINSECAPP")
    plan.append(("WINSECAPP", off("WINSECAPP"), win[:max(0, WINSECAPP_LIMIT - off("WINSECAPP"))]))
    for label, start, data in plan:
        if start + len(data) > 0xFFFFFFFF:
            raise ValueError(f"{label} is beyond the 4 GB address range of the protocol")
    return plan


def load_files(hex_path, ffu_path):
    base, prog = parse_hex(hex_path)
    if "QHSUSB_ARMPRG".encode("utf-16-le") not in prog:
        raise ValueError("HEX is not a V1 emergency programmer (no QHSUSB_ARMPRG string)")
    if base != LOAD_ADDR:
        raise ValueError(f"HEX loads at 0x{base:08x}, expected 0x{LOAD_ADDR:08x}")
    ffu = FFU(ffu_path)
    if "MSM8227" not in ffu.platform:
        raise ValueError(f"FFU platform is {ffu.platform!r}, not an MSM8227 (Lumia 520) image")
    return prog, root_key_hash(prog), ffu, build_plan(ffu), root_key_hash(ffu.partition("SBL1"))


def check_rkh(phone, label, rkh):
    print(f"{label + ' root key':<20}: {rkh.hex() if rkh else 'none found'}")
    if phone != bytes(32) and phone != rkh:  # all-zero = engineering phone, accepts anything
        raise IOError(f"{label} is not signed for this phone (root key mismatch); nothing written")

# ---------------------------------------------------------------- commands

def cmd_probe(_):
    p = Phone()
    print("USB product :", p.product)
    dmss_alive(p)
    print("NOP         : ACK")
    print("Version     :", dmss_version(p))
    print("Root key    :", dmss_rkh(p).hex())
    p.close()


def cmd_info(a):
    prog, rkh, ffu, plan, ffu_rkh = load_files(a.hex, a.ffu)
    print(f"Programmer  : {len(prog)} bytes at 0x{LOAD_ADDR:08x}, root key {rkh.hex() if rkh else 'none'}")
    print(f"FFU platform: {ffu.platform}, SBL1 root key {ffu_rkh.hex() if ffu_rkh else 'none'}")
    for label, start, data in plan:
        print(f"  {label:<10} @ 0x{start:08x}  {len(data):>9} bytes")


def cmd_flash(a):
    prog, rkh, ffu, plan, ffu_rkh = load_files(a.hex, a.ffu)
    p = Phone(want=("QHSUSB_DLOAD", "QHSUSB_ARMPRG"))
    if p.product == "QHSUSB_DLOAD":
        dmss_alive(p)
        phone_rkh = dmss_rkh(p)
        print(f"{'Phone root key':<20}: {phone_rkh.hex()}")
        check_rkh(phone_rkh, "Programmer", rkh)
        check_rkh(phone_rkh, "FFU SBL1", ffu_rkh)
    else:
        # Rerun after a failed stage 2: programmer already running, RKH command not available.
        print("Phone already in QHSUSB_ARMPRG (programmer running); skipping stage 1 and its root key check.")
        if rkh != ffu_rkh:
            raise IOError("programmer and FFU SBL1 root keys differ; nothing written")
    if not a.yes:
        print("Checks passed. Re-run with --yes to write the bootloader.")
        return
    if p.product == "QHSUSB_DLOAD":
        print("Stage 1: uploading programmer")
        dmss_upload(p, LOAD_ADDR, prog)
        dmss_go(p, LOAD_ADDR)
        p.close()
        p = Phone(wait_s=20, want=("QHSUSB_ARMPRG", ""))  # WPinternals also accepts a blank name here
    print("Programmer up as   :", p.product or "(no name)")
    print("Stage 2: writing bootloader to eMMC")
    p.command(HELLO, b"\x02", encode=False)
    p.command(b"\x17\x00", b"\x18", encode=False)
    p.command(b"\x1b\x21", b"\x1c", encode=False)
    for label, start, data in plan:
        flash_write(p, start, data, label)
    p.command(b"\x15", b"\x16", encode=False)
    print("Bootloader written. Rebooting phone.")
    try:
        p.command(b"\x0b", b"\x0c", encode=False)
    except IOError:
        pass  # phone may drop off USB before acking
    print("Next: the phone should show up as a Lumia in flash mode. Flash the FFU from Windows:")
    print("  thor2 -mode uefiflash -ffufile ROM.ffu -do_full_nvi_update -do_factory_reset -reboot")


def cmd_selftest(_):
    # CRC and framing, checked against a real reply from this phone (SW version frame).
    real = bytes.fromhex("0d0f50424c5f446c6f6164564552322e30")
    assert unframe(frame(real)) == real
    assert unframe(frame(b"\x7e\x7d\x01")) == b"\x7e\x7d\x01"
    assert crc16(b"123456789") == 0x906E  # CRC-16/X.25 check value
    # Intel HEX: two records at 0x2A000000, contiguous.
    def rec(addr, kind, data):
        r = bytes([len(data), addr >> 8, addr & 0xFF, kind]) + data
        return ":" + (r + bytes([-sum(r) & 0xFF])).hex().upper()
    import os
    import tempfile
    d = tempfile.mkdtemp()
    hp = os.path.join(d, "t.hex")
    with open(hp, "w") as f:
        f.write("\n".join([rec(0, 4, b"\x2a\x00"), rec(0, 0, b"\x01\x02"), rec(2, 0, b"\x03"), rec(0, 1, b"")]))
    assert parse_hex(hp) == (LOAD_ADDR, b"\x01\x02\x03")
    # Synthetic FFU: chunk 128 KB, MBR + GPT with the required partitions.
    ck = 128 * 1024
    disk = bytearray(ck * 2)
    disk[0:2] = b"\x55\xaa"
    names = ["SBL1", "SBL2", "SBL3", "UEFI", "TZ", "RPM", "WINSECAPP"]
    hdr = bytearray(SECTOR)
    hdr[:8] = b"EFI PART"
    struct.pack_into("<QII", hdr, 0x48, 2, len(names), 128)
    disk[SECTOR:2 * SECTOR] = hdr
    for i, n in enumerate(names):
        e = bytearray(128)
        e[:16] = b"\x01" * 16
        first = 34 + i * 4
        struct.pack_into("<QQ", e, 32, first, first + 3)
        e[56:56 + len(n) * 2] = n.encode("utf-16-le")
        disk[2 * SECTOR + i * 128: 2 * SECTOR + (i + 1) * 128] = e
        disk[first * SECTOR] = i + 1
    sh = bytearray(ck)
    sh[4:16] = b"SignedImage "
    struct.pack_into("<I", sh, 0, 0x20)
    struct.pack_into("<I", sh, 0x10, 128)
    ih = bytearray(ck)
    ih[4:16] = b"ImageFlash  "
    struct.pack_into("<I", ih, 0, 0x1C)
    st = bytearray(ck)
    st[0x0C:0x0C + 18] = b"Nokia.MSM8227.P6036"[:18]
    struct.pack_into("<I", st, 208, 2)        # two write descriptors
    struct.pack_into("<I", st, 212, 2 * 16)   # each: locs, count, (method, idx)
    for k in range(2):
        struct.pack_into("<IIII", st, 248 + k * 16, 1, 1, 0, k)
    fp = os.path.join(d, "t.ffu")
    with open(fp, "wb") as f:
        f.write(sh + ih + st + disk)
    ffu = FFU(fp)
    assert ffu.partitions["SBL2"] == (38, 41)
    with open(fp, "r+b") as f:
        f.truncate(len(sh + ih + st + disk) - SECTOR)
    try:
        FFU(fp)
        raise AssertionError("truncated FFU accepted")
    except ValueError:
        pass
    plan = {l: (s, dt) for l, s, dt in build_plan(ffu)}
    assert plan["MBR"][1][:2] == b"\x55\xaa" and len(plan["GPT"][1]) == 0x41FF
    assert plan["SBL2"][0] == 38 * SECTOR and plan["SBL2"][1][0] == 2
    assert len(plan["SBL1"][1]) == 3 * SECTOR  # last sector skipped
    # Root key hash: two chained fake DER certs; hash is of the last one.
    c1 = b"\x30\x82\x00\x06\x30\x82\x00\x02\xa0\x03"  # cert header, tbs header, version tag
    c2 = b"\x30\x82\x00\x06\x30\x82\x00\x02\xa0\x02"
    assert root_key_hash(b"xx" + c1 + c2 + b"yy") == hashlib.sha256(c2).digest()
    print("selftest OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    sub.add_parser("selftest")
    for name in ("info", "flash"):
        s = sub.add_parser(name)
        s.add_argument("--hex", required=True, help="Nokia V1 emergency programmer (.hex)")
        s.add_argument("--ffu", required=True, help="RM-915 ROM (.ffu)")
        if name == "flash":
            s.add_argument("--yes", action="store_true", help="actually write; without it only checks run")
    a = ap.parse_args()
    try:
        {"probe": cmd_probe, "info": cmd_info, "flash": cmd_flash, "selftest": cmd_selftest}[a.cmd](a)
    except (IOError, ValueError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()

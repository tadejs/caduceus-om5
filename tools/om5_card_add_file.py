#!/usr/bin/env python3
"""Put one file into a one-level folder of an om-emu card image (OMSD) formatted by the firmware (FAT32). Stdlib only.

  python -I tools/om5_card_add_file.py IN.omsd OUT.omsd LOCAL_FILE DIR NAME      e.g. ... BUNDLES.BIN OM5MOD BUNDLES.BIN

Used for the C5 emulator runs (card with / without C:\\OM5MOD\\BUNDLES.BIN). 8.3 upper-case names only (no long names).
OMSD: b'OMSD', u32 1, u32 sectors, u32 n, then n x (u32 sector, 512 bytes); absent sectors are zero. Emulator files only;
nothing here is meant for a real card.
"""
import struct, sys

SEC = 512
DATE = (2026 - 1980) << 9 | 11 << 5 | 8          # fixed, so the output is reproducible


def load(path):
    b = open(path, 'rb').read()
    assert b[:4] == b'OMSD' and struct.unpack_from('<I', b, 4)[0] == 1
    total, n = struct.unpack_from('<II', b, 8)
    return total, {struct.unpack_from('<I', b, 16 + i * 516)[0]: bytearray(b[20 + i * 516:20 + i * 516 + 512]) for i in range(n)}


def name83(n):
    base, _, ext = n.upper().partition('.')
    assert len(base) <= 8 and len(ext) <= 3 and base, n
    return (base.ljust(8) + ext.ljust(3)).encode('ascii')


def main():
    inp, outp, local, ddir, dname = sys.argv[1:6]
    total, sec = load(inp)
    data = open(local, 'rb').read()

    def rd(s):
        return sec.setdefault(s, bytearray(SEC))

    mbr = rd(0)
    assert mbr[510:512] == b'\x55\xaa'
    part = struct.unpack_from('<I', mbr, 0x1c6)[0]
    bpb = rd(part)
    bps, spc, rsv, nfat = struct.unpack_from('<HBHB', bpb, 11)
    fatsz, root = struct.unpack_from('<I', bpb, 36)[0], struct.unpack_from('<I', bpb, 44)[0]
    assert bps == SEC and bpb[82:87] == b'FAT32' and struct.unpack_from('<H', bpb, 22)[0] == 0, 'not FAT32'
    fat0 = part + rsv
    data0 = part + rsv + nfat * fatsz

    def fat_get(c):
        s, o = fat0 + c * 4 // SEC, c * 4 % SEC
        return struct.unpack_from('<I', rd(s), o)[0] & 0x0fffffff

    def fat_set(c, v):
        for f in range(nfat):
            s, o = fat0 + f * fatsz + c * 4 // SEC, c * 4 % SEC
            struct.pack_into('<I', rd(s), o, v)

    def csec(c):
        return data0 + (c - 2) * spc

    def alloc():
        c = 3
        while fat_get(c) != 0:
            c += 1
        fat_set(c, 0x0fffffff)
        for i in range(spc):
            sec[csec(c) + i] = bytearray(SEC)
        return c

    def entry(nm, attr, cl, size):
        e = bytearray(32)
        e[0:11] = nm
        e[11] = attr
        struct.pack_into('<HHH', e, 14, 0, DATE, DATE)           # create time, create date, access date
        struct.pack_into('<H', e, 20, cl >> 16)
        struct.pack_into('<HH', e, 22, 0, DATE)                  # write time, write date
        struct.pack_into('<H', e, 26, cl & 0xffff)
        struct.pack_into('<I', e, 28, size)
        return e

    def free_slot(cl):
        """First free 32-byte slot of a one-cluster directory: (sector, offset)."""
        for i in range(spc):
            s = rd(csec(cl) + i)
            for o in range(0, SEC, 32):
                if s[o] in (0, 0xe5):
                    return csec(cl) + i, o
        raise SystemExit('directory full')

    def find(cl, nm):
        for i in range(spc):
            s = rd(csec(cl) + i)
            for o in range(0, SEC, 32):
                if s[o] != 0 and s[o] != 0xe5 and s[o + 11] != 0x0f and bytes(s[o:o + 11]) == nm:
                    return (s[o + 20] | s[o + 21] << 8) << 16 | (s[o + 26] | s[o + 27] << 8)
        return None

    dn, fn = name83(ddir), name83(dname)
    dcl = find(root, dn)
    if dcl is None:
        dcl = alloc()
        s, o = free_slot(root)
        rd(s)[o:o + 32] = entry(dn, 0x10, dcl, 0)
        d = rd(csec(dcl))
        d[0:32] = entry(b'.          ', 0x10, dcl, 0)
        d[32:64] = entry(b'..         ', 0x10, 0, 0)
    assert find(dcl, fn) is None, 'file exists'
    need = max(1, -(-len(data) // (spc * SEC)))
    chain = [alloc() for _ in range(need)]
    for a, b in zip(chain, chain[1:]):
        fat_set(a, b)
    for i, c in enumerate(chain):
        chunk = data[i * spc * SEC:(i + 1) * spc * SEC].ljust(spc * SEC, b'\0')
        for j in range(spc):
            sec[csec(c) + j] = bytearray(chunk[j * SEC:(j + 1) * SEC])
    s, o = free_slot(dcl)
    rd(s)[o:o + 32] = entry(fn, 0x20, chain[0], len(data))
    keys = sorted(k for k, v in sec.items() if any(v) or k in (0, part))
    out = bytearray(b'OMSD' + struct.pack('<III', 1, total, len(keys)))
    for k in keys:
        out += struct.pack('<I', k) + bytes(sec[k])
    open(outp, 'wb').write(out)
    print(f'{ddir}/{dname}: {len(data)} B in cluster {chain[0]}, folder cluster {dcl}; partition at sector {part}; {len(keys)} sectors kept')


main()

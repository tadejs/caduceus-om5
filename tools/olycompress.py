#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""olycompress containers in an OM-5 main code block: find, expand, repack.

The container format (a 16-byte magic starting "olycompress", u32 packed size
at +16, u32 raw size at +20, 8 zero bytes, then one LZ4 block of `packed`
bytes at +32; the container sits at the start of the span it expands to and
the rest of the span is 0xFF) is taken from the format notes in om-emu's
emu/src/fw/expand.mjs. This is our own implementation of it.

    python -I tools/olycompress.py expand IN OUT
    python -I tools/olycompress.py info IN
    python -I tools/olycompress.py roundtrip IN

Library: find, lz4_decompress, lz4_compress, expand, repack.
"""
import struct
import sys
import hashlib
from collections import namedtuple

MAGIC = b"olycompress"
HEAD = 32

Container = namedtuple("Container", "offset packed raw header")


class OlyError(ValueError):
    pass


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def find(img):
    """Return the containers in img, in file order."""
    out = []
    p = img.find(MAGIC)
    while p >= 0:
        if p + HEAD <= len(img):
            packed, raw = _u32(img, p + 16), _u32(img, p + 20)
            if p + HEAD + packed > len(img) or p + raw > len(img):
                raise OlyError("container at 0x%x overruns the image" % p)
            if img[p + 24:p + 32] != bytes(8):
                raise OlyError("container at 0x%x: header bytes +24..+32 not zero" % p)
            out.append(Container(p, packed, raw, bytes(img[p:p + HEAD])))
        p = img.find(MAGIC, p + 1)
    return out


def lz4_decompress(block, raw_size):
    """Decode one LZ4 block to exactly raw_size bytes; reject anything malformed."""
    src = bytes(block)
    n = len(src)
    out = bytearray()
    i = 0
    while i < n:
        token = src[i]
        i += 1
        ln = token >> 4
        if ln == 15:
            while True:
                if i >= n:
                    raise OlyError("truncated literal length")
                b = src[i]
                i += 1
                ln += b
                if b != 255:
                    break
        if i + ln > n:
            raise OlyError("literals overrun the input")
        if len(out) + ln > raw_size:
            raise OlyError("literals overrun the output size")
        out += src[i:i + ln]
        i += ln
        if i == n:
            break  # last sequence: literals only
        if i + 2 > n:
            raise OlyError("truncated match offset")
        off = src[i] | src[i + 1] << 8
        i += 2
        ml = token & 15
        if ml == 15:
            while True:
                if i >= n:
                    raise OlyError("truncated match length")
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        if off == 0 or off > len(out):
            raise OlyError("bad match offset %d at output %d" % (off, len(out)))
        if len(out) + ml > raw_size:
            raise OlyError("match overruns the output size")
        s = len(out) - off
        if off >= ml:
            out += out[s:s + ml]
        else:
            pat = bytes(out[s:])
            out += (pat * (ml // off + 1))[:ml]
    if len(out) != raw_size:
        raise OlyError("decoded %d bytes, expected %d" % (len(out), raw_size))
    return bytes(out)


def _put_len(out, v):
    while v >= 255:
        out.append(255)
        v -= 255
    out.append(v)


def lz4_compress(data, chain_depth=16):
    """Encode data as one conformant LZ4 block (hash chains, greedy matching)."""
    d = bytes(data)
    n = len(d)
    out = bytearray()
    if n < 13:  # too short for any match: all literals
        out.append(min(n, 15) << 4)
        if n >= 15:
            _put_len(out, n - 15)
        out += d
        return bytes(out)
    last_start = n - 12   # a match may start at or before this
    match_end = n - 5     # a match must end at or before this
    head = {}
    prev = {}
    anchor = 0
    i = 0
    skip = 0
    while i <= last_start:
        key = d[i:i + 4]
        cand = head.get(key, -1)
        prev[i] = cand
        head[key] = i
        best_len = 0
        best_pos = 0
        depth = chain_depth
        limit = match_end - i
        while cand >= 0 and i - cand <= 65535 and depth:
            depth -= 1
            # quick reject: must beat best_len
            if best_len == 0 or d[cand + best_len:cand + best_len + 1] == d[i + best_len:i + best_len + 1]:
                m = 4
                step = 256
                while m + step <= limit and d[cand + m:cand + m + step] == d[i + m:i + m + step]:
                    m += step
                while step > 1:
                    step >>= 1
                    if m + step <= limit and d[cand + m:cand + m + step] == d[i + m:i + m + step]:
                        m += step
                m = min(m, limit)
                if m > best_len:
                    best_len, best_pos = m, cand
                    if m >= limit:
                        break
            cand = prev.get(cand, -1)
        if best_len < 4:
            skip += 1
            i += 1 + (skip >> 6)  # accelerate through incompressible runs
            continue
        skip = 0
        lit = i - anchor
        ml = best_len - 4
        out.append(min(lit, 15) << 4 | min(ml, 15))
        if lit >= 15:
            _put_len(out, lit - 15)
        out += d[anchor:i]
        out += struct.pack("<H", i - best_pos)
        if ml >= 15:
            _put_len(out, ml - 15)
        end = i + best_len
        # index positions inside the match (all when short, sparse when long)
        stepi = 1 if best_len <= 64 else 8
        j = i + 1
        while j < end and j <= last_start:
            k = d[j:j + 4]
            prev[j] = head.get(k, -1)
            head[k] = j
            j += stepi
        i = anchor = end
    lit = n - anchor
    out.append(min(lit, 15) << 4)
    if lit >= 15:
        _put_len(out, lit - 15)
    out += d[anchor:]
    return bytes(out)


def expand(img):
    """The run-time image: every container decoded in place (same length)."""
    out = bytearray(img)
    for c in find(img):
        data = lz4_decompress(img[c.offset + HEAD:c.offset + HEAD + c.packed], c.raw)
        out[c.offset:c.offset + c.raw] = data
    return bytes(out)


def repack(run_image, template_img, only=None):
    """Re-encode each span of run_image into the template's container layout.

    only: optional set of container offsets to re-encode; the other spans are
    copied unchanged from template_img (their stock compressed bytes)."""
    out = bytearray(run_image)
    if len(run_image) != len(template_img):
        raise OlyError("run image and template differ in length")
    for c in find(template_img):
        if only is not None and c.offset not in only:
            out[c.offset:c.offset + c.raw] = template_img[c.offset:c.offset + c.raw]
            continue
        packed = lz4_compress(run_image[c.offset:c.offset + c.raw])
        if HEAD + len(packed) > c.raw:
            raise OlyError("container at 0x%x: %d packed bytes do not fit the %d-byte span"
                           % (c.offset, HEAD + len(packed), c.raw))
        head = c.header[:16] + struct.pack("<II", len(packed), c.raw) + bytes(8)
        span = head + packed
        out[c.offset:c.offset + c.raw] = span + b"\xff" * (c.raw - len(span))
    return bytes(out)


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def main(argv):
    if len(argv) >= 2 and argv[0] == "expand" and len(argv) == 3:
        run = expand(_read(argv[1]))
        with open(argv[2], "wb") as f:
            f.write(run)
        print("%s  %d bytes" % (hashlib.sha256(run).hexdigest(), len(run)))
    elif len(argv) == 2 and argv[0] == "info":
        img = _read(argv[1])
        cs = find(img)
        print("%d container(s)" % len(cs))
        for c in cs:
            print("offset 0x%x  packed %d  raw %d  header %s" % (c.offset, c.packed, c.raw, c.header.hex()))
    elif len(argv) == 2 and argv[0] == "roundtrip":
        img = _read(argv[1])
        run = expand(img)
        rep = repack(run, img)
        run2 = expand(rep)
        for c in find(rep):
            print("container 0x%x: stock %d -> ours %d packed" % (
                c.offset, next(s.packed for s in find(img) if s.offset == c.offset), c.packed))
        ok = run2 == run
        print("roundtrip", "OK" if ok else "MISMATCH", hashlib.sha256(run).hexdigest())
        return 0 if ok else 1
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

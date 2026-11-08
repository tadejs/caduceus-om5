#!/usr/bin/env python3
"""Convert an OM-5 package into an om-emu patch file (stdlib only).

om-emu applies byte patches to the stock main block as stored (LZ4 spans still
compressed) and checks each patch's old bytes. This diffs the package's decoded
main block against the stock decoded main block and emits every differing run
as {block, offset, old, new} with base64 fields (offset = block offset).

usage: python -I tools/om5_omemu_patch.py PACKAGE OUT.json [--stock FILE] [--gap N]
"""
import argparse
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "external" / "oly_fw_analysis" / "tools"))
import oly_container  # noqa: E402

STOCK = ROOT / "analysis" / "om5_1200" / "00_0100_40780020.decoded.bin"


def runs(a, b, gap):
    """Differing [start, end) runs of equal-length a, b; runs closer than gap merge."""
    out = []
    n = len(a)
    i = 0
    while i < n:
        # fast skip over equal 4 KiB chunks
        if a[i:i + 4096] == b[i:i + 4096] and i + 4096 <= n:
            i += 4096
            continue
        if a[i] == b[i]:
            i += 1
            continue
        start = i
        last = i
        j = i + 1
        while j < n and j - last <= gap:
            if a[j] != b[j]:
                last = j
            j += 1
        out.append((start, last + 1))
        i = last + 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("package")
    ap.add_argument("out")
    ap.add_argument("--stock", default=str(STOCK))
    ap.add_argument("--gap", type=int, default=64)
    a = ap.parse_args()
    blocks = oly_container.parse(Path(a.package).read_bytes())
    new = blocks[0].payload
    old = Path(a.stock).read_bytes()
    if len(new) != len(old):
        sys.exit(f"main block size differs: package {len(new)}, stock {len(old)}")
    rs = runs(old, new, a.gap)
    if not rs:
        sys.exit("package main block equals stock: nothing to patch")
    patches = []
    for s, e in rs:
        patches.append({
            "block": 0, "offset": s,
            "old": base64.b64encode(old[s:e]).decode(),
            "new": base64.b64encode(new[s:e]).decode(),
            "label": f"{a.package.replace(chr(92), '/').split('/')[-1]} 0x{s:x}+{e - s}",
        })
    Path(a.out).write_text(json.dumps({"patches": patches}))
    total = sum(e - s for s, e in rs)
    print(f"{len(rs)} runs, {total} bytes (block {len(old)} bytes) -> {a.out}")
    for s, e in rs[:10]:
        print(f"  0x{s:08x} +{e - s}")
    if len(rs) > 10:
        print(f"  ... {len(rs) - 10} more")


if __name__ == "__main__":
    main()

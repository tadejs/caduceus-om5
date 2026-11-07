#!/usr/bin/env python3
"""Verify patches/om5_fw_version (OM-5). Offline; emulator only. Run in WSL (the fishcu engine is a Linux binary):

  ~/ep7fw-toolchains/venv/bin/python -I tools/om5_verify_fw_version.py [patches/om5_fw_version/out/edits.json]

  D  diff: tools/build_package.py --body om5 builds the package (LZ4 span re-encoded); its decoded+expanded main equals
     stock + the spec's edits, differs from stock only at those bytes, only the section-6 span is re-encoded, restore
     == stock; the spec equals a fresh build of build.py (stock bytes asserted again).
  T  targets: the two hooked calls were stock calls to the text setter 0x60d90341 and now call fw_val with the same
     register list; the descriptor was {4, 1725, 0x61241c78}; fw_val's own call goes to the setter; the label is ASCII.
  X  refs: every reference to the touched bytes is accounted for (stock image): no word/branch/call targets the interior of
     a hooked call, the descriptor is referenced only by the table word 0x61241d08, nothing points into the section-7 FF
     tail, the setter's other callers are untouched.
  E  emulator: the view's two Body value steps (real `mov (4,sp),a1; add OFF,a1; call`) on stock and patched with the
     text setter stubbed to capture (widget, string): non-empty field -> L"<ver>", empty field and NULL pass through;
     widget, d2/d3/a2/a3, sp and the caller stack identical to stock.
"""
import argparse, hashlib, json, re, struct, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'patches'))
sys.path.insert(0, str(ROOT / 'patches/om5_fw_version'))
sys.path.insert(0, str(ROOT / 'external/oly_fw_analysis/tools'))
import bodies  # noqa: E402
import build_package as BP  # noqa: E402
import olycompress as oz  # noqa: E402
import oly_container as oc  # noqa: E402
import om5_common as oc5  # noqa: E402
import build as FB  # noqa: E402  (patches/om5_fw_version/build.py)
from penf_emulator import Region, execute  # noqa: E402

BASE = oc5.BASE
OM5 = bodies.OM5
OBJ, STACK, CAP = 0x18100000, 0x18010000, 0x18040000


def words(img, val):
    pat = struct.pack('<I', val)
    out, i = [], -1
    while (i := img.find(pat, i + 1)) >= 0:
        out.append(i + BASE)
    return out


def calls_to(img, target):
    """Addresses of 7-byte `call target,[...],imm` (dd disp32 f0/f1 imm) forms and 5-byte `dd disp32` ones."""
    out = []
    for m in re.finditer(b'\xdd', img):
        o = m.start()
        if o + 7 <= len(img) and (int.from_bytes(img[o + 1:o + 5], 'little') + o + BASE) & 0xffffffff == target:
            out.append(o + BASE)
    return out


def objdump(lo, hi, img_path):
    sh = (f'set -e\ncd {oc5.wsl_path(ROOT)}\n$HOME/ep7fw-toolchains/mn10300/bin/mn10300-elf-objdump -b binary -m am33-2 -D '
          f'--adjust-vma={BASE:#x} --start-address={lo:#x} --stop-address={hi:#x} {oc5.wsl_path(img_path)}\n')
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        return oc5.run_sh(sh, d)


def asm(src, at, syms=None):
    out, _ = oc5.assemble(f' .section .s,"ax"\n{src}\n', {'.s': at}, syms or {})
    return out['.s']


def wstr(b):
    return b.decode('utf-16le').split('\0')[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('spec', type=Path, nargs='?', default=ROOT / 'patches/om5_fw_version/out/edits.json')
    a = ap.parse_args()
    spec = json.loads(a.spec.read_text())
    fv = spec['fw_version']
    ver = fv['mod_version']
    stock = oc5.image()
    edits = [(int(e['addr'], 16), bytes.fromhex(e['new'])) for e in spec['edits']]
    patched = bytearray(stock)
    for ad, new in edits:
        patched[ad - BASE:ad - BASE + len(new)] = new
    patched = bytes(patched)

    # ---- D
    fresh, syms = FB.build(ver, save_claims=False)
    assert fresh['edits'] == spec['edits'], 'spec differs from a fresh build'
    stock_file = (ROOT / 'stock_firmware/om5' / OM5.stock_name).read_bytes()
    assert hashlib.sha256(stock_file).hexdigest() == OM5.stock_sha
    pkg, restore, info = BP.build(spec, stock_file, OM5)
    blocks = {b.kind: b for b in oc.parse(pkg)}
    exp = oz.expand(blocks[0x100].payload)
    assert exp == patched, 'package main (expanded) != stock + edits'
    d = [i for i in range(len(stock)) if stock[i] != exp[i]]
    assert all(any(ad - BASE <= i < ad - BASE + len(n) for ad, n in edits) for i in d)
    stock_blocks = {b.kind: b for b in oc.parse(stock_file)}
    for k, b in blocks.items():
        if k != 0x100:
            assert b.payload == stock_blocks[k].payload, hex(k)
    for rb in oc.parse(restore):
        assert rb.payload == stock_blocks[rb.kind].payload
    assert info['recompressed_spans'] == ['0xc9d7e0'], info['recompressed_spans']
    print(f'D  package {hashlib.sha256(pkg).hexdigest()[:16]}: expanded main == stock + {len(spec["edits"])} edits, '
          f'{len(d)} B differ, re-encoded span(s) {info["recompressed_spans"]} (section 6), restore == stock, spec == fresh build')

    # ---- T
    setter = FB.SETTER
    fw_val = int(fv['fw_val'], 16)
    for n, (s, _) in FB.SITES.items():
        st = oc5.stock(s, 7)
        assert st[0] == 0xdd and st[5:] == bytes([0xf0, 24]) and (int.from_bytes(st[1:5], 'little') + s) & 0xffffffff == setter
        pa = patched[s - BASE:s - BASE + 7]
        assert pa[0] == 0xdd and pa[5:] == bytes([0xf0, FB.FRAME]) and (int.from_bytes(pa[1:5], 'little') + s) & 0xffffffff == fw_val
    assert oc5.stock(FB.DESC, 8) == FB.DESC_STOCK
    assert struct.unpack('<HHI', patched[FB.DESC - BASE:FB.DESC - BASE + 8]) == (5, 1725, int(fv['label_at'], 16))
    lab = patched[int(fv['label_at'], 16) - BASE:][:16]
    assert lab.split(b'\0')[0] == b'Caduceus'
    ext = patched[fw_val - BASE:fw_val - BASE + fv['ext_len']]
    callat = [fw_val + o for o in range(len(ext) - 6) if ext[o] == 0xdd and
              (int.from_bytes(ext[o + 1:o + 5], 'little') + fw_val + o) & 0xffffffff == setter and ext[o + 5:o + 7] == bytes([0xf0, 24])]
    assert len(callat) == 1, callat
    print(f'T  2 hooked calls: stock `call 0x60d90341,[d2,d3,a2,a3],24` -> `call fw_val {fw_val:#x},[d2,d3,a2,a3],{FB.FRAME}`; '
          f'descriptor {{4,1725,0x61241c78}} -> {{5,1725,label {int(fv["label_at"], 16):#x}}} "Caduceus"; fw_val calls the setter at {callat[0]:#x}')

    # ---- X
    lo_t = oc5.section_of(FB.EXT_AT).tail[0]
    for n, (s, _) in FB.SITES.items():
        for t in range(s, s + 7):
            assert not words(stock, t), (hex(t), [hex(x) for x in words(stock, t)])
    dis = objdump(0x60dc5700, 0x60dc5a00, oc5.IMAGE)
    for n, (s, _) in FB.SITES.items():
        for t in range(s, s + 7):
            hits = re.findall(r'\b0x%x\b' % t, dis)
            assert not hits, (hex(t), hits)
    assert words(stock, FB.DESC) == [0x61241d08], [hex(x) for x in words(stock, FB.DESC)]
    for t in range(FB.DESC + 1, FB.DESC + 8):
        assert not words(stock, t)
    tail_end = oc5.section_of(FB.EXT_AT).end
    refs = [(t, [hex(x) for x in words(stock, t)]) for t in range(lo_t, lo_t + 0x80, 1) if words(stock, t)]
    assert not refs, refs
    sc_stock, sc_patch = calls_to(stock, setter), calls_to(patched, setter)
    removed = sorted(set(sc_stock) - set(sc_patch))
    assert removed == sorted(s for s, _ in FB.SITES.values()) and not set(sc_patch) - set(sc_stock) - set(callat), (removed, sc_patch)
    assert patched[:FB.DESC - BASE] != b'' and sum(1 for x, y in zip(stock, patched) if x != y) == len(d)
    for s in (oc5.section_of(FB.DESC),):
        assert s.compressed and s.idx == 6
    print(f'X  no word/branch targets the 2x7 B call interiors (+objdump of 0x60dc5700-0x60dc5a00); descriptor referenced only by '
          f'table word 0x61241d08; no word points into the first 0x80 B of the section-7 FF tail ({lo_t:#x}..{tail_end:#x}); '
          f'{len(sc_stock)} setter calls in stock, only the 2 hooked ones changed (fw_val adds 1)')

    # ---- E
    def setter_stub():
        return asm(f' cmp 0,a1\n beq 2f\n mov {CAP:#x},a2\n1:\n movhu (a1),d0\n movhu d0,(a2)\n add 2,a1\n add 2,a2\n cmp 0,d0\n bne 1b\n2:\n'
                   f' mov a0,({CAP + 0xfc:#x})\n mov 1,d0\n mov d0,({CAP + 0x100:#x})\n ret [d2,d3,a2,a3],24', setter)

    def step(image, site, off, field):
        """The view's Body value step: stack (4,sp) = block; `mov (4,sp),a1; add off,a1; call` at site-6..site+7."""
        im = bytearray(image)
        c = setter_stub()
        im[setter - BASE:setter - BASE + len(c)] = c
        blk = bytearray(0x500)
        if field is not None:
            blk[off:off + 2 * len(field) + 2] = (field + '\0').encode('utf-16le')
        stack = bytearray(0x1100)
        base_ptr = OBJ if field is not None else (-off) & 0xffffffff           # NULL string: block + off == 0
        struct.pack_into('<I', stack, 0x1000 + 4, base_ptr)
        stop = site + 7
        r = execute([Region('img', BASE, bytes(im), 'rx'), Region('obj', OBJ, bytes(blk), 'rw'),
                     Region('stack', STACK, bytes(stack), 'rw', capture=True), Region('cap', CAP, bytes(0x200), 'rw', capture=True)],
                    {'sp': STACK + 0x1000, 'pc': site - 6, 'a0': 0x5a5a0026, 'a3': 0x44, 'd2': 0x22, 'd3': 0x33, 'a2': 0x55},
                    stop, limit=100_000, timeout=60)
        if r['fault'] or r['registers']['pc'] != stop:
            raise SystemExit(f'view step fault {r["fault"]} pc {r["registers"]["pc"]:#x}')
        mem = {m['name']: bytes.fromhex(m['hex']) for m in r['memory']}
        cap = mem['cap']
        regs = {k: r['registers'][k] for k in ('d2', 'd3', 'a2', 'a3', 'sp')}
        called = struct.unpack_from('<I', cap, 0x100)[0]
        return called, wstr(cap[:0xfc]), struct.unpack_from('<I', cap, 0xfc)[0], regs, mem['stack'][0x1000:]

    n = 0
    for s, off in ((0x60dc58f6, 940), (0x60dc5952, 300)):
        for field in ('1.1', '12.34', '', None):
            cs, ts, ws, rs, ks = step(stock, s, off, field)
            cp, tp, wp, rp, kp = step(patched, s, off, field)
            assert cs == cp == 1 and ws == wp == 0x5a5a0026 and rs == rp and ks == kp, (hex(s), field)
            assert ts == tp or (field and tp == ver and ts == field), (hex(s), field, ts, tp)
            assert (tp == ver) == bool(field), (field, tp)
            n += 1
            if field in ('1.1', '', None):
                print(f'E  {s:#x} +{off} field {field!r}: stock setter got {ts!r}  patched setter got {tp!r}')
    print(f'E  {n}/{n} runs: the real setter entry is called once, widget/d2/d3/a2/a3/sp/caller stack identical to stock')
    print('ALL PASS')


if __name__ == '__main__':
    main()

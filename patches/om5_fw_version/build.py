#!/usr/bin/env python3
"""OM-5 port of patches/fw_version: Setup > Firmware shows "Caduceus" as the Body row label and the mod version as the
Body row value. Display only. Offline; writes an edits.json fragment (body om5), needs WSL binutils for mn10300.

  python -I patches/om5_fw_version/build.py [--mod-version 0.1] [--out patches/om5_fw_version/out/edits.json]

Same design as patches/fw_version (E-P7), addresses found by tools/port_sigmatch.py and re-verified by disassembly
(docs/PORT_OM5.md); all run-time addresses of the expanded image (base 0x60021020):

  hook_v940 0x60dc58f6  Body value setter call, field +940 (two-version row)  -> call fw_val   (sec 6, LZ4 span)
  hook_v300 0x60dc5952  Body value setter call, field +300 (single-version row) -> call fw_val (sec 6, LZ4 span)
  desc      0x61241c7c  Body label descriptor {kind 4, id 1725, text ptr} -> {kind 5, id 1725, "Caduceus"} (sec 6)
  ext7      sec 7 FF tail (0x613f2b78..), 0x20 B guard left: fw_val, L"<ver>", "Caduceus"  (plain section)

fw_val(widget a0, wchar string a1): if a1 is non-NULL and its first char non-zero, a1 := L"<ver>"; then it tail-calls
the real text setter 0x60d90341 with the stock frame. Everything else (builder, copier, sprintf, updater) stays stock.
The label copier (0x60e638b8) handles descriptor kind 5 (ASCII text pointer) in stock, as on the E-P7.
"""
import argparse, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import om5_common as oc5  # noqa: E402

PATCH = 'om5_fw_version'
SRC = 'patches/om5_fw_version'
SETTER = 0x60d90341                       # text setter (widget a0, wchar string a1), call [d2,d3,a2,a3],24
SITES = {'hook_v940': (0x60dc58f6, '+940 (two-version Body value, widget 26)'),
         'hook_v300': (0x60dc5952, '+300 (single-version Body value, widget 18)')}
FRAME = 88                                # as the E-P7 fw_val
DESC = 0x61241c7c
DESC_STOCK = bytes.fromhex('0400bd06781c2461')        # {u16 kind 4, u16 id 1725, u32 text ptr 0x61241c78}
LABEL = 'Caduceus'
EXT_AT = 0x613f2ba0                       # section 7 (plain) FF tail starts 0x613f2b78; leave 0x28 B untouched
EXT_MAX = 0x100
MAXSFX = 27


def suffix(ver):
    if not re.fullmatch(r'v?\d+\.\d+[A-Za-z0-9.\-]*', ver):
        raise SystemExit(f'--mod-version {ver!r}: expected e.g. 0.1 or v0.1')
    t = ver[1:] if ver.startswith('v') else ver
    if len(t) > MAXSFX:
        raise SystemExit(f'suffix {t!r} longer than {MAXSFX}')
    return t


def source(ver):
    wide = ''.join(f' .short {ord(c)}\n' for c in ver) + ' .short 0\n'
    hooks = ''.join(f' .section .{n},"ax"\n call fw_val,[d2,d3,a2,a3],{FRAME}\n' for n in SITES)
    return f"""
 .section .ext7,"ax"
 .global fw_val
fw_val:
 cmp 0,a1
 beq 9f
 movhu (a1),d0
 cmp 0,d0
 beq 9f
 mov wver,a1                     /* non-empty Body value -> L"{ver}" */
9:
 call setter,[d2,d3,a2,a3],24
 ret [d2,d3,a2,a3],{FRAME}
 .balign 2
wver:
{wide}label:
 .asciz "{LABEL}"
 .balign 4
 .section .desc,"a"
 .short 5, 0x6bd
 .long label
{hooks}"""


def build(ver, save_claims=True):
    sfx = suffix(ver)
    # stock assertions before anything is emitted
    for n, (a, _) in SITES.items():
        stock = oc5.stock(a, 7)
        want = bytes([0xdd]) + ((SETTER - a) & 0xffffffff).to_bytes(4, 'little') + bytes([0xf0, 24])
        if stock != want:
            raise SystemExit(f'call site {a:#x} is not a stock setter call: {stock.hex()}')
    cl = oc5.Claims(PATCH)
    for n, (a, why) in SITES.items():
        cl.over(n, a, oc5.stock(a, 7), f'Body value setter call {why}', compressed=True)
    cl.over('desc', DESC, DESC_STOCK, 'Body row label descriptor', compressed=True)
    sec = cl.free('ext7', EXT_AT, EXT_MAX, 'fw_val, version string, label text')
    assert not sec.compressed and sec.idx == 7
    # the descriptor's text pointer must be the stock null cell, and the label ids must be the Body row
    assert oc5.stock(0x61241c78, 4) == bytes(4)
    place = {'.ext7': EXT_AT, **{f'.{n}': a for n, (a, _) in SITES.items()}, '.desc': DESC}
    out, syms = oc5.assemble(source(sfx), place, {'setter': SETTER}, cl)
    assert syms['fw_val'] == EXT_AT, hex(syms['fw_val'])
    n = len(out['.ext7'])
    cl.mine['ext7'] = (EXT_AT, n, cl.mine['ext7'][2])          # shrink the claim to the real size
    assert out['.desc'][:4] == bytes([5, 0, 0xbd, 6]) and int.from_bytes(out['.desc'][4:], 'little') == syms['label']
    edits = [oc5.edit(SITES[k][0], out[f'.{k}'], f'fw_version: Firmware screen {SITES[k][1]} setter call -> fw_val (value "{sfx}")', SRC)
             for k in SITES]
    edits.append(oc5.edit(EXT_AT, out['.ext7'], f'fw_version: fw_val ({n} B incl. L"{sfx}" and "{LABEL}"), section-7 FF tail', SRC))
    edits.append(oc5.edit(DESC, out['.desc'], f'fw_version: Firmware screen Body row label -> kind 5 text "{LABEL}" (display only)', SRC))
    if save_claims:
        cl.save()
    info = dict(mod_version=sfx, label=LABEL, sites=[hex(a) for a, _ in SITES.values()], desc=hex(DESC), ext_at=hex(EXT_AT),
                ext_len=n, fw_val=hex(syms['fw_val']), wver=hex(syms['wver']), label_at=hex(syms['label']), frame=FRAME)
    return oc5.fragment(edits, fw_version=info), syms


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--mod-version', default='0.1')
    ap.add_argument('--out', type=Path, default=HERE / 'out/edits.json')
    a = ap.parse_args()
    frag, _ = build(a.mod_version)
    oc5.write(a.out, frag)
    for e in frag['edits']:
        print(e['addr'], len(e['new']) // 2, 'B', e['new'][:48] + ('...' if len(e['new']) > 48 else ''))
    print(frag['fw_version'])

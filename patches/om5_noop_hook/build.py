#!/usr/bin/env python3
"""OM-5 camera step C4: a no-op hook at the half-press (S1) event. Offline; writes an edits.json fragment (body om5),
needs WSL binutils for mn10300.

  python -I patches/om5_noop_hook/build.py [--out patches/om5_noop_hook/out/edits.json]

Behaviour change: none. The camera runs our code at every S1 press, and the code only counts presses.

Site (OM-5 run-time addresses, base 0x60021020; the E-P7's SD-loader trigger 0x602c805f, found by byte pattern and
re-read by disassembly, docs/PORT_OM5.md "C4/C5"):

  hook   0x602ed768  `call 0x602ed895,[d2,d3,a2,a3],36` (cd 2d01 f0 24, 5 B) -> `jmp nh_hook`   (section 1, plain)
  nh_hook             section-1 FF tail (0x605a76c8..), SAME section as the hook, so it is loaded when the hook runs:
                        call 0x602ed895,[d2,d3,a2,a3],36   the displaced stock call, same frame, args and result
                        if (16,sp) == 0x02000100 (canonical TRIG1 press): u16 (0x6218a262) += 1
                        jmp 0x602ed76d                       the stock continuation (mov (16,sp),d0)
  counter u16 0x6218a262 = OlyHfwEventManager singleton 0x6218a1b4 +174 (padding between the u16 at +172 and the u32
          at +176). Boot-cleared .bss, never referenced (tools/om5_verify_noop_hook.py proves it).

Registers: the trampoline touches only d1 (clobbered by any call anyway); d0 and a0 (the callee's results) are kept.
"""
import argparse, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import om5_common as oc5  # noqa: E402

PATCH = 'om5_noop_hook'
SRC = 'patches/om5_noop_hook'
HOOK = 0x602ed768
HOOK_STOCK = bytes.fromhex('cd2d01f024')
TRIG = 0x602ed895                 # event tracker / canonicaliser, a0 = pointer to the event word (E-P7 0x602c818c)
RESUME = 0x602ed76d               # stock continuation after the call: mov (16,sp),d0
CONTEXT_AT = 0x602ed74e           # pinned: low16 == 0x100/0x101 gate, a0 = (12,sp), the call, reload
CONTEXT = bytes.fromhex('5810fae0fffffac80001c80e5810fae0fffffac80101c9095c0ccd2d01f0245810')
EV_PRESS = 0x02000100             # canonical TRIG1 pressed
COUNTER = 0x6218a262
TRAMP_AT = 0x605a7700             # section 1 FF tail starts 0x605a76c8; 0x38 B guard left
TRAMP_MAX = 0x40

SOURCE = f"""
 .section .tramp,"ax"
 .global nh_hook
nh_hook:
 call trig,[d2,d3,a2,a3],36       /* the displaced stock call */
 mov (16,sp),d1                   /* canonical event */
 cmp {EV_PRESS:#x},d1
 bne 1f
 movhu (counter),d1
 add 1,d1
 movhu d1,(counter)
1:
 jmp resume
 .section .hook,"ax"
 jmp nh_hook
"""


def build(save_claims=True):
    oc5.assert_stock(CONTEXT_AT, CONTEXT)
    oc5.assert_stock(HOOK, HOOK_STOCK)
    assert (HOOK + int.from_bytes(HOOK_STOCK[1:3], 'little', signed=True)) == TRIG
    assert HOOK + 5 == RESUME
    cl = oc5.Claims(PATCH, exclusive_with=('om5_sd_probe',))
    sec = cl.over('hook', HOOK, HOOK_STOCK, 'S1 event tracker call (call TRIG,[d2,d3,a2,a3],36)')
    assert sec.idx == 1 and not sec.compressed
    t = cl.free('tramp', TRAMP_AT, TRAMP_MAX, 'nh_hook trampoline')
    assert t.idx == sec.idx and TRAMP_AT >= t.tail[0]
    out, syms = oc5.assemble(SOURCE, {'.tramp': TRAMP_AT, '.hook': HOOK},
                             {'trig': TRIG, 'resume': RESUME, 'counter': COUNTER}, cl)
    assert syms['nh_hook'] == TRAMP_AT, hex(syms['nh_hook'])
    n = len(out['.tramp'])
    assert len(out['.hook']) == 5 and out['.hook'][0] == 0xdc
    cl.mine['tramp'] = (TRAMP_AT, n, cl.mine['tramp'][2])
    edits = [oc5.edit(HOOK, out['.hook'], 'noop_hook: S1 event tracker call -> jmp nh_hook (counts presses, no behaviour change)', SRC),
             oc5.edit(TRAMP_AT, out['.tramp'], f'noop_hook: nh_hook ({n} B), section-1 FF tail', SRC)]
    if save_claims:
        cl.save()
    info = dict(hook=hex(HOOK), tramp=hex(TRAMP_AT), tramp_len=n, trig=hex(TRIG), resume=hex(RESUME), ev_press=hex(EV_PRESS),
                counter=hex(COUNTER), context_at=hex(CONTEXT_AT))
    return oc5.fragment(edits, noop_hook=info, patch=PATCH), syms


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path, default=HERE / 'out/edits.json')
    a = ap.parse_args()
    frag, _ = build()
    oc5.write(a.out, frag)
    for e in frag['edits']:
        print(e['addr'], len(e['new']) // 2, 'B', e['new'][:48] + ('...' if len(e['new']) > 48 else ''))
    print(frag['noop_hook'])

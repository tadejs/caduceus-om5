#!/usr/bin/env python3
"""OM-5 COLOR 1-3 back end (batch 1, emulator only): the profile's 12 saturation values reach the capture. Offline; writes an
edits.json fragment (body om5), needs WSL binutils for mn10300.

  python -I patches/om5_color_core/build.py [--out patches/om5_color_core/out/edits.json]

What it does (OM-5 run-time addresses, base 0x60021020; docs/PORT_OM5.md "COLOR 1-3 back end"):

  The capture-parameter handler 0x614ce341 (E-P7 0x602a84xx) fills cap+28727 (picture mode, property 38017), cap+28757 (profile
  number, property 63153 for mode 11) and cap+28732.. (12 saturation halfwords) from the *effective* sector array, property
  63210 (settings 910-921; E-P7 915-926). On the E-P7 the effective array holds the selected profile's values because the
  picture-parameter builder's handlers 54-56 copy them in when COLOR n is chosen; the OM-5 builder sends modes 54-59 to its
  default (jump table 0x6049a4e0), so the array would stay whatever it was. This patch takes the values from the profile store
  (property 63226, settings 922-957, 3 profiles x 12) instead, when and only when cap mode == 11 and the profile is 1..3:

  hook   0x614ce690  `call 0x6005fa63,[],4` (getptr(d0 = property key) -> a0 = data pointer), 7 B, after `mov 63210,d0`
                     -> `jmp cc_hook; nop; nop`
  cc_hook            section-8 FF tail (the hook's own section, so it is loaded when the hook runs)
                       mode (cap+28727) == 11 and 1 <= profile (cap+28757) <= 3:
                         a0 = getptr(63226) + (profile-1)*24         (a0 == 0: fall back to stock)
                       else: the displaced stock call getptr(63210)
                       jmp 0x614ce697 (the stock continuation, `clr d0`)

Registers: d0, d1, a0, a1 are scratch at the continuation (d0 cleared, a1 and d2 reloaded, d1 loaded in the loop); d2 is
callee-saved across getptr and cleared at 0x614ce698; d3 (= &cap[28727]) and a2/a3 are untouched.
"""
import argparse, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import om5_common as oc5  # noqa: E402

PATCH = 'om5_color_core'
SRC = 'patches/om5_color_core'
HOOK = 0x614ce690
HOOK_STOCK = bytes.fromhex('ddd313b9fe0004')       # call 0x6005fa63,[],4
GETPTR = 0x6005fa63                                # property data pointer: d0 = key -> a0 (0 if absent); `ret [],4`
RESUME = 0x614ce697
CONTEXT_AT = 0x614ce659                            # pinned: the mode/profile/S section of the handler (0x614ce659..0x614ce6ac)
CONTEXT = oc5.stock(0x614ce659, 0x614ce6ac - 0x614ce659)
CONTEXT_SHA = None
TRAMP_AT = 0x615e9860                              # section 8 FF tail starts 0x615e9848; 0x18 B guard
TRAMP_MAX = 0x50
KEY_EFFECTIVE, KEY_PROFILES, MODE_COLOR = 63210, 63226, 11
OFF_MODE, OFF_PROFILE = 28727, 28757

SOURCE = f"""
 .section .tramp,"ax"
 .global cc_hook
cc_hook:
 movbu ({OFF_MODE},a3),d0
 cmp {MODE_COLOR},d0
 bne 1f
 movbu ({OFF_PROFILE},a3),d2
 add -1,d2                        /* profile 1..3 -> 0..2; 0 -> 0xffffffff */
 cmp 2,d2
 bhi 1f
 mov {KEY_PROFILES},d0
 call getptr,[],4
 cmp 0,a0
 beq 1f
 mov d2,d0
 add d2,d0
 add d2,d0                        /* 3 * index */
 asl 3,d0                         /* * 24 = 12 halfwords */
 add d0,a0
 jmp resume
1:
 mov {KEY_EFFECTIVE},d0
 call getptr,[],4                 /* the displaced stock call */
 jmp resume
 .section .hook,"ax"
 jmp cc_hook
 nop
 nop
"""


def build(save_claims=True):
    oc5.assert_stock(HOOK, HOOK_STOCK)
    assert HOOK + 7 == RESUME and HOOK + int.from_bytes(HOOK_STOCK[1:5], 'little', signed=True) == GETPTR
    assert oc5.stock(HOOK - 6, 6) == bytes.fromhex('fccceaf60000'), 'mov 63210,d0 not before the hook'
    assert oc5.stock(RESUME, 2) == bytes([0x00, 0x08]), 'continuation is not clr d0; clr d2'
    cl = oc5.Claims(PATCH, exclusive_with=('om5_sd_probe', 'om5_noop_hook'))
    sec = cl.over('hook', HOOK, HOOK_STOCK, 'getptr(63210) call in the capture handler -> jmp cc_hook')
    assert sec.idx == 8 and not sec.compressed
    t = cl.free('tramp', TRAMP_AT, TRAMP_MAX, 'cc_hook: profile saturation source for mode 11')
    assert t.idx == sec.idx and TRAMP_AT >= t.tail[0]
    out, syms = oc5.assemble(SOURCE, {'.tramp': TRAMP_AT, '.hook': HOOK},
                             {'getptr': GETPTR, 'resume': RESUME}, cl)
    assert syms['cc_hook'] == TRAMP_AT, hex(syms['cc_hook'])
    n = len(out['.tramp'])
    assert len(out['.hook']) == 7 and out['.hook'][0] == 0xdc and out['.hook'][5:] == b'\xcb\xcb'
    cl.mine['tramp'] = (TRAMP_AT, n, cl.mine['tramp'][2])
    edits = [oc5.edit(HOOK, out['.hook'], 'color_core: capture handler getptr(63210) call -> jmp cc_hook (mode 11 takes the profile store 63226)', SRC),
             oc5.edit(TRAMP_AT, out['.tramp'], f'color_core: cc_hook ({n} B), section-8 FF tail', SRC)]
    if save_claims:
        cl.save()
    info = dict(hook=hex(HOOK), tramp=hex(TRAMP_AT), tramp_len=n, getptr=hex(GETPTR), resume=hex(RESUME),
                key_effective=KEY_EFFECTIVE, key_profiles=KEY_PROFILES, off_mode=OFF_MODE, off_profile=OFF_PROFILE)
    return oc5.fragment(edits, color_core=info, patch=PATCH), syms


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path, default=HERE / 'out/edits.json')
    a = ap.parse_args()
    frag, _ = build()
    oc5.write(a.out, frag)
    for e in frag['edits']:
        print(e['addr'], len(e['new']) // 2, 'B', e['new'][:48] + ('...' if len(e['new']) > 48 else ''))
    print(frag['color_core'])

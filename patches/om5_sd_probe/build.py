#!/usr/bin/env python3
"""OM-5 camera step C5: SD probe at the first half-press. Offline; writes an edits.json fragment (body om5), needs WSL binutils.

  python -I patches/om5_sd_probe/build.py [--out patches/om5_sd_probe/out/edits.json]

Same hook as C4 (patches/om5_noop_hook: 0x602ed768, the S1 event tracker call -> jmp), so C5 REPLACES C4 (never both).
At the first gated S1 press after power-on it tries to read the 32-byte header of C:\\OM5MOD\\BUNDLES.BIN and records what
happened in the spare u16 0x6218a262 (OlyHfwEventManager singleton 0x6218a1b4 +174, the C4 counter word):

  0       not attempted yet (no S1 press, or a gate was not met: the next press tries again)
  0xFFFF  attempted, no valid file (missing card, missing file, short file, wrong magic)
  h       valid: h = block - pool + 1, the malloc(1024) block (kept, 1 KiB leaked per power-on) is at 0xa1e27d80 + h - 1,
          its first 32 bytes are the file header; magic 'OM5B' at +0.

One attempt per power-on (the state is set to 0xFFFF before any file call). Nothing else changes: no image store (the loaded
image 0x60021020..0x617e0fe0 is never written), no reader of the state exists. Gate (as the E-P7 SD loader v3 / OM-3 probe 2, OM-5
addresses): all 10 sections copied (0xa2693fa0 & 0x3ff == 0x3ff), get_tid ok and equal to the HLS_EVENT task id stored at
singleton+172 (0x6218a260), one medium status word (0x61e43338/40/48/50) == 0xF000 (MOUNT OK).

  hook 0x602ed768 -> jmp c5_hook (section 1, plain). Hook + gate: section-1 FF tail 0x605a7700 (same section as the hook,
  so it exists when the hook runs); loader: section-8 FF tail 0x615e9900, called only after the gate saw all 10 sections copied.
  Only the LOADED part of a section exists in RAM (boot copy ends 0x605a7800 / 0x615ea000, not the BCS span ends): om5_common enforces it.
"""
import argparse, struct, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import om5_common as oc5  # noqa: E402

PATCH = 'om5_sd_probe'
SRC = 'patches/om5_sd_probe'
HOOK, HOOK_STOCK = 0x602ed768, bytes.fromhex('cd2d01f024')
TRIG, RESUME = 0x602ed895, 0x602ed76d
CONTEXT_AT = 0x602ed74e
CONTEXT = bytes.fromhex('5810fae0fffffac80001c80e5810fae0fffffac80101c9095c0ccd2d01f0245810')
EV_PRESS = 0x02000100
STATE = 0x6218a262
HLS_TID = 0x6218a260                  # singleton + 172, written by the HLS_EVENT task creation 0x6085bed2
SECBITS = 0xa2693fa0                  # bit n-1 set after section n is copied (0x600463ff in sec_copy 0x6004635c)
MEDIA = (0x61e43338, 0x61e43340, 0x61e43348, 0x61e43350)    # per-medium status words, setter 0x6086a42a
# services (OM-5 run-time addresses; found by byte structure against the E-P7 wrappers, docs/PORT_OM5.md)
SERV = dict(malloc=0x60023818, free=0x6002384a, fopen=0x606ab541, fclose=0x606ab54a, fread=0x606ab6d0, ftell=0x606ab75e,
            fseek=0x606ab7c8, get_tid=0x600317b4)
FRAMES = dict(malloc=(), free=(), fopen=(), fclose=('d2', 'a2', 'a3'), fread=('d2', 'd3', 'a2', 'a3'), ftell=('d2', 'a2', 'a3'),
              fseek=('d2', 'd3', 'a2', 'a3'), get_tid=('a2',))
SIZES = dict(malloc=4, free=4, fopen=4, fclose=16, fread=24, ftell=16, fseek=20, get_tid=4)
POOL, POOL_END = 0xa1e27d80, 0xa1e32ac0          # class-1 pool (malloc 129..2048 B), free() range checks 0x6002385f/0x60023867
BUF = 1024
LIM = POOL_END - POOL - BUF + 1                   # h-1 <= LIM-1 keeps the whole block inside the pool
PATH = 'C:\\OM5MOD\\BUNDLES.BIN'
MAGIC = b'OM5B'
HDR = 32
CODE_AT, CODE_MAX = 0x605a7700, 0x100            # section 1 (hook's own section): loaded part ends 0x605a7800, FF tail from 0x605a76c8
LOAD_AT, LOAD_MAX = 0x615e9900, 0x200            # section 8 (idx 8, id 9): loaded part ends 0x615ea000, FF tail from 0x615e9848


def regs(n):
    return '[' + ','.join(FRAMES[n]) + ']'


def source():
    path_asm = PATH.replace('\\', '\\\\')
    magic = int.from_bytes(MAGIC, 'little')
    s = f"""
 .section .code,"ax"
 .global c5_hook
c5_hook:                          /* entered by jmp from {HOOK:#x}; frame = the event function's */
 call trig,[d2,d3,a2,a3],36       /* the displaced stock call */
 mov (16,sp),d0                   /* canonical event */
 cmp {EV_PRESS:#x},d0
 bne c5_back
 movhu (state),d0
 cmp 0,d0
 bne c5_back                      /* already attempted this power-on */
 call c5_gate,[d2,d3,a2,a3],36
c5_back:
 jmp resume

c5_gate:                          /* frame 36: (12,sp) = tid out */
 mov (secbits),d0
 and 0x3ff,d0
 cmp 0x3ff,d0
 bne c5_gout                      /* boot section copy not finished */
 mov sp,a0
 add 12,a0
 call get_tid,{regs('get_tid')},4
 cmp 0,d0
 bne c5_gout                      /* not a task context */
 movhu (hls_tid),d1
 cmp 0,d1
 beq c5_gout
 btst 0x8000,d1
 bne c5_gout                      /* HLS_EVENT not created yet (-1) */
 movhu (12,sp),d0
 cmp d1,d0
 bne c5_gout                      /* not the HLS_EVENT task */
 mov 61440,d1
 mov (ms_a),d0
 cmp d1,d0
 beq c5_go
 mov (ms_b),d0
 cmp d1,d0
 beq c5_go
 mov (ms_c),d0
 cmp d1,d0
 beq c5_go
 mov (ms_d),d0
 cmp d1,d0
 bne c5_gout                      /* no medium reports MOUNT OK: no file call, retry at the next press */
c5_go:
 mov 0xffff,d0
 movhu d0,(state)                 /* one attempt per power-on, whatever happens */
 call c5_load,[d2,d3,a2,a3],36
c5_gout:
 ret [d2,d3,a2,a3],36

 .section .load,"ax"
c5_load:                          /* frame 36: scratch 0..19; (0..11) left to callees; entered only after all 10 sections are copied */
 mov {BUF},d0
 call malloc,[],4
 mov a0,a2
 cmp 0,a2
 beq c5_out
 mov c5_path,a0
 mov c5_mode,a1
 call fopen,[],4
 mov a0,a3
 cmp 0,a3
 beq c5_free                      /* no file */
 mov a3,a0
 clr d0
 mov 1,d1                         /* whence 1 = END */
 call fseek,{regs('fseek')},20
 cmp 0,d0
 bne c5_close
 mov a3,a0
 call ftell,{regs('ftell')},16
 mov d0,d3                        /* file size */
 cmp {HDR},d3
 blt c5_close                     /* shorter than the header (signed: ftell -1 fails too) */
 mov a3,a0
 clr d0
 mov 2,d1                         /* whence 2 = SET */
 call fseek,{regs('fseek')},20
 cmp 0,d0
 bne c5_close
 mov a2,a0
 mov 1,d0
 mov {HDR},d1
 mov a3,a1
 call fread,{regs('fread')},24
 mov d0,d2                        /* bytes read */
 mov a3,a0
 call fclose,{regs('fclose')},16
 cmp {HDR},d2
 bne c5_free
 mov (a2),d0
 cmp {magic:#x},d0                /* 'OM5B' */
 bne c5_free
 mov a2,d0                        /* valid: publish the block instead of freeing it */
 add {-POOL & 0xffffffff:#x},d0   /* block - pool */
 cmp {LIM:#x},d0
 bcc c5_free                      /* not wholly inside the class-1 pool */
 btst 3,d0
 bne c5_free                      /* not 4-aligned */
 inc d0                           /* h = block - pool + 1 */
 movhu d0,(state)
 bra c5_out                       /* block kept for the rest of the power-on */
c5_close:
 mov a3,a0
 call fclose,{regs('fclose')},16
c5_free:
 mov a2,a0
 call free,[],4
c5_out:
 ret [d2,d3,a2,a3],36
c5_path:
 .asciz "{path_asm}"
c5_mode:
 .asciz "r"
 .balign 4
 .section .hook,"ax"
 jmp c5_hook
"""
    return s


def build(save_claims=True):
    oc5.assert_stock(CONTEXT_AT, CONTEXT)
    oc5.assert_stock(HOOK, HOOK_STOCK)
    assert HOOK + int.from_bytes(HOOK_STOCK[1:3], 'little', signed=True) == TRIG and HOOK + 5 == RESUME
    # free() class-1 range test and malloc size classes, from the stock image
    chk = oc5.stock(0x6002385f, 0x16)
    assert chk[0:2] == b'\xfc\xc8' and struct.unpack_from('<I', chk, 2)[0] == POOL and chk[6] == 0xc4, chk.hex()
    assert chk[8:10] == b'\xfc\xc8' and struct.unpack_from('<I', chk, 10)[0] == POOL_END and chk[14] == 0xc6, chk.hex()
    assert oc5.stock(0x60023821, 6).hex() == 'fac80008c506' and oc5.stock(0x60023827, 2).hex() == '8001'   # malloc 129..2048 -> class 1
    cl = oc5.Claims(PATCH, exclusive_with=('om5_noop_hook',))
    sec = cl.over('hook', HOOK, HOOK_STOCK, 'S1 event tracker call (call TRIG,[d2,d3,a2,a3],36)')
    t = cl.free('code', CODE_AT, CODE_MAX, 'c5 hook + gate')
    assert sec.idx == t.idx == 1 and not t.compressed and CODE_AT >= t.tail[0]
    t8 = cl.free('load', LOAD_AT, LOAD_MAX, 'c5 loader (malloc/fopen/fseek/ftell/fread/fclose/free)')
    assert t8.idx == 8 and not t8.compressed and LOAD_AT >= t8.tail[0]
    syms_in = dict(trig=TRIG, resume=RESUME, state=STATE, hls_tid=HLS_TID, secbits=SECBITS, ms_a=MEDIA[0], ms_b=MEDIA[1],
                   ms_c=MEDIA[2], ms_d=MEDIA[3], **{k: v for k, v in SERV.items()})
    out, syms = oc5.assemble(source(), {'.code': CODE_AT, '.load': LOAD_AT, '.hook': HOOK}, syms_in, cl)
    assert syms['c5_hook'] == CODE_AT
    n = len(out['.code'])
    assert len(out['.hook']) == 5 and out['.hook'][0] == 0xdc
    cl.mine['code'] = (CODE_AT, n, cl.mine['code'][2])
    nl = len(out['.load'])
    cl.mine['load'] = (LOAD_AT, nl, cl.mine['load'][2])
    edits = [oc5.edit(HOOK, out['.hook'], 'sd_probe: S1 event tracker call -> jmp c5_hook', SRC),
             oc5.edit(CODE_AT, out['.code'], f'sd_probe: c5 hook and gate ({n} B), section-1 FF tail', SRC),
             oc5.edit(LOAD_AT, out['.load'], f'sd_probe: c5 loader ({nl} B), section-8 FF tail', SRC)]
    if save_claims:
        cl.save()
    info = dict(hook=hex(HOOK), code=hex(CODE_AT), code_len=n, load=hex(LOAD_AT), load_len=nl, state=hex(STATE), path=PATH, magic=MAGIC.decode(), hdr=HDR,
                pool=[hex(POOL), hex(POOL_END)], buf=BUF, lim=hex(LIM), services={k: hex(v) for k, v in SERV.items()},
                symbols={k: hex(v) for k, v in syms.items()}, secbits=hex(SECBITS), hls_tid=hex(HLS_TID),
                media=[hex(m) for m in MEDIA], ev_press=hex(EV_PRESS), trig=hex(TRIG), resume=hex(RESUME), context_at=hex(CONTEXT_AT))
    return oc5.fragment(edits, sd_probe=info, patch=PATCH), syms


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path, default=HERE / 'out/edits.json')
    a = ap.parse_args()
    frag, _ = build()
    oc5.write(a.out, frag)
    for e in frag['edits']:
        print(e['addr'], len(e['new']) // 2, 'B', e['new'][:48] + ('...' if len(e['new']) > 48 else ''))
    print(frag['sd_probe']['code_len'], 'B hook+gate,', frag['sd_probe']['load_len'], 'B loader')

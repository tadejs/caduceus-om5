#!/usr/bin/env python3
"""Verify patches/om5_sd_probe (OM-5 camera step C5). Offline; emulator only. Run in WSL (the fishcu engine is a Linux binary):

  ~/ep7fw-toolchains/venv/bin/python -I tools/om5_verify_sd_probe.py [patches/om5_sd_probe/out/edits.json]
        [--package PATCHED.pkg.bin] [--mod-version 0.3]

  D  diff: shipped package = the probe + the firmware-screen label (patches/om5_fw_version, --mod-version); decoded+expanded main ==
     stock + the spec's edits and differs from stock at no other byte; only the section-6 span is re-encoded; other components
     identical; restore == stock; the spec equals a fresh run of build.py; --package is byte-identical to the one built here.
  T  targets: the hooked call, the stock bytes of every service entry (malloc/free/fopen/fclose/fread/ftell/fseek/get_tid), the
     section-copy bit setter (0xa2693fa0), the HLS_EVENT tid store (singleton+172), the media status setter; free()'s class-1 pool
     bounds and malloc's size classes; every call in the probe uses the dominant stock call frame of its service; the probe's only
     stores are the two halfword stores to the state word, no mul/div/loop macros.
  X  refs: as C4 (hook interior, claimed tail untouched, state word unreferenced and boot-cleared: tools/om5_verify_noop_hook.py's
     proof), nothing points into the code claim; the image is never stored to.
  E  emulator (fishcu MN103 engine), stock service entries replaced by logging stubs; the hook run from entry to the stock
     continuation, 22 scenarios: not a press / state already set / each gate condition failing -> no service call and the state is
     unchanged; malloc, fopen, fseek, ftell (short), fread (short), bad magic, block outside the pool, misaligned block -> state 0xFFFF
     with the exact calls (and the block freed, the file closed); valid header -> state = block - pool + 1, block kept, header in
     place, fclose once, no free. d2/d3/a2/a3/sp and the stack above sp equal stock in every run. Mutation checks.
"""
import argparse, hashlib, json, re, struct, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'patches'))
sys.path.insert(0, str(ROOT / 'external/oly_fw_analysis/tools'))
import bodies  # noqa: E402
import build_package as BP  # noqa: E402
import olycompress as oz  # noqa: E402
import oly_container as oc  # noqa: E402
import om5_common as oc5  # noqa: E402
import om5_verify_noop_hook as NV  # noqa: E402
from penf_emulator import Region, execute  # noqa: E402

import importlib.util  # noqa: E402


def _load(name, path):
    sp = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


SB = _load('om5_sd_probe_build', ROOT / 'patches/om5_sd_probe/build.py')
BASE = oc5.BASE
OM5 = bodies.OM5
STACK, CAP = 0x18010000, 0x18040000
POOLR = SB.POOL & ~0xfff
SVC = ['malloc', 'free', 'fopen', 'fclose', 'fread', 'ftell', 'fseek', 'get_tid']
HDR = bytes.fromhex('4f4d354201000000200000000000000000000000000000000000000000000000')        # 'OM5B', v1, len 32


def call_frame(img, at, form):
    """(register-mask byte, stack size) of the call at `at`."""
    o = at - BASE
    return (img[o + 5], img[o + 6]) if form == 'dd' else (img[o + 3], img[o + 4])


def stock_frames(stock, target):
    out = {}
    for m in re.finditer(b'[\xcd\xdd]', stock):
        o = m.start()
        n = 2 if m.group() == b'\xcd' else 4
        if (o + BASE + int.from_bytes(stock[o + 1:o + 1 + n], 'little', signed=True)) & 0xffffffff == target:
            f = (stock[o + 1 + n], stock[o + 2 + n])
            out[f] = out.get(f, 0) + 1
    return out


def stub_src(cfg):
    """Logging stubs for the services. Log entry (16 B): id, d0, d1, a0. Return values come from cfg (CAP+0x100+4*i)."""
    LOG = CAP + 0xf0
    CF = CAP + 0x100
    HD = CAP + 0x300
    s = ''
    for i, n in enumerate(SVC):
        fr = SB.FRAMES[n]
        rr = '[' + ','.join(fr) + ']'
        sz = SB.SIZES[n]
        pre = f""" .section .st_{n},"ax"
 mov ({LOG:#x}),a1
 mov d0,(4,a1)
 mov d1,(8,a1)
 mov a0,(12,a1)
 mov {i + 1},d0
 mov d0,(a1)
 add 16,a1
 mov a1,({LOG:#x})
"""
        if n == 'fread':
            pre = pre.replace(' mov (', ' mov a1,(' + f'{CAP + 0x2f0:#x})\n mov (', 1)           # keep the handle (a1) before it is overwritten
        body = ''
        if n == 'fread':
            body = f' mov {HD:#x},a1\n' + ''.join(f' mov ({4 * k},a1),d0\n mov d0,({4 * k},a0)\n' for k in range(8))
        if n == 'get_tid':
            body = f' mov ({CF + 0x40:#x}),d0\n movhu d0,(a0)\n'
        if n in ('malloc', 'fopen'):
            tail = f' mov ({CF + 4 * i:#x}),a0\n'
        else:
            tail = f' mov ({CF + 4 * i:#x}),d0\n'
        s += pre + body + tail + f' ret {rr},{sz}\n'
    # the stock tracker: writes the canonical event (cap+0x200) through a0, returns a result in d0, clobbers d1
    s += f""" .section .st_trig,"ax"
 mov ({CAP + 0x200:#x}),d1
 mov d1,(a0)
 mov 0x5a5a1234,d0
 mov 0xdead,d1
 ret [d2,d3,a2,a3],36
"""
    return s


STUBS = 0x18100000


def build_stubs():
    """-> {address: bytes}: a `jmp` at each service entry (the wrappers are only 9 B apart) into stub code at STUBS, plus the stub code."""
    src = stub_src(None)
    place = {f'.st_{n}': STUBS + 0x100 * i for i, n in enumerate(SVC)}
    place['.st_trig'] = STUBS + 0x100 * len(SVC)
    out, _ = oc5.assemble(src, place, {})
    code = bytearray(0x100 * (len(SVC) + 1))
    for sname, a in place.items():
        code[a - STUBS:a - STUBS + len(out[sname])] = out[sname]
    jm = {}
    for sname, a in place.items():
        entry = SB.TRIG if sname == '.st_trig' else SB.SERV[sname[4:]]
        jm[entry] = bytes([0xdc]) + struct.pack('<i', a - entry)
    return jm, bytes(code)


def run(image, scen, stubs):
    jm, stubcode = stubs
    """Run from the hook entry to the stock continuation. -> dict(regs, stack, state, log, fault, hdr, fread_a1)."""
    sp0 = STACK + 0x1000
    stack = bytearray(0x1100)
    for i in range(0, 0x1100, 4):
        struct.pack_into('<I', stack, i, 0xc0de0000 + i)
    struct.pack_into('<I', stack, 0x1000 + 12, sp0 + 16)
    struct.pack_into('<I', stack, 0x1000 + 16, scen['event'])
    im = bytearray(image)
    for a, b in jm.items():
        im[a - BASE:a - BASE + len(b)] = b
    cap = bytearray(0x400)
    struct.pack_into('<I', cap, 0xf0, CAP + 0x10)                  # log pointer, entries from CAP+0x10 (before the 0xf0 slot)
    cfg = scen['cfg']
    for i, n in enumerate(SVC):
        struct.pack_into('<I', cap, 0x100 + 4 * i, cfg.get(n, 0))
    struct.pack_into('<I', cap, 0x140, cfg.get('tid', 0x23))     # the tid get_tid reports (CF+0x40)
    struct.pack_into('<I', cap, 0x200, scen['event'])
    cap[0x300:0x320] = scen.get('hdr', HDR)
    st = bytearray(0x40)                                             # 0x6218a240..0x6218a27f: tid at +0x20, state at +0x22
    struct.pack_into('<H', st, 0x20, scen.get('hls', 0x23))
    struct.pack_into('<H', st, 0x22, scen['state'])
    sb = bytearray(0x40)
    struct.pack_into('<I', sb, 0x20, scen.get('secbits', 0x3ff))          # 0xa2693fa0 = region 0xa2693f80 + 0x20
    ms = bytearray(0x40)
    for k, v in enumerate(scen.get('ms', (0, 0, 0, 0))):
        struct.pack_into('<I', ms, 0x18 + 8 * k, v)                       # 0x61e43338, 40, 48, 50 = region 0x61e43320 + 0x18...
    pool = bytearray(0x10000)
    for i in range(0, len(pool), 4):
        struct.pack_into('<I', pool, i, 0xbadc0de0 ^ i)
    regs = {'sp': sp0, 'pc': SB.HOOK, 'a0': sp0 + 16, 'a1': 0x11a1, 'a2': 0x22a2, 'a3': 0x33a3, 'd0': 0x4d0, 'd1': 0x4d1, 'd2': 0x4d2, 'd3': 0x4d3}
    r = execute([Region('img', BASE, bytes(im), 'rx'), Region('cap', CAP, bytes(cap), 'rw', capture=True),
                 Region('stack', STACK, bytes(stack), 'rw', capture=True), Region('st', 0x6218a240, bytes(st), 'rw', capture=True),
                 Region('sb', 0xa2693f80, bytes(sb), 'r'), Region('ms', 0x61e43320, bytes(ms), 'r'),
                 Region('pool', POOLR, bytes(pool), 'rw', capture=True), Region('stubs', STUBS, stubcode, 'rx')],
                regs, SB.RESUME, limit=50_000, timeout=60)
    mem = {m['name']: bytes.fromhex(m['hex']) for m in r['memory']}
    c = mem['cap']
    end = struct.unpack_from('<I', c, 0xf0)[0]
    log = [struct.unpack_from('<4I', c, 0x10 + 16 * k) for k in range((end - CAP - 0x10) // 16)]
    names = {i + 1: n for i, n in enumerate(SVC)}
    return dict(regs=r['registers'], stack=mem['stack'], state=struct.unpack_from('<H', mem['st'], 0x22)[0], fault=r['fault'],
                log=[(names[e[0]], e[1], e[2], e[3]) for e in log], pool=mem['pool'], fread_a1=struct.unpack_from('<I', c, 0x2f0)[0])


def scenarios():
    P = 0x02000100
    BUF = SB.POOL + 0x400                        # malloc result inside the class-1 pool, 4-aligned
    ok = dict(secbits=0x3ff, hls=0x23, tid=0x23, ms=(0, 0xf000, 0, 0))
    g = dict(malloc=BUF, fopen=0x2000, fseek=0, ftell=100, fread=32)
    S = []

    def add(name, state=0, event=P, scen=None, cfg=None, want_state=None, want_calls=None, **kw):
        sc = dict(event=event, state=state, cfg={**g, **(cfg or {})}, **{**ok, **(scen or {})}, **kw)
        S.append((name, sc, state if want_state is None else want_state, want_calls or []))

    add('release', event=0x02000101, want_calls=[])
    add('other event', event=0x01000100, want_calls=[])
    add('state already 0xFFFF', state=0xffff)
    add('state already a handle', state=0x401)
    add('sections not copied', scen=dict(secbits=0x1ff))
    add('get_tid fails (-25)', cfg=dict(get_tid=0xffffffe7), want_calls=['get_tid'])
    add('HLS tid still -1', scen=dict(hls=0xffff), want_calls=['get_tid'])
    add('HLS tid 0', scen=dict(hls=0), want_calls=['get_tid'])
    add('wrong task', cfg=dict(tid=0x24), want_calls=['get_tid'])
    add('no medium mounted', scen=dict(ms=(0, 0x1234, 0, 0)), want_calls=['get_tid'])
    full = ['get_tid', 'malloc', 'fopen', 'fseek', 'ftell', 'fseek', 'fread', 'fclose']
    add('medium in slot d (0x61e43350)', scen=dict(ms=(0, 0, 0, 0xf000)), want_state=BUF - SB.POOL + 1, want_calls=full)
    add('malloc fails', want_state=0xffff, cfg=dict(malloc=0), want_calls=['get_tid', 'malloc'])
    add('no file (fopen 0)', want_state=0xffff, cfg=dict(fopen=0), want_calls=['get_tid', 'malloc', 'fopen', 'free'])
    add('fseek END fails', want_state=0xffff, cfg=dict(fseek=0xffffffff), want_calls=['get_tid', 'malloc', 'fopen', 'fseek', 'fclose', 'free'])
    add('file shorter than the header', want_state=0xffff, cfg=dict(ftell=31), want_calls=['get_tid', 'malloc', 'fopen', 'fseek', 'ftell', 'fclose', 'free'])
    add('ftell -1', want_state=0xffff, cfg=dict(ftell=0xffffffff), want_calls=['get_tid', 'malloc', 'fopen', 'fseek', 'ftell', 'fclose', 'free'])
    add('short read', want_state=0xffff, cfg=dict(fread=31), want_calls=full + ['free'])
    add('bad magic', want_state=0xffff, hdr=b'XXXX' + HDR[4:], want_calls=full + ['free'])
    add('block outside the pool', want_state=0xffff, cfg=dict(malloc=SB.POOL_END - 0x100), want_calls=full + ['free'])
    add('block misaligned', want_state=0xffff, cfg=dict(malloc=BUF + 2), want_calls=full + ['free'])
    add('valid file', want_state=BUF - SB.POOL + 1, want_calls=full)
    add('valid file at the last allowed block', want_state=SB.POOL_END - 0x400 - SB.POOL + 1, cfg=dict(malloc=SB.POOL_END - 0x400), want_calls=full)
    return S


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('spec', type=Path, nargs='?', default=ROOT / 'patches/om5_sd_probe/out/edits.json')
    ap.add_argument('--package', type=Path)
    ap.add_argument('--mod-version', default='0.3')
    a = ap.parse_args()
    spec = json.loads(a.spec.read_text())
    sp = spec['sd_probe']
    stock = oc5.image()

    # ---- D
    fresh, syms = SB.build(save_claims=False)
    assert fresh['edits'] == spec['edits'], 'spec differs from a fresh build'
    FB = _load('om5_fw_version_build', ROOT / 'patches/om5_fw_version/build.py')
    fw, _ = FB.build(a.mod_version, save_claims=False)
    merged = dict(components=['0x100'], edits=fw['edits'] + spec['edits'])
    edits = [(int(e['addr'], 16), bytes.fromhex(e['new'])) for e in merged['edits']]
    patched = bytearray(stock)
    for ad, new in edits:
        patched[ad - BASE:ad - BASE + len(new)] = new
    patched = bytes(patched)
    stock_file = (ROOT / 'stock_firmware/om5' / OM5.stock_name).read_bytes()
    assert hashlib.sha256(stock_file).hexdigest() == OM5.stock_sha
    pkg, restore, info = BP.build(merged, stock_file, OM5)
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
    psha = hashlib.sha256(pkg).hexdigest()
    if a.package:
        assert a.package.read_bytes() == pkg, f'{a.package} differs from the package built here'
    print(f'D  package {psha}: expanded main == stock + {len(merged["edits"])} edits (label {a.mod_version}), {len(d)} B differ, re-encoded '
          f'span(s) {info["recompressed_spans"]} (section 6 only), restore == stock, spec == fresh build' + (', --package identical' if a.package else ''))

    # ---- T
    H, C = SB.HOOK, SB.CODE_AT
    assert oc5.stock(H, 5) == SB.HOOK_STOCK and oc5.stock(SB.CONTEXT_AT, len(SB.CONTEXT)) == SB.CONTEXT
    pa = patched[H - BASE:H - BASE + 5]
    assert pa[0] == 0xdc and (H + int.from_bytes(pa[1:5], 'little', signed=True)) & 0xffffffff == C
    assert oc5.section_of(H).idx == oc5.section_of(C).idx == 1 and not oc5.section_of(C).compressed
    # service entries: stock bytes by structure
    sv = SB.SERV
    assert stock[sv['fopen'] - BASE:sv['fopen'] - BASE + 1] == b'\x00' and oc5.stock(sv['fopen'] + 1, 1) == b'\xcd' and oc5.stock(sv['fopen'] + 4, 5) == bytes([0xf0, 44, 0xdf, 0, 4])
    assert sv['fclose'] == sv['fopen'] + 9
    for n in ('fclose', 'fread', 'ftell', 'fseek'):                     # the dispatcher prologue shared with the E-P7's wrappers
        o = sv[n]
        pro = {'fclose': b'\x93', 'ftell': b'\x93', 'fread': b'\x47\x04\x87\x82\x93', 'fseek': b'\x87\x82\x93'}[n]
        assert oc5.stock(o, len(pro)) == pro, n
        m = re.match(rb'\xdd....\x00\x08\x1c\xa0\x00\xc8\x0c\xa0\x01\xc8.\xa0\x02\xc8.\xca.', oc5.stock(o + len(pro), 0x1c), re.S)
        assert m, n
    assert (sv['fclose'] + 1 + int.from_bytes(oc5.stock(sv['fclose'] + 2, 4), 'little', signed=True)) & 0xffffffff == 0x606ba159   # the handle-type dispatcher, as the other wrappers
    assert oc5.stock(sv['malloc'] + 9, 6).hex() == 'fac80008c506' and oc5.stock(sv['malloc'] + 15, 2).hex() == '8001'    # 129..2048 -> class 1
    chk = oc5.stock(0x6002385f, 0x16)
    assert struct.unpack_from('<I', chk, 2)[0] == SB.POOL and struct.unpack_from('<I', chk, 10)[0] == SB.POOL_END
    assert re.match(rb'\x30\x50\x80\xfc\xde....\x54\xf3\x92\xf8\x25\x1c\xf0\x45\x80\x01\xa5\x00\xc9\x03\x00\xa0\x01\xc8\x12', oc5.stock(sv['get_tid'], 0x1c), re.S)   # E-P7 0x600317ac body
    # section-copy bit setter, HLS tid store, media setter
    assert oc5.stock(0x600463ff, 18).hex().startswith('8001f29cfca5a03f69a2f214fc81a03f69a2')
    assert oc5.stock(0x6085be93, 5).hex() == '92fad2ac00'                                                       # mov a0,a2 ; add 172,a2
    assert oc5.stock(0x6085bed2, 2).hex() == 'f072'                                                              # movhu d0,(a2)
    m = re.match(rb'\x82\xaa\x04\xc8.\xa0\x08\xc8.\xa0\x01\xc8.\xa0\x20\xc8.\xca.\xfc\x85(....)', oc5.stock(0x6086a42a, 0x20), re.S)
    assert m and int.from_bytes(m.group(1), 'little') == SB.MEDIA[3], m
    # frames: every call in the probe code uses the dominant stock frame of its service
    LD = SB.LOAD_AT
    code = patched[C - BASE:C - BASE + sp['code_len']]
    lcode = patched[LD - BASE:LD - BASE + sp['load_len']]
    for at, n_ in ((C, len(code)), (LD, len(lcode))):        # the claims lie in the LOADED part of their section (RAM at run time)
        sec = oc5.section_of(at)
        assert at + n_ <= sec.loaded_end and not sec.compressed, (hex(at), sec)
    assert oc5.section_of(C).idx == 1 and oc5.section_of(LD).idx == 8
    used = {}
    for n in SVC:
        dom = max(stock_frames(stock, sv[n]).items(), key=lambda x: x[1])
        mine = set()
        for base_, blk in ((C, code), (LD, lcode)):
            for mo in re.finditer(b'\xdd', blk):
                o = mo.start()
                if o + 7 <= len(blk) and (base_ + o + int.from_bytes(blk[o + 1:o + 5], 'little', signed=True)) & 0xffffffff == sv[n]:
                    mine.add((blk[o + 5], blk[o + 6]))
        assert mine == {dom[0]}, (n, mine, stock_frames(stock, sv[n]))
        used[n] = (dom[0], dom[1])
    # instruction scan
    ins = []
    for base_, blk in ((C, code), (LD, lcode)):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / 'code.bin'
            f.write_bytes(blk)
            txt = oc5.run_sh(f'$HOME/ep7fw-toolchains/mn10300/bin/mn10300-elf-objdump -b binary -m am33-2 -D --adjust-vma={base_:#x} {oc5.wsl_path(f)} | tail -n +8\n', td)
        ins += [(int(m.group(1), 16), re.sub(r'\s+', ' ', m.group(2).strip())) for m in re.finditer(r'^\s*([0-9a-f]{8}):\t[0-9a-f ]+\t(\S.*)$', txt, re.M)]
    path_at = int(sp['symbols']['c5_path'], 16)
    ins = [x for x in ins if not path_at <= x[0] < LD + len(lcode)]       # the path strings are data
    stores = [t for _, t in ins if re.match(r'mov(bu|hu)? [da]\d,\(', t)]
    assert stores == ['movhu d0,(0x%x)' % SB.STATE] * 2, stores
    assert not [t for _, t in ins if re.match(r'(mul|div|setlb|lne|leq|llt|lcc|lcs|lra|retf|rets|rti)', t)]
    print(f'T  hook {H:#x} -> jmp {C:#x} (section 1, plain). Services (stock structure checked): ' + ', '.join(f'{n} {sv[n]:#x}' for n in SVC) +
          f'; gate words secbits {SB.SECBITS:#x} (bit setter 0x600463ff), HLS tid {SB.HLS_TID:#x} (store 0x6085bed2), media {[hex(x) for x in SB.MEDIA]} '
          f'(setter 0x6086a42a); class-1 pool [{SB.POOL:#x},{SB.POOL_END:#x}) from free() 0x6002385f; call frames = dominant stock frames {used}; '
          f'{len(ins)} instructions, only stores: 2 x movhu to {SB.STATE:#x}, no mul/div/loop macros')

    # ---- X
    cs = NV.calls_to(stock, SB.TRIG)
    assert cs == [H]
    for t in range(H + 1, H + 5):
        assert not NV.words(stock, t)
    refs = NV.words_in(stock, C, C + SB.CODE_MAX) + NV.words_in(stock, LD, LD + SB.LOAD_MAX)
    assert not refs, [hex(x) for x in refs]
    tail_lo = oc5.section_of(C).tail[0]
    tail8 = oc5.section_of(LD).tail[0]
    assert all(stock[p - BASE] == 0xff for p in range(tail_lo, C)) and all(stock[p - BASE] == 0xff for p in range(tail8, LD))
    pad_msg = NV.pad_evidence(stock, SB.STATE)
    assert SB.STATE == 0x6218a262 and SB.HLS_TID == NV.OBJ + 172
    print(f'X  nothing branches into/points at the hook interior; section-1 FF tail {tail_lo:#x}..{oc5.section_of(C).loaded_end:#x} (the loaded part) untouched below {C:#x}; section-8 FF tail {tail8:#x}.. untouched below {LD:#x}; nothing points into the {sp["code_len"]} B hook+gate or the {sp["load_len"]} B loader. ' + pad_msg)

    # ---- E
    stubs = build_stubs()
    sc = scenarios()
    # stock reference run per scenario (hook absent): d2/d3/a2/a3/sp and the live stack must match the patched run
    def check(img, with_hook=True, verbose=False):
        n = 0
        for name, scen, want_state, want_calls in sc:
            rp = run(img, scen, stubs)
            rs = run(stock, scen, stubs)
            if rp['fault'] or rs['fault']:
                return f'{name}: fault patched {rp["fault"]} stock {rs["fault"]}'
            for k in ('d2', 'd3', 'a2', 'a3', 'sp', 'pc'):
                if rp['regs'][k] != rs['regs'][k]:
                    return f'{name}: {k} {rp["regs"][k]:#x} != stock {rs["regs"][k]:#x}'
            dif = [i for i in range(0x1000, 0x1100, 4) if rp['stack'][i:i + 4] != rs['stack'][i:i + 4]]
            if dif not in ([], [0x1000]):
                return f'{name}: live stack differs {[hex(i) for i in dif]}'
            if rs['state'] != scen['state'] or rs['log']:
                return f'{name}: stock reference changed something'
            if rp['state'] != want_state:
                return f'{name}: state {rp["state"]:#x}, expected {want_state:#x}'
            calls = [c[0] for c in rp['log']]
            if calls != want_calls:
                return f'{name}: calls {calls}, expected {want_calls}'
            # call arguments
            lg = [e for e in rp['log'] if e[0] != 'get_tid']
            if lg:
                if lg[0][:2] != ('malloc', 1024):
                    return f'{name}: malloc arg {lg[0]}'
                if len(lg) > 1 and (lg[1][0] != 'fopen' or lg[1][3] == 0):
                    return f'{name}: fopen {lg[1]}'
                f_ = [e for e in lg if e[0] == 'fseek']
                if f_ and [e[2] for e in f_] != [1, 2][:len(f_)]:
                    return f'{name}: fseek whence {[e[2] for e in f_]}'
                if any(e[3] != 0x2000 for e in lg if e[0] in ('fseek', 'ftell', 'fclose')):
                    return f'{name}: file handle not passed {lg}'
                fr = [e for e in lg if e[0] == 'fread']
                if fr and (fr[0][1], fr[0][2], fr[0][3], rp['fread_a1']) != (1, 32, scen['cfg']['malloc'], 0x2000):
                    return f'{name}: fread args {fr[0]} handle {rp["fread_a1"]:#x}'
                fe = [e for e in lg if e[0] == 'free']
                if fe and fe[0][3] != scen['cfg']['malloc']:
                    return f'{name}: free arg {fe[0]}'
            if want_state not in (scen['state'],) and 1 <= want_state < 0xffff:
                off = scen['cfg']['malloc'] - POOLR
                if rp['pool'][off:off + 32] != HDR or any(e[0] == 'free' for e in lg):
                    return f'{name}: block not kept intact'
            n += 1
        return n

    n = check(patched)
    assert isinstance(n, int), n
    print(f'E  {n} scenarios, each with the exact service-call sequence/arguments and state value expected (gate conditions, every failure path, valid file); '
          f'd2/d3/a2/a3/sp, pc and the live stack equal stock (only the dead return slot (0,sp) differs); image read-only, no fault; valid file keeps '
          f'the 32-byte header in the block and never frees it')
    MAGIC4 = struct.pack('<I', int.from_bytes(SB.MAGIC, 'little'))
    cd = code + lcode

    def mut(find, repl):
        im = bytearray(patched)
        i = cd.index(find)
        o = (C - BASE + i) if i < len(code) else (LD - BASE + i - len(code))
        im[o:o + len(repl)] = repl
        return bytes(im)
    muts = {'secbits mask 0x1ff': mut(bytes.fromhex('fae0ff03'), bytes.fromhex('fae0ff01')),
            'magic compare changed': mut(MAGIC4, b'OM5X'),
            'fread count 16': mut(bytes.fromhex('8520'), bytes.fromhex('8510')),
            'failure state 0xfffe': mut(bytes.fromhex('fcccffff0000'), bytes.fromhex('fcccfeff0000'))}
    for name, im in muts.items():
        r = check(im)
        assert not isinstance(r, int), f'mutation {name!r} was not caught'
        print(f'E  mutation {name}: caught ({r})')
    print('ALL PASS')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Verify patches/om5_noop_hook (OM-5 camera step C4). Offline; emulator only. Run in WSL (the fishcu engine is a Linux binary):

  ~/ep7fw-toolchains/venv/bin/python -I tools/om5_verify_noop_hook.py [patches/om5_noop_hook/out/edits.json]
        [--package PATCHED.pkg.bin] [--mod-version 0.2]

  D  diff: the shipped package is the no-op hook + the firmware-screen label (patches/om5_fw_version, --mod-version). Built with
     tools/build_package.py --body om5: decoded+expanded main == stock + the spec's edits and differs from stock at no other
     byte, only the section-6 span is re-encoded, other components identical, restore == stock; the spec equals a fresh run of
     build.py. With --package the file given is byte-identical to the one built here.
  T  targets: the hooked 5 B was the stock `call 0x602ed895,[d2,d3,a2,a3],36` (context pinned); it is now `jmp nh_hook`
     (section 1, the hook's own section); nh_hook's first instruction is the same call, its last is the jmp to the stock
     continuation; the trampoline has one store (the counter) and no other write, mul/div or loop.
  X  refs: nothing branches into or points at the interior of the hook; the displaced callee's other callers are unchanged;
     nothing points into the claimed FF tail; the counter word 0x6218a262 (OlyHfwEventManager singleton 0x6218a1b4 +174,
     padding between the u16 at +172 and the u32 at +176) is unreferenced and inside the boot-cleared .bss: no 32-bit word equals
     any address of 0x6218a260..67 in either alias; the constructor writes only +172 (u16) and +176 (u32); none of the 36 class
     methods nor any function that calls the singleton getter touches +172..+175; the next object starts at 0x6218a268; the three
     boot fill calls of 0x6002146d cover it.
  E  emulator (fishcu MN103 engine): hook entry -> stock continuation, on stock and patched with the callee stubbed (it stores
     the canonical event through a0, returns d0 = a result and clobbers d1): d0, a0..a3, d2, d3, sp and the whole stack frame
     equal stock; the counter goes +1 only for the press event 0x02000100 (0xffff wraps to 0), is unchanged for the release
     0x02000101 and others; the image is mapped read-only (an image store would fault). Mutation checks: a wrong increment, a
     wrong counter address and an altered result register each make the comparison fail.
"""
import argparse, hashlib, json, re, struct, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'patches'))
sys.path.insert(0, str(ROOT / 'patches/om5_noop_hook'))
sys.path.insert(0, str(ROOT / 'external/oly_fw_analysis/tools'))
import bodies  # noqa: E402
import build_package as BP  # noqa: E402
import olycompress as oz  # noqa: E402
import oly_container as oc  # noqa: E402
import om5_common as oc5  # noqa: E402
import build as NB  # noqa: E402  (patches/om5_noop_hook/build.py)
from penf_emulator import Region, execute  # noqa: E402

BASE = oc5.BASE
OM5 = bodies.OM5
OBJ = 0x6218a1b4            # OlyHfwEventManager singleton
VTABLE = 0x60b4e5f0         # its vtable (36 slots)
GETTER = 0x6085bd1a         # singleton getter
CTOR = 0x6085bb3e
FILL = 0x6002146d           # DMA fill-to-zero(start, end)
STACK, CAP = 0x18010000, 0x18040000
EVENTS = [(0x02000100, 1), (0x02000101, 0), (0x02000102, 0), (0x01000100, 0), (0, 0), (0x02070101, 0)]


_W = {}


def _w(img):
    import numpy as np
    k = id(img)
    if k not in _W:
        a = np.frombuffer(img, dtype=np.uint8).astype(np.uint32)
        _W[k] = (img, a[:-3] | (a[1:-2] << 8) | (a[2:-1] << 16) | (a[3:] << 24))
    return _W[k][1]


def words(img, val, mask=0xffffffff):
    """Addresses (any byte alignment) of 32-bit words w with (w & mask) == val."""
    import numpy as np
    return [int(i) + BASE for i in np.nonzero((_w(img) & np.uint32(mask)) == np.uint32(val))[0]]


def words_in(img, lo, hi):
    """Addresses of 32-bit words in [lo, hi)."""
    import numpy as np
    w = _w(img)
    return [int(i) + BASE for i in np.nonzero((w >= np.uint32(lo)) & (w < np.uint32(hi)))[0]]


def calls_to(img, target):
    """call16 (cd) and call32 (dd) forms of `call target,[...],imm`."""
    out = []
    for m in re.finditer(b'[\xcd\xdd]', img):
        o = m.start()
        n = 2 if m.group() == b'\xcd' else 4
        if o + 1 + n <= len(img) and (o + BASE + int.from_bytes(img[o + 1:o + 1 + n], 'little', signed=True)) & 0xffffffff == target:
            out.append(o + BASE)
    return out


def dis_windows(wins, img_path):
    """One objdump run per window inside one shell script; -> {window start: [(addr, text)]}."""
    sh = f'set -e\ncd {oc5.wsl_path(ROOT)}\nO=$HOME/ep7fw-toolchains/mn10300/bin/mn10300-elf-objdump\n'
    for lo, hi in wins:
        sh += f'echo "@@ {lo:#x}"\n$O -b binary -m am33-2 -D --adjust-vma={BASE:#x} --start-address={lo:#x} --stop-address={hi:#x} {oc5.wsl_path(img_path)} | tail -n +8\n'
    with tempfile.TemporaryDirectory() as d:
        txt = oc5.run_sh(sh, d)
    out, cur = {}, None
    for ln in txt.splitlines():
        if ln.startswith('@@ '):
            cur = int(ln[3:], 16)
            out[cur] = []
            continue
        m = re.match(r'^\s*([0-9a-f]{8}):\t[0-9a-f ]+\t(.*)$', ln)
        if m and cur is not None:
            out[cur].append((int(m.group(1), 16), m.group(2).strip()))
    return out


def asm(src, at, syms=None):
    out, _ = oc5.assemble(f' .section .s,"ax"\n{src}\n', {'.s': at}, syms or {})
    return out['.s']


def run_hook(image, event, counter0, stub_at, stub, result=0x5a5a1234):
    """Run from the hook entry to the stock continuation. Returns (registers, stack, counter, fault)."""
    sp0 = STACK + 0x1000
    stack = bytearray(0x1100)
    for i in range(0, 0x1100, 4):
        struct.pack_into('<I', stack, i, 0xc0de0000 + i)         # recognisable fill, so a stray stack store shows
    struct.pack_into('<I', stack, 0x1000 + 12, sp0 + 16)         # (12,sp) = &event (the stock `mov (12,sp),a0` preceded the hook)
    struct.pack_into('<I', stack, 0x1000 + 16, event)            # (16,sp) = the event word, canonicalised by the stub below
    im = bytearray(image)
    im[stub_at - BASE:stub_at - BASE + len(stub)] = stub
    cap = bytearray(0x200)
    struct.pack_into('<I', cap, 0, event)
    struct.pack_into('<I', cap, 4, result)
    ctr = bytearray(0x20)
    struct.pack_into('<H', ctr, 2, counter0)                     # the counter word is at 0x6218a262 = region +2
    regs = {'sp': sp0, 'pc': NB.HOOK, 'a0': sp0 + 16, 'a1': 0x11a1, 'a2': 0x22a2, 'a3': 0x33a3, 'd0': 0x4d0, 'd1': 0x4d1,
            'd2': 0x4d2, 'd3': 0x4d3}
    r = execute([Region('img', BASE, bytes(im), 'rx'), Region('ctr', NB.COUNTER & ~0x1f, bytes(ctr), 'rw', capture=True),
                 Region('stack', STACK, bytes(stack), 'rw', capture=True), Region('cap', CAP, bytes(cap), 'r')],
                regs, NB.RESUME, limit=10_000, timeout=60)
    mem = {m['name']: bytes.fromhex(m['hex']) for m in r['memory']}
    return r['registers'], mem['stack'], struct.unpack_from('<H', mem['ctr'], 2)[0], r['fault']


def pad_evidence(stock, pad):
    """Static proof that the spare u16 at `pad` is unreferenced, 0 at power-on and inside boot-cleared .bss. Returns a summary."""
    assert pad == OBJ + 174
    assert pad == OBJ + 174
    assert not words(stock, 0x2218a260, 0xbffffff8), [hex(x) for x in words(stock, 0x2218a260, 0xbffffff8)]
    assert oc5.stock(0x6085bb87, 2 + 4 + 1 + 4)[:2] == bytes.fromhex('80ff') and oc5.stock(0x6085bb89, 4).hex() == 'fa72ac00' and oc5.stock(0x6085bb8e, 4).hex() == 'fa12b000'
    ctor_ref = [x for x in words(stock, OBJ)]
    assert ctor_ref == [0x6085bd51, 0x6085bd5e], [hex(x) for x in ctor_ref]
    nxt = sorted(x for x in words(stock, 0x2218a268, 0xbffffff8) if oc5.stock(x - 2, 2) is not None)
    assert nxt and 0x6085c941 in nxt, [hex(x) for x in nxt]
    slots = [struct.unpack_from('<I', stock, VTABLE - BASE + 4 * i)[0] for i in range(40)]
    assert all(0x60021020 <= s < 0x61800000 for s in slots[:36]) and not (0x60021020 <= slots[36] < 0x61800000), slots
    slots = slots[:36]
    getter_calls = calls_to(stock, GETTER)
    assert len(getter_calls) == 67 and calls_to(stock, CTOR) == [0x6085bd57]
    wins = [(s, s + 0x800) for s in slots] + [(c, c + 0x400) for c in getter_calls]
    dd = dis_windows(wins, oc5.IMAGE)
    disp = re.compile(r'\((17[2-5]),a\d\)')
    bad = []
    nacc = 0
    for lo, lst in dd.items():
        for a_, t in lst:
            if re.match(r'(ret|retf|rets|rti)\b', t):
                break
            if disp.search(t):
                bad.append((hex(lo), hex(a_), t))
            nacc += 1
    assert not bad, bad
    fills = calls_to(stock, FILL)
    fl = []
    for c in fills:
        w = dis_windows([(c - 0x20, c)], oc5.IMAGE)[c - 0x20]
        v = {}
        for _, t in w:
            m = re.match(r'mov\s+(-?\d+),(d0|d1)$', t)
            if m:
                v[m.group(2)] = int(m.group(1)) & 0xffffffff
        fl.append((v.get('d0', 0) & 0x3fffffff, v.get('d1', 0) & 0x3fffffff))     # the fill masks both addresses to 30 bits
    covered = [lo for lo, hi in fl if lo <= pad & 0x3fffffff < hi]
    assert len(fills) == 3 and covered, (fills, [(hex(a_ or 0), hex(b or 0)) for a_, b in fl])
    # the whole class code region (0x6085b000..0x6085e000, ctor, getter, task creation, methods) touches +172..+175 in no other form
    # (register-relative or immediate): only the ctor's u16 store (0x6085bb89), its u32 +176 store and the tid code `add 172,a2` (0x6085be94)
    reg = dis_windows([(0x6085b000, 0x6085e000)], oc5.IMAGE)[0x6085b000]
    imm = re.compile(r'(?<![0-9a-fx])(17[2-5]|0xa[c-f])(?![0-9a-fx])')
    seen = [(a_, t) for a_, t in reg if imm.search(t) and not re.match(r'(call|cmp|b[a-z]+)\s', t)]
    assert {a_ for a_, _ in seen} == {0x6085bb89, 0x6085be94}, [(hex(a_), t) for a_, t in seen]
    assert [re.sub(r'\s+', ' ', t) for _, t in seen] == ['movhu d0,(172,a2)', 'add 172,a2'], seen
    return (f'Counter {pad:#x} = singleton {OBJ:#x} +174: no 32-bit word equals 0x[6a]218a260..67; ctor writes +172 u16 / +176 u32 only; '
            f'{len(slots)} class methods + {len(getter_calls)} getter users ({nacc} instructions scanned) never touch +172..+175, nor does any other '
            f'instruction of the class code 0x6085b000..0x6085e000 except the ctor and the tid code (`add 172,a2` at 0x6085be94, a u16 access); next object bytes '
            f'written from 0x6218a268; boot fill [{", ".join((hex(a_) + "-" + hex(b_)) for a_, b_ in fl if a_ <= pad & 0x3fffffff < b_)}] covers it')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('spec', type=Path, nargs='?', default=ROOT / 'patches/om5_noop_hook/out/edits.json')
    ap.add_argument('--package', type=Path)
    ap.add_argument('--mod-version', default='0.2')
    a = ap.parse_args()
    spec = json.loads(a.spec.read_text())
    nh = spec['noop_hook']
    stock = oc5.image()

    # ---- D
    fresh, syms = NB.build(save_claims=False)
    assert fresh['edits'] == spec['edits'], 'spec differs from a fresh build'
    sys.path.insert(0, str(ROOT / 'patches/om5_fw_version'))
    import importlib.util
    sp_ = importlib.util.spec_from_file_location('om5_fw_version_build', ROOT / 'patches/om5_fw_version/build.py')
    FB = importlib.util.module_from_spec(sp_)
    sp_.loader.exec_module(FB)
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
    print(f'D  package {psha}: expanded main == stock + {len(merged["edits"])} edits (label {a.mod_version}), {len(d)} B differ, '
          f're-encoded span(s) {info["recompressed_spans"]} (section 6 only), restore == stock, spec == fresh build'
          + (', --package identical' if a.package else ''))

    # ---- T
    H, T = NB.HOOK, NB.TRAMP_AT
    st = oc5.stock(H, 5)
    assert st == NB.HOOK_STOCK and st[0] == 0xcd and st[3:] == bytes([0xf0, 36]) and H + int.from_bytes(st[1:3], 'little', signed=True) == NB.TRIG
    assert oc5.stock(NB.CONTEXT_AT, len(NB.CONTEXT)) == NB.CONTEXT
    pa = patched[H - BASE:H - BASE + 5]
    assert pa[0] == 0xdc and (H + int.from_bytes(pa[1:5], 'little', signed=True)) & 0xffffffff == T == int(nh['tramp'], 16)
    assert oc5.section_of(H).idx == oc5.section_of(T).idx == 1 and not oc5.section_of(T).compressed
    code = patched[T - BASE:T - BASE + nh['tramp_len']]
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / 'tramp.bin'
        f.write_bytes(code)
        sh = f'$HOME/ep7fw-toolchains/mn10300/bin/mn10300-elf-objdump -b binary -m am33-2 -D --adjust-vma={T:#x} {oc5.wsl_path(f)} | tail -n +8\n'
        txt = oc5.run_sh(sh, td)
    ins = [(int(m.group(1), 16), m.group(2).strip()) for m in re.finditer(r'^\s*([0-9a-f]{8}):\t[0-9a-f ]+\t(\S.*)$', txt, re.M)]
    ins = [x for x in ins if not x[1].startswith('.byte')]
    assert re.fullmatch(r'call\s+0x%x,\[d2,d3,a2,a3\],36' % NB.TRIG, ins[0][1]), ins[0]
    assert re.fullmatch(r'jmp\s+0x%x.*' % NB.RESUME, ins[-1][1]), ins[-1]
    stores = [t for _, t in ins if re.match(r'mov(bu|hu|)?\s+[da]\d,\(', t)]
    assert stores == ['movhu\td1,(0x%x)' % NB.COUNTER] or [s.replace('\t', ' ') for s in stores] == ['movhu d1,(0x%x)' % NB.COUNTER], stores
    assert not [t for _, t in ins if re.match(r'(mul|div|setlb|lne|leq|llt|lcc|lcs|lra|retf|ret|rets)', t)]
    assert not [t for _, t in ins[1:-1] if t.startswith('call')] and all(not re.match(r'.*\b(d0|a0)\b', t) or t.startswith('call') for _, t in ins[:1])
    assert not any(re.search(r'\b(d0|a0|a1|a2|a3|d2|d3)\b', t) for _, t in ins[1:-1]), ins   # only d1 (and sp) are touched
    print(f'T  hook {H:#x}: stock `call {NB.TRIG:#x},[d2,d3,a2,a3],36` (context {NB.CONTEXT_AT:#x}+{len(NB.CONTEXT)} B pinned) -> `jmp {T:#x}` (section 1, '
          f'plain); nh_hook = {len(ins)} instructions: displaced call, canonical-event test, one halfword store to {NB.COUNTER:#x} via d1, jmp {NB.RESUME:#x}; '
          f'registers other than d1 untouched')

    # ---- X
    for t in range(H + 1, H + 5):
        assert not words(stock, t), (hex(t), words(stock, t))
    wins = [(0x602ed700, 0x602ed800)]
    for lo, hi in wins:
        txt = dis_windows([(lo, hi)], oc5.IMAGE)[lo]
        for t in range(H + 1, H + 5):
            assert not [1 for _, x in txt if re.search(r'\b0x%x\b' % t, x)], hex(t)
        assert any(x.endswith('0x%x' % NB.RESUME) and x.startswith('bne') for _, x in txt), 'stock bne to the continuation not seen'
    cs, cp = calls_to(stock, NB.TRIG), calls_to(patched, NB.TRIG)
    assert H in cs and sorted(set(cs) - {H} | {T}) == sorted(cp), (cs, cp)
    tail_lo = oc5.section_of(T).tail[0]
    assert tail_lo <= T - 0x20
    refs = words_in(stock, T, T + 0x40)
    assert not refs, [hex(x) for x in refs]
    for p in range(tail_lo, T):
        assert stock[p - BASE] == 0xff
    pad_msg = pad_evidence(stock, NB.COUNTER)
    print(f'X  nothing branches into/points at the 4 hook-interior bytes; {len(cs)} caller of {NB.TRIG:#x} before, same after (hook replaced by the trampoline call); '
          f'FF tail {tail_lo:#x}.. untouched below {T:#x}, nothing points into {T:#x}+0x40. ' + pad_msg)

    # ---- E
    stub = asm(f' mov ({CAP:#x}),d1\n mov d1,(a0)\n mov ({CAP + 4:#x}),d0\n mov 0xdead,d1\n ret [d2,d3,a2,a3],36', NB.TRIG)

    def compare(img, counter_at=NB.COUNTER, inc=1):
        n = 0
        for ev, want in EVENTS:
            for c0 in (0, 7, 0xffff):
                rs, ss, cs_, fs = run_hook(stock, ev, c0, NB.TRIG, stub)
                rp, sp_, cp_, fp = run_hook(img, ev, c0, NB.TRIG, stub)
                if fs or fp:
                    return f'fault stock {fs} patched {fp}'
                for k in ('d0', 'd2', 'd3', 'a0', 'a1', 'a2', 'a3', 'sp', 'pc'):
                    if rs[k] != rp[k]:
                        return f'{k} differs for event {ev:#x}: {rs[k]:#x} vs {rp[k]:#x}'
                # live frame (sp and above) equal, except the call's return-address slot (0,sp): the stock call stores the stock
                # return address 0x602ed76d there, the trampoline's call stores its own (T+7). The slot is dead after the call.
                dif = [i for i in range(0x1000, 0x1100, 4) if ss[i:i + 4] != sp_[i:i + 4]]
                if dif not in ([], [0x1000]):
                    return f'stack differs for event {ev:#x}: {[hex(i) for i in dif]}'
                if dif and (struct.unpack_from('<I', ss, 0x1000)[0], struct.unpack_from('<I', sp_, 0x1000)[0]) != (NB.RESUME, NB.TRAMP_AT + 7):
                    return 'the (0,sp) return slot holds unexpected values'
                dead = [i for i in range(0, 0x1000, 4) if ss[i:i + 4] != sp_[i:i + 4]]
                if dead:
                    return f'dead-stack words differ at {[hex(i) for i in dead]}'
                if cs_ != c0:
                    return 'stock changed the counter?'
                if cp_ != (c0 + want) & 0xffff:
                    return f'counter {c0:#x} -> {cp_:#x} for event {ev:#x}'
                n += 1
        return n

    n = compare(patched)
    assert isinstance(n, int), n
    print(f'E  {n} runs (6 events x counter 0/7/0xffff): d0/d2/d3/a0-a3/sp/pc and the whole stack frame equal stock (only the dead call return-address slot (0,sp) differs: 0x602ed76d in stock, 0x605a7707 in the trampoline); counter +1 only for the press '
          f'event 0x02000100 (0xffff wraps to 0); image mapped read-only, no fault')
    # mutations: each must be caught
    def mutate(f):
        im = bytearray(patched)
        f(im)
        return bytes(im)
    t0 = T - BASE
    code = bytes(patched[t0:t0 + nh['tramp_len']])
    i_add = code.index(bytes.fromhex('2901'))
    muts = {'increment 2': lambda im: im.__setitem__(slice(t0 + i_add, t0 + i_add + 2), bytes.fromhex('2902')),
            'counter at +4': lambda im: im.__setitem__(slice(t0 + code.index(bytes.fromhex('fc8762a2')), t0 + code.index(bytes.fromhex('fc8762a2')) + 4), bytes.fromhex('fc8764a2')),
            'result d0 clobbered': lambda im: im.__setitem__(slice(t0 + code.index(bytes.fromhex('2901')), t0 + code.index(bytes.fromhex('2901')) + 2), bytes.fromhex('2801'))}
    for name, f in muts.items():
        r = compare(mutate(f))
        assert not isinstance(r, int), f'mutation {name!r} was not caught'
        print(f'E  mutation {name}: caught ({r})')
    print('ALL PASS')


if __name__ == '__main__':
    main()

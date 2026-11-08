#!/usr/bin/env python3
"""Verify patches/om5_color_core (OM-5 COLOR 1-3 back end: the profile's saturation values reach the capture for picture
mode 11). Offline; emulator only. Run in WSL (the fishcu engine is a Linux binary):

  ~/ep7fw-toolchains/venv/bin/python -I tools/om5_verify_color_core.py [patches/om5_color_core/out/edits.json]
        [--package PATCHED.pkg.bin] [--mod-version 0.4]

  D  diff: the package is built with tools/build_package.py (library, --body om5) from the colour-core edits merged with the
     firmware-screen label (patches/om5_fw_version, --mod-version). Expanded main == stock + the edits and differs from stock
     at no other byte; only the section-6 LZ4 span 0xc9d7e0 is re-encoded and the other span is byte-identical; restore ==
     the stock main block; the spec equals a fresh run of build.py; the colour-only spec builds with no span touched and gives
     expanded main == stock + its 2 edits. Files are written to a new temp directory (not SD-named).
  T  targets: the stock hook is `call 0x6005fa63,[],4` after `mov 63210,d0`; the patched hook is `jmp cc_hook` (dc rel32) + cb cb,
     cc_hook is in section 8 (the hook's own section); its last instruction is `jmp 0x614ce697`; exactly 2 calls to getptr with
     frame [],4; no store, no mul/div, no loop; its inner branches stay inside it.
  X  refs: nothing branches or points into the hook interior 0x614ce691..0x614ce696; references to RESUME 0x614ce697 are the stock
     ones plus cc_hook's jmp; the claimed tail is FF in stock and nothing in stock points into it; getptr's callers are the stock
     ones minus the hook plus cc_hook's two calls; the patched image differs from stock only inside the edit ranges.
  E  emulator (fishcu MN103 engine), hook entry -> stop 0x614ce6ac (just after the 12-halfword copy loop), on stock and patched,
     with a getptr stub (modes and profiles below). Mutation checks: each mutant of cc_hook must fail the comparison.
"""
import argparse, hashlib, importlib.util, json, re, struct, sys, tempfile, traceback
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
from penf_emulator import Region, execute  # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CB = _load('om5_color_core_build', 'patches/om5_color_core/build.py')
FB = _load('om5_fw_version_build', 'patches/om5_fw_version/build.py')

BASE = oc5.BASE
OM5 = bodies.OM5
HOOK, HOOK_STOCK, GETPTR, RESUME = CB.HOOK, CB.HOOK_STOCK, CB.GETPTR, CB.RESUME
TRAMP = CB.TRAMP_AT
KEY_EFF, KEY_PROF = CB.KEY_EFFECTIVE, CB.KEY_PROFILES
OFF_MODE, OFF_PROFILE = CB.OFF_MODE, CB.OFF_PROFILE
STOP = 0x614ce6ac                        # first instruction after the copy loop (setlb 0x614ce69e .. lcs 0x614ce6ab)
S_OFF, S_END = 28732, 28732 + 24         # the 12 saturation halfwords cap[28732..28755]
STACK, STACK_SIZE, CAP = 0x18010000, 0x1100, 0x18040000
A210, A226 = 0x18060000, 0x18061000     # profile store for key 63210 (12 halfwords) and 63226 (3 x 12 halfwords)
SPAN = 0xc9d7e0                          # section 6 LZ4 span (main offset)
REF_PAT = r'0x614ce69[1-7]\b|0x615e98(6|7|8|9|a)[0-9a-f]\b'
MUTANTS = [('add -2,d2 for add -1,d2', 'add -1,d2', 'add -2,d2'),
           ('asl 2,d0 for asl 3,d0', 'asl 3,d0', 'asl 2,d0'),
           ('cmp 12,d0 for cmp 11,d0 (mode check)', 'cmp 11,d0', 'cmp 12,d0')]
CASES = ([('mode 11 profile 1', 11, 1, False), ('mode 11 profile 2', 11, 2, False), ('mode 11 profile 3', 11, 3, False),
          ('mode 11 profile 0', 11, 0, False), ('mode 11 profile 4', 11, 4, False), ('mode 11 profile 255', 11, 255, False)]
         + [(f'mode {m} profile 1', m, 1, False) for m in (1, 2, 3, 5, 6, 10, 12, 13, 100)]
         + [('mode 11 profile 2, key 63226 absent', 11, 2, True)])

PASSES, FAILS = [], []
RUNS = 0


def check(sec, label, ok, detail=''):
    if ok:
        PASSES.append(label)
        print(f'PASS {sec}  {label}')
    else:
        FAILS.append(f'{sec} {label}')
        print(f'FAIL {sec}  {label}' + (f' -- {detail}' if detail else ''))
    return bool(ok)


def guarded(sec, fn, *args):
    try:
        return fn(*args)
    except (Exception, SystemExit) as e:
        check(sec, 'section ran to the end', False, f'{type(e).__name__}: {e}\n{traceback.format_exc()}')
        return None


def sext(b):
    return int.from_bytes(b, 'little', signed=True)


def apply(img, edits):
    b = bytearray(img)
    for e in edits:
        ad, new = int(e['addr'], 16), bytes.fromhex(e['new'])
        b[ad - BASE:ad - BASE + len(new)] = new
    return bytes(b)


def edit_ranges(edits):
    return [(int(e['addr'], 16) - BASE, int(e['addr'], 16) - BASE + len(bytes.fromhex(e['new']))) for e in edits]


def diff_positions(a, b):
    import numpy as np
    return np.nonzero(np.frombuffer(a, np.uint8) != np.frombuffer(b, np.uint8))[0].tolist()


_W = {}


def _w(img):
    import numpy as np
    k = id(img)
    if k not in _W:
        a = np.frombuffer(img, dtype=np.uint8).astype(np.uint32)
        _W[k] = (img, a[:-3] | (a[1:-2] << 8) | (a[2:-1] << 16) | (a[3:] << 24))
    return _W[k][1]


def words(img, val):
    """Addresses (any byte alignment) of 32-bit words equal to val."""
    import numpy as np
    return [int(i) + BASE for i in np.nonzero(_w(img) == np.uint32(val))[0]]


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


def disasm(code, at):
    """objdump (am33-2) of a byte string placed at `at` -> [(addr, text)], .byte lines dropped."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / 'code.bin'
        f.write_bytes(code)
        sh = (f'set -e\n$HOME/ep7fw-toolchains/mn10300/bin/mn10300-elf-objdump -b binary -m am33-2 -D --adjust-vma={at:#x} '
              f'{oc5.wsl_path(f)} | tail -n +8\n')
        txt = oc5.run_sh(sh, d)
    ins = [(int(m.group(1), 16), m.group(2).strip()) for m in re.finditer(r'^\s*([0-9a-f]{8}):\t[0-9a-f ]+\t(\S.*)$', txt, re.M)]
    return [x for x in ins if not x[1].startswith('.byte')]


def grep_refs(img, tag):
    """Lines of the whole image's objdump (am33-2) whose instruction text names an address of REF_PAT. [(addr, text)]"""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / f'{tag}.bin'
        f.write_bytes(img)
        sh = ('set -e\nO=$HOME/ep7fw-toolchains/mn10300/bin/mn10300-elf-objdump\n'
              f"$O -b binary -m am33-2 -D --adjust-vma={BASE:#x} {oc5.wsl_path(f)} | tail -n +8 | grep -E '{REF_PAT}' || true\n")
        txt = oc5.run_sh(sh, d)
    out = []
    for ln in txt.splitlines():
        m = re.match(r'^\s*([0-9a-f]{8}):\t[0-9a-f ]+\t(\S.*)$', ln)
        if m:
            out.append((int(m.group(1), 16), m.group(2).strip()))
    return out


def targets(text):
    return {int(h, 16) for h in re.findall(r'0x([0-9a-f]+)', text)}


def asm(src, at):
    out, _ = oc5.assemble(f' .section .s,"ax"\n{src}\n', {'.s': at}, {})
    return out['.s']


def assemble_tramp(src):
    out, _ = oc5.assemble(src, {'.tramp': TRAMP, '.hook': HOOK}, {'getptr': GETPTR, 'resume': RESUME})
    return out['.tramp']


# ---------------------------------------------------------------------------------------------------------------------
def section_d(spec, fw_edits, mod_version, stock_img, package):
    sec = 'D'
    stock_file = (ROOT / 'stock_firmware/om5' / OM5.stock_name).read_bytes()
    check(sec, 'stock file sha256 is the OM-5 stock', hashlib.sha256(stock_file).hexdigest() == OM5.stock_sha)
    fresh, _ = CB.build(save_claims=False)
    check(sec, 'spec edits == a fresh color_core build (claims not saved)', fresh['edits'] == spec['edits'])
    check(sec, 'colour-core spec has its 2 edits (hook, cc_hook)', len(spec['edits']) == 2)
    merged = dict(components=['0x100'], edits=fw_edits + spec['edits'])
    pkg, restore, info = BP.build(merged, stock_file, OM5)
    outdir = Path(tempfile.mkdtemp(prefix='om5_color_core_')) / 'pkg'
    outdir.mkdir()
    (outdir / 'patched.pkg.bin').write_bytes(pkg)
    (outdir / 'restore-stock.pkg.bin').write_bytes(restore)
    (outdir / 'build.json').write_text(json.dumps(info, indent=2) + '\n')
    (outdir / 'spec.json').write_text(json.dumps(merged, indent=1) + '\n')
    print(f'     package written to {outdir} (patched sha256 {hashlib.sha256(pkg).hexdigest()})')
    if package:
        check(sec, f'{package} is byte-identical to the package built here', package.read_bytes() == pkg)

    blocks = {b.kind: b for b in oc.parse(pkg)}
    sblk = {b.kind: b for b in oc.parse(stock_file)}
    check(sec, 'package components are the main block only', list(blocks) == [0x100], str(list(blocks)))
    exp = oz.expand(blocks[0x100].payload)
    want = apply(stock_img, merged['edits'])
    check(sec, 'expanded main == stock + the merged edits', exp == want)
    d = diff_positions(stock_img, exp) if len(exp) == len(stock_img) else []
    rng = edit_ranges(merged['edits'])
    stray = [i for i in d if not any(a <= i < b for a, b in rng)]
    check(sec, f'expanded main differs from stock only inside the edits ({len(d)} B differ)', not stray,
          f'stray byte at {stray[0] + BASE:#x}' if stray else '')
    sp_s = oz.find(sblk[0x100].payload)
    sp_p = oz.find(blocks[0x100].payload)
    check(sec, 'LZ4 container layout unchanged', [(c.offset, c.raw) for c in sp_s] == [(c.offset, c.raw) for c in sp_p])
    check(sec, 'only the section-6 span 0xc9d7e0 is re-encoded', info.get('recompressed_spans') == ['0xc9d7e0'],
          str(info.get('recompressed_spans')))
    for c in sp_s:
        pb = blocks[0x100].payload[c.offset:c.offset + c.raw]
        sb = sblk[0x100].payload[c.offset:c.offset + c.raw]
        if c.offset == SPAN:
            check(sec, f'span {c.offset:#x} (section 6) is the re-encoded one and differs from stock', pb != sb)
        else:
            check(sec, f'span {c.offset:#x} is byte-identical to stock', pb == sb)
    sb0 = sblk[0x100]
    check(sec, 'restore == the stock main block (byte-exact)', restore == stock_file[sb0.offset:sb0.offset + 64 + len(sb0.payload)])

    co = dict(components=['0x100'], edits=spec['edits'])
    pk2, _, info2 = BP.build(co, stock_file, OM5)
    check(sec, 'colour-only spec builds with no compressed span touched', info2.get('recompressed_spans') == [],
          str(info2.get('recompressed_spans')))
    exp2 = oz.expand(oc.parse(pk2)[0].payload)
    check(sec, 'colour-only expanded main == stock + its 2 edits', exp2 == apply(stock_img, spec['edits']))
    p2 = {b.kind: b for b in oc.parse(pk2)}[0x100].payload
    check(sec, 'colour-only: every span byte-identical to stock',
          all(p2[c.offset:c.offset + c.raw] == sblk[0x100].payload[c.offset:c.offset + c.raw] for c in sp_s))
    return merged


# ---------------------------------------------------------------------------------------------------------------------
def section_t(patched, n):
    sec = 'T'
    st = oc5.stock(HOOK, 7)
    check(sec, 'stock hook is `call 0x6005fa63,[],4` (dd rel32 00 04)',
          st == HOOK_STOCK and st[0] == 0xdd and (HOOK + sext(st[1:5])) & 0xffffffff == GETPTR and st[5:] == bytes([0, 4]), st.hex())
    check(sec, 'stock: `mov 63210,d0` (6 B) before the hook and RESUME = `clr d0; clr d2`',
          oc5.stock(HOOK - 6, 6) == bytes.fromhex('fccceaf60000') and int.from_bytes(oc5.stock(HOOK - 4, 4), 'little') == KEY_EFF
          and oc5.stock(RESUME, 2) == bytes([0x00, 0x08]))
    ph = patched[HOOK - BASE:HOOK - BASE + 7]
    tgt = (HOOK + sext(ph[1:5])) & 0xffffffff
    check(sec, f'patched hook = dc rel32 cb cb with target cc_hook {TRAMP:#x}',
          ph[0] == 0xdc and tgt == TRAMP and ph[5:] == b'\xcb\xcb' and TRAMP == 0x615e9860, f'{ph.hex()} -> {tgt:#x}')
    sh_, st_ = oc5.section_of(HOOK), oc5.section_of(TRAMP)
    check(sec, 'hook and cc_hook are in section 8 (plain), cc_hook inside its loaded part',
          sh_.idx == st_.idx == 8 and not sh_.compressed and TRAMP + n <= st_.loaded_end, f'{sh_} {st_}')
    ins = disasm(patched[TRAMP - BASE:TRAMP - BASE + n], TRAMP)
    check(sec, 'cc_hook last instruction is `jmp 0x614ce697` (RESUME)',
          bool(ins) and re.fullmatch(r'jmp\s+0x%x' % RESUME, ins[-1][1]) is not None, ins[-1][1] if ins else 'empty')
    calls = [k for k, (_, t) in enumerate(ins) if t.startswith('call')]
    check(sec, 'exactly 2 calls, both `call 0x6005fa63,[],4`',
          len(calls) == 2 and all(re.fullmatch(r'call\s+0x%x,\[\],4' % GETPTR, ins[k][1]) for k in calls),
          str([ins[k][1] for k in calls]))
    stores = [t for _, t in ins if re.match(r'mov', t) and re.search(r',\s*\(', t)]
    check(sec, 'no store instruction in cc_hook', not stores, str(stores))
    check(sec, 'no mul/div in cc_hook', not [t for _, t in ins if re.match(r'(mul|div)', t)])
    check(sec, 'no loop and no return in cc_hook', not [t for _, t in ins if re.match(r'(setlb|lne|lcc|lcs|lra|lea|leq|llt|ret)', t)])
    branches = [(a, re.match(r'b[a-z]+\s+0x([0-9a-f]+)', t)) for a, t in ins]
    bad = [hex(a) for a, m in branches if m and not TRAMP <= int(m.group(1), 16) < TRAMP + n]
    check(sec, 'inner branches of cc_hook stay inside cc_hook', not bad, str(bad))
    rets = {ins[k + 1][0] for k in calls}
    check(sec, 'trampoline is the 66 B claim at 0x615e9860', n == 66, f'n={n}')
    return ins, rets


# ---------------------------------------------------------------------------------------------------------------------
def section_x(stock, patched, ins, n):
    sec = 'X'
    H = HOOK
    for t in range(H + 1, H + 7):
        check(sec, f'no abs32 word in stock or patched equals the hook interior {t:#x}', not words(stock, t) and not words(patched, t),
              str([hex(x) for x in words(stock, t) + words(patched, t)]))
    for t in range(H + 1, H + 7):
        check(sec, f'no call16/call32 target {t:#x} in stock or patched', not calls_to(stock, t) and not calls_to(patched, t))
    ls = grep_refs(stock, 'stock')
    lp = grep_refs(patched, 'patched')
    interior = lambda tg: any(H + 1 <= x <= H + 6 for x in tg)
    check(sec, 'no branch/jump/immediate text names the hook interior (stock, patched)',
          not [a for a, t in ls + lp if interior(targets(t))], str([hex(a) for a, t in ls + lp if interior(targets(t))][:5]))
    sres = {a for a, t in ls if RESUME in targets(t)}
    pres = {a for a, t in lp if RESUME in targets(t)}
    jmp_addr = {a for a, t in ins if re.fullmatch(r'jmp\s+0x%x' % RESUME, t)}
    jmp_abs = {a for a in jmp_addr}
    check(sec, 'references to RESUME: stock ones kept, the only new one is the cc_hook jmp',
          sres <= pres and pres - sres == jmp_abs, f'stock {sorted(hex(a) for a in sres)} '
          f'patched {sorted(hex(a) for a in pres)} new jmp {sorted(hex(a) for a in jmp_abs)}')
    tail = range(TRAMP, TRAMP + n)
    check(sec, 'claimed tail 0x615e9860..0x615e98a2 is FF in stock', oc5.stock(TRAMP, n) == b'\xff' * n)
    check(sec, 'nothing in stock points into the claimed tail (abs32 words, branch/call text)',
          not words_in(stock, TRAMP, TRAMP + n) and not [a for a, t in ls if any(x in tail for x in targets(t))])
    into = sorted({a for a, t in lp if any(x in tail for x in targets(t))})
    allowed = {HOOK} | {a for a, _ in ins}
    check(sec, 'in the patched image only the hook (new jmp) and cc_hook itself point into the tail',
          set(into) <= allowed, str([hex(a) for a in into]))
    cs, cp = calls_to(stock, GETPTR), calls_to(patched, GETPTR)
    tramp_calls = {a for a, t in ins if t.startswith('call')}
    expect = (set(cs) - {HOOK}) | tramp_calls
    check(sec, 'getptr callers = stock callers minus the hook plus the 2 calls of cc_hook',
          set(cp) == expect, f'missing {sorted(hex(x) for x in expect - set(cp))} extra {sorted(hex(x) for x in set(cp) - expect)}')
    return None


def section_x_edits(stock, patched, ranges):
    sec = 'X'
    d = diff_positions(stock, patched)
    stray = [i for i in d if not any(a <= i < b for a, b in ranges)]
    check(sec, f'patched image differs from stock only inside the edit ranges ({len(d)} B; 2 colour + 4 fw sites)', not stray,
          hex(stray[0] + BASE) if stray else '')


# ---------------------------------------------------------------------------------------------------------------------
def stub_src(absent):
    first = 'mov 0,a0' if absent else f'mov {A226:#x},a0'
    return f"""
 cmp {KEY_PROF},d0
 bne 1f
 {first}
 ret [],4
1:
 cmp {KEY_EFF},d0
 bne 2f
 mov {A210:#x},a0
 ret [],4
2:
 mov 0,a0
 ret [],4
"""


def s_vals(cap):
    return [struct.unpack_from('<H', cap, S_OFF + 2 * i)[0] for i in range(12)]


def expect_s(mode, profile, absent):
    if mode == 11 and profile in (1, 2, 3) and not absent:
        return [200 + 12 * (profile - 1) + i for i in range(12)]
    return [100 + i for i in range(12)]


def run_case(img, mode, profile, stub):
    global RUNS
    RUNS += 1
    sp0 = STACK + 0x1000
    stack = bytearray(STACK_SIZE)
    for i in range(0, STACK_SIZE, 4):
        struct.pack_into('<I', stack, i, 0xc0de0000 + i)
    cap = bytearray(0x10000)
    cap[OFF_MODE], cap[OFF_PROFILE] = mode, profile
    im = bytearray(img)
    im[GETPTR - BASE:GETPTR - BASE + len(stub)] = stub
    a210 = b''.join(struct.pack('<H', 100 + i) for i in range(12))
    a226 = b''.join(struct.pack('<H', 200 + i) for i in range(36))
    regs = {'sp': sp0, 'pc': HOOK, 'd0': KEY_EFF, 'd1': 0x4d1, 'd2': 0x4d2, 'd3': CAP + OFF_MODE,
            'a0': 0xa0a0, 'a1': 0x11a1, 'a2': 0x22a2, 'a3': CAP}
    r = execute([Region('img', BASE, bytes(im), 'rx'),
                 Region('stack', STACK, bytes(stack), 'rw', capture=True),
                 Region('cap', CAP, bytes(cap), 'rw', capture=True),
                 Region('a210', A210, a210, 'r'), Region('a226', A226, a226, 'r')],
                regs, STOP, limit=10_000, timeout=120)
    mem = {m['name']: bytes.fromhex(m['hex']) for m in r['memory']}
    return dict(regs=r['registers'], stack=mem['stack'], cap=mem['cap'], fault=r['fault'], steps=r['completed'])


def check_case(mode, profile, absent, img_s, img_p, stub, rets):
    rs, rp = run_case(img_s, mode, profile, stub), run_case(img_p, mode, profile, stub)
    bad = []
    for tag, r in (('stock', rs), ('patched', rp)):
        if r['fault']:
            bad.append(f'{tag} fault {r["fault"]}')
    if bad:
        return bad, rs, rp
    want_s, want_p = [100 + i for i in range(12)], expect_s(mode, profile, absent)
    if s_vals(rs['cap']) != want_s:
        bad.append(f'stock S {s_vals(rs["cap"])} != {want_s}')
    if s_vals(rp['cap']) != want_p:
        bad.append(f'patched S {s_vals(rp["cap"])} != {want_p}')
    if rs['regs']['pc'] != STOP or rp['regs']['pc'] != STOP:
        bad.append(f'stop pc stock {rs["regs"]["pc"]:#x} patched {rp["regs"]["pc"]:#x}')
    for k in ('d0', 'd1', 'd2', 'd3', 'a1', 'a2', 'a3', 'sp'):
        if rs['regs'][k] != rp['regs'][k]:
            bad.append(f'{k} stock {rs["regs"][k]:#x} patched {rp["regs"][k]:#x}')
    dif = [i for i in range(0, STACK_SIZE, 4) if rs['stack'][i:i + 4] != rp['stack'][i:i + 4]]
    for i in dif:
        sv = struct.unpack_from('<I', rs['stack'], i)[0]
        pv = struct.unpack_from('<I', rp['stack'], i)[0]
        if sv != RESUME or pv not in rets:
            bad.append(f'stack +{i:#x} stock {sv:#x} patched {pv:#x}')
    if rs['cap'][:S_OFF] + rs['cap'][S_END:] != rp['cap'][:S_OFF] + rp['cap'][S_END:]:
        bad.append('cap bytes outside 28732..28755 differ')
    return bad, rs, rp, dif


def section_e(stock, patched, n, ins, rets):
    sec = 'E'
    stub_main, stub_abs = asm(stub_src(False), GETPTR), asm(stub_src(True), GETPTR)
    tramp = patched[TRAMP - BASE:TRAMP - BASE + n]
    check(sec, 'the unmodified cc_hook source re-assembles to the patched bytes', assemble_tramp(CB.SOURCE) == tramp)
    for name, mode, profile, absent in CASES:
        stub = stub_abs if absent else stub_main
        out = check_case(mode, profile, absent, stock, patched, stub, rets)
        bad, rs, rp = out[0], out[1], out[2]
        if bad:
            check(sec, f'{name}: stock vs patched', False, '; '.join(bad))
            continue
        dif = out[3]
        ndiff = [(hex(rs['regs'][k]), hex(rp['regs'][k])) for k in ('a0',)]
        steps = rp['steps'] - rs['steps']
        check(sec, f'{name}: patched S = {s_vals(rp["cap"])[:3]}.. ({"200+12*(p-1)" if expect_s(mode, profile, absent)[0] >= 200 else "stock 100+i"}), '
                   f'stock S = 100+i, regs/stack/cap equal (stack diff {len(dif)} return-slot word(s)), '
                   f'steps stock {rs["steps"]} patched {rp["steps"]}, a0 stock/patched {ndiff[0][0]}/{ndiff[0][1]}',
              0 < steps <= 40 and steps > 0)
    # mutation checks: each mutant of cc_hook must make the comparison fail
    caught = 0
    for label, old, new in MUTANTS:
        if CB.SOURCE.count(old) != 1:
            check(sec, f'mutation {label}: source contains {old!r} once', False, str(CB.SOURCE.count(old)))
            continue
        mut = assemble_tramp(CB.SOURCE.replace(old, new))
        if mut == tramp:
            check(sec, f'mutation {label}: assembles to different bytes', False)
            continue
        img_m = bytearray(patched)
        img_m[TRAMP - BASE:TRAMP - BASE + len(mut)] = mut
        img_m = bytes(img_m)
        hit = None
        for name, mode, profile, absent in CASES:
            if absent:
                continue
            bad = check_case(mode, profile, absent, stock, img_m, stub_main, rets)[0]
            if bad:
                hit = (name, bad[0])
                break
        if hit:
            caught += 1
            check(sec, f'mutation {label}: caught by {hit[0]} ({hit[1]})', True)
        else:
            check(sec, f'mutation {label}: NOT caught', False)
    check(sec, f'{len(CASES)} cases x (stock, patched) runs + {len(MUTANTS)} mutants: {RUNS} engine runs, {caught} of '
               f'{len(MUTANTS)} mutations caught', caught == len(MUTANTS))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('spec', type=Path, nargs='?', default=ROOT / 'patches/om5_color_core/out/edits.json')
    ap.add_argument('--package', type=Path)
    ap.add_argument('--mod-version', default='0.4')
    a = ap.parse_args()
    spec = json.loads(a.spec.read_text())
    stock = oc5.image()
    frag, _ = CB.build(save_claims=False)
    n = frag['color_core']['tramp_len']
    fw, _ = FB.build(a.mod_version, save_claims=False)
    patched = apply(stock, fw['edits'] + spec['edits'])
    ranges = edit_ranges(fw['edits'] + spec['edits'])

    guarded('D', section_d, spec, fw['edits'], a.mod_version, stock, a.package)
    tr = guarded('T', section_t, patched, n)
    if tr is None:
        ins, rets = [], set()
    else:
        ins, rets = tr
    guarded('X', section_x, stock, patched, ins, n)
    guarded('X', section_x_edits, stock, patched, ranges)
    guarded('E', section_e, stock, patched, n, ins, rets)

    print(f'PASS lines: {len(PASSES)}   FAIL lines: {len(FAILS)}   engine runs: {RUNS}')
    if FAILS:
        print('FAILED:')
        for f in FAILS:
            print('  ' + f)
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    main()

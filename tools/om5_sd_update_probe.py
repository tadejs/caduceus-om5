#!/usr/bin/env python3
"""Offline emulator probe of the OM-5 1.2 firmware-update validator/writer.

Modelled on tools/ep7_sd_update_probe.py. Runs the NATIVE OM-5 routine 0x60ce6846 (the
counterpart of the E-P7 0x60c964c1; same code apart from the model constant) on `OE` update
containers held only in emulator RAM. Nothing is written to disk except an optional JSON
report; no file with an SD-update name is ever created. The OM-5 firmware image is read from
analysis/om5_1200/main.expanded.bin (run-time image, base 0x60021020; the old decoded file has
compressed spans). Needs the fishcu MN103 engine, so run it in WSL:

  ~/ep7fw-toolchains/venv/bin/python -I tools/om5_sd_update_probe.py \
      [--stock stock_firmware/om5/OLY_E_144_1200_0000_0000.BIN] \
      [--patched path/to/patched.pkg.bin] [--restore path/to/restore-stock.pkg.bin] [--json out.json]

Real packages (24.9 MB main, 92 MB full file) need more than the engine's 100M-instruction cap
for checksum + descramble, so the run is chained: each slice's registers and RAM are fed to a
fresh engine process until the validator returns (state is fully carried over).

Mode argument (d1), as in the E-P7 (FIRMUP_SRV): 0 = SD request (validate all, then write),
2 = USB check (validate only), 1 = USB commit (skip checksum, write).

Stubs (emulator RAM only):
  0x60023512 / 0x60023703  debug log -> plain `ret` with the callee's frame.
  0x6005c9b7               flash write -> logs (dst a0, src a1, size d0, mode d1), returns 0.
  0x60ce7628               LUKE (sub-processor) send -> logs (a0, d1) separately, returns 0.
"""
import argparse, hashlib, json, os, struct, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'external/oly_fw_analysis/tools'))
from penf_emulator import execute, Region  # noqa: E402  (generic runner only)

IMAGE = ROOT / 'analysis/om5_1200/main.expanded.bin'
IMAGE_LEN = 24903616
BASE = 0x60021020                                        # run-time address of image offset 0
STOCK = ROOT / 'stock_firmware/om5/OLY_E_144_1200_0000_0000.BIN'
TC = Path(os.environ.get('MN10300_BIN', Path.home() / 'ep7fw-toolchains/mn10300/bin'))

MODEL = b'\x14\x40'                                      # OM-5; the E-P7 expects 14 20
VALIDATE = 0x60ce6846                                    # E-P7 0x60c964c1
CODE_LO, CODE_HI = 0x60ce5a00, 0x60ce7a00                # checksum 0x60ce5ad2, descramble 0x60ce5b9b,
                                                         # validator, writer wrapper 0x60ce7566
HDR_LO, HDR_HI = 0x60110240, 0x60110360                  # header helpers; splitter 0x601102c0, O/E check 0x60110310
STRCMP_LO, STRCMP_HI = 0x600ee67e, 0x600ee6a2
ADDR_LO, ADDR_HI = 0x6006937d, 0x600693c1                # 0x4xxxxxxx -> flash alias mapping
DATA_LO, DATA_HI = 0x60f79688, 0x60f79f88                # log strings + kind jump table (E-P7 0x60f2d000 + 0x4c688)
OSTR = 0x601c4180                                        # 8 bytes of string data used by 0x60110310
FLASH_WRITE = 0x6005c9b7                                 # E-P7 0x600c4f35
LUKE_SEND = 0x60ce7628                                   # E-P7 0x60c972a7
LOG_A, LOG_B = 0x60023512, 0x60023703                    # E-P7 0x6002247c / 0x6002266d
SUFFIX, SUFFIX_END = 0x60ce6358, 0x60ce63ad              # E-P7 0x60c95fd8

CALLER, STACK, BUF, LOG, LOG2 = 0x18000000, 0x18100000, 0x20000000, 0x1c000000, 0x1c100000
PERM = [1, 3, 0, 2, 6, 4, 7, 5, 11, 10, 9, 8, 13, 14, 12, 15]   # decoded[i] = ~enc[PERM[i]]

STUB = r"""
 .am33_2
 .text
 .global _start
_start:
 mov {log},a2
 mov (a2),d2
 mov d2,d3
 asl 4,d3
 add d3,a2
 mov a0,(4,a2)
 mov a1,(8,a2)
 mov d0,(12,a2)
 mov d1,(16,a2)
 inc d2
 mov {log},a2
 mov d2,(a2)
 clr d0
 ret {ret}
"""
# flash write is called with [d2,d3,a2,a3],36 ; LUKE send with [d2,d3,a2],32 (see the call sites)
RET_FLASH, RET_LUKE = '[d2,d3,a2,a3],36', '[d2,d3,a2],32'


def image():
    d = IMAGE.read_bytes()
    assert len(d) == IMAGE_LEN, 'unexpected OM-5 expanded image length'
    return d


def encode(dec):
    out = bytearray(len(dec))
    for g in range(0, len(dec), 16):
        for i, p in enumerate(PERM):
            out[g + p] = dec[g + i] ^ 0xff
    return bytes(out)


def decode(enc):
    out = bytearray(len(enc))
    for g in range(0, len(enc), 16):
        for i, p in enumerate(PERM):
            out[g + i] = enc[g + p] ^ 0xff
    return bytes(out)


def component(kind=0x100, version=0x1200, model=MODEL, sig=b'OE', payload=None,
              address=0x40780000, break_sum=False, break_trailer=False):
    payload = payload if payload is not None else bytes((i * 7 + 3) & 0xff for i in range(0x400))
    assert len(payload) % 16 == 0
    hdr = bytearray(32)
    hdr[0:2], hdr[2:4] = sig, model
    struct.pack_into('<IIHHH', hdr, 8, address, len(payload), version, 0, kind)
    trl = bytearray(hdr)
    words = struct.unpack(f'<{(32 + len(payload) + 28) // 4}I', bytes(hdr) + payload + bytes(trl[:28]))
    struct.pack_into('<I', trl, 28, (-sum(words)) & 0xffffffff)
    if break_sum:
        trl[28] ^= 1
    if break_trailer:
        trl[16] ^= 1                                      # build field differs header vs trailer
    return bytes(hdr) + encode(payload) + bytes(trl)


def flash_alias(a):
    """Python model of 0x6006937d (E-P7 0x60067102): 0x40xxxxxx / 0x6xxxxxxx -> 0x80xxxxxx."""
    if 0x40000000 <= a < 0x41000000 or 0x60000000 <= a < 0x80000000:
        return (a & 0x3fffffff) | 0x80000000
    return a


def asm_stub(log, ret, at):
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        (t / 's.s').write_text(STUB.format(log=hex(log), ret=ret))
        subprocess.run([TC / 'mn10300-elf-as', '-o', t / 's.o', t / 's.s'], check=True)
        subprocess.run([TC / 'mn10300-elf-ld', '-Ttext', hex(at), '-e', '_start',
                        '-o', t / 's.elf', t / 's.o'], check=True)
        subprocess.run([TC / 'mn10300-elf-objcopy', '-O', 'binary', '-j', '.text', t / 's.elf', t / 's.bin'], check=True)
        return (t / 's.bin').read_bytes()


def regions_for(d, stubs, pkg, fixed):
    """Validator regions. `stubs` = {address: bytes} carved out of the code range."""
    img = lambda n, lo, hi, p='rx': Region(n, lo, d[lo - BASE:hi - BASE], p)
    code, cur = [], CODE_LO
    for i, (a, b) in enumerate(sorted((a, b) for a, b in stubs.items() if CODE_LO <= a < CODE_HI)):
        code += [img(f'code{i}', cur, a), Region(f'stub{i}', a, b, 'rx')]
        cur = a + len(b)
    code.append(img('code_end', cur, CODE_HI))
    other = [Region(f'stub_x{a:x}', a, b, 'rx') for a, b in stubs.items() if not CODE_LO <= a < CODE_HI]
    return [
        Region('caller', CALLER, b'\xdd' + struct.pack('<i', VALIDATE - CALLER) + b'\xf0\x10\xcb', 'rx'),
        *code, *other,
        img('hdr', HDR_LO, HDR_HI), img('strcmp', STRCMP_LO, STRCMP_HI), img('addr', ADDR_LO, ADDR_HI),
        img('data', DATA_LO, DATA_HI, 'r'), img('ostr', OSTR, OSTR + 8, 'r'),
        Region('stack', STACK - 0x4000, bytes(0x4000), 'rw', True),
        Region('buf', BUF, pkg + bytes(0x1000), 'rw', True),   # zero tail: firmware does not know the file length
        Region('log', LOG, bytes(0x400), 'rw', True),
        Region('log2', LOG2, bytes(0x400), 'rw', True),
    ]


def run(d, stubs, pkg, mode, slice_timeout=1800):
    """Run the validator to completion, chaining 100M-instruction slices (full state carried)."""
    regions = regions_for(d, stubs, pkg, True)
    regs = {'pc': CALLER, 'sp': STACK - 0x100, 'a0': BUF, 'd0': len(pkg), 'd1': mode, 'a1': 0,
            'd2': 0x2222, 'd3': 0x3333, 'a2': 0xa2a2, 'a3': 0xa3a3}
    total, slices = 0, 0
    while True:
        r = execute(regions, regs, CALLER + 7, limit=100_000_000, timeout=slice_timeout)
        total += r['completed']
        slices += 1
        mem = {m['name']: bytes.fromhex(m['hex']) for m in r['memory']}
        done = r['registers']['pc'] == CALLER + 7 or r['fault'] or r['status'] != 'instruction_limit'
        if done:
            break
        regs = dict(r['registers'])
        regions = [Region(g.name, g.address, mem.get(g.name, g.data), g.permissions, g.capture) for g in regions]
    rd = lambda name, base: [dict(zip(('a0', 'a1', 'd0', 'd1'), struct.unpack_from('<4I', mem[name], 4 + 16 * i)))
                             for i in range(struct.unpack_from('<I', mem[name], 0)[0])]
    writes = [dict(dst=w['a0'], src=w['a1'], size=w['d0'], mode=w['d1']) for w in rd('log', 0)]
    luke = [dict(buf=w['a0'], arg=w['d1']) for w in rd('log2', 0)]
    rr = r['registers']
    return dict(returned=rr['pc'] == CALLER + 7, status=rr['d0'] & 0xffff, fault=r['fault'],
                instructions=total, slices=slices, writes=writes, luke=luke, buf=mem['buf'],
                callee_saved=(rr['d2'], rr['d3'], rr['a2'], rr['a3']) == (0x2222, 0x3333, 0xa2a2, 0xa3a3),
                engine_status=r['status'])


def suffix(d, name):
    regions = [Region('caller', CALLER, b'\xdd' + struct.pack('<i', SUFFIX - CALLER) + b'\xf0\x10\xcb', 'rx'),
               Region('code', SUFFIX, d[SUFFIX - BASE:SUFFIX_END - BASE], 'rx'),
               Region('stack', STACK - 0x1000, bytes(0x1000), 'rw'), Region('name', BUF, name)]
    r = execute(regions, {'pc': CALLER, 'sp': STACK - 0x100, 'a0': BUF + 4, 'd0': 4}, CALLER + 7, limit=2000)
    return r['registers']['d0']


def components(pkg):
    """[(kind, address, length_with_framing, offset)] as the splitter sees them."""
    out, off = [], 0
    while off < len(pkg):
        ln = struct.unpack_from('<I', pkg, off + 12)[0]
        out.append((struct.unpack_from('<H', pkg, off + 20)[0], struct.unpack_from('<I', pkg, off + 8)[0], ln + 64, off))
        off += ln + 64
    return out


# firmware write order (bits tested from 0x60ce6e31, same sequence as E-P7): PIC, RESCOM, OSD, then USR last
ORDER = {0x10e: 0, 0x102: 1, 0x106: 2, 0x101: 3, 0x100: 4}


def expected_writes(pkg):
    exp = [c for c in components(pkg)]
    last = {}
    for c in exp:
        last[c[0]] = c                                    # duplicate kind: last instance wins
    exp = sorted(last.values(), key=lambda c: ORDER[c[0]])
    flash = [(flash_alias(c[1]), BUF + c[3], c[2]) for c in exp if c[0] != 0x10e]
    luke = [BUF + c[3] for c in exp if c[0] == 0x10e]
    return flash, luke


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json')
    ap.add_argument('--stock', default=str(STOCK))
    ap.add_argument('--patched', help='main-component package, recompressed (build_package.py output)')
    ap.add_argument('--restore', help='main-component package, stock main block')
    ap.add_argument('--skip-big', action='store_true', help='synthetic cases only')
    a = ap.parse_args()
    d = image()
    stock_p = Path(a.stock)
    if stock_p.is_file():                                 # self-check of our encoder against stock main payload
        with stock_p.open('rb') as f:
            f.seek(0x20)
            assert decode(f.read(0x1000)) == d[:0x1000], 'scramble model does not match stock'
    stubs = {FLASH_WRITE: asm_stub(LOG, RET_FLASH, FLASH_WRITE), LUKE_SEND: asm_stub(LOG2, RET_LUKE, LUKE_SEND)}
    stubs[LOG_A] = b'\xdf\x30\x1c'                        # ret [a2,a3],28
    stubs[LOG_B] = b'\xdf\xa0\x18'                        # ret [d2,a2],24
    main_c = component(0x100)
    osd_c = component(0x101, version=0x0100, address=0x42140000)
    syn = [
        # name, package, mode, expected status, expected write count
        ('synthetic main-only valid, build 0x1200 (mode 2)', main_c, 2, 0, 0),
        ('synthetic main-only build 0x0000 / 0xffff (mode 2)', None, 2, 0, 0),
        ('bad checksum', component(0x100, break_sum=True), 2, 0x3211, 0),
        ('header/trailer mismatch', component(0x100, break_trailer=True), 2, 0x3202, 0),
        ('E-P7 model 0x1420 rejected', component(0x100, model=b'\x14\x20'), 2, 0x3202, 0),
        ('PEN-F model 0x1220 rejected', component(0x100, model=b'\x12\x20'), 2, 0x3202, 0),
        ('truncated synthetic (trailer cut; RAM beyond is zero)', main_c[:-32], 2, 0x3202, 0),
        ('SD mode: synthetic main -> 1 write', main_c, 0, 0, 1),
        ('SD mode: OSD+main -> 2 writes', osd_c + main_c, 0, 0, 2),
        ('SD mode: main + corrupt OSD -> no write', main_c + component(0x101, version=0x0100, address=0x42140000,
                                                                         break_sum=True), 0, 0x3211, 0),
    ]
    big = []
    if not a.skip_big:
        if stock_p.is_file():
            big.append(('FULL STOCK FILE (5 components), SD mode', stock_p.read_bytes(), 0, 0, None))
        if a.patched:
            big.append(('RECOMPRESSED main package, SD mode', Path(a.patched).read_bytes(), 0, 0, 1))
        if a.restore:
            rp = Path(a.restore).read_bytes()
            big.append(('RESTORE-STOCK main package, SD mode', rp, 0, 0, 1))
            big.append(('RESTORE-STOCK with last trailer byte flipped, SD mode', rp[:-1] + bytes([rp[-1] ^ 1]), 0, 0x3211, 0))
            big.append(('RESTORE-STOCK truncated by 32 bytes (RAM beyond is zero), SD mode', rp[:-32], 0, 0x3202, 0))
    results, ok_all, flash_of = [], True, {}
    for name, pkg, mode, want, nwrites in syn + big:
        if pkg is None:                                   # build 0x0000 and 0xffff
            r0 = [run(d, stubs, component(0x100, version=v), 2) for v in (0x0000, 0xffff)]
            ok = all(r['returned'] and r['status'] == 0 and not r['writes'] for r in r0)
            print(f"{'PASS' if ok else 'FAIL'}  mode=2 status={[hex(r['status']) for r in r0]} {name}")
            ok_all &= ok
            results.append(dict(case=name, passed=ok))
            continue
        t0 = time.time()
        r = run(d, stubs, pkg, mode)
        ok = r['returned'] and r['callee_saved'] and not r['fault'] and r['status'] == want
        if nwrites is not None:
            ok &= len(r['writes']) == nwrites
        detail = ''
        if mode == 0 and want == 0:
            exp_f, exp_l = expected_writes(pkg)
            got = [(w['dst'], w['src'], w['size']) for w in r['writes']]
            ok &= got == exp_f and [w['buf'] for w in r['luke']] == exp_l
            # flash gets header + DESCRAMBLED payload + trailer, in place (checked for the main component)
            for c in components(pkg):
                if c[0] == 0x100:
                    o = c[3]
                    h = hashlib.sha256(r['buf'][o + 32:o + c[2] - 32]).hexdigest()
                    want_h = hashlib.sha256(decode(pkg[o + 32:o + c[2] - 32])).hexdigest()
                    ok &= h == want_h
                    detail += f' main_payload_sha={h[:12]}'
        if r['writes']:
            detail = ', '.join(f"dst={w['dst']:#x} size={w['size']:#x}" for w in r['writes']) + detail
            flash_of[name] = r['writes']
        if r['luke']:
            detail += f"  luke_send={len(r['luke'])}"
        ok_all &= bool(ok)
        results.append(dict(case=name, mode=mode, passed=bool(ok), status=hex(r['status']), want=hex(want),
                            writes=[(hex(w['dst']), hex(w['size'])) for w in r['writes']], luke=len(r['luke']),
                            instructions=r['instructions'], slices=r['slices'], fault=r['fault'],
                            seconds=round(time.time() - t0, 1)))
        print(f"{'PASS' if ok else 'FAIL'}  mode={mode} status={r['status']:#06x} writes={len(r['writes'])} "
              f"insn={r['instructions']:,}  {name}" + (f'\n      [{detail}]' if detail else ''), flush=True)
        if r['fault']:
            print('      fault:', r['fault'])
    # main-component equivalence: recompressed vs restore vs stock-file main write
    # only the real packages: the 0x440-byte synthetic cases use a different payload size by design
    real = ('FULL STOCK FILE', 'RECOMPRESSED main package, SD mode', 'RESTORE-STOCK main package, SD mode')
    mains = {k: [w for w in v if w['dst'] == flash_alias(0x40780000)] for k, v in flash_of.items()
             if k.startswith(real)}
    sig = {k: [(hex(w['dst']), hex(w['size'])) for w in v] for k, v in mains.items() if v}
    if len(sig) > 1:
        same = len({tuple(v) for v in sig.values()}) == 1
        ok_all &= same
        print(('PASS' if same else 'FAIL') + '  main write (dst,size) identical across packages:', sig)
        results.append(dict(case='main write identical', passed=same, detail=sig))
    # file-name suffix: chars 4..7 parsed as hex; the directory callback keeps the largest value > 0
    for nm, want in ((b'E1449999BIN', 0x9999), (b'E1440000BIN', 0), (b'E1429999BIN', 0x9999),
                     (b'E14400A1BIN', 0xa1), (b'E144ffffBIN', 0xffff), (b'E144G001BIN', 1), (b'E144 999BIN', 0)):
        got = suffix(d, nm)
        ok = got == want
        ok_all &= ok
        results.append(dict(case=f'suffix {nm.decode()}', passed=ok, value=hex(got)))
        print(f"{'PASS' if ok else 'FAIL'}  suffix {nm.decode()!r} -> {got:#x}")
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=1))
    print('ALL PASS' if ok_all else 'SOME FAILED')
    return 0 if ok_all else 1


if __name__ == '__main__':
    sys.exit(main())

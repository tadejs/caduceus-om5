#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Shared helpers for OM-5 patches (build side). Offline; no camera.

  sys.path.insert(0, 'patches'); import om5_common as oc5

* Image / sections: the run-time (expanded) main image `analysis/om5_1200/main.expanded.bin`, CPU address = offset +
  0x60021020, and the boot section table (BCS, main file 0x6fd8: 10 x {dest, flash src, flag}); flag 1 = LZ4 span
  (tools/build_package.py re-encodes a touched span, so a patch may edit one, but must say so).
* Claims: every byte a patch writes is claimed first (`Claims.free()` for FF space, `Claims.over()` for existing
  bytes with the stock bytes asserted). A claim is refused when it overlaps another claim of this patch or of any
  other patch listed in `claims.json`, spills across a section edge, lies in a compressed section without
  `compressed=True`, or (free) is not all FF in the stock image.
* assemble(): one generated linker script (section name -> address), stock entry points as symbols, WSL binutils
  (mn10300-elf, -m am33-2). Output sections are checked against their claims.
* edit() / fragment(): the edits.json fragment (kind 0x100, "body": "om5") with `expect` = stock bytes.
"""
import json, os, struct, subprocess, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGE = ROOT / 'analysis/om5_1200/main.expanded.bin'
BASE = 0x60021020
BCS_OFF = 0x6fd8
REGISTRY = Path(__file__).with_name('claims.json')
TC = '$HOME/ep7fw-toolchains/mn10300/bin'          # inside WSL (or the native Linux shell)
_img = None


def image():
    global _img
    if _img is None:
        _img = IMAGE.read_bytes()
    return _img


def stock(addr, n):
    o = addr - BASE
    if not 0 <= o <= len(image()) - n:
        raise ValueError(f'{addr:#x}+{n} outside the image')
    return image()[o:o + n]


def assert_stock(addr, expect):
    """Assert the stock image holds `expect` (bytes) at addr. Returns expect."""
    expect = bytes(expect)
    if stock(addr, len(expect)) != expect:
        raise SystemExit(f'stock bytes at {addr:#x} are not {expect.hex()} (found {stock(addr, len(expect)).hex()})')
    return expect


class Section:
    def __init__(self, idx, start, end, compressed):
        self.idx, self.start, self.end, self.compressed = idx, start, end, compressed

    @property
    def tail(self):
        """(start, length) of the trailing FF run of the section."""
        d = image()
        o = self.end - BASE
        n = 0
        while n < self.end - self.start and d[o - 1 - n] == 0xff:
            n += 1
        return self.end - n, n

    def __repr__(self):
        return f'<sec {self.idx} {self.start:#x}-{self.end:#x}{" LZ4" if self.compressed else ""}>'


def sections():
    d = image()
    assert d[BCS_OFF:BCS_OFF + 4] == b'BCS\0', 'BCS table not where expected'
    ents = [struct.unpack_from('<III', d, BCS_OFF + 4 + 12 * i) for i in range(10)]
    end = BASE + len(d)
    return [Section(i, e[0], ents[i + 1][0] if i + 1 < len(ents) else end, bool(e[2])) for i, e in enumerate(ents)]


def section_of(addr):
    for s in sections():
        if s.start <= addr < s.end:
            return s
    raise ValueError(f'{addr:#x} is in no section')


class Claims:
    """Claim registry. Other patches' claims are read from claims.json; `save()` stores this patch's (replacing its old)."""

    def __init__(self, patch):
        self.patch, self.mine = patch, {}
        self.others = []
        if REGISTRY.exists():
            for p, lst in json.loads(REGISTRY.read_text()).items():
                if p != patch:
                    self.others += [(int(c['addr'], 16), int(c['len']), p, c['what']) for c in lst]

    def _claim(self, name, addr, n, what, compressed):
        s = section_of(addr)
        if addr + n > s.end:
            raise SystemExit(f'claim {name}: {addr:#x}+{n:#x} crosses the end of section {s.idx}')
        if s.compressed and not compressed:
            raise SystemExit(f'claim {name} at {addr:#x} is in compressed section {s.idx} (LZ4, re-encoded by the builder): '
                             'pass compressed=True to accept that')
        for a, l, p, w in [(a, l, self.patch, w) for a, l, w in self.mine.values()] + self.others:
            if addr < a + l and a < addr + n:
                raise SystemExit(f'claim {name} {addr:#x}+{n:#x} overlaps {p}: {w} ({a:#x}+{l:#x})')
        if name in self.mine:
            raise SystemExit(f'claim {name} twice')
        self.mine[name] = (addr, n, what)
        return s

    def free(self, name, addr, n, what='', compressed=False):
        """Claim n bytes of FF space (asserted in the stock image)."""
        s = self._claim(name, addr, n, what, compressed)
        if set(stock(addr, n)) != {0xff}:
            raise SystemExit(f'claim {name}: {addr:#x}+{n:#x} is not all FF in the stock image')
        return s

    def over(self, name, addr, expect, what='', compressed=False):
        """Claim existing bytes; the stock bytes must equal `expect`."""
        assert_stock(addr, expect)
        return self._claim(name, addr, len(expect), what, compressed)

    def save(self):
        reg = json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}
        reg[self.patch] = [dict(addr=hex(a), len=n, what=f'{k}: {w}')
                           for k, (a, n, w) in sorted(self.mine.items(), key=lambda x: x[1][0])]
        REGISTRY.write_text(json.dumps(reg, indent=1) + '\n')


def wsl_path(p):
    s = str(Path(p).resolve()).replace('\\', '/')
    return f'/mnt/{s[0].lower()}{s[2:]}' if os.name == 'nt' else s


def run_sh(script, d):
    sh = Path(d) / 'run.sh'
    sh.write_bytes(script.replace('\r', '').encode())
    cmd = ['wsl', '-d', 'Ubuntu', '--', 'bash', wsl_path(sh)] if os.name == 'nt' else ['bash', str(sh)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f'toolchain failed:\n{r.stdout}\n{r.stderr}')
    return r.stdout


def assemble(src, placements, symbols=None, claims=None):
    """src: assembly text (`.section NAME,"ax"` blocks; .am33_2 is added). placements: {section: address}.
    symbols: {name: stock address} made visible to the code. -> ({section: bytes}, {symbol: address}).
    If `claims` is given, every section's size must fit its claim (name without the dot); `.hook*` must fill it exactly."""
    symbols = symbols or {}
    ld = 'SECTIONS {\n' + ''.join(f'  {s} {a:#x} : {{ *({s}) }}\n' for s, a in placements.items()) + '}\n' + \
         ''.join(f'{k} = {v:#x};\n' for k, v in symbols.items())
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / 'a.s').write_text(' .am33_2\n' + src + '\n')
        (Path(d) / 'a.ld').write_text(ld)
        sh = f'set -e\nT={TC}\ncd {wsl_path(d)}\n$T/mn10300-elf-as -o a.o a.s\n$T/mn10300-elf-ld -T a.ld -e 0 -o a.elf a.o\n'
        sh += ''.join(f'$T/mn10300-elf-objcopy -O binary -j {s} a.elf {s.lstrip(".")}.bin\n' for s in placements)
        sh += '$T/mn10300-elf-nm a.elf > nm.txt\n'
        run_sh(sh, d)
        out = {s: (Path(d) / f'{s.lstrip(".")}.bin').read_bytes() for s in placements}
        syms = {}
        for ln in (Path(d) / 'nm.txt').read_text().splitlines():
            p = ln.split()
            if len(p) == 3:
                syms[p[2]] = int(p[0], 16)
    if claims is not None:
        for s, b in out.items():
            a, n, _ = claims.mine[s.lstrip('.')]
            if a != placements[s] or len(b) > n or (len(b) < n and s.startswith('.hook')):
                raise SystemExit(f'section {s}: {len(b)} B at {placements[s]:#x} vs claim {a:#x}+{n}')
    return out, syms


def edit(addr, new, why, source):
    new = bytes(new)
    return dict(kind='0x100', addr=hex(addr), expect=stock(addr, len(new)).hex(), new=new.hex(), why=why, source=source)


def fragment(edits, **extra):
    """The edits.json fragment: main block only, body om5."""
    return dict(body='om5', components=['0x100'], edits=edits, **extra)


def write(path, frag):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(frag, indent=1) + '\n')

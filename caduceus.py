#!/usr/bin/env python3
"""Caduceus user-side builder for the Olympus E-P7 firmware 1.1 patches.

We do not redistribute OM Digital Solutions firmware. This script downloads the
stock 1.1 update from OM's own server (or uses a copy you already have), checks
its SHA-256, applies a release's edit list (releases/<ver>/edits.json) and
writes the update files for your SD card. Every output hash must match the
release's SHA256SUMS exactly, otherwise nothing is written.

  python3 caduceus.py build --release v0.15 [--out DIR] [--stock FILE]
  python3 caduceus.py verify DIR
  python3 caduceus.py list

Python 3.10+, standard library only. Never talks to a camera.

Container format (scrambled "OE" blocks: byte inversion + 16-byte lane
permutation, additive 32-bit checksum) as documented publicly by Altsoph and
mx3 in CHDK forum thread 2471
(https://chdk.setepontos.com/d/2471-olympus-emjustylus-series-fw-analysis).
This is a small independent implementation; it was cross-checked against
fishcu's oly_container.py (https://github.com/fishcu/oly_fw_analysis), which the
Caduceus project used during development.
"""
import re, argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RELEASES = ROOT / 'releases'

CATALOG = 'https://fw01.om-digitalsolutions.net/ww_inform/fcache.do'


# One record per camera body. Minimal copy of tools/bodies.py (this file must stay a single
# standard-library file); keep the two in step. base = CPU address of main-component payload
# byte 0 (of the EXPANDED run-time image when compressed); card_name = SD-update file name.
class Body:
    def __init__(self, key, label, fw, stock_name, stock_sha, stock_size, hard_code, model_id,
                 base, card_name, mod_dir, compressed):
        self.key, self.label, self.fw, self.stock_name, self.stock_sha = key, label, fw, stock_name, stock_sha
        self.stock_size, self.hard_code, self.model_id, self.base = stock_size, hard_code, model_id, base
        self.card_name, self.mod_dir, self.compressed = card_name, mod_dir, compressed

    @property
    def outputs(self):
        """release SHA256SUMS name -> output path (relative to the output directory)"""
        return {'patched.pkg.bin': f'DCOLYMP/{self.card_name}',
                'restore-stock.pkg.bin': f'restore/{self.card_name}'}


BODIES = {b.key: b for b in (
    Body('ep7', 'E-P7', '1.1', 'OLY_E_142_1100_0000_0000.BIN',
         '9d1610124ad98f6edae4540182205f06c9c703d9033ef0c4f19847193f95eaeb', 111017984,
         'OLYE142000000000', '1420', 0x60020020, 'E1429999.BIN', 'EP7MOD', False),
    Body('om5', 'OM-5', '1.2', 'OLY_E_144_1200_0000_0000.BIN',
         'a0c91f3ac7caaf0925b2af609e711183cf3b5982b3333224bcef26c0c5943d11', 92274688,
         'OLYE144000000000', '1440', 0x60021020, 'E1449999.BIN', 'OM5MOD', True),
)}
EP7 = BODIES['ep7']


class Refuse(Exception):
    pass


def sha(b):
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------- container
LANES = (1, 3, 0, 2, 6, 4, 7, 5, 11, 10, 9, 8, 13, 14, 12, 15)
INV = bytes(i ^ 0xFF for i in range(256))


def decode(data):
    out = bytearray(len(data))
    for o, i in enumerate(LANES):
        out[o::16] = data[i::16].translate(INV)
    return bytes(out)


def encode(data):
    out = bytearray(len(data))
    for i, o in enumerate(LANES):
        out[o::16] = data[i::16].translate(INV)
    return bytes(out)


def sum32(data):
    w = array('I')
    if w.itemsize != 4:
        return sum(x for (x,) in struct.iter_unpack('<I', data)) & 0xFFFFFFFF
    w.frombytes(data)
    if sys.byteorder != 'little':
        w.byteswap()
    return sum(w) & 0xFFFFFFFF


def parse(data):
    """-> list of (offset, header, payload, trailer); payload decoded."""
    blocks, off, model = [], 0, None
    while off < len(data):
        if len(data) - off < 64:
            raise Refuse(f'container: truncated block at {off:#x}')
        hdr = data[off:off + 32]
        if hdr[:2] != b'OE' or struct.unpack_from('<I', hdr, 20)[0] == 0:
            raise Refuse(f'container: unsupported block at {off:#x}')
        model = model or hdr[2:4]
        size = struct.unpack_from('<I', hdr, 12)[0]
        end = off + size + 64
        if hdr[2:4] != model or not size or size % 16 or end > len(data):
            raise Refuse(f'container: bad block header at {off:#x}')
        trl = data[end - 32:end]
        if hdr[:28] != trl[:28] or hdr[28:] != b'\0' * 4:
            raise Refuse(f'container: header/trailer mismatch at {off:#x}')
        pay = decode(data[off + 32:end - 32])
        if (sum32(hdr) + sum32(pay) + sum32(trl)) & 0xFFFFFFFF:
            raise Refuse(f'container: checksum mismatch at {off:#x}')
        blocks.append((off, hdr, pay, trl))
        off = end
    return blocks


def kind(hdr):
    return struct.unpack_from('<H', hdr, 20)[0]


def reseal(hdr, pay, trl):
    t = bytearray(trl)
    t[28:] = b'\0' * 4
    struct.pack_into('<I', t, 28, -(sum32(hdr) + sum32(pay) + sum32(t)) & 0xFFFFFFFF)
    return bytes(t)


# ---------------------------------------------------------------- build
def apply_edits(spec, stock, body=EP7):
    """Same rules and checks as tools/build_package.py. -> (patched, restore)."""
    if spec.get('body', body.key) != body.key:
        raise Refuse(f'edits.json is for body {spec["body"]!r}, not {body.key!r}')
    blocks = parse(stock)
    by_kind = {kind(h): (o, h, p, t) for o, h, p, t in blocks}
    kinds = [int(k, 16) for k in spec['components']]
    if (0x100 in kinds and kinds[0] != 0x100) or len(set(kinds)) != len(kinds) \
            or any(k not in by_kind for k in kinds):
        raise Refuse('edits.json: bad component list')
    orig = {k: by_kind[k][2] for k in kinds}   # what edits are checked and applied against
    comp = []                                  # (offset, raw) of olycompress spans in stock main
    if body.compressed and 0x100 in kinds:
        oz = load_olycompress()
        comp = [(c.offset, c.raw) for c in oz.find(orig[0x100])]
        if not comp:
            raise Refuse('internal: no olycompress span found in main')
        orig[0x100] = oz.expand(orig[0x100])
    pays = {k: bytearray(orig[k]) for k in kinds}
    spans, total = {}, 0
    for e in spec['edits']:
        k = int(e['kind'], 16)
        a = e['addr']
        if k not in pays:
            raise Refuse(f'edits.json: edit {a} targets a component not in the package')
        if a.startswith('off:'):
            off = int(a[4:], 16)
        elif k == 0x100:
            off = int(a, 16) - body.base
        else:
            raise Refuse(f'edits.json: edit {a}: CPU address on a non-main component')
        exp, new = bytes.fromhex(e['expect']), bytes.fromhex(e['new'])
        if len(exp) != len(new) or not new or not 0 <= off <= len(pays[k]) - len(new):
            raise Refuse(f'edits.json: edit {a}: bad length or range')
        if any(off < y and x < off + len(new) for x, y in spans.get(k, [])):
            raise Refuse(f'edits.json: edit {a}: overlaps another edit')
        if orig[k][off:off + len(exp)] != exp:
            raise Refuse(f'edits.json: edit {a}: stock bytes differ from expected')
        spans.setdefault(k, []).append((off, off + len(new)))
        pays[k][off:off + len(new)] = new
        total += sum(x != y for x, y in zip(exp, new))
    touched = set()
    if comp:
        for x, y in spans.get(0x100, []):
            if any(x < o + r and o < y and not (o <= x and y <= o + r) for o, r in comp):
                raise Refuse('edits.json: an edit straddles the edge of a compressed span')
        touched = {o for o, r in comp if any(x < o + r and o < y for x, y in spans.get(0x100, []))}
    parts = []
    for k in kinds:
        _, h, p, t = by_kind[k]
        if k == 0x100 and comp:
            np_ = oz.repack(bytes(pays[k]), p, only=touched) if touched else p
        else:
            np_ = bytes(pays[k])
        parts.append(h + encode(np_) + (reseal(h, np_, t) if np_ != p else t))
    patched = b''.join(parts)
    restore = b''.join(stock[o:o + 64 + len(p)] for o, h, p, t in (by_kind[k] for k in kinds))
    # re-verify outputs from scratch
    rp, rr = parse(patched), parse(restore)
    if [kind(b[1]) for b in rp] != kinds or [kind(b[1]) for b in rr] != kinds:
        raise Refuse('internal: output component list wrong')
    changed = 0
    for (_, h, p, t), (_, sh, sp, st), (_, rh, rpay, rt) in zip(rp, (by_kind[k] for k in kinds), rr):
        if h != sh or t[:28] != st[:28] or rh != sh or rpay != sp:
            raise Refuse('internal: output metadata/restore differs from stock')
        if kind(h) == 0x100 and comp:
            changed += check_compressed(oz, p, sp, orig[0x100], spans.get(0x100, []), comp, touched)
        else:
            changed += sum(x != y for x, y in zip(p, sp))
    if changed != total:
        raise Refuse(f'internal: changed bytes {changed} != edited bytes {total}')
    return patched, restore


def load_olycompress():
    """LZ4 for compressed bodies (OM-5). Imported from tools/olycompress.py next to this script rather
    than embedded: one tested copy (~250 lines with its own tests), and the E-P7 path never needs it.
    The explicit file path avoids picking up a same-named module from the current directory."""
    f = ROOT / 'tools' / 'olycompress.py'
    if not f.is_file():
        raise Refuse('this camera needs tools/olycompress.py next to caduceus.py (is the download complete?)')
    import importlib.util
    spec = importlib.util.spec_from_file_location('olycompress', f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_compressed(oz, new_main, stock_main, stock_run, edit_spans, comp, touched):
    """Compressed main: expanded images differ only inside the edits; bytes outside the re-encoded
    spans are stock bytes. -> changed bytes in the expanded image."""
    if [(c.offset, c.raw) for c in oz.find(new_main)] != comp:
        raise Refuse('internal: output container layout differs from stock')
    new_run = oz.expand(new_main)
    changed, pos = 0, 0
    for a, b in sorted(edit_spans) + [(len(new_run), len(new_run))]:
        if new_run[pos:a] != stock_run[pos:a]:
            raise Refuse('internal: expanded image differs from stock outside the edits')
        changed += sum(x != y for x, y in zip(new_run[a:b], stock_run[a:b]))
        pos = b
    pos = 0
    for o, r in sorted((o, r) for o, r in comp if o in touched) + [(len(new_main), 0)]:
        if new_main[pos:o] != stock_main[pos:o]:
            raise Refuse('internal: bytes outside the re-encoded spans differ from stock')
        pos = o + r
    return changed


# ---------------------------------------------------------------- download
def fetch_stock(body=EP7):
    t = time.strftime('%Y%m%d%H%M%S', time.gmtime())
    req = ("<?xml version='1.0' encoding='utf-8'?><firmware_demand><revision>0001</revision>"
           "<mac_address>00-00-00-00-00-00</mac_address><time>" + t + "</time><language>ENU</language>"
           "<application>FWUT</application><type>0000</type><version>00005000</version><os>WIN</os>"
           "<country_code>0001</country_code><hardwares><firmup_mode>0</firmup_mode><hardware_info>"
           "<hard_code>" + body.hard_code + "</hard_code><firmware_version>00001000</firmware_version>"
           "<serial_no>000000000</serial_no></hardware_info></hardwares></firmware_demand>")
    hdrs = {'User-Agent': 'Olympus', 'Content-Type': 'text/xml; Charset=UTF-8'}
    print(f'Asking OM Digital Solutions for the {body.label} {body.fw} firmware ...')
    try:
        with urllib.request.urlopen(urllib.request.Request(CATALOG, req.encode(), hdrs), timeout=60) as r:
            info = ET.fromstring(r.read()).find('.//firmware_info')
        if info is None or info.findtext('model_id') != body.model_id or info.findtext('file_name') != body.stock_name:
            raise Refuse(f'OM server did not offer {body.label} firmware {body.fw} (it may have moved). '
                         f'Download {body.stock_name} yourself and use --stock FILE.')
        url = info.findtext('url')
        print(f'Downloading {body.stock_name} from {url.split("/")[2]}')
        buf = bytearray()
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Olympus'}),
                                    timeout=120) as r:
            while chunk := r.read(1 << 20):
                buf += chunk
                print(f'\r  {len(buf) >> 20} / {body.stock_size >> 20} MiB', end='', flush=True)
        print()
        return bytes(buf)
    except (OSError, ET.ParseError) as e:
        raise Refuse(f'download failed: {e}\nIf this keeps happening, download {body.stock_name} '
                     'another way and pass --stock FILE (see docs/INSTALL.md).')


# ---------------------------------------------------------------- releases
def read_sums(path):
    sums = {}
    for line in path.read_text().splitlines():
        if line.strip():
            h, name = line.split(maxsplit=1)
            sums[name.lstrip('*')] = h.lower()
    return sums


def release_body(d):
    """Body a release is for: the optional "body" key of its edits.json (absent = ep7)."""
    try:
        return json.loads((d / 'edits.json').read_bytes()).get('body', 'ep7')
    except (OSError, ValueError, AttributeError):
        return 'ep7'


def releases(body=EP7):
    if not RELEASES.is_dir():
        return []
    return sorted(d.name for d in RELEASES.iterdir()
                  if (d / 'edits.json').is_file() and (d / 'SHA256SUMS').is_file()
                  and 'patched.pkg.bin' in read_sums(d / 'SHA256SUMS')
                  and release_body(d) == body.key)


def load_release(name, body=EP7):
    d = RELEASES / name
    if name not in releases(body):
        avail = ', '.join(r for r in releases(body) if r.startswith('v')) or 'none'
        raise Refuse(f'unknown release {name!r}. Available: {avail}')
    sums = read_sums(d / 'SHA256SUMS')
    spec_bytes = (d / 'edits.json').read_bytes()
    if 'edits.json' in sums and sha(spec_bytes) != sums['edits.json']:
        raise Refuse(f'releases/{name}/edits.json does not match releases/{name}/SHA256SUMS '
                     '(repository out of sync; update your copy).')
    # card files ship in the repository (our own data, not OM firmware)
    card = {}
    for n, h in sums.items():
        if n.startswith('card/'):
            f = d / n
            if not f.is_file():
                raise Refuse(f'missing releases/{name}/{n}')
            b = f.read_bytes()
            if sha(b) != h:
                raise Refuse(f'releases/{name}/{n} does not match SHA256SUMS')
            card[n[len('card/'):]] = b
    return json.loads(spec_bytes), sums, card


def cmd_build(a):
    body = BODIES[getattr(a, 'body', None) or 'ep7']
    out = Path(a.out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise Refuse(f'{out} already exists and is not empty; choose another --out or remove it.')
    custom = getattr(a, 'edits', None)   # explicit edits file (no release): no hash to compare with
    if custom:
        ef = Path(custom)
        if not ef.is_file():
            raise Refuse(f'--edits: no such file {ef}')
        try:
            spec = json.loads(ef.read_bytes())
        except ValueError as e:
            raise Refuse(f'--edits: {ef} is not valid JSON: {e}')
        sums, card = {}, {}
        label, src = 'custom-edits', str(ef)
    elif a.release:
        spec, sums, card = load_release(a.release, body)
        label, src = a.release, f'releases/{a.release}/edits.json'
    else:
        raise Refuse('give --release (see: caduceus.py list) or --edits FILE.')
    if a.stock:
        p = Path(a.stock)
        if not p.is_file():
            raise Refuse(f'--stock: no such file {p}')
        stock = p.read_bytes()
    else:
        stock = fetch_stock(body)
    if sha(stock) != body.stock_sha:
        raise Refuse(f'stock firmware SHA-256 is {sha(stock)}\nexpected {body.stock_sha}\n'
                     f'This is not the official {body.label} {body.fw} file {body.stock_name} '
                     f'({body.stock_size} bytes). Nothing written.')
    print(f'Stock firmware OK. Applying {len(spec["edits"])} edits from {src} ...')
    patched, restore = apply_edits(spec, stock, body)
    outputs = body.outputs
    files = {outputs['patched.pkg.bin']: patched, outputs['restore-stock.pkg.bin']: restore}
    files.update(card)
    for rel_name, data in (('patched.pkg.bin', patched), ('restore-stock.pkg.bin', restore)):
        if sums and sha(data) != sums[rel_name]:
            raise Refuse(f'{rel_name}: built hash {sha(data)} != release {sums[rel_name]}. Nothing written.')
    # write into a temp dir next to the target, then move into place
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix='.caduceus-', dir=out.parent))
    try:
        for rel, data in files.items():
            f = tmp / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
        (tmp / 'SHA256SUMS').write_text(''.join(f'{sha(d)}  {r}\n' for r, d in sorted(files.items())))
        (tmp / 'RELEASE').write_text(label + '\n')
        if body.key != 'ep7':
            (tmp / 'BODY').write_text(body.key + '\n')
        if out.exists():
            out.rmdir()
        os.replace(tmp, out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    print(f'\nCaduceus {label} written to {out}:')
    for r in sorted(files):
        print(f'  {r}')
    if custom:
        print('  SHA256SUMS\nBuilt from your edits file; there is no release to compare with. '
              'Container validity is not bootability.')
    else:
        print('  SHA256SUMS\nAll hashes match the release. Next: docs/INSTALL.md (copy DCOLYMP/ and '
              f'{body.mod_dir}/ to the card; keep restore/ on your computer).')
    return 0


def cmd_verify(a):
    d = Path(a.dir)
    man = d / 'SHA256SUMS'
    if not man.is_file():
        raise Refuse(f'{man} not found; is {d} a caduceus build directory?')
    body = BODIES[getattr(a, 'body', None) or ((d / 'BODY').read_text().strip() if (d / 'BODY').is_file()
                                                else 'ep7')]
    sums, bad = read_sums(man), 0
    for rel, h in sorted(sums.items()):
        f = d / rel
        ok = f.is_file() and sha(f.read_bytes()) == h
        bad += not ok
        print(f'  {"OK  " if ok else "FAIL"}  {rel}')
    rel_name = (d / 'RELEASE').read_text().strip() if (d / 'RELEASE').is_file() else None
    if rel_name and rel_name in releases(body):
        rs = read_sums(RELEASES / rel_name / 'SHA256SUMS')
        want = {body.outputs[n]: h for n, h in rs.items() if n in body.outputs}
        want.update({n[len('card/'):]: h for n, h in rs.items() if n.startswith('card/')})
        if want != sums:
            bad += 1
            print(f'  FAIL  file list/hashes differ from releases/{rel_name}/SHA256SUMS')
        else:
            print(f'  OK    matches releases/{rel_name}/SHA256SUMS')
    else:
        print(f'  note: release {rel_name!r} not found here; checked the local manifest only')
    if bad:
        raise Refuse(f'{bad} problem(s). Do not flash these files; rebuild them.')
    print('All good.')
    return 0


def cmd_list(a):
    for r in releases(BODIES[getattr(a, 'body', None) or 'ep7']):
        if r.startswith('v'):
            print(r)
    return 0


# ---------------------------------------------------------------- one-press mode
def vkey(name):
    """v0.29 < v0.29a < v0.29b < v0.30."""
    m = re.fullmatch(r'v(\d+(?:\.\d+)*)([a-z]?)', name)
    if not m:
        return (-1,)
    return tuple(int(x) for x in m.group(1).split('.')) + (ord(m.group(2)) - 96 if m.group(2) else 0,)


def latest_release(body=EP7):
    rs = [r for r in releases(body) if r.startswith('v')]
    if not rs:
        raise Refuse('no releases found next to caduceus.py (is the download complete?)' if body.key == 'ep7'
                     else f'no {body.label} release exists yet (use build --body {body.key} --edits FILE).')
    return max(rs, key=vkey)


def find_stock(body=EP7):
    """An already downloaded OM file next to the script, in cache/, or in Downloads."""
    here = Path(__file__).resolve().parent
    n = body.stock_name
    for p in (here / n, here / 'cache' / n, Path.home() / 'Downloads' / n):
        try:
            if p.is_file() and p.stat().st_size == body.stock_size:
                return p
        except OSError:
            pass
    return None


def _scan_cards(found):
    if os.name == 'nt':
        import ctypes, string
        for L in string.ascii_uppercase:
            r = Path(f'{L}:\\')
            try:
                if ctypes.windll.kernel32.GetDriveTypeW(f'{L}:\\') == 2 and (r / 'DCIM').is_dir():
                    found.append(r)
            except OSError:
                pass
        return
    user = os.environ.get('USER', '')
    for base in (Path('/Volumes'), Path('/media') / user, Path('/run/media') / user, Path('/media')):
        try:
            entries = list(base.iterdir()) if base.is_dir() else []
        except OSError:
            continue
        for r in entries:
            try:
                if r.is_dir() and (r / 'DCIM').is_dir() and r not in found:
                    found.append(r)
            except OSError:
                pass


def sd_cards(limit=10.0):
    """Mounted cards that look like camera cards (have a DCIM folder).
    CADUCEUS_CARD_DIRS (os.pathsep-separated) replaces the scan (for testing). The scan runs in a
    daemon thread with a time limit so a dead network/optical mount cannot hang us."""
    env = os.environ.get('CADUCEUS_CARD_DIRS')
    if env is not None:
        return [Path(x) for x in env.split(os.pathsep) if x and (Path(x) / 'DCIM').is_dir()]
    import threading
    found = []
    t = threading.Thread(target=_scan_cards, args=(found,), daemon=True)
    t.start()
    t.join(limit)
    if t.is_alive():
        print('  (card search took too long; skipped slow drives)')
    return list(found)


def ask(q, default=True):
    """y/n question; without an interactive terminal, answer no (never copy unasked)."""
    try:
        if not (sys.stdin and sys.stdin.isatty()):
            print(f'{q} -> no terminal to ask, so: no')
            return False
        a = input(f'{q} [{"Y/n" if default else "y/N"}] ').strip().lower()
    except (EOFError, OSError, RuntimeError):
        return False
    return default if not a else a.startswith('y')


def pause():
    """Keep a double-clicked window open (the launchers pause themselves and set CADUCEUS_LAUNCHER)."""
    if os.environ.get('CADUCEUS_LAUNCHER'):
        return
    try:
        if sys.stdin and sys.stdin.isatty():
            input('\nPress Enter to close...')
    except (EOFError, OSError, RuntimeError, KeyboardInterrupt):
        pass


def copy_synced(src, dst):
    """Copy src to dst and force the data out to the card (macOS in particular caches FAT writes, so a card
    pulled without ejecting can hold truncated files, which the camera's loader silently ignores)."""
    with open(src, 'rb') as i, open(dst, 'wb') as o:
        shutil.copyfileobj(i, o, 1 << 20)
        o.flush()
        try:
            if sys.platform == 'darwin':
                import fcntl
                fcntl.fcntl(o.fileno(), fcntl.F_FULLFSYNC)
            else:
                os.fsync(o.fileno())
        except (OSError, ImportError, AttributeError):
            pass


def copy_to_card(build, card, body=EP7):
    """DCOLYMP/E1429999.BIN + LOOKS.BIN always; BUNDLES.BIN and MOVIE.BIN only if the card has none (they hold the user's settings).
    Other bodies: the update file, plus the body's card folder files when the build has them."""
    dco = card / 'DCOLYMP'
    try:
        dco.mkdir(exist_ok=True)
    except PermissionError:
        raise Refuse(f'not allowed to write to {card}.' + (
            ' On macOS: System Settings > Privacy & Security > Files and Folders (or Full Disk Access), allow '
            'Terminal to access Removable Volumes, then run this again, or drag the folders across in Finder.'
            if sys.platform == 'darwin' else ' Check the card is not write-locked (the small switch on the card).'))
    for old in dco.glob('*.BIN'):
        if old.name.upper() != body.card_name:
            print(f'  note: {old} is another update file; remove it before flashing.')
    print('  copying the update (about 100 MB) ...')
    upd = f'DCOLYMP/{body.card_name}'
    copy_synced(build / upd, dco / body.card_name)
    check = [upd]
    md = body.mod_dir
    if body.key == 'ep7' or (build / md).is_dir():
        mod = card / md
        mod.mkdir(exist_ok=True)
        if body.key == 'ep7' or (build / md / 'LOOKS.BIN').exists():
            copy_synced(build / md / 'LOOKS.BIN', mod / 'LOOKS.BIN')
            check.append(f'{md}/LOOKS.BIN')
        if body.key == 'ep7' or (build / md / 'BUNDLES.BIN').exists():
            if (mod / 'BUNDLES.BIN').exists():
                print(f'  kept your existing {md}/BUNDLES.BIN (your saved looks)')
            else:
                copy_synced(build / md / 'BUNDLES.BIN', mod / 'BUNDLES.BIN')
                check.append(f'{md}/BUNDLES.BIN')
        if (build / md / 'MOVIE.BIN').exists():
            if (mod / 'MOVIE.BIN').exists():
                print(f'  kept your existing {md}/MOVIE.BIN (your Movie colour settings)')
            else:
                copy_synced(build / md / 'MOVIE.BIN', mod / 'MOVIE.BIN')
                check.append(f'{md}/MOVIE.BIN')
    for rel in check:
        if sha((card / rel).read_bytes()) != sha((build / rel).read_bytes()):
            raise Refuse(f'copy check failed for {card / rel}; copy it again by hand. Do not flash.')
    print(f'  copied and checked on {card}')
    print('  IMPORTANT: eject the card from the computer (Finder eject button / Windows "Eject" / Unmount) before '
          'removing it, or the files can be cut short and the camera will quietly ignore them.')


def open_folder(p):
    """Show the folder without waiting for the file manager (xdg-open can block until the window closes,
    and a child holding our stdout pipe would keep a captured run hanging)."""
    try:
        if os.name == 'nt':
            os.startfile(p)
            return
        cmd = ['open', str(p)] if sys.platform == 'darwin' else ['xdg-open', str(p)]
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


def build_matches(out, rel, body=EP7):
    """True if out is a complete build of rel whose files match releases/<rel>/SHA256SUMS."""
    try:
        if (out / 'RELEASE').read_text().strip() != rel:
            return False
        rs = read_sums(RELEASES / rel / 'SHA256SUMS')
        want = {body.outputs[n]: h for n, h in rs.items() if n in body.outputs}
        want.update({n[len('card/'):]: h for n, h in rs.items() if n.startswith('card/')})
        if read_sums(out / 'SHA256SUMS') != want:
            return False
        return all(sha((out / r).read_bytes()) == h for r, h in want.items())
    except (OSError, ValueError):
        return False


def cmd_easy(a):
    body = BODIES[getattr(a, 'body', None) or 'ep7']
    here = Path(__file__).resolve().parent
    rel = latest_release(body)
    print(f'Caduceus one-press build: {rel}\n')
    out = here / (f'Caduceus-{rel}' if body.key == 'ep7' else f'Caduceus-{body.key}-{rel}')
    old = sorted(p.name for p in here.glob('Caduceus-v*' if body.key == 'ep7' else f'Caduceus-{body.key}-v*')
                 if p.is_dir() and p != out)
    if old:
        print(f'(older build folders, not used: {", ".join(old)})')
    if out.exists() and build_matches(out, rel, body):
        print(f'{out.name} already exists and matches the release.')
    else:
        if out.exists():
            if not (out / 'SHA256SUMS').is_file():
                raise Refuse(f'{out} exists but is not a Caduceus build; rename or delete it and run again.')
            n = 1
            while (stale := out.with_name(f'{out.name}.old{n}')).exists():
                n += 1
            os.replace(out, stale)
            print(f'{out.name} was out of date or incomplete; moved it to {stale.name} and rebuilding.')
        stock = find_stock(body)
        if stock is None:
            data = fetch_stock(body)
            if sha(data) == body.stock_sha:  # keep it so a rebuild doesn't download again
                (here / 'cache').mkdir(exist_ok=True)
                (here / 'cache' / body.stock_name).write_bytes(data)
                stock = here / 'cache' / body.stock_name
        else:
            print(f'Using the OM file already on this computer: {stock}')
        cmd_build(argparse.Namespace(out=str(out), release=rel, stock=str(stock) if stock else None,
                                     body=body.key, edits=None))
    print('\nLooking for a camera SD card ...')
    cards = sd_cards()
    copied = False
    if len(cards) == 1 and ask(f'Copy the update to the SD card at {cards[0]}?'):
        try:
            copy_to_card(out, cards[0], body)
            copied = True
        except (Refuse, OSError) as e:
            print(f'\nCould not copy to the card: {e}\nThe build itself is fine. Copy the DCOLYMP and {body.mod_dir} folders by hand '
                  '(below), then eject the card properly.')
    if copied:
        print('\nNow eject the card, put it in the camera, then: full battery, hold OK and switch on.\n'
              'LED steady = writing (do not switch off), ~2 Hz blink = done. See docs/INSTALL.md.')
    else:
        if len(cards) > 1:
            print(f'Several camera cards found ({", ".join(map(str, cards))}); copy by hand.')
        elif not cards:
            print('No camera card found (a card formatted in the camera has a DCIM folder).')
        print(f'\nCopy the DCOLYMP and {body.mod_dir} folders from {out} to the top level of your SD card\n'
              f'(keep any {body.mod_dir}/BUNDLES.BIN already on the card: it holds your saved looks).\n'
              'Keep the "restore" folder on your computer. Then follow docs/INSTALL.md "Flash".')
        open_folder(out)
    return 0


def main(argv=None):
    if sys.version_info < (3, 10):
        sys.exit('Python 3.10 or newer is required.')
    ap = argparse.ArgumentParser(prog='caduceus.py', description='Build Caduceus E-P7 firmware files locally.')
    sub = ap.add_subparsers(dest='cmd')
    ap.add_argument('--body', choices=sorted(BODIES), default=None, help='camera body (default: ep7)')
    b = sub.add_parser('build', help='download stock 1.1, apply a release, write card files')
    b.add_argument('--release', help='e.g. v0.15 (see: caduceus.py list)')
    b.add_argument('--edits', help='an edits.json instead of a release (no hash comparison)')
    b.add_argument('--out', default='out', help='new or empty output directory (default: out)')
    b.add_argument('--stock', help='use an already downloaded stock file (see --body) instead of downloading')
    b.add_argument('--body', choices=sorted(BODIES), default=argparse.SUPPRESS, help='camera body (default: ep7)')
    b.set_defaults(fn=cmd_build)
    v = sub.add_parser('verify', help='re-check a build directory')
    v.add_argument('dir')
    v.add_argument('--body', choices=sorted(BODIES), default=argparse.SUPPRESS,
                   help='camera body (default: from the BODY file, else ep7)')
    v.set_defaults(fn=cmd_verify)
    ls = sub.add_parser('list', help='list available releases')
    ls.add_argument('--body', choices=sorted(BODIES), default=argparse.SUPPRESS, help='camera body (default: ep7)')
    ls.set_defaults(fn=cmd_list)
    a = ap.parse_args(argv)
    for f in (sys.stdout, sys.stderr):  # progress shows up at once, even through a pipe
        try:
            f.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass
    easy = a.cmd is None  # double-click / no arguments: one-press mode
    if easy:
        a.fn = cmd_easy
    try:
        return a.fn(a)
    except Refuse as e:
        print(f'refused: {e}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('interrupted.', file=sys.stderr)
        return 130
    finally:
        if easy:
            pause()


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""Build an E-P7 component-selective update package + matching stock restore.

OFFLINE. Never contacts a camera. Output names are deliberately NOT SD-loadable
(`*.pkg.bin`); copying to DCOLYMP/E142xxxx.BIN is a separate, manual,
human-approved step.

Edits file (JSON):
  {"components": ["0x100"],            # kinds to include (main always first)
   "edits": [{"kind": "0x100", "addr": "0x603b0250", "expect": "aabb", "new": "ccdd",
              "why": "..."}]}
  addr is a CPU address for main (base 0x60020020) or "off:0x..." decoded offset.

Checks (all must pass, else nothing is written):
  - stock input SHA-256 exact; container parse + exact round trip
  - every edit's stock bytes match `expect`; no overlapping edits
  - same payload length; metadata (header, trailer[:28]) unchanged
  - output re-parses; every component checksum valid; untouched components
    byte-identical to stock; changed-byte count == sum of edit lengths
  - restore package == exact byte slices of the stock file

Bodies (--body ep7|om5, default ep7; tools/bodies.py). For a compressed body (OM-5)
the main block holds LZ4 "olycompress" spans: edits are checked and applied on the
EXPANDED run-time image (offset = addr - body.base), then only the spans an edit
touches are re-encoded (tools/olycompress.py); untouched spans keep their stock
bytes. Extra checks: expanded patched vs stock differ only inside the edits, and
bytes outside the touched spans are identical to stock. `--recompress-all`
(compressed bodies only, spec optional) re-encodes every span with no edits.
"""
import argparse, hashlib, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'external/oly_fw_analysis/tools'))
import oly_container as oc  # noqa: E402  (fishcu; imported, not copied)
sys.path.insert(0, str(ROOT / 'tools'))
import bodies  # noqa: E402
import olycompress as oz  # noqa: E402

STOCK = ROOT / 'stock_firmware/OLY_E_142_1100_0000_0000.BIN'
STOCK_SHA = bodies.EP7.stock_sha
MAIN_BASE = bodies.EP7.base


def sha(b):
    return hashlib.sha256(b).hexdigest()


def resolve(kind, addr, base=MAIN_BASE):
    if addr.startswith('off:'):
        return int(addr[4:], 16)
    a = int(addr, 16)
    if kind != 0x100:
        raise ValueError('CPU addresses only valid for main (0x100); use off:')
    return a - base


def diff_count(a, b):
    if a == b:
        return 0
    return sum(x != y for x, y in zip(a, b))


def build(spec, stock_bytes, body=bodies.EP7, recompress_all=False):
    if sha(stock_bytes) != body.stock_sha:
        raise ValueError('stock firmware hash mismatch')
    if spec.get('body', body.key) != body.key:
        raise ValueError(f'edits file is for body {spec["body"]!r}, not {body.key!r}')
    if recompress_all and not body.compressed:
        raise ValueError('--recompress-all only applies to a compressed body')
    comp_spans = []   # (offset, raw) of olycompress spans in the stock main payload
    blocks = oc.parse(stock_bytes)
    if oc.reconstruct(blocks) != stock_bytes:
        raise ValueError('stock round trip failed')
    by_kind = {b.kind: b for b in blocks}
    kinds = [int(k, 16) for k in spec['components']]
    if (0x100 in kinds and kinds[0] != 0x100) or len(set(kinds)) != len(kinds) or any(k not in by_kind for k in kinds):
        raise ValueError('components must be unique and exist (0x100 first if present)')
    orig = {k: by_kind[k].payload for k in kinds}   # what edits are checked and applied against
    if body.compressed and 0x100 in kinds:
        comp_spans = [(c.offset, c.raw) for c in oz.find(orig[0x100])]
        if not comp_spans:
            raise ValueError('compressed body but no olycompress span found in main')
        orig[0x100] = oz.expand(orig[0x100])
    payloads = {k: bytearray(orig[k]) for k in kinds}
    spans, total = {}, 0
    for e in spec['edits']:
        k = int(e['kind'], 16)
        if k not in payloads:
            raise ValueError(f'edit for component {e["kind"]} not included')
        off = resolve(k, e['addr'], body.base)
        exp, new = bytes.fromhex(e['expect']), bytes.fromhex(e['new'])
        if len(exp) != len(new) or not new:
            raise ValueError(f'edit {e["addr"]}: expect/new length mismatch')
        if not 0 <= off <= len(payloads[k]) - len(new):
            raise ValueError(f'edit {e["addr"]}: out of range')
        for (a, b) in spans.get(k, []):
            if off < b and a < off + len(new):
                raise ValueError(f'edit {e["addr"]}: overlaps another edit')
        if bytes(orig[k][off:off + len(exp)]) != exp:
            raise ValueError(f'edit {e["addr"]}: stock bytes differ from expect')
        spans.setdefault(k, []).append((off, off + len(new)))
        payloads[k][off:off + len(new)] = new
        total += sum(x != y for x, y in zip(exp, new))
    touched = set()   # offsets of compressed spans that must be re-encoded
    if comp_spans:
        for a, b in spans.get(0x100, []):
            if any(a < o + raw and o < b and not (o <= a and b <= o + raw) for o, raw in comp_spans):
                raise ValueError('an edit straddles the edge of a compressed span')
        touched = {o for o, raw in comp_spans
                   if recompress_all or any(a < o + raw and o < b for a, b in spans.get(0x100, []))}
    out_blocks = []
    for k in kinds:
        b = by_kind[k]
        if k == 0x100 and comp_spans:
            newpay = oz.repack(bytes(payloads[k]), b.payload, only=touched) if touched else b.payload
        else:
            newpay = bytes(payloads[k])
        nb = oc.replace_payload(b, newpay) if newpay != b.payload else b
        if nb.header != b.header or nb.trailer[:28] != b.trailer[:28]:
            raise ValueError('metadata changed')
        out_blocks.append(nb)
    pkg = b''.join(x.header + oc.encode(x.payload) + x.trailer for x in out_blocks)
    restore = b''.join(stock_bytes[b.offset:b.offset + 64 + len(b.payload)] for b in (by_kind[k] for k in kinds))
    # ---- verification of outputs
    re_pkg, re_rst = oc.parse(pkg), oc.parse(restore)
    if [b.kind for b in re_pkg] != kinds or [b.kind for b in re_rst] != kinds:
        raise ValueError('output component list wrong')
    changed = 0
    for nb, sb in zip(re_pkg, (by_kind[k] for k in kinds)):
        if nb.header != sb.header or nb.trailer[:28] != sb.trailer[:28]:
            raise ValueError('output metadata differs from stock')
        if nb.kind == 0x100 and comp_spans:
            changed += check_compressed(nb.payload, sb.payload, orig[0x100], spans.get(0x100, []),
                                        comp_spans, touched)
        else:
            changed += sum(x != y for x, y in zip(nb.payload, sb.payload))
    if changed != total:
        raise ValueError(f'changed bytes {changed} != edited bytes {total}')
    for rb, sb in zip(re_rst, (by_kind[k] for k in kinds)):
        if rb.payload != sb.payload:
            raise ValueError('restore payload differs from stock')
    info = dict(components=[hex(k) for k in kinds], edits=len(spec['edits']),
                changed_payload_bytes=changed)
    if body.compressed:
        info['recompressed_spans'] = [hex(o) for o in sorted(touched)]
    return pkg, restore, info


def check_compressed(new_main, stock_main, stock_run, edit_spans, comp_spans, touched):
    """Verify a built compressed main. -> number of changed bytes in the expanded image."""
    if [(c.offset, c.raw) for c in oz.find(new_main)] != comp_spans:
        raise ValueError('output container layout differs from stock')
    new_run = oz.expand(new_main)
    changed, pos = 0, 0   # expanded images differ only inside the edit spans
    for a, b in sorted(edit_spans) + [(len(new_run), len(new_run))]:
        if new_run[pos:a] != stock_run[pos:a]:
            raise ValueError('expanded image differs from stock outside the edits')
        changed += diff_count(new_run[a:b], stock_run[a:b])
        pos = b
    pos = 0   # compressed bytes outside the re-encoded spans are stock bytes
    for o, raw in sorted((o, raw) for o, raw in comp_spans if o in touched) + [(len(new_main), 0)]:
        if new_main[pos:o] != stock_main[pos:o]:
            raise ValueError('bytes outside the re-encoded spans differ from stock')
        pos = o + raw
    return changed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('spec', type=Path, nargs='?', help='edits file (optional with --recompress-all)')
    ap.add_argument('--out', type=Path, required=True, help='new directory')
    ap.add_argument('--body', default=bodies.DEFAULT, choices=sorted(bodies.BODIES))
    ap.add_argument('--stock', type=Path, help="stock firmware file (default: the body's file in stock_firmware/)")
    ap.add_argument('--recompress-all', action='store_true',
                    help='compressed bodies: re-encode every span even with no edits')
    a = ap.parse_args()
    body = bodies.get(a.body)
    if a.out.exists():
        sys.exit('output directory must not exist')
    if a.spec is None and not a.recompress_all:
        sys.exit('error: an edits file is required (or --recompress-all)')
    spec = json.loads(a.spec.read_text()) if a.spec else dict(components=['0x100'], edits=[])
    if a.stock:
        stock_path = a.stock
    elif body.key == 'ep7':
        stock_path = STOCK
    else:
        stock_path = ROOT / 'stock_firmware' / body.key / body.stock_name
    pkg, restore, info = build(spec, stock_path.read_bytes(), body, a.recompress_all)
    a.out.mkdir(parents=True)
    (a.out / 'patched.pkg.bin').write_bytes(pkg)
    (a.out / 'restore-stock.pkg.bin').write_bytes(restore)
    info.update(spec=str(a.spec) if a.spec else None,
                spec_sha256=sha(a.spec.read_bytes()) if a.spec else None, stock_sha256=body.stock_sha,
                patched_sha256=sha(pkg), restore_sha256=sha(restore), bytes=len(pkg),
                camera_contacted=False,
                note='Not SD-loadable by name. Container validity != bootability.')
    if body.key != 'ep7' or a.recompress_all:
        info.update(body=body.key, recompress_all=a.recompress_all)
    (a.out / 'build.json').write_text(json.dumps(info, indent=2) + '\n')
    print(json.dumps(info, indent=2))


if __name__ == '__main__':
    try:
        main()
    except ValueError as e:
        sys.exit(f'error: {e}')

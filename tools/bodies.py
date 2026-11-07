#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Camera bodies the package builder knows. One record per camera.

Facts only (stock file identity, run-time base of the main block, SD-update name,
card folder). caduceus.py keeps a minimal copy of this table because it must stay
a single standard-library file; keep the two in step.

Main block (kind 0x100): CPU address = `base` + decoded-payload offset. For a
`compressed` body the decoded payload still holds LZ4 "olycompress" spans
(tools/olycompress.py); `base` is the address in the EXPANDED run-time image.
"""
from collections import namedtuple

Body = namedtuple('Body', 'key label fw stock_name stock_sha stock_size model hard_code model_id '
                          'base card_name mod_dir compressed')

EP7 = Body('ep7', 'E-P7', '1.1', 'OLY_E_142_1100_0000_0000.BIN',
           '9d1610124ad98f6edae4540182205f06c9c703d9033ef0c4f19847193f95eaeb', 111017984,
           '1420', 'OLYE142000000000', '1420', 0x60020020, 'E1429999.BIN', 'EP7MOD', False)
OM5 = Body('om5', 'OM-5', '1.2', 'OLY_E_144_1200_0000_0000.BIN',
           'a0c91f3ac7caaf0925b2af609e711183cf3b5982b3333224bcef26c0c5943d11', 92274688,
           '1440', 'OLYE144000000000', '1440', 0x60021020, 'E1449999.BIN', 'OM5MOD', True)

BODIES = {b.key: b for b in (EP7, OM5)}
DEFAULT = 'ep7'


def get(key):
    try:
        return BODIES[key]
    except KeyError:
        raise ValueError('unknown body %r (known: %s)' % (key, ', '.join(BODIES)))

import os
import random
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import olycompress as oc  # noqa: E402


def sample(seed, n, alphabet=256):
    r = random.Random(seed)
    return bytes(r.randrange(alphabet) for _ in range(n))


class RoundTrip(unittest.TestCase):
    def rt(self, x):
        blk = oc.lz4_compress(x)
        self.assertEqual(oc.lz4_decompress(blk, len(x)), x)
        return blk

    def test_edges(self):
        for n in list(range(0, 40)) + [255, 256, 270, 271]:
            self.rt(sample(n, n))
            self.rt(b"a" * n)

    def test_random(self):
        for seed, n in ((1, 1000), (2, 70000), (3, 5000)):
            self.rt(sample(seed, n))
            self.rt(sample(seed, n, 4))

    def test_repetitive(self):
        blk = self.rt(b"abcd" * 50000)
        self.assertLess(len(blk), 2000)
        self.rt(bytes(300000))
        self.rt((sample(9, 100) * 1000) + sample(10, 70000) + (sample(9, 100) * 1000))

    def test_end_rules(self):
        x = b"abcdefgh" * 40
        blk = oc.lz4_compress(x)
        # last sequence is literals-only with at least 5 literals
        i = 0
        last_lit = None
        last_match_start = None
        out = 0
        while i < len(blk):
            t = blk[i]
            i += 1
            ln = t >> 4
            if ln == 15:
                while blk[i] == 255:
                    ln += 255
                    i += 1
                ln += blk[i]
                i += 1
            i += ln
            out += ln
            if i == len(blk):
                last_lit = ln
                break
            last_match_start = out
            i += 2
            ml = t & 15
            if ml == 15:
                while blk[i] == 255:
                    ml += 255
                    i += 1
                ml += blk[i]
                i += 1
            out += ml + 4
        self.assertGreaterEqual(last_lit, 5)
        self.assertLessEqual(last_match_start, len(x) - 12)

    def test_far_offsets(self):
        x = sample(5, 60000) + sample(5, 60000)
        self.rt(x)


class Reject(unittest.TestCase):
    def bad(self, blk, n):
        with self.assertRaises(oc.OlyError):
            oc.lz4_decompress(blk, n)

    def test_malformed(self):
        self.bad(b"\x40abcd" + b"\x00\x00", 8)           # offset 0
        self.bad(b"\x10a\x05\x00", 10)                   # offset beyond output
        self.bad(b"\x50abc", 5)                          # literals overrun input
        self.bad(b"\xf0", 15)                            # truncated literal length
        self.bad(b"\x10a\x01", 9)                        # truncated offset
        self.bad(b"\x1fa\x01\x00", 9)                    # truncated match length
        self.bad(b"\x40abcd", 5)                         # short output
        self.bad(b"\x40abcd", 3)                         # literals overrun size
        self.bad(b"\x14a\x01\x00" + b"\x10b", 9)         # match overruns output size
        self.bad(b"", 0 + 1)                             # empty vs size

    def test_match_overrun(self):
        good = b"\x1fa\x01\x00\x00" + b"\x50abcde"
        # 'a' + match(offset1, 4+15) = 20 bytes + 5 literals = 25
        self.assertEqual(len(oc.lz4_decompress(good, 25)), 25)
        self.bad(good, 24)
        self.bad(good, 26)


class Container(unittest.TestCase):
    def make(self, spans):
        img = bytearray(b"\xff" * 64)
        offs = []
        for raw in spans:
            offs.append(len(img))
            data = (sample(len(img), 40) * 10000)[:raw // 2] + bytes(raw - raw // 2)
            pk = oc.lz4_compress(data)
            head = oc.MAGIC + bytes(5) + struct.pack("<II", len(pk), raw) + bytes(8)
            span = head + pk
            img += span + b"\xff" * (raw - len(span))
        img += b"tail"
        return bytes(img), offs

    def test_expand_repack(self):
        img, offs = self.make([5000, 9000])
        cs = oc.find(img)
        self.assertEqual([c.offset for c in cs], offs)
        run = oc.expand(img)
        self.assertEqual(len(run), len(img))
        self.assertEqual(run[-4:], b"tail")
        rep = oc.repack(run, img)
        self.assertEqual(oc.expand(rep), run)
        self.assertEqual(len(rep), len(img))

    def test_repack_too_big(self):
        img, offs = self.make([5000])
        run = bytearray(oc.expand(img))
        run[offs[0]:offs[0] + 5000] = sample(1, 5000)
        with self.assertRaises(oc.OlyError):
            oc.repack(bytes(run), img)

    def test_bad_container(self):
        img, offs = self.make([5000])
        b = bytearray(img)
        b[offs[0] + 20:offs[0] + 24] = struct.pack("<I", 4999)  # wrong raw size
        with self.assertRaises(oc.OlyError):
            oc.expand(bytes(b))


if __name__ == "__main__":
    unittest.main()

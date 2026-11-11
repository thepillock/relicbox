"""Small dependency-free QR code generator (byte mode, error correction level M, versions 1-10).

Based on the algorithm in ISO/IEC 18004; structure follows Project Nayuki's reference implementation.
"""

# version -> (ec codewords per block, [(block count, data codewords per block), ...])  for level M
_EC_M = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]), 5: (24, [(2, 43)]),
    6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
          9: [6, 26, 46], 10: [6, 28, 50]}
_FORMAT_M = 0  # level M format bits


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _data_capacity(version):
    ec, groups = _EC_M[version]
    return sum(n * d for n, d in groups)


ALNUM = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"


def _is_alnum(text):
    return all(ch in ALNUM for ch in text)


def _data_bits(text, version):
    """Bits needed for the text: alphanumeric mode when possible (denser), otherwise bytes."""
    if _is_alnum(text):
        n = len(text)
        return 4 + (9 if version < 10 else 11) + 11 * (n // 2) + 6 * (n % 2)
    return 4 + (8 if version < 10 else 16) + 8 * len(text.encode("utf-8"))


def _encode_data(text, version):
    bits = []

    def put(val, n):
        bits.extend((val >> i) & 1 for i in reversed(range(n)))

    if _is_alnum(text):
        put(0b0010, 4)
        put(len(text), 9 if version < 10 else 11)
        for i in range(0, len(text) - 1, 2):
            put(ALNUM.index(text[i]) * 45 + ALNUM.index(text[i + 1]), 11)
        if len(text) % 2:
            put(ALNUM.index(text[-1]), 6)
    else:
        payload = text.encode("utf-8")
        put(0b0100, 4)
        put(len(payload), 8 if version < 10 else 16)
        for b in payload:
            put(b, 8)
    cap = _data_capacity(version) * 8
    put(0, min(4, cap - len(bits)))
    put(0, (-len(bits)) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < cap // 8:
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    return data


def _add_ec_and_interleave(data, version):
    ec_len, groups = _EC_M[version]
    div = _rs_divisor(ec_len)
    blocks, k = [], 0
    for count, dlen in groups:
        for _ in range(count):
            d = data[k:k + dlen]
            k += dlen
            blocks.append((d, _rs_remainder(d, div)))
    out = []
    maxd = max(len(d) for d, _ in blocks)
    for i in range(maxd):
        for d, _ in blocks:
            if i < len(d):
                out.append(d[i])
    for i in range(ec_len):
        for _, e in blocks:
            out.append(e[i])
    return out


class _Matrix:
    def __init__(self, version):
        self.v = version
        self.size = version * 4 + 17
        self.m = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]

    def setf(self, x, y, dark):
        self.m[y][x] = dark
        self.fn[y][x] = True

    def draw_function_patterns(self):
        s = self.size
        for i in range(s):
            self.setf(6, i, i % 2 == 0)
            self.setf(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (s - 4, 3), (3, s - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < s and 0 <= y < s:
                        self.setf(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _ALIGN[self.v]
        n = len(pos)
        for i in range(n):
            for j in range(n):
                if (i == 0 and j == 0) or (i == 0 and j == n - 1) or (i == n - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.setf(pos[i] + dx, pos[j] + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)
        if self.v >= 7:
            rem = self.v
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.v << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = s - 11 + i % 3, i // 3
                self.setf(a, b, bit)
                self.setf(b, a, bit)

    def draw_format(self, mask):
        s = self.size
        data = _FORMAT_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        g = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        for i in range(6):
            self.setf(8, i, g(i))
        self.setf(8, 7, g(6))
        self.setf(8, 8, g(7))
        self.setf(7, 8, g(8))
        for i in range(9, 15):
            self.setf(14 - i, 8, g(i))
        for i in range(8):
            self.setf(s - 1 - i, 8, g(i))
        for i in range(8, 15):
            self.setf(8, s - 15 + i, g(i))
        self.setf(8, s - 8, True)

    def draw_codewords(self, cw):
        s, i, total = self.size, 0, len(cw) * 8
        right = s - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(s):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = s - 1 - vert if upward else vert
                    if not self.fn[y][x] and i < total:
                        self.m[y][x] = (cw[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        f = [
            lambda x, y: (x + y) % 2 == 0,
            lambda x, y: y % 2 == 0,
            lambda x, y: x % 3 == 0,
            lambda x, y: (x + y) % 3 == 0,
            lambda x, y: (x // 3 + y // 2) % 2 == 0,
            lambda x, y: x * y % 2 + x * y % 3 == 0,
            lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
            lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
        ][mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fn[y][x] and f(x, y):
                    self.m[y][x] = not self.m[y][x]

    def penalty(self):
        s, m, score = self.size, self.m, 0
        for lines in (m, [list(c) for c in zip(*m)]):
            for row in lines:
                run, prev = 0, None
                for v in row:
                    if v == prev:
                        run += 1
                    else:
                        if run >= 5:
                            score += run - 2
                        run, prev = 1, v
                if run >= 5:
                    score += run - 2
                txt = "".join("1" if v else "0" for v in row)
                score += 40 * (txt.count("1011101" + "0000") + txt.count("0000" + "1011101"))
        for y in range(s - 1):
            for x in range(s - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(r) for r in m)
        score += abs(dark * 20 - s * s * 10) // (s * s) * 10
        return score


def matrix(text):
    for version in range(1, 11):
        if _data_bits(text, version) <= _data_capacity(version) * 8:
            break
    else:
        raise ValueError("Text too long for QR label")
    cw = _add_ec_and_interleave(_encode_data(text, version), version)
    best, best_score = None, None
    # Mask 2 (vertical stripes) is valid but some camera decoders struggle with it, so skip it.
    for mask in (0, 1, 3, 4, 5, 6, 7):
        q = _Matrix(version)
        q.draw_function_patterns()
        q.draw_codewords(cw)
        q.apply_mask(mask)
        q.draw_format(mask)
        sc = q.penalty()
        if best_score is None or sc < best_score:
            best, best_score = q, sc
    return best.m


def svg(text, border=4):
    m = matrix(text)
    n = len(m) + border * 2
    parts = []
    for y, row in enumerate(m):
        x = 0
        while x < len(row):
            if row[x]:
                start = x
                while x < len(row) and row[x]:
                    x += 1
                parts.append(f"M{start + border},{y + border}h{x - start}v1h-{x - start}z")
            else:
                x += 1
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            f'<rect width="100%" height="100%" fill="#fff"/><path fill="#000" d="{"".join(parts)}"/></svg>')

"""Pure-Python LZO1X-1 compressor (and a safe LZO1X decompressor).

The PM2411BT's ``BITMAP ...,4,`` payload is the packed raster cut into
4096-byte slices, each compressed with ``lzo1x_1_compress`` from miniLZO 2.10.
Windows has no system liblzo2, so this module ports that compressor to plain
Python. It mirrors the reference C code on a 64-bit little-endian build
(``LZO_DETERMINISTIC``, 14-bit dictionary, 8-byte match scanning), so for the
same input it emits the same bytes as ``liblzo2.so.2`` on x86-64; the tests
check both that and that liblzo2's ``lzo1x_decompress_safe`` accepts the output.

``decompress`` is an independent, bounds-checked LZO1X decompressor used for
round-trip checks and for inspecting captured jobs.
"""

from __future__ import annotations

# Stream format constants (lzo1x_d.ch / lzo_conf.h).
M2_MAX_LEN = 8
M3_MAX_LEN = 33
M4_MAX_LEN = 9
M2_MAX_OFFSET = 0x0800
M3_MAX_OFFSET = 0x4000
M4_MAX_OFFSET = 0xBFFF
M3_MARKER = 32
M4_MARKER = 16

# lzo1x_1: D_BITS = 14, DINDEX(dv) = ((dv * 0x1824429d) >> (32 - D_BITS)) & mask.
_D_BITS = 14
_D_SIZE = 1 << _D_BITS
_D_MASK = _D_SIZE - 1
_D_SHIFT = 32 - _D_BITS
_HASH_MUL = 0x1824429D
_U32 = 0xFFFFFFFF

# The compressor works on blocks of at most this many bytes (as the C code).
_BLOCK = 49152
# The end-of-stream marker: an M4 match with offset 0.
_EOF = bytes((M4_MARKER | 1, 0, 0))


class LzoError(ValueError):
    """The compressed stream is malformed, truncated, or overruns its bounds."""


def max_compressed_size(n: int) -> int:
    """Worst-case output size for ``n`` input bytes (as documented by LZO)."""
    return n + n // 16 + 64 + 3


def _common_length(data: bytes, a: int, b: int, limit: int) -> int:
    """Length of the common prefix of ``data[a:]`` and ``data[b:]``, at most ``limit``.

    Binary search over slice equality: every comparison runs in C, so even a
    4 KB run of white costs about a dozen slice compares instead of 4096
    Python-level byte compares.
    """
    if limit <= 0:
        return 0
    if data[a : a + limit] == data[b : b + limit]:
        return limit
    lo, hi = 0, limit  # data[a:a+lo] == data[b:b+lo]; the first hi bytes differ
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if data[a : a + mid] == data[b : b + mid]:
            lo = mid
        else:
            hi = mid
    return lo


def _emit_literal_run(out: bytearray, data: bytes, start: int, count: int) -> None:
    """Emit ``count`` literals that precede a match (never the stream's first run)."""
    if count <= 3:
        # Short runs ride in the low two bits of the previous match's last
        # instruction byte (op[-2] in the C code).
        out[-2] |= count
    elif count <= 18:
        out.append(count - 3)
    else:
        rest = count - 18
        out.append(0)
        while rest > 255:
            rest -= 255
            out.append(0)
        out.append(rest)
    out += data[start : start + count]


def _emit_match(out: bytearray, m_len: int, m_off: int) -> None:
    if m_len <= M2_MAX_LEN and m_off <= M2_MAX_OFFSET:
        m_off -= 1
        out.append((((m_len - 1) << 5) | ((m_off & 7) << 2)) & 0xFF)
        out.append(m_off >> 3)
        return
    if m_off <= M3_MAX_OFFSET:
        m_off -= 1
        if m_len <= M3_MAX_LEN:
            out.append(M3_MARKER | (m_len - 2))
        else:
            m_len -= M3_MAX_LEN
            out.append(M3_MARKER)
            while m_len > 255:
                m_len -= 255
                out.append(0)
            out.append(m_len)
    else:
        m_off -= 0x4000
        high = (m_off >> 11) & 8
        if m_len <= M4_MAX_LEN:
            out.append(M4_MARKER | high | (m_len - 2))
        else:
            m_len -= M4_MAX_LEN
            out.append(M4_MARKER | high)
            while m_len > 255:
                m_len -= 255
                out.append(0)
            out.append(m_len)
    out.append((m_off << 2) & 0xFF)
    out.append((m_off >> 6) & 0xFF)


def _do_compress(data: bytes, base: int, length: int, ti: int, out: bytearray) -> int:
    """Compress ``data[base:base+length]`` into ``out``; return trailing literals.

    A line-by-line port of ``do_compress`` from LZO 2.10's ``lzo1x_c.ch``.
    ``ti`` is the count of literals still pending from the previous block.
    """
    # pylint: disable=too-many-locals
    from_bytes = int.from_bytes
    dictionary = [0] * _D_SIZE
    in_end = base + length
    ip_end = in_end - 20
    ii = base
    ip = base + (4 - ti if ti < 4 else 0)

    after_match = False
    while True:
        # literal: the C loop is entered here, so the first probe is at +5;
        # after a match the C code jumps straight to "next" instead.
        if not after_match:
            ip += 1 + ((ip - ii) >> 5)
        after_match = False
        # next:
        if ip >= ip_end:
            break
        dv = from_bytes(data[ip : ip + 4], "little")
        dindex = (((dv * _HASH_MUL) & _U32) >> _D_SHIFT) & _D_MASK
        m_pos = base + dictionary[dindex]
        dictionary[dindex] = ip - base
        if dv != from_bytes(data[m_pos : m_pos + 4], "little"):
            continue

        # A match: flush the literals between ii and ip first.
        ii -= ti
        ti = 0
        count = ip - ii
        if count:
            _emit_literal_run(out, data, ii, count)

        # Match length, exactly as the 64-bit C build scans it: compare 8 bytes
        # at a time from offset 4 and stop scanning (possibly up to 7 bytes
        # past ip_end) once ip + m_len reaches ip_end.
        limit = ip_end - ip  # m_len values >= limit stop the scan
        diff = 4 + _common_length(data, ip + 4, m_pos + 4, limit + 8)
        blocks = (diff - 4) // 8  # how many equal 8-byte blocks precede the miss
        m_len = diff
        if blocks:
            # Loop body: m_len = 4 + 8k for k = 1..blocks, stopping at limit.
            k_stop = max(1, -(-(limit - 4) // 8))
            if k_stop <= blocks:
                m_len = 4 + 8 * k_stop

        m_off = ip - m_pos
        ip += m_len
        ii = ip
        _emit_match(out, m_len, m_off)
        after_match = True

    return in_end - (ii - ti)


def compress(data: bytes) -> bytes:
    """LZO1X-1 compress ``data`` (the output of liblzo2's ``lzo1x_1_compress``)."""
    data = bytes(data)
    in_len = len(data)
    out = bytearray()
    ip = 0
    remaining = in_len
    t = 0
    while remaining > 20:
        block = min(remaining, _BLOCK)
        if (t + block) >> 5 == 0:
            # The C code's pointer-overflow guard also stops here, so tiny
            # inputs (< 32 bytes) are stored as one literal run.
            break
        t = _do_compress(data, ip, block, t, out)
        ip += block
        remaining -= block
    t += remaining

    if t > 0:
        start = in_len - t
        if not out and t <= 238:
            out.append(17 + t)
        elif t <= 3:
            out[-2] |= t
        elif t <= 18:
            out.append(t - 3)
        else:
            rest = t - 18
            out.append(0)
            while rest > 255:
                rest -= 255
                out.append(0)
            out.append(rest)
        out += data[start:]

    out += _EOF
    return bytes(out)


def decompress(src: bytes, max_out: int | None = None) -> bytes:
    """Decompress an LZO1X stream with full bounds checking.

    Args:
        max_out: Refuse to produce more than this many bytes (None: no limit).

    Raises:
        LzoError: on any malformed, truncated, or out-of-range stream, or input
            left over after the end-of-stream marker.
    """
    # pylint: disable=too-many-branches,too-many-statements,too-many-locals
    src = bytes(src)
    n = len(src)
    out = bytearray()
    limit = max_out if max_out is not None else -1
    ip = 0

    def need(count: int) -> None:
        if ip + count > n:
            raise LzoError(f"input overrun at {ip} (need {count}, have {n - ip})")

    def extend_len(base_len: int) -> int:
        nonlocal ip
        extra = 0
        while True:
            need(1)
            if src[ip] != 0:
                break
            extra += 255
            ip += 1
            if extra > 1 << 31:
                raise LzoError("run length overflow")
        value = base_len + extra + src[ip]
        ip += 1
        return value

    def copy_literals(count: int) -> None:
        nonlocal ip
        need(count)
        if 0 <= limit < len(out) + count:
            raise LzoError("output overrun")
        out.extend(src[ip : ip + count])
        ip += count

    def copy_match(distance: int, count: int) -> None:
        start = len(out) - distance
        if distance <= 0 or start < 0:
            raise LzoError(f"lookbehind overrun (distance {distance}, have {len(out)})")
        if 0 <= limit < len(out) + count:
            raise LzoError("output overrun")
        if distance >= count:
            out.extend(out[start : start + count])
        else:
            for i in range(count):
                out.append(out[start + i])

    need(1)
    state = 0  # 0: expect an instruction; >0 literals follow a match (1..3)
    after_literal_run = False
    if src[0] > 17:
        t = src[0] - 17
        ip = 1
        copy_literals(t)
        if t < 4:
            state = t
            after_literal_run = False
        else:
            after_literal_run = True

    while True:
        need(1)
        t = src[ip]
        ip += 1
        if t < 16:
            if state == 0 and not after_literal_run:
                # Literal run of t + 3 bytes (t == 0: extended length).
                count = extend_len(15) + 3 if t == 0 else t + 3
                copy_literals(count)
                after_literal_run = True
                continue
            need(1)
            if after_literal_run and state == 0:
                # M1 right after a literal run of >= 4: 3 bytes, far offset.
                distance = 1 + M2_MAX_OFFSET + (t >> 2) + (src[ip] << 2)
                ip += 1
                copy_match(distance, 3)
            else:
                # M1 after a match: 2 bytes, near offset.
                distance = 1 + (t >> 2) + (src[ip] << 2)
                ip += 1
                copy_match(distance, 2)
        elif t >= 64:
            need(1)
            distance = 1 + ((t >> 2) & 7) + (src[ip] << 3)
            ip += 1
            copy_match(distance, (t >> 5) - 1 + 2)
        elif t >= 32:
            count = t & 31
            if count == 0:
                count = extend_len(31)
            need(2)
            distance = 1 + ((src[ip] | (src[ip + 1] << 8)) >> 2)
            ip += 2
            copy_match(distance, count + 2)
        else:  # 16 <= t < 32: M4, or end of stream
            far = (t & 8) << 11
            count = t & 7
            if count == 0:
                count = extend_len(7)
            need(2)
            distance = far + ((src[ip] | (src[ip + 1] << 8)) >> 2)
            ip += 2
            if distance == 0:
                if count != 1:
                    raise LzoError("bad end-of-stream marker")
                if ip != n:
                    raise LzoError(f"{n - ip} bytes of input left after end of stream")
                return bytes(out)
            copy_match(distance + 0x4000, count + 2)

        # match_done: the low two bits of the instruction's second-to-last byte
        # say how many literals (0..3) follow before the next instruction.
        state = src[ip - 2] & 3
        after_literal_run = False
        if state:
            copy_literals(state)

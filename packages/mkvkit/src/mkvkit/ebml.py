# -*- coding: utf-8 -*-
"""Minimal EBML scanner: which top-level Segment children exist (Cues, Chapters, ...).

Fast path: parse the SeekHead(s) at the head of the Segment, following a SeekHead entry that
points at a second SeekHead (mkvmerge writes a small one at the front and the full one at the
end). Only if no SeekHead names Cues do we walk every top-level element, which on a multi-GiB
file means thousands of random reads.
"""
import os

SEEKHEAD = 0x114D9B74
CUES = 0x1C53BB6B
CHAPTERS = 0x1043A770
ATTACHMENTS = 0x1941A469
TAGS = 0x1254C367
TRACKS = 0x1654AE6B
INFO = 0x1549A966
CLUSTER = 0x1F43B675
NAMES = {INFO: 'Info', TRACKS: 'Tracks', CUES: 'Cues', CLUSTER: 'Cluster', CHAPTERS: 'Chapters',
         ATTACHMENTS: 'Attachments', TAGS: 'Tags', SEEKHEAD: 'SeekHead', 0xEC: 'Void', 0xBF: 'CRC-32'}


def _vint(f, keep_marker):
    b = f.read(1)
    if not b:
        return None, 0
    b0 = b[0]
    if b0 == 0:
        return None, 0
    n, mask = 1, 0x80
    while not (b0 & mask):
        mask >>= 1
        n += 1
        if n > 8:
            return None, 0
    rest = f.read(n - 1)
    if len(rest) != n - 1:
        return None, 0
    val = b0 if keep_marker else (b0 & (mask - 1))
    for c in rest:
        val = (val << 8) | c
    return val, n


def _vint_buf(buf, i, keep_marker):
    if i >= len(buf):
        return None, i
    b0 = buf[i]
    if b0 == 0:
        return None, i
    n, mask = 1, 0x80
    while not (b0 & mask):
        mask >>= 1
        n += 1
        if n > 8:
            return None, i
    if i + n > len(buf):
        return None, i
    val = b0 if keep_marker else (b0 & (mask - 1))
    for c in buf[i + 1:i + n]:
        val = (val << 8) | c
    return val, i + n


def _parse_seekhead(data):
    """-> {element id: position relative to the segment data start}"""
    out = {}
    i = 0
    while i < len(data):
        eid, j = _vint_buf(data, i, True)
        if eid is None:
            break
        esz, j = _vint_buf(data, j, False)
        if esz is None:
            break
        if eid == 0x4DBB:                       # Seek
            sub = data[j:j + esz]
            k, sid, spos = 0, None, None
            while k < len(sub):
                sk, m = _vint_buf(sub, k, True)
                if sk is None:
                    break
                sl, m = _vint_buf(sub, m, False)
                if sl is None:
                    break
                v = sub[m:m + sl]
                if sk == 0x53AB:                # SeekID
                    sid = int.from_bytes(v, 'big') if v else None
                elif sk == 0x53AC:              # SeekPosition
                    spos = int.from_bytes(v, 'big') if v else None
                k = m + sl
            if sid is not None and spos is not None:
                out.setdefault(sid, spos)
        i = j + esz
    return out


def toplevel(path):
    """returns (set_of_top_level_names, set_of_names_the_seek_index_points_at, status)"""
    found, seek = set(), set()
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        eid, _ = _vint(f, True)
        if eid != 0x1A45DFA3:
            return found, seek, 'not-ebml'
        esz, _ = _vint(f, False)
        f.seek(esz, 1)
        eid, _ = _vint(f, True)
        if eid != 0x18538067:
            return found, seek, 'no-segment'
        esz, _ = _vint(f, False)
        seg_start = f.tell()
        seg_end = seg_start + esz if esz and esz < (1 << 56) - 1 else size

        # --- fast path: the seek index --------------------------------------
        seen_sh = set()
        pos = seg_start
        for _ in range(4):
            f.seek(pos)
            cid, _ = _vint(f, True)
            if cid != SEEKHEAD:
                break
            csz, _ = _vint(f, False)
            if csz is None or csz > 4 << 20:
                break
            entries = _parse_seekhead(f.read(csz))
            for k in entries:
                if k in NAMES:
                    seek.add(NAMES[k])
            if CUES in entries:
                found.add('SeekHead')
                return found, seek, 'ok-seekhead'
            nxt = entries.get(SEEKHEAD)
            if nxt is None or nxt in seen_sh:
                break
            seen_sh.add(nxt)
            pos = seg_start + nxt

        # --- slow path: walk every top-level element -------------------------
        f.seek(seg_start)
        while f.tell() < seg_end:
            cid, _ = _vint(f, True)
            if cid is None:
                break
            csz, _ = _vint(f, False)
            if csz is None:
                break
            found.add(NAMES.get(cid, hex(cid)))
            if csz >= (1 << 56) - 1:
                break
            f.seek(csz, 1)
    return found, seek, 'ok-walk'


if __name__ == '__main__':
    import sys, time
    for p in sys.argv[1:]:
        t = time.time()
        print('%.2fs' % (time.time() - t), toplevel(p), p)

"""Minimal FIT reader: just the 'record' messages (time, distance, heart rate). Standard library only.

Handles definition and data messages, compressed-timestamp headers, both byte orders, and developer
fields (skipped). Enough for Garmin activity files; not a general FIT library.
"""
import io
import struct
import zipfile

RECORD = 20
FIELDS = {253: "timestamp", 3: "hr", 5: "distance", 6: "speed", 73: "enhanced_speed"}
INVALID = {1: 0xFF, 2: 0xFFFF, 4: 0xFFFFFFFF}


def _uint(raw, little):
    if len(raw) not in (1, 2, 4):
        return None
    v = int.from_bytes(raw, "little" if little else "big")
    return None if v == INVALID[len(raw)] else v


def records(data):
    """Yield dicts with timestamp (s), distance (m), hr (bpm) for each record message."""
    if data[:2] == b"PK":  # Garmin downloads are zipped
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".fit"))
            data = z.read(name)
    hsize = data[0]
    if data[8:12] != b".FIT":
        raise ValueError("not a FIT file")
    size = struct.unpack_from("<I", data, 4)[0]
    pos, end = hsize, hsize + size
    defs, last_ts = {}, None
    while pos < end:
        h = data[pos]
        pos += 1
        if h & 0x80:  # compressed timestamp header -> data message
            local, offset = (h >> 5) & 0x03, h & 0x1F
            d = defs[local]
            vals, pos = _read(data, pos, d)
            if last_ts is not None:
                ts = (last_ts & ~0x1F) + offset
                if offset < (last_ts & 0x1F):
                    ts += 0x20
                vals["timestamp"] = ts
        elif h & 0x40:  # definition message
            local, dev = h & 0x0F, bool(h & 0x20)
            little = data[pos + 1] == 0
            gnum = struct.unpack_from("<H" if little else ">H", data, pos + 2)[0]
            n = data[pos + 4]
            pos += 5
            fields = [(data[pos + 3 * i], data[pos + 3 * i + 1]) for i in range(n)]
            pos += 3 * n
            dev_size = 0
            if dev:
                nd = data[pos]
                dev_size = sum(data[pos + 1 + 3 * i + 1] for i in range(nd))
                pos += 1 + 3 * nd
            defs[local] = (gnum, little, fields, dev_size)
            continue
        else:
            d = defs[h & 0x0F]
            vals, pos = _read(data, pos, d)
        if "timestamp" in vals and vals["timestamp"] is not None:
            last_ts = vals["timestamp"]
        if d[0] == RECORD:
            yield vals


def _read(data, pos, d):
    gnum, little, fields, dev_size = d
    vals = {}
    for num, size in fields:
        if gnum == RECORD and num in FIELDS:
            vals[FIELDS[num]] = _uint(data[pos:pos + size], little)
        elif num == 253:
            vals["timestamp"] = _uint(data[pos:pos + size], little)
        pos += size
    return vals, pos + dev_size


def track(data):
    """[[t_seconds_from_start, meters, hr], ...] from a FIT file (hr 0 when missing)."""
    out, t0 = [], None
    for r in records(data):
        ts = r.get("timestamp")
        if ts is None:
            continue
        t0 = ts if t0 is None else t0
        dist = r.get("distance")
        out.append([ts - t0, round(dist / 100, 1) if dist is not None else None, r.get("hr") or 0])
    # carry distance forward over gaps
    last = 0.0
    for p in out:
        if p[1] is None:
            p[1] = last
        last = p[1]
    return out

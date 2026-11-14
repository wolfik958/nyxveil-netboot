"""Minimal read-only ISO 9660 reader (Rock Ridge and Joliet names, multi-extent files).

Pure standard library, Python 3.8+, so it runs on Synology DSM without extra packages.
Windows install discs keep their real file tree in UDF only; those are handled with 7z instead.
"""
import os
import struct

SECTOR = 2048


class IsoError(Exception):
    pass


class Entry(object):
    __slots__ = ('name', 'is_dir', 'extents', 'size')

    def __init__(self, name, is_dir):
        self.name = name
        self.is_dir = is_dir
        self.extents = []  # list of (lba, length)
        self.size = 0


class Iso(object):
    def __init__(self, path):
        self.path = path
        self.f = open(path, 'rb')
        self.label = ''
        self._root = None
        self._rr = False
        self._joliet = False
        self._dirs = {}
        self._read_descriptors()

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---- low level -------------------------------------------------------------------------------
    def _read(self, lba, length):
        self.f.seek(lba * SECTOR)
        return self.f.read(length)

    def _read_descriptors(self):
        pvd = None
        svd = None
        for i in range(16, 64):
            d = self._read(i, SECTOR)
            if len(d) < SECTOR or d[1:6] != b'CD001':
                break
            t = d[0]
            if t == 1 and pvd is None:
                pvd = d
            elif t == 2 and d[88:91] in (b'%/@', b'%/C', b'%/E'):
                svd = d
            elif t == 255:
                break
        if pvd is None:
            raise IsoError('no ISO 9660 primary volume descriptor')
        self.label = pvd[40:72].decode('ascii', 'replace').strip()
        root = self._parse_record(pvd[156:190], joliet=False)
        self._rr = self._detect_rr(root)
        if not self._rr and svd is not None:
            self._joliet = True
            root = self._parse_record(svd[156:190], joliet=True)
        self._root = root

    def _parse_record(self, rec, joliet):
        lba, length = struct.unpack_from('<I', rec, 2)[0], struct.unpack_from('<I', rec, 10)[0]
        flags = rec[25]
        nlen = rec[32]
        raw = rec[33:33 + nlen]
        if joliet:
            name = raw.decode('utf-16-be', 'replace')
        else:
            name = raw.decode('ascii', 'replace')
        e = Entry(name, bool(flags & 2))
        e.extents.append((lba, length))
        e.size = length
        return e

    def _detect_rr(self, root):
        lba, length = root.extents[0]
        data = self._read(lba, min(length, SECTOR))
        rlen = data[0]
        rec = data[:rlen]
        nlen = rec[32]
        su = rec[33 + nlen + (1 - nlen % 2):]
        return b'SP' in su[:16] or b'RR' in su or b'PX' in su

    def _rr_name(self, su):
        name = b''
        i = 0
        while i + 4 <= len(su):
            sig = su[i:i + 2]
            ln = su[i + 2]
            if ln < 4:
                break
            if sig == b'NM':
                fl = su[i + 4]
                if not fl & 6:
                    name += su[i + 5:i + ln]
            i += ln
        return name.decode('utf-8', 'replace') if name else None

    def _listdir(self, ent):
        key = ent.extents[0][0]
        if key in self._dirs:
            return self._dirs[key]
        lba, length = ent.extents[0]
        data = self._read(lba, length)
        out = []
        pos = 0
        last = None
        while pos < len(data):
            rlen = data[pos]
            if rlen == 0:
                pos = (pos // SECTOR + 1) * SECTOR
                continue
            rec = data[pos:pos + rlen]
            pos += rlen
            nlen = rec[32]
            raw = rec[33:33 + nlen]
            if raw in (b'\x00', b'\x01'):
                continue
            flags = rec[25]
            elba = struct.unpack_from('<I', rec, 2)[0]
            elen = struct.unpack_from('<I', rec, 10)[0]
            if self._joliet:
                name = raw.decode('utf-16-be', 'replace')
            else:
                name = None
                if self._rr:
                    su = rec[33 + nlen + (1 - nlen % 2):]
                    name = self._rr_name(su)
                if name is None:
                    name = raw.decode('ascii', 'replace')
            if not self._rr:
                if ';' in name:
                    name = name.split(';')[0]
                if name.endswith('.') and not (flags & 2):
                    name = name[:-1]
            if last is not None and last.name == name and last.extents and getattr(last, '_more', False):
                last.extents.append((elba, elen))
                last.size += elen
                last._more = bool(flags & 0x80)
                continue
            e = _MEntry(name, bool(flags & 2))
            e.extents.append((elba, elen))
            e.size = elen
            e._more = bool(flags & 0x80)
            out.append(e)
            last = e
        self._dirs[key] = out
        return out

    # ---- public API ------------------------------------------------------------------------------
    def lookup(self, path, nocase=True):
        """Find an entry by '/'-separated path. Case-insensitive by default (8.3 / Joliet discs)."""
        cur = self._root
        for part in [p for p in path.strip('/').split('/') if p]:
            if not cur.is_dir:
                return None
            nxt = None
            for e in self._listdir(cur):
                if e.name == part or (nocase and e.name.lower() == part.lower()):
                    nxt = e
                    if e.name == part:
                        break
            if nxt is None:
                return None
            cur = nxt
        return cur

    def exists(self, path):
        return self.lookup(path) is not None

    def listdir(self, path='/'):
        e = self.lookup(path)
        if e is None or not e.is_dir:
            return []
        return [x.name for x in self._listdir(e)]

    def walk(self, path='/', depth=3):
        """Yield '/a/b' paths of files and directories, up to `depth` levels below `path`."""
        start = self.lookup(path)
        if start is None:
            return
        stack = [(path.rstrip('/'), start, 0)]
        while stack:
            base, ent, d = stack.pop()
            for e in self._listdir(ent):
                p = base + '/' + e.name
                yield p, e
                if e.is_dir and d + 1 < depth:
                    stack.append((p, e, d + 1))

    def size(self, path):
        e = self.lookup(path)
        return e.size if e else 0

    def read(self, path, limit=None):
        e = self.lookup(path)
        if e is None or e.is_dir:
            raise IsoError('not found: ' + path)
        chunks = []
        left = e.size if limit is None else min(limit, e.size)
        for lba, ln in e.extents:
            if left <= 0:
                break
            n = min(ln, left)
            chunks.append(self._read(lba, n))
            left -= n
        return b''.join(chunks)

    def extract(self, path, dest):
        """Copy one file out of the image (streamed; works for multi-GB files)."""
        e = self.lookup(path)
        if e is None or e.is_dir:
            raise IsoError('not found: ' + path)
        tmp = dest + '.part'
        d = os.path.dirname(dest)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with open(tmp, 'wb') as out:
            for lba, ln in e.extents:
                self.f.seek(lba * SECTOR)
                left = ln
                while left > 0:
                    buf = self.f.read(min(left, 8 << 20))
                    if not buf:
                        raise IsoError('short read in ' + path)
                    out.write(buf)
                    left -= len(buf)
        os.replace(tmp, dest)
        return e.size


class _MEntry(Entry):
    __slots__ = ('_more',)

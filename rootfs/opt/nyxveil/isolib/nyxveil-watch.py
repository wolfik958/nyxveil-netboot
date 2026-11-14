#!/usr/bin/env python3
"""Nyxveil ISO watcher: keeps the netboot ISO library in sync with the ISO folder.

Drop an ISO into the folder (SMB, File Station, rsync...) and it shows up in netboot.xyz's
Custom URL Menu once the copy has finished; delete it and it disappears. Runs forever.

  - polls the folder every few seconds (cheap: one directory listing + a stat per file)
  - a file counts as copied once its size and modification time stop changing for NYX_SETTLE
    seconds (Windows preallocates the full size, so size alone is not enough)
  - while a copy is running the menu lists it under "Arriving", then nyxveil-isolib.py does
    the real work: only new/changed ISOs are extracted, removed ones are cleaned up

Configuration (environment, all optional):
  NYX_ISO_DIR   folder to watch                       (default /volume1/ISO/template/iso)
  NYX_HTTP      URL that folder is served at           (default http://192.168.24.20:8080/iso)
  NYX_SMB       UNC path of the folder for Windows setup (default derived from NYX_ISO_DIR)
  NYX_ARGS      extra nyxveil-isolib.py arguments, e.g. "--no-windows --smb-user netboot"
  NYX_POLL      seconds between checks                 (default 5)
  NYX_SETTLE    seconds a file must stay unchanged     (default 30)
  NYX_RESCAN    seconds between full safety re-runs    (default 21600 = 6 h)
"""
import os
import re
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, 'nyxveil-isolib.py')
ISO_DIR = os.environ.get('NYX_ISO_DIR', '/volume1/ISO/template/iso')
HTTP = os.environ.get('NYX_HTTP', 'http://192.168.24.20:8080/iso')
SMB = os.environ.get('NYX_SMB', '')
SMB_LIB = os.environ.get('NYX_SMB_LIB', '')
OUT_DIR = os.environ.get('NYX_OUT_DIR', '')      # default: <ISO dir>/_nyxveil
OUT_URL = os.environ.get('NYX_OUT_URL', '')
CHECK_URL = os.environ.get('NYX_CHECK_URL', '')
EXTRA = shlex.split(os.environ.get('NYX_ARGS', ''))
POLL = float(os.environ.get('NYX_POLL', '5'))
SETTLE = float(os.environ.get('NYX_SETTLE', '30'))
RESCAN = float(os.environ.get('NYX_RESCAN', '21600'))
WATCHED = re.compile(r'\.(iso|img)$', re.I)
TITLES = 'nyxveil-titles.conf'


def log(msg):
    print(time.strftime('%Y-%m-%d %H:%M:%S ') + msg, flush=True)


def snapshot():
    """{name: (size, mtime_ns)} for the ISOs (and the titles file) in the top level of ISO_DIR."""
    snap = {}
    try:
        names = os.listdir(ISO_DIR)
    except OSError as e:
        log('!! cannot list %s: %s' % (ISO_DIR, e))
        return None
    for n in names:
        if not (WATCHED.search(n) or n == TITLES) or n.startswith('.'):
            continue
        try:
            st = os.stat(os.path.join(ISO_DIR, n))
        except OSError:
            continue  # vanished between listdir and stat
        if os.path.isfile(os.path.join(ISO_DIR, n)):
            snap[n] = (st.st_size, st.st_mtime_ns)
    return snap


def run(copying=(), preparing=()):
    cmd = [sys.executable, '-u', GEN, '--iso-dir', ISO_DIR, '--http', HTTP] + EXTRA
    for flag, val in (('--smb', SMB), ('--smb-lib', SMB_LIB), ('--out-dir', OUT_DIR),
                      ('--out-url', OUT_URL), ('--check-url', CHECK_URL)):
        if val:
            cmd += [flag, val]
    for n in copying:
        cmd += ['--copying', n]
    for n in preparing:
        cmd += ['--preparing', n]
    quick = bool(copying or preparing)
    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for line in p.stdout:
        line = line.decode('utf-8', 'replace').rstrip()
        # the quick "arriving" refreshes only need their summary line in the log
        if not quick or line.startswith(('!!', 'HTTP check', '   ')) or 'bootable,' in line:
            log('   ' + line)
    rc = p.wait()
    if rc:
        log('!! nyxveil-isolib.py exited with %d' % rc)
    elif not quick:
        log('   library updated in %ds' % (time.time() - t0))
    return rc == 0


def main():
    log('Nyxveil ISO watcher - watching %s (poll %gs, settle %gs)' % (ISO_DIR, POLL, SETTLE))
    log('   served as %s%s' % (HTTP, ('  extra args: ' + ' '.join(EXTRA)) if EXTRA else ''))
    if not os.path.isfile(GEN):
        log('!! %s missing' % GEN)
        sys.exit(1)

    seen = {}          # name -> (size, mtime) at last poll
    changed_at = {}    # name -> monotonic time its (size, mtime) last changed
    done = None        # snapshot that the last full run processed
    shown_copying = None
    last_full = 0.0
    failures = 0
    first = True

    while True:
        snap = snapshot()
        now = time.monotonic()
        if snap is None:
            time.sleep(POLL * 4)
            continue
        for n, sig in snap.items():
            if seen.get(n) != sig:
                # files already there when the watcher starts count as settled
                changed_at[n] = -SETTLE if first else now
        for n in list(changed_at):
            if n not in snap:
                del changed_at[n]
        seen = snap
        first = False

        copying = sorted(n for n in snap if n != TITLES and now - changed_at.get(n, 0) < SETTLE)
        if copying:
            if copying != shown_copying:
                for n in copying:
                    if n not in (shown_copying or []):
                        log('-> %s arriving (%.1f GB so far)' % (n, snap[n][0] / 1e9))
                run(copying=copying)
                shown_copying = copying
            time.sleep(POLL)
            continue
        shown_copying = None

        stable = dict(snap)
        menu_missing = not os.path.isfile(os.path.join(OUT_DIR or os.path.join(ISO_DIR, '_nyxveil'), 'custom.ipxe'))
        due = now - last_full > RESCAN
        if stable != done or menu_missing or due:
            old = done or {}
            new = sorted(n for n in stable if n != TITLES and old.get(n) != stable[n])
            gone = sorted(n for n in old if n not in stable and n != TITLES)
            if done is None:
                log('== initial scan')
            for n in new:
                if done is not None:
                    log('+  %s (%.1f GB) copied - adding it' % (n, stable[n][0] / 1e9))
            for n in gone:
                log('-  %s removed - dropping it' % n)
            if new and done is not None:
                run(preparing=new)  # menu shows "being prepared" while the extraction runs
            ok = run()
            last_full = now
            if ok:
                done = stable
                failures = 0
            else:
                failures += 1
                time.sleep(min(600, 30 * failures))  # back off, then retry
                continue
        time.sleep(POLL)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass

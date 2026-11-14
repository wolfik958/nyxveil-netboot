#!/usr/bin/env python3
"""Nyxveil ISO library: boot your own ISOs from the NAS through netboot.xyz.

Scans the ISO folder, works out how each image boots, copies out the few files a network boot needs
(kernels, initrds, Windows setup trees) into <iso-dir>/_nyxveil/, and writes a themed iPXE menu
(_nyxveil/custom.ipxe) that netboot.xyz shows as "Custom URL Menu".

Runs on Synology DSM's own python3 (3.8) with the standard library only; 7z is used for Windows discs.
Safe to re-run: unchanged ISOs are skipped, removed ISOs get their extracted files cleaned up.

    sudo python3 nyxveil-isolib.py                 # scan, extract, write menu
    sudo python3 nyxveil-isolib.py --dry-run       # only show what would happen
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from isofs import Iso, IsoError  # noqa: E402

GEN = 3  # bump when extraction rules change, forces a re-extract
HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = '_nyxveil'

# Hand-tuned titles for the library on longgong; anything else falls back to the file name.
TITLES = [
    (r'^gparted-live-([\d.]+-\d+)', 'GParted Live {0}'),
    (r'^HBCD_PE', "Hiren's BootCD PE x64"),
    (r'^lmde-(\d+)-(\w+)', 'LMDE {0} {1!t}'),
    (r'^Nyarch-(\w+)-([\d.]+)', 'Nyarch Linux {1} {0}'),
    (r'^proxmox-ve_([\d.]+-\d+)', 'Proxmox VE {0}'),
    (r'^ryoku-([\d.]+)', 'Ryoku {0} (CachyOS)'),
    (r'^tails-amd64-([\d.]+)', 'Tails {0}'),
    (r'^Windows 7', 'Windows 7 Professional x64'),
    (r'^Zorin-OS-(\d+)-(\w+)', 'Zorin OS {0} {1}'),
    (r'^bazzite-deck-gnome', 'Bazzite Deck GNOME'),
    (r'^bazzite-(\w+)', 'Bazzite {0!t}'),
    (r'^cachyos-desktop-linux-(\d+)', 'CachyOS Desktop {0}'),
    (r'^clonezilla-live-([\d.]+-\d+)', 'Clonezilla Live {0}'),
    (r'^cs-cz_windows_10_enterprise_ltsc_2021', 'Windows 10 Ent LTSC 2021 cs-CZ'),
    (r'^Fedora-Workstation-Live-(\d+)', 'Fedora Workstation {0}'),
    (r'^Rocky-([\d.]+)-x86_64-dvd', 'Rocky Linux {0}'),
    (r'^win11_LTSC_(\w\w-\w\w)', 'Windows 11 LTSC {0}'),
    (r'^virtio-win-([\d.]+)', 'VirtIO drivers {0}'),
    (r'^en_windows_server_2022', 'Windows Server 2022'),
    (r'^Nobara-(\d+)', 'Nobara {0}'),
    (r'^archlinux-([\d.]+)', 'Arch Linux {0}'),
]

GROUPS = [
    ('live', 'Live systems'),
    ('install', 'Installers'),
    ('windows', 'Windows'),
    ('tools', 'Tools'),
]

DROP_TOKENS = re.compile(
    r'^(root=|iso-scan/|findiso=|fromiso=|live-media=|bootfrom=|archisolabel=|archisosearchuuid=|'
    r'archisosearchfilename=|archisodevice=|img_dev=|img_loop=|inst\.stage2=|inst\.repo=|rd\.live\.check|'
    r'cdrom-detect/|BOOT_IMAGE=|initrd=|iso-url=|url=|netboot=|fetch=|toram|copytoram|ip=|BOOTIF=|rd\.neednet=)')


def log(*a):
    print(*a, flush=True)


def human(n):
    for unit in ('B', 'K', 'M', 'G', 'T'):
        if n < 1024 or unit == 'T':
            return ('%.1f%s' % (n, unit)) if unit in ('G', 'T') else ('%d%s' % (n, unit))
        n /= 1024.0


def slugify(stem):
    s = re.sub(r'[^a-z0-9]+', '-', stem.lower()).strip('-')
    return s[:48].rstrip('-') or 'iso'


def title_for(fname, overrides):
    if fname in overrides:
        return overrides[fname]
    stem = os.path.splitext(fname)[0]
    for rx, fmt in TITLES:
        m = re.match(rx, stem, re.I)
        if m:
            parts = list(m.groups())
            out = fmt
            for i, p in enumerate(parts):
                out = out.replace('{%d!t}' % i, p.capitalize()).replace('{%d}' % i, p)
            return out
    return stem


# ---- boot config parsing ---------------------------------------------------------------------------
CFG_CANDIDATES = [
    'boot/grub/grub.cfg', 'boot/grub2/grub.cfg', 'EFI/BOOT/grub.cfg', 'EFI/boot/grub.cfg',
    'boot/grub/loopback.cfg', 'boot/grub/kernels.cfg', 'boot/grub/x86_64-efi/grub.cfg',
    'isolinux/isolinux.cfg', 'isolinux/live.cfg', 'isolinux/txt.cfg', 'syslinux/syslinux.cfg',
    'syslinux/archiso_sys-linux.cfg', 'boot/syslinux/archiso_sys-linux.cfg', 'boot/syslinux/syslinux.cfg',
]


def _clean_path(p):
    p = re.sub(r'^\(\$?\{?\w*\}?\)', '', p)  # ($root) / (${root}) / (cd)
    p = p.replace('${prefix}', '/boot/grub')
    return '/' + p.lstrip('/')


def parse_cfgs(iso):
    """Return (kernel, [initrds], [args]) for the first real boot entry, or None."""
    texts = []
    for c in CFG_CANDIDATES:
        if iso.exists(c):
            try:
                texts.append((c, iso.read(c, 1 << 20).decode('utf-8', 'replace')))
            except IsoError:
                pass
    # systemd-boot entries (archiso / CachyOS UEFI)
    for d in ('loader/entries',):
        for n in sorted(iso.listdir(d)):
            if n.endswith('.conf'):
                texts.append((d + '/' + n, iso.read(d + '/' + n, 1 << 16).decode('utf-8', 'replace')))

    skip_title = re.compile(r'memtest|safe|nomodeset|check|debug|fail|recovery|basic graphics|rescue|'
                            r'serial|speech|accessib|hdt|reboot|shutdown|firmware|uefi shell', re.I)
    for name, text in texts:
        if name.startswith('loader/entries'):
            kern, inits, args = None, [], []
            title = ''
            kind = None
            for line in text.splitlines():
                t = line.strip().split(None, 1)
                if len(t) < 2:
                    continue
                k, v = t[0], t[1].strip()
                if k == 'title':
                    title = v
                elif k in ('linux', 'efi'):
                    kern, kind = v, k
                elif k == 'initrd':
                    inits.append(v)
                elif k == 'options':
                    args += v.split()
            if kern and not skip_title.search(title) and kind == 'linux':
                return _norm(kern, inits, args)
            continue
        if name.endswith('.cfg') and ('syslinux' in name or 'isolinux' in name):
            res = _parse_syslinux(text, skip_title)
        else:
            res = _parse_grub(text, skip_title)
        if res:
            return res
    return None


def _norm(kern, inits, args):
    kern = _clean_path(kern)
    out_inits = []
    for i in inits:
        for p in i.split():
            out_inits.append(_clean_path(p))
    clean = [a for a in args if '$' not in a and not a.startswith('@')]
    return kern, out_inits, clean


def _parse_grub(text, skip_title):
    title = ''
    kern = None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"menuentry\s+(['\"])(.*?)\1", line)
        if m:
            title, kern = m.group(2), None
            continue
        if title and skip_title.search(title):
            continue
        t = line.split()
        if not t:
            continue
        if t[0] in ('$linux_cmd', '${linux_cmd}'):
            t[0] = 'linux'
        elif t[0] in ('$initrd_cmd', '${initrd_cmd}'):
            t[0] = 'initrd'
        if t[0] in ('linux', 'linuxefi', 'linux16') and len(t) > 1 and '$' not in t[1].replace('($root)', '').replace('(${root})', ''):
            kern = (t[1], t[2:])
        elif t[0] in ('initrd', 'initrdefi', 'initrd16') and kern and len(t) > 1:
            return _norm(kern[0], [' '.join(t[1:])], kern[1])
    return None


def _parse_syslinux(text, skip_title):
    label = ''
    kern = None
    for raw in text.splitlines():
        line = raw.strip()
        t = line.split(None, 1)
        if not t:
            continue
        k = t[0].lower()
        v = t[1] if len(t) > 1 else ''
        if k == 'label':
            label, kern = v, None
        elif k in ('menu',) and v.lower().startswith('label'):
            label = v[5:].strip() or label
        elif k in ('kernel', 'linux') and not skip_title.search(label) and not v.endswith('.c32'):
            kern = v
        elif k == 'append' and kern:
            args = v.split()
            inits = []
            rest = []
            for a in args:
                if a.startswith('initrd='):
                    inits += a[7:].split(',')
                else:
                    rest.append(a)
            if inits:
                return _norm(kern, inits, rest)
        elif k == 'initrd' and kern:
            # syslinux 'initrd' line; args follow on 'append'
            pass
    return None


# ---- classification --------------------------------------------------------------------------------
class Item(object):
    def __init__(self, fname, path):
        self.fname = fname
        self.path = path
        st = os.stat(path)
        self.size = st.st_size
        self.mtime = int(st.st_mtime)
        self.slug = slugify(os.path.splitext(fname)[0])
        self.title = fname
        self.family = None
        self.group = None
        self.reason = None
        self.kernel = None
        self.initrds = []
        self.args = []
        self.label = ''
        self.extra = {}

    def key(self):
        return '%d:%d:%d' % (GEN, self.size, self.mtime)


def classify(it, have_7z):
    low = it.fname.lower()
    if not low.endswith('.iso'):
        it.reason = 'disk image, not an ISO' + (' (grab the Tails ISO instead)' if 'tails' in low else '')
        return
    try:
        iso = Iso(it.path)
    except (IsoError, OSError) as e:
        if have_7z and any(f.lower() == 'sources/boot.wim' for f in sevenzip_list(it.path)):
            return classify_windows(it, None, have_7z)  # pure UDF image
        it.reason = 'not readable as ISO 9660 (%s)' % e
        return
    with iso:
        it.label = iso.label
        names = [n.lower() for n in iso.listdir('/')]
        # Windows / WinPE: real tree lives in UDF, the ISO 9660 view only has a README
        if names in (['readme.txt'], []) or iso.exists('sources/boot.wim'):
            return classify_windows(it, iso, have_7z)
        if 'tails' in low or iso.label.upper().startswith('TAILS'):
            it.reason = 'Tails refuses network boot; use netboot.xyz > Live CDs > Tails'
            return
        res = parse_cfgs(iso)
        if not res:
            if iso.exists('virtio-win_license.txt') or 'virtio' in low:
                it.reason = 'driver disc, not bootable'
            else:
                it.reason = 'no Linux boot entry found'
            return
        it.kernel, it.initrds, it.args = res
        a = ' '.join(it.args)
        missing = [p for p in [it.kernel] + it.initrds if not iso.exists(p)]
        if missing:
            it.reason = 'boot files missing: ' + ', '.join(missing)
            return
        if it.kernel.endswith('/linux26') or iso.exists('proxmox') and iso.exists('.cd-info'):
            it.family, it.group = 'proxmox', 'install'
        elif 'boot=casper' in a:
            it.family, it.group = 'casper', 'live'
        elif 'boot=live' in a:
            it.family = 'livedeb'
            it.group = 'tools' if re.search(r'clonezilla|gparted|rescue', low) else 'live'
        elif re.search(r'archiso(basedir|label|searchuuid|device)=', a):
            it.family, it.group = 'archiso', 'live'
            m = re.search(r'archisobasedir=(\S+)', a)
            base = m.group(1).strip('/') if m else 'arch'
            it.extra['basedir'] = base
            sfs = None
            for cand in ('%s/x86_64/airootfs.sfs' % base, '%s/x86_64/airootfs.erofs' % base):
                if iso.exists(cand):
                    sfs = cand
            if not sfs:
                it.family, it.reason = None, 'archiso without %s/x86_64/airootfs.sfs' % base
                return
            it.extra['rootfs'] = sfs
            it.extra['rootfs_size'] = iso.size(sfs)
        elif 'inst.stage2=' in a or 'inst.repo=' in a:
            it.family, it.group = 'anaconda', 'install'
            if not iso.exists('images/install.img'):
                it.family, it.reason = None, 'installer without images/install.img'
                return
            ti = iso.read('.treeinfo').decode('utf-8', 'replace') if iso.exists('.treeinfo') else ''
            fam = re.search(r'^family\s*=\s*(.+)$', ti, re.M)
            ver = re.search(r'^version\s*=\s*([\d.]+)$', ti, re.M)
            it.extra['family'] = fam.group(1).strip() if fam else ''
            it.extra['version'] = ver.group(1) if ver else ''
            it.extra['stage2_size'] = iso.size('images/install.img')
        elif 'rd.live.image' in a or 'root=live:' in a:
            it.family, it.group = 'dracut', 'live'
        else:
            it.reason = 'unknown boot style (%s)' % (a[:60] or it.kernel)


def sevenzip_list(path):
    out = subprocess.run(['7z', 'l', '-ba', '-slt', '-tudf', path], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL).stdout.decode('utf-8', 'replace')
    files = []
    cur = {}
    for line in out.splitlines() + ['']:
        if not line.strip():
            if cur.get('Path') and cur.get('Folder') != '+':
                files.append(cur['Path'].replace('\\', '/'))
            cur = {}
            continue
        k, _, v = line.partition(' = ')
        cur[k] = v
    return files


def classify_windows(it, iso, have_7z):
    if iso is not None and iso.exists('sources/boot.wim'):
        files = None  # ISO 9660 view is complete (WinPE builds such as Hiren's)
        lower = {}
        for p, e in iso.walk('/', 2):
            if not e.is_dir:
                lower[p.lstrip('/').lower()] = p.lstrip('/')
    else:
        if not have_7z:
            it.reason = 'Windows disc needs 7z (not installed)'
            return
        files = sevenzip_list(it.path)
        lower = dict((f.lower(), f) for f in files)
    it.extra['via'] = 'iso' if files is None else 'udf'
    if 'sources/boot.wim' not in lower:
        it.reason = 'Windows disc without sources/boot.wim'
        return
    need = {'bcd': 'boot/bcd', 'boot.sdi': 'boot/boot.sdi', 'boot.wim': 'sources/boot.wim'}
    for k, v in list(need.items()):
        if v not in lower:
            it.reason = 'Windows disc without ' + v
            return
        need[k] = lower[v]
    if 'bootmgr.efi' in lower:
        need['bootmgr.efi'] = lower['bootmgr.efi']
    setup = any(k in lower for k in ('sources/install.wim', 'sources/install.esd', 'sources/install.swm'))
    if setup and ('bootmgr.efi' not in lower or re.search(r'windows[ _-]?7|win7', it.fname, re.I)):
        it.reason = 'needs BIOS/CSM video - UEFI-only PCs cannot boot it'
        return
    it.extra['boot'] = need
    it.family = 'winsetup' if setup else 'winpe'
    it.group = 'windows' if setup else 'tools'
    if setup and files is None:
        it.extra['via'] = 'iso'


# ---- extraction -------------------------------------------------------------------------------------
def fixperm(path):
    for root, dirs, files in os.walk(path):
        os.chmod(root, 0o755)
        for f in files:
            os.chmod(os.path.join(root, f), 0o644)


def extract(it, out, dry):
    """Copy the files this ISO needs for a network boot into out/<slug>. Returns bytes written."""
    dest = os.path.join(out, it.slug)
    plan = []  # (src-in-iso, dest-rel)
    if it.family in ('casper', 'livedeb', 'dracut', 'proxmox', 'archiso', 'anaconda'):
        plan.append((it.kernel, 'vmlinuz'))
        for i, p in enumerate(it.initrds):
            plan.append((p, 'initrd%d' % i if len(it.initrds) > 1 else 'initrd'))
    if it.family == 'archiso':
        # flat layout, as netboot.xyz's archiso_pxe_http hook expects: <srv>/airootfs.sfs, <srv>/airootfs.sha512
        ext = it.extra['rootfs'].rsplit('.', 1)[1]
        plan.append((it.extra['rootfs'], 'airootfs.' + ext))
        plan.append((it.extra['rootfs'] + '.sha512', 'airootfs.sha512'))
        plan.append((it.extra['rootfs'] + '.sig', 'airootfs.%s.sig' % ext))
    if it.family == 'anaconda':
        plan.append(('images/install.img', 'images/install.img'))
        plan.append(('.treeinfo', '.treeinfo'))
        plan.append(('.discinfo', '.discinfo'))
    if it.family == 'winpe' and it.extra['via'] == 'iso':
        for p in it.extra['boot'].values():
            plan.append((p, p))

    if dry:
        return 0
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    written = 0
    if plan:
        with Iso(it.path) as iso:
            for src, rel in plan:
                if not iso.exists(src):
                    continue
                written += iso.extract(src, os.path.join(dest, rel))
    if it.family in ('winsetup', 'winpe') and it.extra['via'] == 'udf':
        cmd = ['7z', 'x', '-y', '-tudf', '-o' + dest, it.path]
        if it.family == 'winpe':
            cmd += list(it.extra['boot'].values())
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise RuntimeError('7z failed: ' + r.stderr.decode('utf-8', 'replace')[-300:])
        for root, _, files in os.walk(dest):
            written += sum(os.path.getsize(os.path.join(root, f)) for f in files)
    elif it.family == 'winsetup':
        with Iso(it.path) as iso:
            for p, e in iso.walk('/', 12):
                if not e.is_dir:
                    written += iso.extract(p, os.path.join(dest, p.lstrip('/')))
    fixperm(dest)
    return written


# ---- iPXE menu -------------------------------------------------------------------------------------
def q(s):
    return urllib.parse.quote(s, safe='/-_.~')


def win_cmd(it, smb_lib, smb_user, smb_pass):
    """smb_lib: UNC path of the library folder (the one holding <slug>/ and custom.ipxe)."""
    parts = smb_lib.strip('\\').split('\\')
    host = parts[0]
    share = '\\\\' + '\\'.join(parts[:2])
    folder = ''.join('\\' + p for p in parts[2:]) + '\\' + it.slug
    cred = ''
    if smb_user:
        cred = ' /user:%s %s' % (smb_user, smb_pass if smb_pass else '*')
    lines = [
        '@echo off',
        'title Nyxveil :: %s' % it.title,
        'color 0B',
        'wpeinit',
        'cls',
        'echo.',
        'echo    NYXVEIL // network install',
        'echo    %s' % it.title,
        'echo    source  %s%s' % (share, folder),
        'echo.',
        'set /a N=0',
        ':wait',
        'ping -n 1 -w 1000 %s >nul 2>&1 && goto mount' % host,
        'set /a N+=1',
        'if %N% geq 60 goto nonet',
        'ping -n 2 127.0.0.1 >nul',
        'goto wait',
        ':nonet',
        'echo    No network - is there a driver for this NIC in WinPE? Dropping to a shell.',
        'cmd /k',
        ':mount',
        'echo    Connecting to %s  (sign in with a NAS account that can read it)' % share,
        'net use N: %s%s /persistent:no' % (share, cred),
        'if errorlevel 1 goto mount',
        'N:',
        'cd "%s"' % folder,
        'echo    Starting Windows Setup...',
        'setup.exe',
        'cmd /k',
        '',
    ]
    return '\r\n'.join(lines)


def pad(text, width):
    """iPXE squeezes runs of spaces in menu items, so pad with ${nyx_sp} (one space each)."""
    text = text[:width]
    return text + '${nyx_sp}' * (width - len(text))


def url_host(url):
    m = re.match(r'[a-z]+://([^/:]+)', url)
    return m.group(1) if m else url


def build_menu(items, skipped, http_base, nyx_url):
    def W(*lines):
        out.extend(lines)

    out = []
    W('#!ipxe',
      '# Nyxveil ISO library - generated by nyxveil-isolib.py on %s. Do not edit; re-run the script.' %
      time.strftime('%Y-%m-%d %H:%M'),
      '',
      'set nyx %s' % nyx_url,
      'set nyx_iso %s' % http_base,
      'isset ${dhcp-server} && set nyx_ip ip=dhcp || set nyx_ip ip=${ip}::${gateway}:${netmask}:::none nameserver=${dns}',
      'isset ${nyx_default} || set nyx_default %s' % (('nyx_' + sorted(items, key=lambda i: ([g for g, _ in GROUPS].index(i.group), i.title.lower()))[0].slug) if items else 'nyx_back'),
      'set nyx_sp:hex 20 && set nyx_sp ${nyx_sp:string}',
      '',
      ':nyx_menu',
      'menu Nyxveil // ISO library on %s' % url_host(http_base))
    waiting = [i for i in skipped if getattr(i, 'wait', False)]
    skipped = [i for i in skipped if not getattr(i, 'wait', False)]
    if waiting:
        W('item --gap Arriving  (press Esc and reopen this menu to refresh)')
        for i in waiting:
            W('item --gap ${nyx_sp}${nyx_sp}%s%s' % (pad(i.title, 40), i.reason))
    for gid, gname in GROUPS:
        sel = [i for i in items if i.group == gid]
        if not sel:
            continue
        W('item --gap %s' % gname)
        for i in sel:
            kind = {'casper': 'live', 'livedeb': 'live', 'dracut': 'live', 'archiso': 'live',
                    'anaconda': 'installer', 'proxmox': 'installer', 'winsetup': 'setup', 'winpe': 'WinPE'}[i.family]
            W('item nyx_%s ${nyx_sp}${nyx_sp}%s%s%s' % (i.slug, pad(i.title, 40), pad(kind, 10),
                                                       '${nyx_sp}' * (7 - len(human(i.size))) + human(i.size)))
    if skipped:
        W('item --gap Not network-bootable')
        for i in skipped:
            W('item --gap ${nyx_sp}${nyx_sp}%s%s' % (pad(i.title, 40), i.reason[:64]))
    W('item --gap',
      'item nyx_back ${nyx_sp}${nyx_sp}Back to netboot.xyz',
      'choose --default ${nyx_default} nyx_sel || goto nyx_back',
      'set nyx_default ${nyx_sel}',
      'goto ${nyx_sel}',
      '',
      ':nyx_back',
      'exit 0',
      '',
      ':nyx_failed',
      'echo',
      'echo Boot failed. Press any key to return to the ISO library.',
      'prompt',
      'goto nyx_menu',
      '')

    for i in items:
        d = '${nyx}/' + i.slug
        iso_url = '${nyx_iso}/' + q(i.fname)
        args = [a for a in i.args if not DROP_TOKENS.match(a)]
        tail = []
        for sep in ('---', '--'):
            if sep in args:
                k = args.index(sep)
                args, tail = args[:k], args[k:]
                break
        W(':nyx_%s' % i.slug, 'imgfree',
          'echo', 'echo ${space} NYXVEIL // %s' % i.title)
        initrd_files = ['initrd%d' % n if len(i.initrds) > 1 else 'initrd' for n in range(len(i.initrds))]
        if i.family == 'casper':
            net = ['${nyx_ip}', 'boot=casper', 'netboot=url', 'url=' + iso_url]
            args = [a for a in args if a != 'boot=casper']
        elif i.family == 'livedeb':
            net = ['fetch=' + iso_url]
        elif i.family == 'dracut':
            net = ['${nyx_ip}', 'rd.neednet=1', 'root=live:' + iso_url]
            if 'rd.live.image' not in args:
                net.append('rd.live.image')
        elif i.family == 'archiso':
            net = ['BOOTIF=${netX/mac}', 'ip=dhcp', 'archiso_http_srv=%s' % d, 'cow_spacesize=10G']
            if not any(a.startswith('archisobasedir=') for a in args):
                net.append('archisobasedir=' + i.extra['basedir'])
        elif i.family == 'anaconda':
            net = ['${nyx_ip}', 'inst.stage2=%s/' % d]
            if i.extra.get('family', '').lower().startswith('rocky') and i.extra.get('version'):
                major = i.extra['version'].split('.')[0]
                net.append('inst.repo=https://dl.rockylinux.org/pub/rocky/%s/BaseOS/x86_64/os/' % major)
        elif i.family == 'proxmox':
            net = []
        else:
            net = None
        if i.family in ('winsetup', 'winpe'):
            b = i.extra['boot']
            W('echo ${space} loading Windows PE over HTTP...',
              'kernel ${nyx}/wimboot%s' % (' index=1' if i.family == 'winsetup' else ''))
            if 'bootmgr.efi' in b:
                W('initrd -n bootmgr.efi %s/%s bootmgr.efi' % (d, q(b['bootmgr.efi'])))
            W('initrd -n BCD %s/%s BCD' % (d, q(b['bcd'])),
              'initrd -n boot.sdi %s/%s boot.sdi' % (d, q(b['boot.sdi'])),
              'initrd -n boot.wim %s/%s boot.wim' % (d, q(b['boot.wim'])))
            if i.family == 'winsetup':
                W('initrd -n winpeshl.ini ${nyx}/winpeshl.ini winpeshl.ini',
                  'initrd -n nyx-setup.cmd %s/nyx-setup.cmd nyx-setup.cmd' % d)
            W('boot || goto nyx_failed', '')
            continue
        keys = set(n.split('=', 1)[0] for n in net)
        args = [a for a in args if a.split('=', 1)[0] not in keys]
        cmd = ' '.join(args + net + ['initrd=initrd.magic'] + tail)
        if i.family in ('casper', 'livedeb', 'dracut', 'proxmox'):
            W('echo ${space} the whole ISO (%s) is loaded into RAM - this PC needs at least %s' %
              (human(i.size), human(i.size + (2 << 30))))
        W('kernel %s/vmlinuz %s' % (d, cmd))
        for f in initrd_files:
            W('initrd %s/%s' % (d, f))
        if i.family == 'archiso':
            W('initrd ${nyx}/archiso_pxe_http /hooks/archiso_pxe_http mode=755 mkdir=1')
        if i.family == 'proxmox':
            W('initrd %s /proxmox.iso' % iso_url)
        W('boot || goto nyx_failed', '')
    return '\n'.join(out) + '\n'


# ---- main -------------------------------------------------------------------------------------------
def load_overrides(path):
    out = {}
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                out[k.strip()] = v.strip()
    return out


def default_smb(iso_dir, host):
    if not host or '$' in host:
        return None
    parts = os.path.abspath(iso_dir).strip('/').split('/')
    if len(parts) >= 2 and parts[0].startswith('volume'):
        return '\\\\%s\\%s' % (host, '\\'.join(parts[1:]))
    return '\\\\%s\\%s' % (host, parts[-1])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--iso-dir', default='/volume1/ISO/template/iso')
    ap.add_argument('--http', default='http://192.168.24.20:8080/iso',
                    help='URL where the netboot.xyz container serves --iso-dir (default %(default)s)')
    ap.add_argument('--smb', help='UNC path of --iso-dir for Windows setup (default derived, e.g. \\\\host\\ISO\\template\\iso)')
    ap.add_argument('--out-dir', help='where the library (boot files + menu) is written (default <iso-dir>/_nyxveil)')
    ap.add_argument('--out-url', help='URL of --out-dir as iPXE sees it; may use iPXE variables such as ${next-server}')
    ap.add_argument('--check-url', help='URL of --out-dir for the HTTP self-check (default --out-url)')
    ap.add_argument('--smb-lib', help='UNC path of --out-dir, for Windows setup (default derived from --smb)')
    ap.add_argument('--smb-user', help='NAS user for Windows setup to connect as (password is asked on screen)')
    ap.add_argument('--only', help='regex: only (re)process matching ISO file names')
    ap.add_argument('--force', action='store_true', help='re-extract even if an ISO looks unchanged')
    ap.add_argument('--no-windows', action='store_true', help='skip Windows installers (they need a full copy of the disc)')
    ap.add_argument('--copying', action='append', default=[], metavar='FILE',
                    help='list FILE in the menu as still being copied (used by nyxveil-watch.py)')
    ap.add_argument('--preparing', action='append', default=[], metavar='FILE',
                    help='list FILE in the menu as being prepared, without processing it yet')
    ap.add_argument('--dry-run', action='store_true')
    o = ap.parse_args()

    iso_dir = os.path.abspath(o.iso_dir)
    http_base = o.http.rstrip('/')
    host = url_host(http_base)
    out = os.path.abspath(o.out_dir) if o.out_dir else os.path.join(iso_dir, OUTDIR)
    nyx_url = (o.out_url or http_base + '/' + OUTDIR).rstrip('/')
    check_url = (o.check_url or nyx_url).rstrip('/')
    smb_lib = o.smb_lib
    if not smb_lib and out == os.path.join(iso_dir, OUTDIR):
        smb_iso = o.smb or default_smb(iso_dir, host)
        smb_lib = (smb_iso.rstrip('\\') + '\\' + OUTDIR) if smb_iso else None
    have_7z = shutil.which('7z') is not None
    overrides = load_overrides(os.path.join(iso_dir, 'nyxveil-titles.conf'))

    log('Nyxveil ISO library')
    log('  ISOs      %s' % iso_dir)
    log('  served as %s' % http_base)
    log('  menu      %s/custom.ipxe' % nyx_url)
    if not have_7z:
        log('  note      7z not found - Windows discs will be skipped')

    # one run at a time (the watcher and a manual setup.sh may overlap)
    lock = None
    if not o.dry_run:
        if not os.path.isdir(out):
            os.makedirs(out)
        import fcntl
        lock = open(os.path.join(out, '.lock'), 'w')
        fcntl.flock(lock, fcntl.LOCK_EX)

    manifest_path = os.path.join(out, 'manifest.json')
    try:
        manifest = json.load(open(manifest_path))
    except (OSError, ValueError):
        manifest = {}

    files = sorted(f for f in os.listdir(iso_dir)
                   if os.path.isfile(os.path.join(iso_dir, f)) and re.search(r'\.(iso|img)$', f, re.I))
    items, skipped = [], []
    slugs = set()
    for f in files:
        it = Item(f, os.path.join(iso_dir, f))
        while it.slug in slugs:
            it.slug += '-x'
        slugs.add(it.slug)
        it.title = title_for(f, overrides)
        if f in o.copying or f in o.preparing:
            it.reason = 'still copying to the NAS...' if f in o.copying else 'being prepared, back in a moment...'
            it.wait = True
            skipped.append(it)
            log('  %-60s -- %s' % (f[:60], it.reason))
            continue
        classify(it, have_7z)
        if it.family in ('winsetup',) and o.no_windows:
            it.family, it.reason = None, 'skipped (--no-windows)'
        elif it.family == 'winsetup' and not smb_lib:
            it.family, it.reason = None, 'Windows setup needs the library on SMB (NYX_SMB_LIB)'
        (items if it.family else skipped).append(it)
        log('  %-60s %s' % (f[:60], it.family or ('-- ' + it.reason)))

    if not o.dry_run:
        if not os.path.isdir(out):
            os.makedirs(out)
    new_manifest = {}
    for it in skipped:
        # keep the extracted files of an ISO that is only being re-copied or re-prepared
        if getattr(it, 'wait', False) and it.fname in manifest:
            new_manifest[it.fname] = manifest[it.fname]
    for it in items:
        old = manifest.get(it.fname)
        fresh = old and old.get('key') == it.key() and old.get('slug') == it.slug and \
            os.path.isdir(os.path.join(out, it.slug))
        if o.only and not re.search(o.only, it.fname):
            if old:
                new_manifest[it.fname] = old
            else:
                it.family, it.reason = None, 'not extracted yet (run without --only)'
            continue
        if fresh and not o.force:
            new_manifest[it.fname] = old
            continue
        need = it.size if it.family == 'winsetup' else it.extra.get('rootfs_size', 0) + it.extra.get('stage2_size', 0)
        if not o.dry_run:
            free = shutil.disk_usage(iso_dir).free
            if need + (1 << 30) > free:
                log('!! not enough space for %s (needs %s, free %s) - skipped' % (it.fname, human(need), human(free)))
                it.reason, it.family = 'not enough free space on the NAS', None
                continue
        log('==> %s  [%s]%s' % (it.title, it.family, ('  copying ~%s' % human(need)) if need > (64 << 20) else ''))
        t0 = time.time()
        n = extract(it, out, o.dry_run)
        if not o.dry_run:
            if it.family == 'winsetup':
                with open(os.path.join(out, it.slug, 'nyx-setup.cmd'), 'w', newline='') as fh:
                    fh.write(win_cmd(it, smb_lib, o.smb_user, os.environ.get('NYX_SMB_PASS')))
                os.chmod(os.path.join(out, it.slug, 'nyx-setup.cmd'), 0o644)
            log('    %s in %ds' % (human(n), time.time() - t0))
        new_manifest[it.fname] = {'key': it.key(), 'slug': it.slug, 'family': it.family}

    skipped += [i for i in items if not i.family]
    items = [i for i in items if i.family]
    items.sort(key=lambda i: i.title.lower())
    skipped.sort(key=lambda i: i.title.lower())
    menu = build_menu(items, skipped, http_base, nyx_url)

    if o.dry_run:
        sys.stdout.write('\n' + menu)
        return

    # remove extracted folders of ISOs that are gone
    keep = set(v['slug'] for v in new_manifest.values())
    for fname, v in manifest.items():
        if v.get('slug') not in keep and os.path.isdir(os.path.join(out, v['slug'])):
            log('--  removing %s (ISO gone)' % v['slug'])
            shutil.rmtree(os.path.join(out, v['slug']))

    for name in ('wimboot', 'archiso_pxe_http'):
        shutil.copyfile(os.path.join(HERE, 'vendor', name), os.path.join(out, name))
    with open(os.path.join(out, 'winpeshl.ini'), 'w', newline='') as fh:
        fh.write('[LaunchApps]\r\n"%SYSTEMROOT%\\System32\\cmd.exe", "/c %SYSTEMROOT%\\System32\\nyx-setup.cmd"\r\n')
    with open(os.path.join(out, 'custom.ipxe'), 'w') as fh:
        fh.write(menu)
    json.dump(new_manifest, open(manifest_path, 'w'), indent=1, sort_keys=True)
    for f in os.listdir(out):
        p = os.path.join(out, f)
        if os.path.isfile(p):
            os.chmod(p, 0o644)
    os.chmod(out, 0o755)

    log('')
    log('%d bootable, %d skipped. Menu written to %s' % (len(items), len(skipped), os.path.join(out, 'custom.ipxe')))
    if '$' in check_url:
        return  # URL only resolvable by iPXE (e.g. ${next-server}); nothing to check from here
    try:
        with urllib.request.urlopen(check_url + '/custom.ipxe', timeout=5) as r:
            ok = r.read(6) == b'#!ipxe'
        log('HTTP check: %s/custom.ipxe %s' % (check_url, 'OK' if ok else 'served, but not the generated file?'))
    except Exception as e:  # noqa: BLE001
        log('!! HTTP check failed: %s/custom.ipxe -> %s' % (check_url, e))
        log('   netboot.xyz must serve %s at %s - check the container port (docker port ...)' % (out, nyx_url))


if __name__ == '__main__':
    main()

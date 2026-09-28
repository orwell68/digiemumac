# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the macOS app (onedir, wrapped in digiemu.app).

Build it with tools/build-macos.sh, which makes the build venv, passes
DIGIEMU_VERSION and DIGIEMU_UC_SHA256, runs the self-test on the new app and
zips it through packaging/bundle_guard.py --layout macos. By hand, from a
venv that has PyInstaller, unicorn and capstone:
    python -m PyInstaller --noconfirm --clean --distpath OUT/dist --workpath OUT/work packaging/digiemu-mac.spec

What it makes: dist/digiemu.app, with two executables in Contents/MacOS:
digiemu (windowed; what the Finder opens) and digiemu-console (the same
program with its output on the terminal, for support and for CI), sharing
one Contents/Frameworks. It also makes dist/digiemu/, the plain onedir
folder the .app is assembled from; the script only ships the .app.

This spec is packaging/digiemu.spec (the Windows one) with the Windows
matters taken out and the macOS ones put in:
  * The patched Unicorn library is libunicorn.2.dylib, added by hand to
    unicorn/lib just as unicorn.dll is on Windows (nothing else collects it),
    and its sha256 must equal DIGIEMU_UC_SHA256 or, failing that, the hash
    of the library in this repo's .venv. That pins the SOURCE: when the
    bundle is sealed, PyInstaller ad-hoc signs every binary in it, so the
    bytes in the finished .app differ from the source's. The pin proves the
    patched build went in; the frozen --selftest then proves the patches by
    behaviour (emu.unicorn_compat and the speed options), as on Windows.
    digiemu-build.json therefore carries unicorn_source_sha256, and no
    unicorn_sha256 for the self-test to compare the loaded file against.
  * No Control Flow Guard, no PE checksum, no version resource: those are
    Windows. Gatekeeper is the macOS matter, and it is the build script's
    (an ad-hoc signature, which is what PyInstaller applies, opens after
    a right-click > Open or System Settings > Privacy & Security).
  * The app's data lives in ~/Library/Application Support/digiemu
    (emu.portable.app_root), not next to the executable: a .app is dragged
    into /Applications as one icon.
  * The same spec runs on Linux (libunicorn.so.2, no .app; BUNDLE is a
    no-op there), which is how the build is exercised without a Mac.
Everything else -- the datas, the hidden imports, the private-module and
firmware guards -- is the same as the Windows spec, for the same reasons.
"""
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import sys
import sysconfig

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))
IS_MAC = sys.platform == 'darwin'
UC_NAME = 'libunicorn.2.dylib' if IS_MAC else 'libunicorn.so.2'


def _load_guard():
    # By path, not by sys.path: anything on sys.path is also an import root
    # for the analysis below.
    spec = importlib.util.spec_from_file_location(
        'digiemu_bundle_guard', os.path.join(SPECPATH, 'bundle_guard.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load_guard()


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


# -- version -----------------------------------------------------------------
VERSION = os.environ.get('DIGIEMU_VERSION', '0.1.0')
if not re.fullmatch(r'\d{1,5}\.\d{1,5}\.\d{1,5}', VERSION):
    raise SystemExit('DIGIEMU_VERSION must be x.y.z, got %r' % VERSION)

# -- the patched Unicorn library ---------------------------------------------
UC_LIB = os.environ.get('DIGIEMU_UC_DLL') or os.path.join(
    importlib.util.find_spec('unicorn').submodule_search_locations[0], 'lib', UC_NAME)
UC_REFERENCE = os.path.join(ROOT, '.venv', 'lib', 'python%d.%d' % sys.version_info[:2],
                            'site-packages', 'unicorn', 'lib', UC_NAME)
UC_SHA256 = (os.environ.get('DIGIEMU_UC_SHA256') or '').strip().lower()
if not UC_SHA256:
    if not os.path.isfile(UC_REFERENCE):
        raise SystemExit('set DIGIEMU_UC_SHA256 to the patched %s hash '
                         '(no reference library at %s)' % (UC_NAME, UC_REFERENCE))
    UC_SHA256 = _sha256(UC_REFERENCE)
if not os.path.isfile(UC_LIB):
    raise SystemExit('no %s at %s' % (UC_NAME, UC_LIB))
_got = _sha256(UC_LIB)
if _got != UC_SHA256:
    raise SystemExit('refusing to bundle %s (sha256 %s): expected the patched build %s'
                     % (UC_LIB, _got, UC_SHA256))


# -- licences ----------------------------------------------------------------
def _dist_file(dist, pattern):
    """-> the absolute path of the first file of installed `dist` whose name
    matches `pattern` (e.g. its LICENSE), or stop the build."""
    d = importlib.metadata.distribution(dist)
    for f in d.files or ():
        if re.fullmatch(pattern, os.path.basename(str(f)), re.I):
            return os.path.abspath(str(d.locate_file(f)))
    raise SystemExit('no %s in the installed %s distribution' % (pattern, dist))


def _python_license():
    # <stdlib>/LICENSE.txt on python.org, uv (python-build-standalone) and
    # Homebrew builds; <base>/LICENSE.txt is where Windows keeps it.
    for p in (os.path.join(sysconfig.get_path('stdlib'), 'LICENSE.txt'),
              os.path.join(sys.base_prefix, 'LICENSE.txt'),
              os.path.join(sys.base_prefix, 'lib', 'python%d.%d' % sys.version_info[:2], 'LICENSE.txt')):
        if os.path.isfile(p):
            return p
    raise SystemExit('no Python LICENSE.txt under %s' % sys.base_prefix)


# -- build info, read by the self-test ------------------------------------
# Written under workpath, which --clean empties. unicorn_sha256 is left out
# on purpose (see the module docstring): the loaded file is re-signed.
BUILD_INFO = os.path.join(workpath, 'digiemu-build.json')
os.makedirs(workpath, exist_ok=True)
with open(BUILD_INFO, 'w', encoding='utf-8', newline='\n') as fh:
    json.dump({'version': VERSION, 'unicorn_source_sha256': UC_SHA256,
               'unicorn_library': UC_NAME, 'platform': sys.platform,
               'python': sys.version.split()[0]}, fh, indent=2, sort_keys=True)
    fh.write('\n')

datas = [
    (os.path.join(ROOT, 'devices', '*.toml'), 'devices'),
    (os.path.join(ROOT, 'LICENSE'), '.'),
    (os.path.join(ROOT, 'patches', '*.patch'), 'patches'),
    (os.path.join(ROOT, 'patches', 'README.md'), 'patches'),
    (_dist_file('capstone', r'LICENSE(\.txt)?'), os.path.join('licenses', 'capstone')),
    (_python_license(), os.path.join('licenses', 'python')),
    (BUILD_INFO, '.'),
]

HIDDEN = list(guard.REQUIRED_MODULES)
EXCLUDES = (list(guard.PRIVATE_MODULES)
            + ['tools.' + m for m in guard.PRIVATE_MODULES if '.' not in m]
            + ['unicorn.unicorn_py2', 'pypcode'])

a = Analysis(
    [os.path.join(SPECPATH, 'digiemu_main.py')],
    pathex=[ROOT],
    binaries=[(UC_LIB, os.path.join('unicorn', 'lib'))],
    datas=datas,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(SPECPATH, 'rth_digiemu.py')],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)


# -- guards: fail the build before anything is written -------------------
def _n(p):
    return os.path.normcase(os.path.normpath(p))


_uc = [(d, s) for d, s, _t in a.binaries if os.path.basename(d) == UC_NAME]
if len(_uc) != 1 or _n(_uc[0][0]) != _n('unicorn/lib/' + UC_NAME):
    raise SystemExit('unexpected %s collection: %r' % (UC_NAME, _uc))
if not any(_n(d).startswith(_n('capstone/lib/libcapstone.')) for d, _s, _t in a.binaries):
    raise SystemExit('libcapstone was not collected (is hooks-contrib installed?)')
_leak = guard.toc_problems(list(a.datas) + list(a.binaries), root=ROOT)
if _leak:
    raise SystemExit('firmware-derived files would be bundled:\n  ' + '\n  '.join(_leak[:20]))
_private = [m for m in [name for name, _s, _t in a.pure] + [name for name, _s, _t in a.scripts]
            if guard.module_problem(m)]
if _private:
    raise SystemExit('private modules would be bundled: %r' % _private)
_have = {name for name, _s, _t in a.pure}
_lacking = [m for m in guard.REQUIRED_MODULES if m not in _have]
if _lacking:
    raise SystemExit('the analysis lacks modules the app imports at run time: %s'
                     % ', '.join(_lacking))

pyz = PYZ(a.pure)

_icns = os.path.join(SPECPATH, 'digiemu.icns')
ICON = _icns if os.path.exists(_icns) else None


def _exe(name, console):
    return EXE(pyz, a.scripts, [('X utf8', None, 'OPTION')],
               exclude_binaries=True, name=name, debug=False,
               bootloader_ignore_signals=False, strip=False, upx=False,
               console=console, disable_windowed_traceback=False,
               icon=ICON, target_arch=None, codesign_identity=None,
               entitlements_file=None, contents_directory='_internal')


exe = _exe('digiemu', console=False)
exe_console = _exe('digiemu-console', console=True)
# The windowed exe goes last: COLLECT (and so BUNDLE) takes `console` from
# the last EXE it is given, and a console app's Info.plist gets
# LSBackgroundOnly, which would keep the panel off the Dock and unfocused.
coll = COLLECT(exe_console, exe, a.binaries, a.datas,
               strip=False, upx=False, upx_exclude=[], name='digiemu')

# The .app: `exe` first, so CFBundleExecutable is digiemu (BUNDLE names the
# bundle after the first EXECUTABLE it sees); the COLLECT brings
# digiemu-console and everything else. A no-op on Linux.
app = BUNDLE(exe, coll,
             name='digiemu.app',
             icon=ICON,
             bundle_identifier='io.github.irpina.digiemu',
             version=VERSION,
             info_plist={
                 'CFBundleName': 'digiemu',
                 'CFBundleDisplayName': 'digiemu',
                 'CFBundleShortVersionString': VERSION,
                 'CFBundleVersion': VERSION,
                 'NSHighResolutionCapable': True,
                 'LSBackgroundOnly': False,
                 'LSMinimumSystemVersion': '11.0',
                 'NSHumanReadableCopyright': 'GPL-2.0-or-later; see Contents/Resources/LICENSE',
                 # Tk apps are not dark-mode aware; a light appearance keeps
                 # the panel legible whatever the system setting.
                 'NSRequiresAquaSystemAppearance': True,
             })

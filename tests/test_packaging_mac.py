"""packaging/bundle_guard.py's macOS layout, on a synthetic digiemu.app.

PyInstaller lays an .app out as Contents/MacOS (the executables),
Contents/Frameworks (code, and _MEIPASS) and Contents/Resources (data), with
relative symlinks so that both trees look complete. The audit must accept
those links and nothing else, the Mach-O check replaces the PE one, and
make_zip() must write the links as links so the unzipped app still runs.
Runs anywhere symlinks can be made; the Windows tests in test_packaging.py
cover the default layout.
"""
import importlib.util
import os
import stat
import sys
import tempfile
import unittest
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGING = os.path.join(REPO, 'packaging')

spec = importlib.util.spec_from_file_location(
    'digiemu_bundle_guard_mac', os.path.join(PACKAGING, 'bundle_guard.py'))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

MACHO64 = b'\xcf\xfa\xed\xfe' + b'\0' * 28
FAT = b'\xca\xfe\xba\xbe' + b'\0' * 28
UC_BYTES = b'\xcf\xfa\xed\xfe patched unicorn dylib'


def _put(root, rel, data=b'x'):
    path = os.path.join(root, *rel.split('/'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(data)
    return path


def _link(root, rel, target):
    path = os.path.join(root, *rel.split('/'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.symlink(target, path)
    return path


def make_app(root):
    """A minimal digiemu.app as PyInstaller 6 would assemble it."""
    app = os.path.join(root, 'digiemu.app')
    _put(app, 'Contents/Info.plist', b'<plist/>')
    _put(app, 'Contents/MacOS/digiemu', MACHO64)
    _put(app, 'Contents/MacOS/digiemu-console', FAT)
    _put(app, 'Contents/Frameworks/unicorn/lib/libunicorn.2.dylib', UC_BYTES)
    _put(app, 'Contents/Frameworks/capstone/lib/libcapstone.dylib', MACHO64)
    _put(app, 'Contents/Frameworks/base_library.zip', b'PK')
    _put(app, 'Contents/Resources/devices/digitakt.toml', b'[device]\n')
    _put(app, 'Contents/Resources/digiemu-build.json', b'{}')
    _put(app, 'Contents/Resources/LICENSE', b'GPL')
    # The cross-links PyInstaller makes between the two trees.
    _link(app, 'Contents/Frameworks/devices/digitakt.toml',
          '../../Resources/devices/digitakt.toml')
    _link(app, 'Contents/Resources/unicorn/lib/libunicorn.2.dylib',
          '../../../Frameworks/unicorn/lib/libunicorn.2.dylib')
    _link(app, 'Contents/Resources/capstone/lib/libcapstone.dylib',
          '../../../Frameworks/capstone/lib/libcapstone.dylib')
    _link(app, 'Contents/Resources/base_library.zip', '../Frameworks/base_library.zip')
    return app


@unittest.skipUnless(hasattr(os, 'symlink'), 'needs symlinks')
class MacLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = make_app(self.tmp.name)
        if sys.platform == 'win32':
            try:
                os.readlink(os.path.join(self.app, 'Contents', 'Resources', 'base_library.zip'))
            except OSError:
                self.skipTest('cannot make symlinks here')

    def audit(self, **kw):
        kw.setdefault('layout', guard.MACOS)
        kw.setdefault('unicorn_sha256', guard.hashlib.sha256(UC_BYTES).hexdigest())
        return guard.audit(self.app, **kw)

    def test_clean_app_passes_and_lists_links(self):
        files, problems = self.audit()
        self.assertEqual(problems, [])
        self.assertIn('Contents/MacOS/digiemu', files)
        self.assertIn('Contents/Frameworks/unicorn/lib/libunicorn.2.dylib', files)
        self.assertIn('Contents/Resources/unicorn/lib/libunicorn.2.dylib', files)   # the link
        self.assertIn('Contents/Frameworks/devices/digitakt.toml', files)           # the link

    def test_archives_checked_with_layout_paths(self):
        seen = []

        def lister(path):
            seen.append(os.path.relpath(path, self.app))
            return list(guard.REQUIRED_MODULES)

        files, problems = self.audit(lister=lister, require_pyz=True)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(seen), ['Contents/MacOS/digiemu', 'Contents/MacOS/digiemu-console'])
        _files, problems = self.audit(lister=lambda p: ['emu.portable'], require_pyz=True)
        self.assertTrue(any('lacks modules' in p for p in problems))
        _files, problems = self.audit(lister=lambda p: list(guard.REQUIRED_MODULES) + ['machinepatch'])
        self.assertTrue(any('machinepatch' in p for p in problems))

    def test_wrong_unicorn_hash_refused(self):
        _files, problems = self.audit(unicorn_sha256='00' * 32)
        self.assertTrue(any('expected the patched build' in p for p in problems))

    def test_not_macho_refused(self):
        _put(self.app, 'Contents/MacOS/digiemu', b'#!/bin/sh\n')
        _files, problems = self.audit()
        self.assertTrue(any('not a Mach-O executable' in p for p in problems))

    def test_link_out_of_the_bundle_refused(self):
        outside = _put(self.tmp.name, 'elsewhere/Digitakt_OS1.53.syx', b'\xf0\x00\x20\x3c')
        _link(self.app, 'Contents/Resources/loot.bin', os.path.relpath(outside, os.path.join(self.app, 'Contents', 'Resources')))
        _files, problems = self.audit()
        self.assertTrue(any('loot.bin: link' in p for p in problems))
        os.remove(os.path.join(self.app, 'Contents', 'Resources', 'loot.bin'))
        _link(self.app, 'Contents/Resources/loot.bin', outside)      # absolute
        _files, problems = self.audit()
        self.assertTrue(any('loot.bin: link' in p for p in problems))

    def test_firmware_inside_refused(self):
        _put(self.app, 'Contents/Resources/firmware/digitakt-1-53/Digitakt_OS1.53.syx', b'\xf0\x00\x20\x3c')
        _files, problems = self.audit()
        self.assertTrue(any('firmware' in p for p in problems))
        # Even through a link that itself has a harmless name.
        os.remove(os.path.join(self.app, 'Contents/Resources/firmware/digitakt-1-53/Digitakt_OS1.53.syx'))
        os.removedirs(os.path.join(self.app, 'Contents/Resources/firmware/digitakt-1-53'))
        _put(self.app, 'Contents/Resources/notes.txt', b'\xf0\x00\x20\x3c...')
        _files, problems = self.audit()
        self.assertTrue(any('SysEx header' in p for p in problems))

    def test_top_level_must_be_contents_only(self):
        _put(self.app, 'logs/launcher.log', b'')
        _files, problems = self.audit()
        self.assertTrue(any('unexpected top-level entry' in p for p in problems))

    def test_windows_layout_rejects_the_app(self):
        _files, problems = guard.audit(self.app, layout=guard.WINDOWS)
        self.assertTrue(problems)

    def test_zip_keeps_links_and_modes(self):
        os.chmod(os.path.join(self.app, 'Contents', 'MacOS', 'digiemu'), 0o755)
        files, problems = self.audit()
        self.assertEqual(problems, [])
        out = os.path.join(self.tmp.name, 'app.zip')
        guard.make_zip(self.app, files, out, arcroot='digiemu.app')
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            link = zf.getinfo('digiemu.app/Contents/Resources/unicorn/lib/libunicorn.2.dylib')
            self.assertTrue(stat.S_ISLNK(link.external_attr >> 16))
            self.assertEqual(zf.read(link).decode(), '../../../Frameworks/unicorn/lib/libunicorn.2.dylib')
            exe = zf.getinfo('digiemu.app/Contents/MacOS/digiemu')
            self.assertEqual((exe.external_attr >> 16) & 0o777, 0o755)
            self.assertEqual(zf.read(exe), MACHO64)
        self.assertEqual(len(names), len(files))
        self.assertTrue(all(n.startswith('digiemu.app/') for n in names))

    def test_host_layout(self):
        self.assertIs(guard.host_layout('darwin'), guard.MACOS)
        self.assertIs(guard.host_layout('win32'), guard.WINDOWS)
        self.assertIs(guard.host_layout('linux'), guard.LINUX)

    def test_cli_layout_flag(self):
        import contextlib
        import io
        from unittest import mock
        buf = io.StringIO()
        # The fake executables have no PyInstaller archive to list.
        with mock.patch.object(guard, 'pyinstaller_lister', return_value=None):
            with contextlib.redirect_stdout(buf):
                rc = guard.main([self.app, '--layout', 'macos',
                                 '--unicorn-sha256', guard.hashlib.sha256(UC_BYTES).hexdigest()])
            self.assertEqual(rc, 0, buf.getvalue())
            with contextlib.redirect_stdout(buf):
                rc = guard.main([self.app])                 # the Windows default
            self.assertEqual(rc, 1)


if __name__ == '__main__':
    unittest.main()

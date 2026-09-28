#!/usr/bin/env bash
# Build the macOS app into <out>/digiemu-macos-<arch>-<version>.zip.
#
#     tools/build-macos.sh --out ../build-out [--build-venv ../.venv-build] [--version x.y.z]
#
# The counterpart of tools/build-windows.ps1. It needs this repo's .venv with
# the patched Unicorn installed (uv sync; tools/install-patched-unicorn.sh),
# and Xcode's command line tools for codesign (PyInstaller ad-hoc signs the
# bundle). --version defaults to APP_VERSION in emu/portable.py, which the app
# shows and stamps into firmware folders; a release tag must match it.
#
# Steps, each checked before the next:
#  1. The build venv (default: ../.venv-build, always outside the repo). If it
#     is missing, make it from the same base interpreter as .venv (pyvenv.cfg
#     'home') and pip install requirements-build-macos.txt into it. unicorn
#     and capstone are mirrored from .venv on every run, so a rebuilt patched
#     dylib is always the one bundled; the static libunicorn.a and the
#     headers are left out.
#  2. Verify: pip check, the PyInstaller version, the hooks-contrib entry
#     point, the dylib hash against .venv, the compat check (by behaviour) in
#     the build venv, and that the app code compiles on this interpreter.
#  3. PyInstaller with packaging/digiemu-mac.spec, dist and work under --out
#     (never inside the repo). It makes dist/digiemu.app; PyInstaller signs
#     it ad hoc and verifies the signature.
#  4. The frozen self-test, run by both executables inside the bundle. It
#     checks the patched Unicorn by behaviour (the bundle's own copy, not one
#     from the environment), capstone, the device files, Tk, and every module
#     the app imports inside functions.
#  5. packaging/bundle_guard.py --layout macos audits the .app (no firmware,
#     no private modules, every required module in both archives, one Unicorn
#     library, only in-bundle symlinks) and writes the zip from the list it
#     has just audited, links as links. Only this step may zip dist.
#
# On Linux the same steps make dist/digiemu (a plain folder, no .app) and a
# digiemu-linux-<arch> zip: that is how the spec and this script are tested
# on a box without macOS (and how CI checks a pull request), not a supported
# way to ship digiemu.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
out=''
build_venv="$root/../.venv-build"
version=''
while [[ $# -gt 0 ]]; do
  case $1 in
    --out) out=$2; shift 2 ;;
    --build-venv) build_venv=$2; shift 2 ;;
    --version) version=$2; shift 2 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "build-macos: unknown argument $1" >&2; exit 2 ;;
  esac
done

fail() { echo "build-macos: FAILED: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }
sha256() { shasum -a 256 "$1" | awk '{print $1}'; }
abspath() { python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$1"; }

[[ -n $out ]] || fail '--out DIR is required'
case $(uname -s) in
  Darwin) os=macos; lib_name=libunicorn.2.dylib ;;
  Linux)  os=linux; lib_name=libunicorn.so.2 ;;
  *) fail "unsupported platform $(uname -s)" ;;
esac
arch=$(uname -m)
[[ $arch == aarch64 ]] && arch=arm64

if [[ -z $version ]]; then
  version=$(sed -n "s/^APP_VERSION = '\([^']*\)'.*/\1/p" "$root/emu/portable.py" | head -1)
  [[ -n $version ]] || fail 'no APP_VERSION in emu/portable.py; pass --version'
fi
[[ $version =~ ^[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}$ ]] || fail "--version must be x.y.z, got '$version'"

dev_venv=${DIGIEMU_VENV:-$root/.venv}
dev_py="$dev_venv/bin/python"
[[ -x $dev_py ]] || fail "no project venv at $dev_venv (uv sync first)"
dev_sp=$("$dev_py" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
ref_lib="$dev_sp/unicorn/lib/$lib_name"
[[ -f $ref_lib ]] || fail "no patched $lib_name at $ref_lib (run tools/install-patched-unicorn.sh)"
uc_sha=$(sha256 "$ref_lib")

mkdir -p "$out"
out=$(abspath "$out")
build_venv=$(abspath "$build_venv")
case "$build_venv/" in "$root/"*) fail "--build-venv must be outside the repo ($build_venv)" ;; esac
case "$out/" in "$root/"*) fail "--out must be outside the repo ($out): dist/ would sit next to sections/ and snapshots/" ;; esac
py="$build_venv/bin/python"

if [[ $os == macos ]]; then
  dist="$out/dist/digiemu.app"
  exes=("$dist/Contents/MacOS/digiemu-console" "$dist/Contents/MacOS/digiemu")
  zip="$out/digiemu-macos-$arch-$version.zip"
  arcroot=digiemu.app
else
  dist="$out/dist/digiemu"
  exes=("$dist/digiemu-console" "$dist/digiemu")
  zip="$out/digiemu-linux-$arch-$version.zip"
  arcroot=digiemu
fi

echo "repo        $root"
echo "reference   $ref_lib"
echo "            sha256 $uc_sha"
echo "target      $os $arch, digiemu $version"

# -- 1. build venv ------------------------------------------------------------
step 'build venv'
if [[ ! -x $py ]]; then
  # The very interpreter .venv was made from (not whatever python3 is first
  # in its 'home' folder, which can be another version without tkinter).
  base_py=$("$dev_py" -c 'import sys; print(sys._base_executable)')
  [[ -x $base_py ]] || fail "base interpreter $base_py of $dev_venv is missing"
  echo "creating $build_venv from $base_py"
  "$base_py" -m venv "$build_venv"
  "$py" -m pip install --quiet --disable-pip-version-check -r "$root/requirements-build-macos.txt"
fi
bv_sp=$("$py" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
# unicorn and capstone: mirrored from the dev venv every time. The static
# library, the headers and bytecode caches stay behind.
for pkg in unicorn capstone; do
  rm -rf "$bv_sp/$pkg" "$bv_sp/$pkg"-*.dist-info
  cp -R "$dev_sp/$pkg" "$bv_sp/$pkg"
  for d in "$dev_sp/$pkg"-*.dist-info; do cp -R "$d" "$bv_sp/$(basename "$d")"; done
  find "$bv_sp/$pkg" -name __pycache__ -type d -prune -exec rm -rf {} +
  rm -rf "$bv_sp/$pkg/include" "$bv_sp/$pkg/lib/libunicorn.a"
done
# unicorn/lib holds exactly the one library the binding loads.
find "$bv_sp/unicorn/lib" -mindepth 1 ! -name "$lib_name" -exec rm -rf {} + 2>/dev/null || true

# -- 2. verify ------------------------------------------------------------------
step 'verify build venv'
export PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1
unset TCL_LIBRARY TK_LIBRARY LIBUNICORN_PATH LIBCAPSTONE_PATH PYTHONPATH PYTHONHOME DIGIEMU_UC_DLL || true
"$py" -m pip check | sed 's/^/  /' || fail 'pip check'
piv=$("$py" -m PyInstaller --version) || fail 'PyInstaller does not run in the build venv'
echo "  PyInstaller $piv"
ep=$("$py" -c "import importlib.metadata as m; print(sorted(e.value for e in m.entry_points(group='pyinstaller40')))")
echo "  pyinstaller40 entry points: $ep"
[[ $ep == *_pyinstaller_hooks_contrib* ]] || fail 'hooks-contrib is not registered'
bv_sha=$(sha256 "$bv_sp/unicorn/lib/$lib_name")
[[ $bv_sha == "$uc_sha" ]] || fail "build venv $lib_name sha256 $bv_sha != reference $uc_sha"
echo "  $lib_name matches the reference"
(cd "$root" && "$py" -m emu.unicorn_compat > /dev/null) || fail 'emu.unicorn_compat in the build venv'
echo '  emu.unicorn_compat: compatible'
"$py" - "$root" <<'PY' | sed 's/^/  /'
import os, sys
root, bad, n = sys.argv[1], [], 0
paths = [os.path.join(root, 'tools', f) for f in ('introboot.py', 'uisettle.py', 'ekfsadd.py')]
for pkg in ('emu', 'dt2', 'packaging'):
    for here, _dirs, files in os.walk(os.path.join(root, pkg)):
        paths += [os.path.join(here, f) for f in files if f.endswith('.py')]
for p in paths:
    if os.path.isfile(p):
        n += 1
        try:
            with open(p, 'rb') as fh:
                compile(fh.read(), p, 'exec')
        except SyntaxError as exc:
            bad.append('%s: %s' % (p, exc))
print('compiled %d files on Python %s' % (n, sys.version.split()[0]))
for b in bad:
    print('SYNTAX ERROR ' + b)
sys.exit(1 if bad else 0)
PY
[[ ${PIPESTATUS[0]} -eq 0 ]] || fail 'app code does not compile on the build interpreter'

# -- 3. PyInstaller -------------------------------------------------------------
step "PyInstaller (version $version)"
export DIGIEMU_VERSION=$version DIGIEMU_UC_SHA256=$uc_sha
log="$out/pyinstaller.log"
rm -rf "$out/dist" "$out/work"
(cd "$out" && "$py" -m PyInstaller --noconfirm --clean \
    --distpath "$out/dist" --workpath "$out/work" \
    "$root/packaging/digiemu-mac.spec") > "$log" 2>&1 || {
  grep -E 'ERROR|SystemExit|refusing|would be bundled|lacks modules' "$log" | sed 's/^/  /' || true
  fail "PyInstaller failed (log: $log)"
}
grep -E 'WARNING|ERROR|refusing|would be bundled|lacks modules|Build complete' "$log" | sed 's/^/  /' || true
grep -q 'tkinter installation is broken' "$log" && fail "PyInstaller dropped tkinter (log: $log)"
[[ -x ${exes[0]} ]] || fail "no ${exes[0]}"
echo "  log: $log"
echo "  missing-module report: $out/work/digiemu/warn-digiemu.txt"
if [[ $os == macos ]]; then
  codesign --verify --deep --strict "$dist" && echo '  codesign: ad-hoc signature verifies' \
    || fail 'the bundle does not verify with codesign'
fi

# -- 4. frozen self-test ----------------------------------------------------------
step 'self-test (frozen)'
# Both run with their output on files here, so nothing is written into the
# bundle or the person's data folder (a windowed exe with no stdout would
# open launcher.log).
for exe in "${exes[@]}"; do
  name=$(basename "$exe")
  st="$out/selftest-$name.json"
  "$exe" --selftest --json "$st" > "$out/selftest-$name.out" 2> "$out/selftest-$name.err" \
    || { sed 's/^/  /' "$out/selftest-$name.err"; fail "$name --selftest failed (report: $st)"; }
  "$py" - "$st" "$name" <<'PY'
import json, sys
r = json.load(open(sys.argv[1], encoding='utf-8'))
for c in r['checks']:
    print('    %-15s %s' % (c['name'], 'ok' if c['ok'] else 'FAILED: %s' % c['detail']))
print('  %s: ok (%s)' % (sys.argv[2], sys.argv[1]))
PY
done

# -- 5. audit and zip ---------------------------------------------------------------
step 'audit and zip'
bundled=$(find "$dist" -type f -name "$lib_name" | head -1)
[[ -n $bundled ]] || fail "no $lib_name inside $dist"
bundled_sha=$(sha256 "$bundled")
# On macOS PyInstaller re-signs the dylib when it seals the bundle, so its
# bytes differ from the source's; the audit pins what is there now, and the
# self-test above has just proved it is the patched build.
"$py" "$root/packaging/bundle_guard.py" "$dist" --layout "$os" --devices "$root/devices" \
    --unicorn-sha256 "$bundled_sha" --require-pyz --zip "$zip" --arcroot "$arcroot" | sed 's/^/  /'
[[ ${PIPESTATUS[0]} -eq 0 ]] || fail 'bundle audit'
{
  echo "unicorn_source_sha256=$uc_sha"
  echo "unicorn_bundled_sha256=$bundled_sha"
  echo "python=$("$py" -c 'import sys; print(sys.version.split()[0])')"
  echo "platform=$os-$arch"
} > "$out/build-info.txt"
echo
echo "built $zip"
echo "  $(du -k "$zip" | cut -f1) KB zipped"
echo "  sha256 $(sha256 "$zip")"

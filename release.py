"""Build ThermalWordProcessor.exe, and publish new versions as GitHub Releases.

  python release.py build
      Build dist\\ThermalWordProcessor.exe (and its .sha256 fingerprint) at the current version.

  python release.py publish 1.0.1 "What changed, in a sentence or two"
      Bump the version, build, then, after you type "y", commit, tag, push to GitHub and
      create the release. Copies of the app already out there offer the update the next
      time they start.

Needs: PyInstaller (pip install pyinstaller), git, and the GitHub CLI (gh) signed in.
"""

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "thermal_word_processor.py"
BUILD = HERE / "build"
DIST = HERE / "dist"
EXE = DIST / "ThermalWordProcessor.exe"
SHA = DIST / "ThermalWordProcessor.exe.sha256"
ICON = BUILD / "icon.ico"
VERSION_LINE = re.compile(r'^APP_VERSION = "([0-9.]+)"', re.M)


def run(*cmd, **kw):
    print("  >", " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], cwd=HERE, check=True, **kw)


def current_version():
    return VERSION_LINE.search(APP.read_text(encoding="utf-8")).group(1)


def set_version(version):
    source = APP.read_text(encoding="utf-8")
    APP.write_text(VERSION_LINE.sub(f'APP_VERSION = "{version}"', source, count=1), encoding="utf-8")


def as_tuple(version):
    return tuple(int(p) for p in version.split("."))


def make_icon():
    """A little receipt with a torn edge, drawn with Pillow."""
    from PIL import Image, ImageDraw
    BUILD.mkdir(exist_ok=True)
    s = 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    left, right, top, bottom = 52, 204, 18, 214
    teeth = [(left, bottom)]
    for i in range(8):
        x = left + (right - left) * (i + 0.5) / 8
        teeth += [(x, bottom + 20), (left + (right - left) * (i + 1) / 8, bottom)]
    d.polygon([(left, top), (right, top), (right, bottom)] + teeth[::-1], fill=(255, 255, 255, 255),
              outline=(40, 40, 40, 255), width=8)
    d.rectangle([left + 26, top + 30, right - 26, top + 52], fill=(30, 30, 30, 255))      # title bar
    for i, w in enumerate((1.0, 0.8, 0.95, 0.6, 0.85)):
        y = top + 78 + i * 24
        d.rectangle([left + 26, y, left + 26 + (right - left - 52) * w, y + 9], fill=(110, 110, 110, 255))
    img.save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def build():
    print(f"Building version {current_version()}...")
    make_icon()
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", EXE.stem, "--icon", ICON, "--distpath", DIST, "--workpath", BUILD / "pyinstaller",
        "--specpath", BUILD, APP)
    digest = hashlib.sha256(EXE.read_bytes()).hexdigest()
    SHA.write_text(f"{digest}  {EXE.name}\n", encoding="ascii")
    print(f"\nBuilt {EXE}  ({EXE.stat().st_size / 1e6:.1f} MB)\nSHA-256 {digest}")


def publish(version, notes, assume_yes):
    old = current_version()
    if as_tuple(version) <= as_tuple(old):
        sys.exit(f"Version {version} isn't newer than the current {old}.")
    run("gh", "auth", "status")
    if subprocess.run(["git", "status", "--porcelain"], cwd=HERE, capture_output=True, text=True).stdout.strip():
        print("\nNote: there are uncommitted changes. They'll be included in this release's commit.\n")
    set_version(version)
    try:
        build()
    except Exception:
        set_version(old)
        raise
    answer = "y" if assume_yes else input(f"\nPublish version {version} to GitHub now? Everyone with the app "
                                          f"will be offered it. [y/N] ").strip().lower()
    if answer != "y":
        print(f"Not published. The version in {APP.name} is now {version}; the .exe is in {DIST}.")
        return
    run("git", "add", "-A")
    run("git", "commit", "-m", f"Release v{version}\n\n{notes}")
    run("git", "tag", f"v{version}")
    run("git", "push", "origin", "HEAD", "--tags")
    run("gh", "release", "create", f"v{version}", EXE, SHA,
        "--title", f"Thermal Word Processor {version}", "--notes", notes)
    print(f"\nPublished version {version}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="build the .exe at the current version")
    p = sub.add_parser("publish", help="bump the version, build and publish a release")
    p.add_argument("version", help='new version number, e.g. "1.0.1"')
    p.add_argument("notes", help="what changed, shown to people when they're offered the update")
    p.add_argument("--yes", action="store_true", help="don't ask before publishing")
    args = parser.parse_args()
    if args.command == "build":
        build()
    else:
        publish(args.version, args.notes, args.yes)

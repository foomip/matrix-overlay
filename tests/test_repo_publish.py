"""M6: the apt repository that makes the store see us.

A .deb is not enough. libappstream only ingests metadata from *trusted*
archives, so the repository has to be signed (InRelease) and has to publish the
AppStream catalogue (DEP-11 YAML + icon tarballs) that the Release file lists.
This test builds a throwaway package and runs the real publishing script over
it, then checks the parts clients actually consume: the signature verifies, the
index points at the pool file, and the catalogue ties the component to the
package (without that link the store can show the app but cannot install it).

The dummy package carries no remote media on purpose, so the test needs no
network: compose takes the icons from the installed hicolor set.
"""
from __future__ import annotations

import shutil
import subprocess
import tarfile
import gzip
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "packaging/publish_repo.sh"
TOOLS = ("gpg", "apt-ftparchive", "appstreamcli", "dpkg-deb", "tar")
ARCH = subprocess.run(["dpkg", "--print-architecture"], capture_output=True,
                      text=True).stdout.strip() or "amd64"

pytestmark = pytest.mark.skipif(
    any(shutil.which(t) is None for t in TOOLS),
    reason="repo tooling absent: " + " ".join(t for t in TOOLS if not shutil.which(t)))

PKG = "mo-test"
CID = "local.test.matrixoverlay"


def build_dummy_deb(tmp: Path) -> Path:
    src = tmp / "src"
    (src / "DEBIAN").mkdir(parents=True)
    (src / "usr/bin").mkdir(parents=True)
    (src / "usr/share/metainfo").mkdir(parents=True)
    (src / "usr/share/applications").mkdir(parents=True)
    (src / "usr/share/icons/hicolor/scalable/apps").mkdir(parents=True)
    for size in ("64x64", "128x128"):
        (src / f"usr/share/icons/hicolor/{size}/apps").mkdir(parents=True)
        shutil.copy2(REPO / f"assets/hicolor/{size}/apps/tech.i2r.matrixoverlay.png",
                     src / f"usr/share/icons/hicolor/{size}/apps/{CID}.png")
    shutil.copy2(REPO / "assets/tech.i2r.matrixoverlay.svg",
                 src / f"usr/share/icons/hicolor/scalable/apps/{CID}.svg")
    (src / "usr/bin/mo-thing").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (src / "usr/bin/mo-thing").chmod(0o755)
    (src / "DEBIAN/control").write_text(
        f"Package: {PKG}\nVersion: 0.9.0\nArchitecture: {ARCH}\n"
        "Maintainer: Test <test@example.invalid>\n"
        "Priority: optional\nSection: graphics\n"
        "Description: throwaway package for the repository test\n"
        " Used only to exercise publish_repo.sh, so nothing here is real.\n",
        encoding="utf-8")
    (src / f"usr/share/applications/{CID}.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Repo test\nExec=mo-thing\n"
        f"Icon={CID}\nCategories=Utility;\n", encoding="utf-8")
    (src / f"usr/share/metainfo/{CID}.metainfo.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<component type="desktop-application">\n'
        f"  <id>{CID}</id>\n  <metadata_license>CC0-1.0</metadata_license>\n"
        "  <project_license>MIT</project_license>\n  <name>Repo test</name>\n"
        "  <summary>Throwaway component used to test the apt repository build</summary>\n"
        "  <description><p>Throwaway component used to test that publish_repo.sh signs a "
        "repository and publishes a catalogue that ties this component to its package.</p>"
        "</description>\n  <categories><category>Utility</category></categories>\n"
        f"  <launchable type=\"desktop-id\">{CID}.desktop</launchable>\n"
        "  <url type=\"homepage\">https://github.com/foomip/matrix-overlay</url>\n"
        "  <content_rating type=\"oars-1.1\"/>\n"
        "  <releases><release version=\"0.9.0\" date=\"2026-11-09\"/></releases>\n"
        "</component>\n", encoding="utf-8")
    deb = tmp / f"{PKG}_0.9.0_{ARCH}.deb"
    proc = subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(src),
                           str(deb)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return deb


def publish(tmp: Path, deb: Path) -> Path:
    out = tmp / "apt"
    proc = subprocess.run(["sh", str(SCRIPT), "--deb", str(deb), "--out", str(out),
                           "--origin", PKG, "--suite", "stable"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return out


def test_the_repository_is_signed_and_indexed(tmp_path):
    repo = publish(tmp_path, build_dummy_deb(tmp_path))
    dists = repo / "dists/stable"
    assert (dists / "Release").exists() and (dists / "InRelease").exists()

    # apt-ftparchive must index the metadata next to the package index, or
    # libappstream never learns the catalogue exists.
    release = (dists / "Release").read_text(encoding="utf-8")
    assert f"main/dep11/Components-{ARCH}.yml.gz" in release, release
    assert f"main/binary-{ARCH}/Packages" in release
    assert "Origin: mo-test" in release or "Label: mo-test" in release
    assert "NotAutomatic" not in release          # would force -t on every client

    # the signature must verify against the exported key: apt refuses unsigned or
    # untrusted metadata, and libappstream ignores metadata from untrusted sources
    keys = tmp_path / "gk"
    keys.mkdir()
    imp = subprocess.run(["gpg", "--homedir", str(keys), "--batch", "--import",
                          str(repo / "keyring/matrix-overlay.asc")],
                         capture_output=True, text=True)
    assert imp.returncode == 0, imp.stderr
    ver = subprocess.run(["gpg", "--homedir", str(keys), "--batch", "--status-fd", "1",
                          "--verify", str(dists / "InRelease")],
                         capture_output=True, text=True)
    assert "VALIDSIG" in ver.stdout, ver.stdout + ver.stderr

    packages = (dists / f"main/binary-{ARCH}/Packages").read_text(encoding="utf-8")
    assert f"Package: {PKG}" in packages
    assert f"Filename: pool/main/{PKG[0]}/{PKG}/{PKG}_0.9.0_{ARCH}.deb" in packages
    assert (repo / "pool/main" / PKG[0] / PKG / f"{PKG}_0.9.0_{ARCH}.deb").exists()


def test_the_catalogue_links_the_component_to_its_package(tmp_path):
    repo = publish(tmp_path, build_dummy_deb(tmp_path))
    gz = repo / f"dists/stable/main/dep11/Components-{ARCH}.yml.gz"
    assert gz.exists()
    catalog = gzip.decompress(gz.read_bytes()).decode("utf-8")
    assert catalog.startswith("---\nFile: DEP-11"), catalog[:80]
    # appstream attributes every entry to a catalogue by this key; without it it
    # warns "No origin found for file ..." and the entries are not usable.
    assert "Origin: mo-test-stable-main" in catalog, catalog[:200]
    assert f"ID: {CID}" in catalog
    assert f"Package: {PKG}" in catalog, "component must be installable, not just visible"
    for line in catalog.splitlines():
        if line.startswith("ID: "):
            assert line[4:].strip() == CID
            break
    icon = repo / "dists/stable/main/dep11/icons-64x64.tar"
    assert icon.exists()
    with tarfile.open(icon) as tar:
        names = tar.getnames()
    assert f"./{PKG}_{CID}.png" in names, names


def test_publishing_twice_is_idempotent_and_keeps_the_signing_key(tmp_path):
    """Rotating the signing key silently would break every client that already
    trusts it, so a re-run must reuse the key and leave a valid repository."""
    deb = build_dummy_deb(tmp_path)
    first = publish(tmp_path, deb)
    key = (first / "keyid").read_text().strip()
    second = publish(tmp_path, deb)
    assert (second / "keyid").read_text().strip() == key, "signing key changed"
    assert (second / "dists/stable/InRelease").exists()
    assert (second / "dists/stable/main/dep11" / f"Components-{ARCH}.yml.gz").exists()

"""The repository-configuration package (matrix-overlay-repo).

COSMIC Store only lists an app whose metadata comes from a repository the machine
trusts, so "add our repository" is the step that decides whether anyone else can
install us. It has to be a single file a person can double-click, which means the
signing *public* key and the source entry are shipped inside a .deb.

That inverts a risk: a package that carries a key can accidentally carry the wrong
one, or the wrong half of it. These tests are the guards - the shipped key must be
public only, must be the key that actually signs the repository, and the source
entry must point at the file this package installs.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "packaging/repo-config/build.sh"
ASSET = REPO / "packaging/repo-config/matrix-overlay.gpg"
KEYS = ("gpg", "dpkg-deb")
# Updated deliberately, together with the ceremony in
# docs/packaging-and-store.md: rotate the key, re-export the public half into the
# asset above, publish the new repository, then change this constant. A silent
# mismatch would leave clients pinning a key that cannot sign our metadata.
EXPECTED_FPR = "1552C6EFF1B7B8A7B79C405937ED4872956B954C"

pytestmark = pytest.mark.skipif(
    any(shutil.which(k) is None for k in KEYS) or not BUILD.exists(),
    reason="gpg/dpkg-deb or the repo-config build script is absent")


@pytest.fixture(scope="module")
def deb(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("rc-artifacts")
    proc = subprocess.run(["sh", str(BUILD)], capture_output=True, text=True,
                          env={"OUTDIR": str(out), "PATH": "/usr/bin:/bin:/usr/local/bin",
                               "HOME": str(tmp_path_factory.getbasetemp().parent),
                               "LANG": "C.UTF-8"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    built = sorted(out.glob("matrix-overlay-repo_*.deb"))
    assert built, f"build.sh produced no .deb in {out}: {proc.stdout}"
    return built[-1]


@pytest.fixture(scope="module")
def control(deb: deb) -> dict[str, str]:
    proc = subprocess.run(["dpkg-deb", "-f", str(deb)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    fields, key = {}, None
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        if line[0].isspace():
            fields[key] += "\n" + line
        else:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


@pytest.fixture(scope="module")
def root(deb: deb, tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("rc-root")
    subprocess.run(["dpkg-deb", "-x", str(deb), str(root)], check=True)
    return root


def test_it_is_a_small_metadata_only_package(deb, control):
    assert control["Package"] == "matrix-overlay-repo"
    assert control["Architecture"] == "all"
    assert deb.stat().st_size < 20_000, "a repository-config package should stay tiny"
    listing = subprocess.run(["dpkg-deb", "-c", str(deb)], capture_output=True,
                             text=True).stdout.split()
    programs = [p for p in listing if p.startswith("./usr/bin") or p.startswith("./usr/lib")]
    assert programs == [], f"no code belongs in this package, found {programs}"


def test_it_ships_the_public_key_and_never_the_secret_one(root):
    key = root / "etc/apt/keyrings/matrix-overlay.gpg"
    assert key.is_file()
    colons = subprocess.run(["gpg", "--show-keys", "--with-colons", str(key)],
                            capture_output=True, text=True).stdout
    kinds = {line.split(":")[0] for line in colons.splitlines()}
    assert "pub" in kinds, colons
    assert "sec" not in kinds, "a SECRET key would be published to every client!"


def test_the_shipped_key_is_the_one_that_signs_the_repository(root):
    colons = subprocess.run(["gpg", "--show-keys", "--with-colons",
                             str(root / "etc/apt/keyrings/matrix-overlay.gpg")],
                            capture_output=True, text=True).stdout
    fprs = [line.split(":")[9] for line in colons.splitlines() if line.startswith("fpr:")]
    assert fprs == [EXPECTED_FPR], (
        f"the key in the package is {'/'.join(fprs)}; expected {EXPECTED_FPR}. "
        "If the signing key was rotated, follow the rotation checklist in "
        "docs/packaging-and-store.md and update EXPECTED_FPR in this test.")
    # and the repository we ship points at it, not at apt's trusted dir
    sources = (root / "etc/apt/sources.list.d/matrix-overlay.sources").read_text()
    assert "Signed-By: /etc/apt/keyrings/matrix-overlay.gpg" in sources, sources
    assert "Trusted:" not in sources, "the key must stay scoped to this one source"


def test_the_asset_and_the_package_agree(root):
    shipped = (root / "etc/apt/keyrings/matrix-overlay.gpg").read_bytes()
    assert shipped == ASSET.read_bytes(), (
        "packaging/repo-config/matrix-overlay.gpg is the source of truth; the "
        "built package must carry those exact bytes")


def test_the_source_entry_matches_the_published_repository(root):
    text = (root / "etc/apt/sources.list.d/matrix-overlay.sources").read_text()
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    assert fields["Types"] == "deb"
    assert fields["URIs"].rstrip("/").endswith("/matrix-overlay")
    assert fields["Suites"] == "stable" and fields["Components"] == "main"
    # Naming the architecture keeps apt from probing binary-<arch> we do not
    # publish, which otherwise prints a 404 at every single update.
    assert fields["Architectures"] == "amd64"


def test_the_config_files_survive_upgrade_and_purge(deb, tmp_path):
    stage = tmp_path / "control"
    proc = subprocess.run(["dpkg-deb", "-e", str(deb), str(stage)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    listed = (stage / "conffiles").read_text().split()
    assert listed == ["/etc/apt/keyrings/matrix-overlay.gpg",
                      "/etc/apt/sources.list.d/matrix-overlay.sources"]


def test_installing_it_cannot_run_apt_or_a_daemon(deb, tmp_path):
    stage = tmp_path / "scripts"
    stage.mkdir()
    subprocess.run(["dpkg-deb", "-e", str(deb), str(stage)], check=True)
    post = (stage / "postinst").read_text()
    mode = (stage / "postinst").stat().st_mode & 0o777
    assert mode >= 0o555, "dpkg requires executable maintainer scripts"
    assert "apt-get install" not in post and "apt-get update" not in post
    assert "systemctl" not in post
    assert post.rstrip().endswith("exit 0")


def test_the_description_satisfies_debian_formatting(control):
    desc = control["Description"]
    first, _, rest = desc.partition("\n")
    assert len(first) <= 60, f"summary line too long: {first!r}"
    assert "\t" not in desc, "tabs are not allowed in package descriptions"
    for line in rest.splitlines():
        assert len(line) <= 80, f"description line too long: {line!r}"

"""M5: desktop-integration assets, the FHS placement, and version consistency.

The store listing is only as good as these files: appstream-compose merges the
metainfo with the .desktop file *by basename*, so one string has to appear in the
component id, the .desktop name, the icon basenames and the install paths. These
tests fail loudly when the two version numbers drift apart, which is the failure
mode that ships a 0.1.0 package describing itself as 0.0.x.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from matrix_overlay import __version__

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "assets"
ID = "tech.i2r.matrixoverlay"
CHANGELOG = REPO / "debian/changelog"


def read(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def desktop_fields() -> dict[str, str]:
    fields = {}
    for line in read(f"{ID}.desktop").splitlines():
        if not line.strip() or line.startswith("[") or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
    return fields


def metainfo_root() -> ET.Element:
    return ET.fromstring(read(f"{ID}.metainfo.xml"))


# ---------------------------------------------------------------- identities ---


def test_every_artefact_carries_the_same_component_id():
    """One string, five places. A mismatch silently drops the app from the store:
    appstream-compose merges metainfo and .desktop by basename."""
    assert (ASSETS / f"{ID}.desktop").exists()
    assert (ASSETS / f"{ID}.metainfo.xml").exists()
    assert (ASSETS / f"{ID}.svg").exists()
    pngs = sorted(p.parent.parent.name for p in ASSETS.glob(f"hicolor/*/apps/{ID}.png"))
    assert pngs == ["128x128", "16x16", "24x24", "32x32", "48x48", "64x64"], pngs
    assert metainfo_root().findtext("id") == ID
    assert desktop_fields()["X-AppStream-ID"] == ID
    assert desktop_fields()["Icon"] == ID


def test_metainfo_launchable_matches_the_desktop_file_basename():
    launchable = metainfo_root().find("launchable")
    assert launchable is not None
    assert launchable.get("type") == "desktop-id"
    assert launchable.text.strip() == f"{ID}.desktop"


# ------------------------------------------------------------------ content ---


def test_metainfo_has_the_fields_a_store_listing_needs():
    root = metainfo_root()
    for tag in ("name", "summary", "project_license", "metadata_license",
                "description", "url", "releases", "content_rating"):
        assert root.find(tag) is not None, f"metainfo missing <{tag}>"
    assert root.findtext("project_license").strip() == "MIT"
    assert root.findtext("metadata_license").strip() == "CC0-1.0"
    assert root.find("description").find("p") is not None


def test_summary_fits_the_appstream_limit():
    """appstreamcli warns above 80 characters and stores truncate it anyway."""
    summary = metainfo_root().findtext("summary").strip()
    assert 0 < len(summary) <= 80, summary


def test_no_placeholder_text_anywhere_in_the_assets():
    for path in sorted(ASSETS.glob(f"{ID}.*")):
        text = path.read_text(encoding="utf-8")
        for bad in ("TODO", "TBD", "FIXME", "lorem"):
            assert bad not in text.lower(), f"{path.name}: {bad}"


# raw.githubusercontent, not github.com/.../raw: the latter answers 302 and
# appstream-compose reports file-read-error for the media, which drops the
# component from the store catalogue entirely.
ASSET_URL = "https://raw.githubusercontent.com/foomip/matrix-overlay/main/assets/"


def test_metainfo_icons_are_remote_only():
    """A distro metainfo may carry only `stock` or `remote` icons: the cached
    hicolor ones are extracted by appstream-compose from the icons the package
    installs, and a local path in metainfo is a hard error (measured with
    appstreamcli 1.0.2 on both boxes). What is remote must still resolve to a
    file that is actually in the repo, or the store shows a missing icon."""
    icons = metainfo_root().findall("icon")
    assert icons, "no <icon> in metainfo"
    for icon in icons:
        assert icon.get("type") == "remote", ET.tostring(icon).decode()
        url = icon.text.strip()
        assert url.startswith(ASSET_URL), url
        rel = url[len(ASSET_URL):]
        assert (ASSETS / rel).exists(), url
        if rel.endswith(".png"):
            want = rel.split("hicolor/")[1].split("/")[0].split("x")[0]
            assert (icon.get("width"), icon.get("height")) == (want, want), url


def test_the_icon_theme_set_the_store_needs_is_installed():
    """appstream-compose takes the store's icons from the hicolor tree, so the
    sizes have to exist on disk and the scalable one must be a real SVG."""
    sizes = {p.parent.parent.name for p in ASSETS.glob(f"hicolor/*/apps/{ID}.png")}
    assert {"16x16", "24x24", "32x32", "48x48", "64x64", "128x128"} <= sizes, sizes
    svg = (ASSETS / f"{ID}.svg").read_text(encoding="utf-8")
    assert svg.startswith("<?xml") and "<svg" in svg


def test_screenshot_references_a_committed_asset():
    shot = metainfo_root().find("screenshots/screenshot/image")
    assert shot is not None, "metainfo has no screenshot"
    url = shot.text.strip()
    assert url.startswith(ASSET_URL), url
    assert (ASSETS / url[len(ASSET_URL):]).exists(), url


def test_assets_stay_small_enough_to_review_in_git():
    """A 2 MB photo sneaks in the same way every time: assets here are code, not
    artwork, so keep them reviewable and LFS-free."""
    assert (ASSETS / f"{ID}.svg").stat().st_size < 8 * 1024
    for png in ASSETS.glob(f"hicolor/*/apps/{ID}.png"):
        assert png.stat().st_size < 32 * 1024, png
    assert (ASSETS / "screenshot.png").stat().st_size < 400 * 1024


# ---------------------------------------------------------------- desktop ---


def test_desktop_entry_launches_the_shim_by_its_bare_name():
    fields = desktop_fields()
    assert fields["Type"] == "Application"
    assert fields["Exec"] == "matrix-rain"
    assert fields["TryExec"] == "matrix-rain"
    assert fields["Name"]
    assert "Utility" in fields["Categories"]
    assert fields["StartupNotify"] == "false"      # no window to wait for


def test_desktop_file_has_no_comment_lines():
    """The key-file spec has no comments; GLib tolerates them but
    desktop-file-validate is allowed to complain, and a package build should not
    depend on a parser's tolerance."""
    for line in read(f"{ID}.desktop").splitlines():
        assert not line.startswith("#"), line


# ------------------------------------------------------------------ versions ---


@pytest.mark.skipif(not CHANGELOG.exists(), reason="debian/ not written yet")
def test_changelog_version_matches_the_package_version():
    head = CHANGELOG.read_text(encoding="utf-8").splitlines()[0]
    found = subprocess.run(["dpkg-parsechangelog", "-S", "version"], cwd=str(REPO),
                           capture_output=True, text=True)
    if found.returncode != 0:
        pytest.skip("dpkg-parsechangelog not installed")
    full = found.stdout.strip()
    # The archive version carries a Debian revision (0.1.1-1); the metainfo, the
    # app and the tag speak the upstream version (0.1.1). Compare those two, and
    # assert the shape so a stray hyphen in an upstream version cannot pass.
    upstream, _, revision = full.rpartition("-")
    if not revision:
        upstream, revision = full, ""
    assert upstream == __version__, (full, __version__)
    assert revision == "" or revision[0].isdigit(), (
        f"debian revision must start with a digit: {full}")
    assert head.startswith(f"matrix-overlay ({full})")


# ------------------------------------------------------- archive readiness --- --


@pytest.mark.skipif(not CHANGELOG.exists(), reason="debian/ not written yet")
def test_the_source_format_is_a_real_dpkg_source_format():
    """'3.0 (native)' is not a format dpkg-source knows - native is '1.0 (native)'.

    A binary-only build tolerated the typo; a source build (what the archive
    does) would not. The archive wants 3.0 (quilt) for a package whose tarball
    comes from an upstream tag.
    """
    fmt = (REPO / "debian/source/format").read_text(encoding="utf-8").strip()
    assert fmt in {"3.0 (quilt)", "1.0 (native)"}, fmt
    assert fmt == "3.0 (quilt)"


@pytest.mark.skipif(not CHANGELOG.exists(), reason="debian/ not written yet")
def test_the_changelog_names_the_current_control_stanza():
    """dh_gypm/dpkg reject an entry whose section or urgency is missing."""
    head = CHANGELOG.read_text(encoding="utf-8").splitlines()[0]
    assert "; urgency=" in head, head


@pytest.mark.skipif(not CHANGELOG.exists(), reason="debian/ not written yet")
def test_control_declares_a_supported_standards_version_and_no_priority():
    control = (REPO / "debian/control").read_text(encoding="utf-8")
    match = re.search(r"^Standards-Version:\s*([0-9.]+)\s*$", control, re.M)
    assert match, "debian/control has no Standards-Version"
    parts = tuple(int(x) for x in match.group(1).split("."))
    assert parts >= (4, 7, 4), f"Standards-Version {match.group(1)} is behind"
    source_stanza = control.split("\n\n", 1)[0]
    # Exactly one Priority, in the source stanza: Policy 4.7.3 stopped requiring
    # it, but dpkg-genchanges on Ubuntu 24.04 still warns (and writes
    # "Priority: -") when a binary stanza has none and the source has none
    # either. Declaring it once keeps both happy.
    assert re.search(r"^Priority: optional$", source_stanza, re.M), (
        "declare Priority once, in the source stanza, and inherit it")
    binary_stanza = control.split("\n\n", 1)[1]
    assert not re.search(r"^Priority:", binary_stanza, re.M), (
        "the binary stanza must inherit, not restate")


@pytest.mark.skipif(not CHANGELOG.exists(), reason="debian/ not written yet")
def test_upstream_metadata_and_watch_exist_and_agree_with_the_repo():
    """The sponsor needs to know where upstream is and how to refresh it."""
    meta = (REPO / "debian/upstream/metadata").read_text(encoding="utf-8")
    for field in ("Repository:", "Repository-Browse:", "Bug-Database:", "Bug-Submit:"):
        assert re.search(rf"^{field} https?://", meta, re.M), f"{field} missing"
    watch = (REPO / "debian/watch").read_text(encoding="utf-8")
    first = next(line for line in watch.splitlines()
                 if line.strip() and not line.lstrip().startswith("#"))
    assert first.strip() == "version=4", first
    assert "github.com/foomip/matrix-overlay" in watch
    assert re.search(r"tags .*refs/tags/", watch), "watch must match tag tarballs"


@pytest.mark.skipif(not CHANGELOG.exists(), reason="debian/ not written yet")
def test_the_autopkgtest_needs_no_display_and_no_network():
    """A CI test that opens a window or fetches the internet is not a test.

    The suite stays headless on purpose; the CI entry point exercises the
    installed package only, so it cannot depend on a compositor being present.
    """
    control = (REPO / "debian/tests/control").read_text(encoding="utf-8")
    assert "Test-Command:" in control, "no autopkgtest entry point"
    for forbidden in ("--self-test", "--start", "--monitor-list", "curl ", "wget "):
        assert forbidden not in control, f"autopkgtest must not use {forbidden}"
    assert "--preflight" in control, "the installed command should be preflighted"


def test_metainfo_latest_release_matches_the_package_version():
    releases = metainfo_root().findall("releases/release")
    assert releases, "metainfo declares no releases"
    assert releases[0].get("version") == __version__


# ------------------------------------------------------------- FHS placement ---


def test_the_system_profile_installs_the_desktop_integration(tmp_path):
    root = tmp_path / "root"
    proc = subprocess.run(["sh", str(REPO / "install.sh"), "--mode", "system",
                           "--destdir", str(root)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    share = root / "usr/share"
    assert (share / f"metainfo/{ID}.metainfo.xml").exists()
    assert (share / f"applications/{ID}.desktop").exists()
    assert (share / f"icons/hicolor/scalable/apps/{ID}.svg").exists()
    assert (share / "icons/hicolor/64x64/apps" / f"{ID}.png").exists()
    # the build stamp travels with the artifact: in a read-only /usr install it
    # is what stops every launch from recompiling.
    assert (root / "usr/lib/matrix-overlay/native/libmwl.so").exists()
    # the stamp travels when the build produced one (debian/rules writes it);
    # an unstamped packaged artifact is still trusted by mwl.is_stale
    if (REPO / "native/libmwl.so.stamp").exists():
        assert (root / "usr/lib/matrix-overlay/native/libmwl.so.stamp").exists()


APPSTREAMCLI = shutil.which("appstreamcli")


@pytest.mark.skipif(not APPSTREAMCLI, reason="appstreamcli not installed")
def test_appstreamcli_accepts_the_installed_layout(tmp_path):
    """The gate that matters for COSMIC Store: the validator run against the tree
    the package actually ships, not against the repo layout."""
    root = tmp_path / "root"
    proc = subprocess.run(["sh", str(REPO / "install.sh"), "--mode", "system",
                           "--destdir", str(root)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    res = subprocess.run([APPSTREAMCLI, "validate", "--no-net", "--explain",
                          str(root / f"usr/share/metainfo/{ID}.metainfo.xml")],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr

def test_the_svg_icon_is_readable_by_gdk_pixbuf():
    """gdk-pixbuf sniffs SVG with the magic " <svg": an XML comment (or anything
    else) between the prolog and the root element hides it, the file then looks
    like an unknown format, and appstream-compose fails the *component* with
    file-read-error and drops it from the catalogue entirely."""
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except (ImportError, ValueError):  # pragma: no cover - no gir1.2-gdkpixbuf
        pytest.skip("GdkPixbuf typelib not available")
    pb = GdkPixbuf.Pixbuf.new_from_file(str(ASSETS / f"{ID}.svg"))
    assert pb.get_width() >= 128 and pb.get_height() >= 128


def test_svg_has_no_content_before_the_root_element():
    """The same trap, caught without a typelib: nothing but the prolog and
    whitespace may precede '<svg'."""
    text = (ASSETS / f"{ID}.svg").read_text(encoding="utf-8")
    head = text.split("<svg", 1)[0]
    stripped = head.replace("<?xml", "").split("?>", 1)[-1].strip()
    assert stripped == "", f"content before <svg>: {stripped[:40]!r}"


COMPOSE = shutil.which("appstreamcli")


@pytest.mark.skipif(not COMPOSE, reason="appstreamcli not installed")
def test_appstream_compose_accepts_the_package_and_lists_the_component(tmp_path):
    """The gate that decides whether COSMIC Store ever sees us: compose reads the
    installed tree (metainfo + .desktop + hicolor icons) and must produce a
    catalogue naming our component. It fails the whole component for one invalid
    category, an unreadable SVG or a media URL that answers a redirect."""
    root = tmp_path / "root"
    inst = subprocess.run(["sh", str(REPO / "install.sh"), "--mode", "system",
                           "--destdir", str(root)], capture_output=True, text=True)
    assert inst.returncode == 0, inst.stderr
    out = tmp_path / "meta"
    proc = subprocess.run([COMPOSE, "compose", "--prefix=/usr",
                           f"--result-root={out}", f"--data-dir={out}",
                           "--origin=matrix-overlay",
                           f"--components={ID}", str(root)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    catalog = out / "matrix-overlay.xml.gz"
    assert catalog.exists(), sorted(p.name for p in out.iterdir())
    body = subprocess.run(["gunzip", "-c", str(catalog)], capture_output=True, text=True)
    assert f"<id>{ID}</id>" in body.stdout
    # The package link is *not* added here (compose cannot know the owning
    # package from a bare tree); publish_repo.sh adds it, and
    # tests/test_repo_publish.py checks that. A component without it is a store
    # entry that cannot be installed.

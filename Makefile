# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
# Single build entry point for every profile: native build, tests and the two
# install modes. Packaging (M5, debian/rules) calls these same targets, so there
# is exactly one way to compile the GL client.

PYTHON ?= /usr/bin/python3
# conftest.py aborts unless pytest runs under the system interpreter: the PATH
# shims are asdf pythons without gi.

.PHONY: native test live selftest preflight diagnose install-dotfile \
	uninstall-dotfile install-system uninstall-system repo-config publish orig clean

# The upstream version, read from the one place that defines it. The drift tests
# make sure debian/changelog and the AppStream release agree with it.
UPSTREAM_VERSION = $(shell $(PYTHON) -c "import sys; sys.path.insert(0, '.'); \
	import matrix_overlay; print(matrix_overlay.__version__)")
# Where the orig tarball lands. Default: beside the checkout, which is what a
# normal ~/src/<pkg> layout wants; a gate run stages it elsewhere so a working
# tree never accumulates .dsc/.changes/.buildinfo next to it.
ORIGDIR ?= ..

native:
	sh native/build.sh

test:
	$(PYTHON) -m pytest -q

live:
	MATRIX_OVERLAY_LIVE=1 $(PYTHON) -m pytest -q -s tests/live_smoke.py

preflight:
	PYTHONPATH=$(CURDIR) $(PYTHON) -m matrix_overlay --preflight

diagnose:
	PYTHONPATH=$(CURDIR) $(PYTHON) -m matrix_overlay --diagnose

selftest:
	PYTHONPATH=$(CURDIR) $(PYTHON) -m matrix_overlay --self-test

install-dotfile:
	sh install.sh --mode dotfile

uninstall-dotfile:
	sh install.sh --mode dotfile --uninstall

install-system:
	sh install.sh --mode system $(if $(DESTDIR),--destdir "$(DESTDIR)",)

uninstall-system:
	sh install.sh --mode system $(if $(DESTDIR),--destdir "$(DESTDIR)",) --uninstall

clean:
	rm -f native/libmwl.so native/*.stamp
	rm -f native/*-client-protocol.c native/*-client-protocol.h
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache

# The "add our repository" package: the signing public key plus the source entry,
# so installing from the store is one file for anyone else.
repo-config:
	sh packaging/repo-config/build.sh

# The orig tarball an archive build wants, written next to this directory.
#
# It is a repack, not the GitHub tarball: this repository carries debian/, and a
# debian/ inside an orig under 3.0 (quilt) can silently resurrect packaging files
# the current debian/ no longer has (measured: it builds clean, which is the
# problem). So export the tag and strip debian/. Determinism is on purpose - two
# runs must give the same sha256 or "reproducible" means nothing.
orig:
	@git rev-parse -q --verify "refs/tags/$(UPSTREAM_VERSION)" >/dev/null \
		|| { echo "make orig: no tag named $(UPSTREAM_VERSION); cut the release tag first" >&2; exit 1; }
	@d=$$(mktemp -d); trap 'rm -rf "$$d"' EXIT INT TERM; \
	git archive --format=tar --prefix=matrix-overlay-$(UPSTREAM_VERSION)/ "$(UPSTREAM_VERSION)" \
		| tar -xf - -C "$$d"; \
	rm -rf "$$d/matrix-overlay-$(UPSTREAM_VERSION)/debian"; \
	epoch=$$(git log -1 --format=%ct "refs/tags/$(UPSTREAM_VERSION)"); \
	mkdir -p "$(ORIGDIR)"; \
	tar --sort=name --format=gnu --owner=0 --group=0 --numeric-owner \
		--mtime="@$$epoch" -czf "$(ORIGDIR)/matrix-overlay_$(UPSTREAM_VERSION).orig.tar.gz" \
		-C "$$d" matrix-overlay-$(UPSTREAM_VERSION); \
	echo "orig: $(ORIGDIR)/matrix-overlay_$(UPSTREAM_VERSION).orig.tar.gz (SOURCE_DATE_EPOCH=$$epoch, debian/ stripped)"; \
	sha256sum "$(ORIGDIR)/matrix-overlay_$(UPSTREAM_VERSION).orig.tar.gz"

# Build the application and the repository package, then sign and index them into
# one repository. Prints the push-to-Pages and client instructions afterwards.
publish:
	sh packaging/publish.sh

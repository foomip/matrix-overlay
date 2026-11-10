# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
# Single build entry point for every profile: native build, tests and the two
# install modes. Packaging (M5, debian/rules) calls these same targets, so there
# is exactly one way to compile the GL client.

PYTHON ?= /usr/bin/python3
# conftest.py aborts unless pytest runs under the system interpreter: the PATH
# shims are asdf pythons without gi.

.PHONY: native test live selftest preflight diagnose install-dotfile \
	uninstall-dotfile install-system uninstall-system repo-config publish clean

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

# Build the application and the repository package, then sign and index them into
# one repository. Prints the push-to-Pages and client instructions afterwards.
publish:
	sh packaging/publish.sh

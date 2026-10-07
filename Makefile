# User-level install of the qwayland server (no root needed).
PREFIX  ?= $(HOME)/.local
LIBDIR  := $(PREFIX)/lib/qwayland
BINDIR  := $(PREFIX)/bin
APPDIR  ?= $(HOME)/.local/share/applications
UNITDIR ?= $(HOME)/.config/systemd/user

all: server/vout/qw-vout

server/vout/qw-vout:
	$(MAKE) -C server/vout

apk:
	client/build.sh

install: all
	install -Dm755 server/vout/qw-vout $(LIBDIR)/vout/qw-vout
	install -Dm755 server/qwayland_server.py $(LIBDIR)/qwayland_server.py
	install -d $(BINDIR)
	ln -sf $(LIBDIR)/qwayland_server.py $(BINDIR)/qwayland-server
	# KWin only grants the virtual-output and fake-input protocols to
	# executables whitelisted by a desktop file.
	sed 's|@VOUT@|$(LIBDIR)/vout/qw-vout|' packaging/qwayland-vout.desktop.in > qwayland-vout.desktop
	install -Dm644 qwayland-vout.desktop $(APPDIR)/qwayland-vout.desktop
	rm qwayland-vout.desktop
	sed 's|@BINDIR@|$(BINDIR)|' packaging/qwayland.service.in > qwayland.service
	install -Dm644 qwayland.service $(UNITDIR)/qwayland.service
	rm qwayland.service
	-kbuildsycoca6 >/dev/null 2>&1
	@echo "Installed. Start with: systemctl --user enable --now qwayland"

install-apk: apk
	adb install -r client/build/qwayland.apk

uninstall:
	-systemctl --user disable --now qwayland
	rm -rf $(LIBDIR) $(BINDIR)/qwayland-server $(APPDIR)/qwayland-vout.desktop $(UNITDIR)/qwayland.service

clean:
	$(MAKE) -C server/vout clean
	rm -rf client/build

.PHONY: all apk install install-apk uninstall clean

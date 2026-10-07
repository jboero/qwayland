PLASMA_PROTOCOLS ?= /usr/share/plasma-wayland-protocols
WL_PROTOCOLS     := $(shell pkg-config --variable=pkgdatadir wayland-protocols)
vpath %.xml $(PLASMA_PROTOCOLS) $(WL_PROTOCOLS)/unstable/xdg-output

PROTOCOLS := zkde-screencast-unstable-v1 fake-input xdg-output-unstable-v1
GEN_H := $(PROTOCOLS:%=gen/%-client-protocol.h)
GEN_C := $(PROTOCOLS:%=gen/%-protocol.c)

CFLAGS ?= -O2 -g -Wall -Wextra -Wno-unused-parameter
CFLAGS += -Igen $(shell pkg-config --cflags wayland-client)
LDLIBS += $(shell pkg-config --libs wayland-client)

qw-vout: qw-vout.c $(GEN_H) $(GEN_C)
	$(CC) $(CFLAGS) -o $@ qw-vout.c $(GEN_C) $(LDLIBS)

gen/%-client-protocol.h: %.xml
	@mkdir -p gen
	wayland-scanner client-header $< $@

gen/%-protocol.c: %.xml
	@mkdir -p gen
	wayland-scanner private-code $< $@

clean:
	rm -rf gen qw-vout

.PHONY: clean

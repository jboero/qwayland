// qw-vout: small KWin helper for qwayland.
//
// Creates/destroys virtual outputs (monitors) through KWin's privileged
// zkde_screencast_unstable_v1 protocol, reports the PipeWire node of each and
// its position in the global compositor space, and injects pointer/keyboard
// input through org_kde_kwin_fake_input.
//
// KWin only exposes these protocols to executables whitelisted by a .desktop
// file with X-KDE-Wayland-Interfaces (see qwayland-vout.desktop).
//
// Line protocol on stdin:
//   create <id> <width> <height> <scale>
//   close <id>
//   motion <id> <nx> <ny>        normalized [0,1] position on output <id>
//   button <evdev-code> <0|1>
//   axis <0=vertical|1=horizontal> <value>
//   key <evdev-code> <0|1>
// Events on stdout:
//   ready
//   node <id> <pipewire-node-id>
//   geom <id> <x> <y> <w> <h> <output-name>
//   failed <id> <message>
//   closed <id>
//   error <message>

#define _GNU_SOURCE
#include <errno.h>
#include <stdarg.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <wayland-client.h>

#include "fake-input-client-protocol.h"
#include "xdg-output-unstable-v1-client-protocol.h"
#include "zkde-screencast-unstable-v1-client-protocol.h"

#define MAX_VOUTS 16
#define MAX_OUTPUTS 32
#define POINTER_EMBEDDED 2

struct vout {
    int used;
    int id;
    char name[64];
    struct zkde_screencast_stream_unstable_v1 *stream;
    int have_geom;
    int x, y, w, h;
};

struct output {
    int used;
    uint32_t global;
    struct wl_output *wl;
    struct zxdg_output_v1 *xdg;
    char name[128];
    int x, y, w, h;
};

static struct wl_display *display;
static struct zkde_screencast_unstable_v1 *screencast;
static struct org_kde_kwin_fake_input *fake_input;
static struct zxdg_output_manager_v1 *xdg_output_manager;
static struct vout vouts[MAX_VOUTS];
static struct output outputs[MAX_OUTPUTS];

static void emit(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void emit(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    putchar('\n');
    fflush(stdout);
}

static struct vout *vout_by_id(int id)
{
    for (int i = 0; i < MAX_VOUTS; i++)
        if (vouts[i].used && vouts[i].id == id)
            return &vouts[i];
    return NULL;
}

// Match a compositor output to one of our virtual outputs by name and publish
// its geometry. KWin may decorate the name, so match on substring.
static void sync_geometry(struct output *o)
{
    for (int i = 0; i < MAX_VOUTS; i++) {
        struct vout *v = &vouts[i];
        if (!v->used || !strstr(o->name, v->name))
            continue;
        if (v->have_geom && v->x == o->x && v->y == o->y && v->w == o->w && v->h == o->h)
            continue;
        v->have_geom = 1;
        v->x = o->x;
        v->y = o->y;
        v->w = o->w;
        v->h = o->h;
        emit("geom %d %d %d %d %d %s", v->id, v->x, v->y, v->w, v->h, o->name);
    }
}

static void xdg_logical_position(void *data, struct zxdg_output_v1 *xdg, int32_t x, int32_t y)
{
    struct output *o = data;
    o->x = x;
    o->y = y;
}

static void xdg_logical_size(void *data, struct zxdg_output_v1 *xdg, int32_t w, int32_t h)
{
    struct output *o = data;
    o->w = w;
    o->h = h;
}

static void xdg_done(void *data, struct zxdg_output_v1 *xdg)
{
    // Only sent for xdg_output < v3; newer versions use wl_output.done.
    sync_geometry(data);
}

static void xdg_name(void *data, struct zxdg_output_v1 *xdg, const char *name)
{
    struct output *o = data;
    snprintf(o->name, sizeof(o->name), "%s", name);
}

static void xdg_description(void *data, struct zxdg_output_v1 *xdg, const char *desc)
{
}

static const struct zxdg_output_v1_listener xdg_output_listener = {
    .logical_position = xdg_logical_position,
    .logical_size = xdg_logical_size,
    .done = xdg_done,
    .name = xdg_name,
    .description = xdg_description,
};

static void wl_output_geometry(void *data, struct wl_output *wl, int32_t x, int32_t y, int32_t pw,
                               int32_t ph, int32_t subpixel, const char *make, const char *model,
                               int32_t transform)
{
}

static void wl_output_mode(void *data, struct wl_output *wl, uint32_t flags, int32_t w, int32_t h,
                           int32_t refresh)
{
}

static void wl_output_done(void *data, struct wl_output *wl)
{
    sync_geometry(data);
}

static void wl_output_scale(void *data, struct wl_output *wl, int32_t factor)
{
}

static const struct wl_output_listener wl_output_listener = {
    .geometry = wl_output_geometry,
    .mode = wl_output_mode,
    .done = wl_output_done,
    .scale = wl_output_scale,
};

static void watch_output(struct output *o)
{
    if (o->xdg || !xdg_output_manager)
        return;
    o->xdg = zxdg_output_manager_v1_get_xdg_output(xdg_output_manager, o->wl);
    zxdg_output_v1_add_listener(o->xdg, &xdg_output_listener, o);
}

static void registry_global(void *data, struct wl_registry *reg, uint32_t name, const char *iface,
                            uint32_t version)
{
    if (!strcmp(iface, zkde_screencast_unstable_v1_interface.name)) {
        // v5: newest version that still sends the "created" event with a node id.
        screencast = wl_registry_bind(reg, name, &zkde_screencast_unstable_v1_interface,
                                      version < 5 ? version : 5);
    } else if (!strcmp(iface, org_kde_kwin_fake_input_interface.name)) {
        fake_input = wl_registry_bind(reg, name, &org_kde_kwin_fake_input_interface,
                                      version < 4 ? version : 4);
    } else if (!strcmp(iface, zxdg_output_manager_v1_interface.name)) {
        xdg_output_manager = wl_registry_bind(reg, name, &zxdg_output_manager_v1_interface,
                                              version < 3 ? version : 3);
        for (int i = 0; i < MAX_OUTPUTS; i++)
            if (outputs[i].used)
                watch_output(&outputs[i]);
    } else if (!strcmp(iface, wl_output_interface.name)) {
        for (int i = 0; i < MAX_OUTPUTS; i++) {
            if (outputs[i].used)
                continue;
            struct output *o = &outputs[i];
            memset(o, 0, sizeof(*o));
            o->used = 1;
            o->global = name;
            o->wl = wl_registry_bind(reg, name, &wl_output_interface, version < 3 ? version : 3);
            wl_output_add_listener(o->wl, &wl_output_listener, o);
            watch_output(o);
            break;
        }
    }
}

static void registry_global_remove(void *data, struct wl_registry *reg, uint32_t name)
{
    for (int i = 0; i < MAX_OUTPUTS; i++) {
        struct output *o = &outputs[i];
        if (!o->used || o->global != name)
            continue;
        if (o->xdg)
            zxdg_output_v1_destroy(o->xdg);
        wl_output_destroy(o->wl);
        o->used = 0;
    }
}

static const struct wl_registry_listener registry_listener = {
    .global = registry_global,
    .global_remove = registry_global_remove,
};

static void stream_closed(void *data, struct zkde_screencast_stream_unstable_v1 *s)
{
    struct vout *v = data;
    emit("closed %d", v->id);
    zkde_screencast_stream_unstable_v1_destroy(s);
    v->used = 0;
}

static void stream_created(void *data, struct zkde_screencast_stream_unstable_v1 *s, uint32_t node)
{
    struct vout *v = data;
    emit("node %d %u", v->id, node);
}

static void stream_failed(void *data, struct zkde_screencast_stream_unstable_v1 *s, const char *err)
{
    struct vout *v = data;
    emit("failed %d %s", v->id, err);
}

static const struct zkde_screencast_stream_unstable_v1_listener stream_listener = {
    .closed = stream_closed,
    .created = stream_created,
    .failed = stream_failed,
};

static void cmd_create(int id, int w, int h, double scale)
{
    if (vout_by_id(id)) {
        emit("failed %d already exists", id);
        return;
    }
    struct vout *v = NULL;
    for (int i = 0; i < MAX_VOUTS && !v; i++)
        if (!vouts[i].used)
            v = &vouts[i];
    if (!v) {
        emit("failed %d too many outputs", id);
        return;
    }
    memset(v, 0, sizeof(*v));
    v->used = 1;
    v->id = id;
    snprintf(v->name, sizeof(v->name), "qwayland-%d", id);
    if (zkde_screencast_unstable_v1_get_version(screencast) >= 4) {
        char desc[64];
        snprintf(desc, sizeof(desc), "Quest display %d", id);
        v->stream = zkde_screencast_unstable_v1_stream_virtual_output_with_description(
            screencast, v->name, desc, w, h, wl_fixed_from_double(scale), POINTER_EMBEDDED);
    } else {
        v->stream = zkde_screencast_unstable_v1_stream_virtual_output(
            screencast, v->name, w, h, wl_fixed_from_double(scale), POINTER_EMBEDDED);
    }
    zkde_screencast_stream_unstable_v1_add_listener(v->stream, &stream_listener, v);
    // Outputs that already exist may get renamed/announced later; geometry is
    // reported from the xdg_output done event.
}

static void cmd_close(int id)
{
    struct vout *v = vout_by_id(id);
    if (!v)
        return;
    zkde_screencast_stream_unstable_v1_close(v->stream);
    v->used = 0;
    emit("closed %d", id);
}

static void cmd_motion(int id, double nx, double ny)
{
    struct vout *v = vout_by_id(id);
    if (!v || !v->have_geom || !fake_input)
        return;
    nx = nx < 0 ? 0 : nx > 1 ? 1 : nx;
    ny = ny < 0 ? 0 : ny > 1 ? 1 : ny;
    // Stay strictly inside the output so the cursor never lands on a neighbour.
    double gx = v->x + nx * (v->w - 1);
    double gy = v->y + ny * (v->h - 1);
    org_kde_kwin_fake_input_pointer_motion_absolute(fake_input, wl_fixed_from_double(gx),
                                                    wl_fixed_from_double(gy));
}

static void handle_line(char *line)
{
    int id, a, b;
    double x, y;
    if (sscanf(line, "create %d %d %d %lf", &id, &a, &b, &x) == 4)
        cmd_create(id, a, b, x);
    else if (sscanf(line, "close %d", &id) == 1)
        cmd_close(id);
    else if (sscanf(line, "motion %d %lf %lf", &id, &x, &y) == 3)
        cmd_motion(id, x, y);
    else if (fake_input && sscanf(line, "button %d %d", &a, &b) == 2)
        org_kde_kwin_fake_input_button(fake_input, a, b);
    else if (fake_input && sscanf(line, "axis %d %lf", &a, &x) == 2)
        org_kde_kwin_fake_input_axis(fake_input, a, wl_fixed_from_double(x));
    else if (fake_input && sscanf(line, "key %d %d", &a, &b) == 2)
        org_kde_kwin_fake_input_keyboard_key(fake_input, a, b);
    else
        emit("error bad command: %s", line);
}

int main(void)
{
    display = wl_display_connect(NULL);
    if (!display) {
        emit("error cannot connect to wayland display");
        return 1;
    }
    struct wl_registry *reg = wl_display_get_registry(display);
    wl_registry_add_listener(reg, &registry_listener, NULL);
    wl_display_roundtrip(display);
    wl_display_roundtrip(display);

    if (!screencast) {
        emit("error zkde_screencast_unstable_v1 not available (is qwayland-vout.desktop installed?)");
        return 1;
    }
    if (fake_input) {
        org_kde_kwin_fake_input_authenticate(fake_input, "qwayland",
                                             "Forward input from the Quest headset");
    } else {
        emit("error org_kde_kwin_fake_input not available, input forwarding disabled");
    }
    emit("ready");

    char buf[4096];
    size_t used = 0;
    struct pollfd fds[2] = {
        {.fd = wl_display_get_fd(display), .events = POLLIN},
        {.fd = STDIN_FILENO, .events = POLLIN},
    };
    for (;;) {
        while (wl_display_prepare_read(display) != 0)
            wl_display_dispatch_pending(display);
        wl_display_flush(display);
        if (poll(fds, 2, -1) < 0) {
            wl_display_cancel_read(display);
            if (errno == EINTR)
                continue;
            break;
        }
        if (fds[0].revents & POLLIN) {
            if (wl_display_read_events(display) < 0)
                break;
        } else {
            wl_display_cancel_read(display);
        }
        if (wl_display_dispatch_pending(display) < 0)
            break;
        if (fds[0].revents & (POLLERR | POLLHUP))
            break;

        if (fds[1].revents & (POLLIN | POLLHUP)) {
            ssize_t n = read(STDIN_FILENO, buf + used, sizeof(buf) - 1 - used);
            if (n <= 0)
                break; // parent went away: KWin removes our outputs on disconnect
            used += n;
            char *start = buf, *nl;
            while ((nl = memchr(start, '\n', buf + used - start))) {
                *nl = 0;
                handle_line(start);
                start = nl + 1;
            }
            used = buf + used - start;
            memmove(buf, start, used);
            if (used == sizeof(buf) - 1)
                used = 0;
        }
    }
    wl_display_disconnect(display);
    return 0;
}

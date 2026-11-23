/* Local TSD peer and synthetic DSI services; no hardware transport. */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <inttypes.h>
#include <poll.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

struct __attribute__((packed)) header {
    uint32_t length, event;
    uint64_t sender, destination;
    uint8_t type, reserved[3];
};
struct hello {
    uint32_t version;
    uint8_t bits, reserved[3];
    uint64_t base, nameserver;
};
_Static_assert(sizeof(struct header) == 28, "TSD socket header");
_Static_assert(sizeof(struct hello) == 24, "TSD negotiation payload");

struct registration { char name[256]; uint64_t address; } names[128];
static size_t name_count;
static const uint64_t nameserver = 0x1000001;
static int observe_only;
static int names_only;
static uint64_t hmi_address;
static int hmi_fd = -1;
static uint64_t registration_done;
static const uint64_t dsi_address = 0x1000010;
static const struct { uint32_t event, version; const char *name; } services[] = {
    {33796096, 1504078919, "DSIPersistence"},
    {33816576, (uint32_t)-618193930, "DSIPowerManagement"},
    {33632256, (uint32_t)-618193897, "DSICarTimeUnitsLanguage"},
    {33660928, (uint32_t)-618193901, "DSIGeneralVehicleStates"},
    {33673216, (uint32_t)-618193839, "DSIKeyPanel"},
    {33652736, (uint32_t)-618193872, "DSIDisplayManagement"}
};
#define SERVICE_COUNT (sizeof(services) / sizeof(services[0]))

static int packet(int fd, uint8_t type, uint32_t event, uint64_t destination,
                  const void *data, uint32_t length);
static int dsi_start(int fd, uint64_t address);
static int dsi_request(int fd, const struct header *h, const uint8_t *data);

static int receive(int fd, void *data, size_t size) {
    size_t done = 0;
    while (done < size) {
        ssize_t n = read(fd, (char *)data + done, size - done);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return 0;
        done += (size_t)n;
    }
    return 1;
}

static int send_all(int fd, const void *data, size_t size) {
    size_t done = 0;
    while (done < size) {
        ssize_t n = send(fd, (const char *)data + done, size - done, MSG_NOSIGNAL);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return 0;
        done += (size_t)n;
    }
    return 1;
}

static int packet(int fd, uint8_t type, uint32_t event, uint64_t destination,
                  const void *data, uint32_t length) {
    struct header h = {.length = length, .event = event, .sender = dsi_address,
                       .destination = destination, .type = type};
    printf("{\"action\":\"dsi_send\",\"type\":%u,\"event\":%u,\"length\":%u}\n", type, event, length);
    return send_all(fd, &h, sizeof(h)) && send_all(fd, data, length);
}

static void put_int(uint8_t *data, uint32_t value) {
    data[0] = 13; memcpy(data + 1, &value, 4);
}

static uint32_t u32(const uint8_t *data) { uint32_t v; memcpy(&v, data, 4); return v; }

static int dsi_payload(int fd, uint32_t event, const uint8_t *body, uint32_t size) {
    if (size > 65531) return 0;
    uint8_t data[65536];
    data[0] = 42; memcpy(data + 1, &size, 4); memcpy(data + 5, body, size);
    return packet(fd, 0, event, hmi_address, data, size + 5);
}

static int dsi_ints(int fd, uint32_t event, const uint32_t *values, size_t count) {
    uint8_t data[128];
    assert(count <= 24);
    put_int(data, event);
    for (size_t i = 0; i < count; ++i) put_int(data + 5 + i * 5, values[i]);
    return dsi_payload(fd, event, data, (uint32_t)(5 + count * 5));
}

/* shortcut: volatile storage, 256 keys / 64 KiB per value; extend after startup needs it. */
struct stored_value { uint32_t space, kind, size; uint64_t key; uint8_t *data; } values[256];
static size_t value_count;

static int rpc_value(const uint8_t *data, size_t size, size_t *used, unsigned depth) {
    if (!size || depth > 3) return 0;
    uint8_t tag = data[0];
    size_t n = 1;
    if (tag == 1 || tag == 2 || tag == 38) { /* bool / null */ }
    else if (tag >= 8 && tag <= 15) n = 5;
    else if (tag >= 32 && tag <= 34) n = 9;
    else if (tag == 37) {
        const uint8_t *end = memchr(data + 1, 0, size - 1);
        if (!end) return 0;
        n = (size_t)(end - data) + 1;
    } else if (tag == 39) {
        size_t child;
        if (!rpc_value(data + 1, size - 1, &child, depth + 1)) return 0;
        n += child;
    } else if (tag == 4) {
        if (size < 5) return 0;
        uint32_t count = u32(data + 1); n = 5;
        if (count > size - 5) return 0;
        for (uint32_t i = 0; i < count; ++i) {
            size_t child;
            if (!rpc_value(data + n, size - n, &child, depth + 1)) return 0;
            n += child;
        }
    } else return 0;
    if (n > size) return 0;
    *used = n; return 1;
}

static int persistence(int fd, uint32_t method, const uint8_t *args, uint32_t size) {
    if (method == 0 || method == 3839 || method == 1016)
        return dsi_ints(fd, 0x203b800, (uint32_t[]){1504078919, 1, 1, 1}, 4);
    uint32_t operation = method;
    if (method >= 1019 && method <= 1023) operation = 1001 + 2 * (method - 1019);
    if (operation < 1000 || operation > 1009) {
        printf("{\"action\":\"dsi_unhandled\",\"service\":\"DSIPersistence\",\"method\":%u}\n", method);
        return 1;
    }
    if (size < 14 || args[0] != 13 || args[5] != 34) return 0;
    uint32_t space = u32(args + 1), kind = (operation - 1000) / 2;
    uint64_t key; memcpy(&key, args + 6, 8);
    size_t index = 0;
    while (index < value_count && (values[index].space != space || values[index].key != key)) ++index;
    uint32_t status = 0;
    int writing = !(operation & 1);
    if (writing) {
        size_t used;
        if (!rpc_value(args + 14, size - 14, &used, 0) || used != size - 14) return 0;
        if (index == 256 || size > 65000) status = 3;
        else {
            uint8_t *copy = malloc(size - 14);
            if (!copy) return 0;
            memcpy(copy, args + 14, size - 14);
            free(values[index].data);
            values[index] = (struct stored_value){space, kind, size - 14, key, copy};
            if (index == value_count) ++value_count;
        }
    } else {
        if (size != (method == operation ? 14u : 19u)) return 0;
        if (method != operation && args[14] != 13) return 0;
        if (index == value_count || values[index].kind != kind) status = 2;
    }
    uint8_t response[65531];
    put_int(response, 0x203b800); put_int(response + 5, 1504078919);
    put_int(response + 10, operation + 1000); memcpy(response + 15, args, 14);
    uint32_t length = 29;
    if (!writing) {
        if (!status) {
            memcpy(response + length, values[index].data, values[index].size);
            length += values[index].size;
        } else if (kind == 0) { put_int(response + length, 0); length += 5; }
        else if (kind == 2) {
            memcpy(response + length, (uint8_t[]){39, 37, 0}, 3); length += 3;
        } else {
            memcpy(response + length, (uint8_t[]){39, 4, 0, 0, 0, 0}, 6); length += 6;
        }
    }
    put_int(response + length, status); length += 5;
    printf("{\"action\":\"persistence_reply\",\"method\":%u,\"namespace\":%u,\"key\":%" PRIu64 ",\"status\":%u,\"stored\":%s}\n",
           operation + 1000, space, key, status, writing && !status ? "true" : "false");
    return dsi_payload(fd, 0x203b800, response, length);
}

static int dsi_start(int fd, uint64_t address) {
    hmi_address = address;
    hmi_fd = fd;
    if (!packet(fd, 1, 0, address, NULL, 0)) return 0;
    const uint32_t events[] = {0x1000001, 0x1000005, 0x100000a, 0x20fe000, 0x203b000};
    for (size_t i = 0; i < sizeof(events) / sizeof(events[0]); ++i)
        if (!packet(fd, 6, events[i], address, NULL, 0)) return 0;
    for (size_t i = 1; i < SERVICE_COUNT; ++i)
        if (!packet(fd, 6, services[i].event, address, NULL, 0)) return 0;
    /* The first received event initializes JNI and is intentionally consumed there. */
    return dsi_ints(fd, 0x20ff800, (uint32_t[]){0x203b000}, 1);
}

static int attribute(int fd, size_t service, uint32_t attr) {
    uint32_t event = services[service].event | 2048, version = services[service].version;
    uint8_t body[64]; put_int(body, event); put_int(body + 5, version); put_int(body + 10, attr);
    if (service == 0 && attr == 1) return persistence(fd, 3839, NULL, 0);
    if (service == 1) {
        if (attr == 1 || attr == 2)
            return dsi_ints(fd, event, (uint32_t[]){version, attr, 1, 0, 1}, 5);
        if (attr == 3 || attr == 5 || attr == 6 || attr == 9)
            return dsi_ints(fd, event, (uint32_t[]){version, attr, attr == 6 ? 1 : 0, 1}, 4);
        if (attr == 7 || attr == 8 || attr == 11 || attr == 12 || attr == 13) {
            body[15] = 2; put_int(body + 16, 1); return dsi_payload(fd, event, body, 21);
        }
    }
    if (service == 2 && ((attr >= 11 && attr <= 20) || attr == 22 || attr == 23 || attr == 24 || attr == 4))
        return dsi_ints(fd, event, (uint32_t[]){version, attr, attr == 11 ? 1 : 0, 1}, 4);
    if (service == 3 && ((attr >= 5 && attr <= 20) || attr == 22 || attr == 28 || attr == 35)) {
        body[15] = attr == 22 || attr == 28 ? 1 : 2;
        put_int(body + 16, 1); return dsi_payload(fd, event, body, 21);
    }
    if (service == 4) {
        if (attr == 7 || attr == 19)
            return dsi_ints(fd, event, (uint32_t[]){version, attr, 0, 1}, 4);
        if (attr == 9 || attr == 18 || attr == 26)
            return dsi_ints(fd, event, (uint32_t[]){version, attr, 0, attr == 26 ? 1 : 0, 1}, 5);
        /* No synthetic key/touch event is emitted merely by subscribing. */
        if (attr == 20 || attr == 21 || attr == 23 || attr == 25 || attr == 28) return 1;
    }
    printf("{\"action\":\"attribute_unhandled\",\"service\":\"%s\",\"attribute\":%u}\n", services[service].name, attr);
    return 1;
}

static int display(int fd, uint32_t method, const uint8_t *args, uint32_t size) {
    static uint32_t settings[256][5];
    if (size < 5 || args[0] != 13) return 1;
    uint32_t id = u32(args + 1), event = services[5].event | 2048, v = services[5].version;
    if (id >= 256) {
        printf("{\"action\":\"display_id_unhandled\",\"id\":%u}\n", id); return 1;
    }
    if (method == 1043) return dsi_ints(fd, event, (uint32_t[]){v, 2029, id, 1280, 720, 0}, 6);
    if (method == 1005) return dsi_ints(fd, event, (uint32_t[]){v, 2000, id, 1280, 720}, 5);
    if (method == 1010) return dsi_ints(fd, event, (uint32_t[]){v, 2004, id, 1}, 4);
    if (method >= 1012 && method <= 1019) {
        unsigned index = (method - 1012) / 2;
        if (!(method & 1)) {
            if (size != 10 || args[5] != 13) return 0;
            settings[id][index] = u32(args + 6); return 1;
        }
        return dsi_ints(fd, event, (uint32_t[]){v, 2005 + index, id, settings[id][index]}, 4);
    }
    if (method == 1040 && size == 10 && args[5] == 13)
        return dsi_ints(fd, event, (uint32_t[]){v, 2031, id, u32(args + 6), 0}, 5);
    if (method == 1039) {
        size_t array_size;
        if (size > 1000 || !rpc_value(args + 5, size - 5, &array_size, 0) ||
            size != 10 + array_size || args[5 + array_size] != 13) return 0;
        uint8_t body[1030]; put_int(body, event); put_int(body + 5, v); put_int(body + 10, 2027);
        memcpy(body + 15, args, 5 + array_size); put_int(body + 20 + array_size, 0);
        memcpy(body + 25 + array_size, args + 5 + array_size, 5);
        return dsi_payload(fd, event, body, (uint32_t)(30 + array_size));
    }
    printf("{\"action\":\"dsi_unhandled\",\"service\":\"DSIDisplayManagement\",\"method\":%u}\n", method);
    return 1;
}

static int dsi_request(int fd, const struct header *h, const uint8_t *data) {
    if (!names_only && h->event == 0x20fe000 && (h->type == 0 || h->type == 3)) {
        for (size_t i = 0; i < SERVICE_COUNT; ++i) {
            printf("{\"action\":\"dsi_announce\",\"service\":\"%s\",\"instance\":0}\n", services[i].name);
            if (!dsi_ints(fd, 0x20ff800, &services[i].event, 1)) return 0;
        }
        return 1;
    }
    for (size_t service = 0; !names_only && h->type == 3 && service < SERVICE_COUNT; ++service) {
        if (h->event != services[service].event) continue;
        if (h->sender != hmi_address || h->destination != dsi_address || h->length < 20 ||
            data[0] != 42 || u32(data + 1) != h->length - 5 || data[5] != 13 ||
            u32(data + 6) != h->event || data[10] != 13 || u32(data + 11) != services[service].version || data[15] != 13) return 0;
        uint32_t method; memcpy(&method, data + 16, 4);
        printf("{\"action\":\"dsi_request\",\"service\":\"%s\",\"method\":%u}\n", services[service].name, method);
        if (method == 3839) {
            if (h->length != 25 || data[20] != 13) return 0;
            return attribute(fd, service, u32(data + 21));
        }
        if (method == 0) {
            if (h->length < 26 || data[20] != 39 || data[21] != 4) return 0;
            uint32_t count = u32(data + 22);
            if (count > 128 || h->length != 26 + count * 5) return 0;
            for (uint32_t i = 0; i < count; ++i) {
                if (data[26 + i * 5] != 13 || !attribute(fd, service, u32(data + 27 + i * 5))) return 0;
            }
            return 1;
        }
        if (service == 0) return persistence(fd, method, data + 20, h->length - 20);
        if (service == 4 && method == 1005) {
            if (h->length != 35 || data[20] != 13 || data[25] != 13 || data[30] != 13) return 0;
            return dsi_ints(fd, services[4].event | 2048, (uint32_t[]){services[4].version, 2000,
                            u32(data + 21), u32(data + 26), u32(data + 31)}, 5);
        }
        if (service == 5) return display(fd, method, data + 20, h->length - 20);
        printf("{\"action\":\"dsi_unhandled\",\"service\":\"%s\",\"method\":%u}\n", services[service].name, method);
    }
    return 1;
}

/* The upstream viewer supplies text commands; the guest still receives original TSD RpcBuffers. */
static int input_command(int fd, char *line) {
    char *state, *name = strtok_r(line, " ", &state), *token;
    int32_t numbers[6] = {0, 0, 0, 1, 0, 1};
    size_t count = 0;
    if (!name) return 0;
    while ((token = strtok_r(NULL, " ", &state))) {
        char *end; errno = 0;
        long value = strtol(token, &end, 10);
        if (count == 6 || end == token || *end || errno || value < -1000000 || value > 1000000) return 0;
        numbers[count++] = (int32_t)value;
    }
    uint32_t event = services[4].event | 2048, version = services[4].version;
    struct timespec now; if (clock_gettime(CLOCK_MONOTONIC, &now)) return 0;
    uint32_t stamp = (uint32_t)((uint64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000) & 0x7fffffff;
    if ((!strcmp(name, "tecla") || !strcmp(name, "encoder")) && count == 3) {
        if (numbers[0] < 0 || numbers[0] > 255 || numbers[1] < 0 || numbers[1] > 255 ||
            (!strcmp(name, "tecla") && numbers[2] != 0 && numbers[2] != 1)) return 0;
        uint32_t attr = !strcmp(name, "tecla") ? 25 : 23;
        printf("{\"action\":\"input\",\"attribute\":%u,\"keyboard\":%d,\"key\":%d,\"value\":%d}\n",
               attr, numbers[0], numbers[1], numbers[2]);
        return dsi_ints(fd, event, (uint32_t[]){version, attr, numbers[0], numbers[1], numbers[2], stamp, 1}, 7);
    }
    if (!strcmp(name, "gesto") && count >= 3 && count <= 6) {
        if (numbers[0] < 1 || numbers[0] > 5 || numbers[1] < 0 || numbers[1] >= 1280 ||
            numbers[2] < 0 || numbers[2] >= 720 || numbers[5] < 1 || numbers[5] > 10) return 0;
        uint8_t body[61];
        put_int(body, event); put_int(body + 5, version); put_int(body + 10, 20);
        put_int(body + 15, 13); put_int(body + 20, numbers[0]); put_int(body + 25, numbers[5]);
        body[30] = 2;
        uint32_t tail[] = {numbers[1], numbers[2], numbers[3], numbers[4], stamp, 1};
        for (size_t i = 0; i < 6; ++i) put_int(body + 31 + i * 5, tail[i]);
        printf("{\"action\":\"input\",\"attribute\":20,\"gesture\":%d,\"x\":%d,\"y\":%d}\n",
               numbers[0], numbers[1], numbers[2]);
        return dsi_payload(fd, event, body, sizeof(body));
    }
    return 0;
}

static int reply(int fd, const struct header *request, uint32_t event, const void *data, uint32_t length) {
    struct header h = {.length = length, .event = event, .sender = nameserver,
                       .destination = request->sender, .type = 0};
    printf("{\"action\":\"reply\",\"event\":%u,\"destination\":\"%016" PRIx64 "\",\"length\":%u}\n",
           event, h.destination, length);
    return send_all(fd, &h, sizeof(h)) && send_all(fd, data, length);
}

static int name_request(int fd, const struct header *h, const uint8_t *data) {
    if (h->type != 0 || h->destination != nameserver || (h->event != 2 && h->event != 6)) return 1;
    if (h->length < 2 || data[0] != 0x25) return 0;
    const uint8_t *end = memchr(data + 1, 0, h->length - 1);
    if (!end || end - data >= 256) return 0;
    const char *name = (const char *)data + 1;
    size_t pos = (size_t)(end - data) + 1;
    size_t index = 0;
    while (index < name_count && strcmp(names[index].name, name)) ++index;
    if (h->event == 2) {
        if (h->length != pos + 10 || data[pos] != 0x21 || data[pos + 9] != 2) return 0;
        uint64_t address;
        memcpy(&address, data + pos + 1, 8);
        if (address >> 24 == 0) address |= h->sender & ~UINT64_C(0xffffff);
        if ((address ^ h->sender) >> 24) return 0;
        uint8_t response[] = {2, 0x25, '/', 't', 'u', 'x', 0};
        if (index == name_count && name_count < 128) {
            strcpy(names[index].name, name);
            names[index].address = address;
            ++name_count;
            response[0] = 1;
        }
        printf("{\"action\":\"register\",\"success\":%s,\"address\":\"%016" PRIx64 "\"}\n",
               response[0] == 1 ? "true" : "false", address);
        if (!reply(fd, h, 3, response, sizeof(response))) return 0;
        if (response[0] == 1 && !strcmp(name, "HMITransceiver")) hmi_address = address;
        if (response[0] == 1 && !strcmp(name, "HMITransceiverInv")) registration_done = h->sender + 1;
        return 1;
    }
    if (h->length != pos) return 0;
    uint8_t response[11] = {2, 2, 0x21};
    if (index < name_count) { response[0] = 1; memcpy(response + 3, &names[index].address, 8); }
    return reply(fd, h, 7, response, sizeof(response));
}

static int hello(int fd, unsigned id) {
    struct ucred peer;
    socklen_t peer_size = sizeof(peer);
    if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &peer_size)) return 0;
    struct timeval limit = {.tv_sec = 2};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &limit, sizeof(limit));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &limit, sizeof(limit));
    struct header h = {.length = 24, .type = 5};
    struct hello hello = {.version = 1, .bits = 8, .base = (uint64_t)id << 56, .nameserver = nameserver};
    printf("{\"action\":\"hello\",\"client\":%u,\"pid\":%d,\"uid\":%d,\"base\":\"%016" PRIx64 "\"}\n",
           id, peer.pid, peer.uid, hello.base);
    return send_all(fd, &h, sizeof(h)) && send_all(fd, &hello, sizeof(hello));
}

static int client_message(int fd, unsigned id) {
    struct header h;
    if (receive(fd, &h, sizeof(h))) {
        if (h.length > 65536 || h.type > 7 || h.sender >> 56 != id) return 0;
        uint8_t *data = malloc(h.length ? h.length : 1);
        if (!data) return 0;
        if (!receive(fd, data, h.length)) { free(data); return 0; }
        printf("{\"action\":\"receive\",\"client\":%u,\"event\":%u,\"type\":%u,"
               "\"sender\":\"%016" PRIx64 "\",\"destination\":\"%016" PRIx64 "\",\"length\":%u,\"payload_hex\":\"",
               id, h.event, h.type, h.sender, h.destination, h.length);
        for (uint32_t i = 0; i < h.length && i < 512; ++i) printf("%02x", data[i]);
        puts("\"}");
        /* shortcut: only this HMI's DSI contracts; no general-purpose CommMgr routing. */
        int ok = observe_only || (name_request(fd, &h, data) && dsi_request(fd, &h, data));
        if (!observe_only && !names_only && h.type == 2 && h.sender == registration_done)
            ok = ok && dsi_start(fd, hmi_address);
        free(data);
        return ok;
    }
    return 0;
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    observe_only = argc == 2 && !strcmp(argv[1], "--observe");
    names_only = argc == 2 && !strcmp(argv[1], "--nameserver-only");
    if (argc == 2 && !strcmp(argv[1], "--self-test")) {
        int pair[2]; assert(socketpair(AF_UNIX, SOCK_STREAM, 0, pair) == 0);
        struct header h = {.length = 24, .type = 5}, copy;
        assert(send_all(pair[0], &h, 3)); assert(send_all(pair[0], (char *)&h + 3, sizeof(h) - 3));
        assert(receive(pair[1], &copy, sizeof(copy))); assert(!memcmp(&h, &copy, sizeof(h)));
        uint8_t request[] = {0x25, 'H', 'M', 'I', 0, 0x21, 3, 0, 0, 0, 0, 0, 0, 0, 2};
        h = (struct header){.event = 2, .length = sizeof(request), .sender = UINT64_C(0x100000001000004), .destination = nameserver};
        assert(name_request(pair[0], &h, request)); assert(receive(pair[1], &copy, sizeof(copy)));
        uint8_t response[11]; assert(copy.event == 3 && copy.length == 7);
        assert(receive(pair[1], response, copy.length)); assert(response[0] == 1);
        h.event = 6; h.length = 5;
        assert(name_request(pair[0], &h, request)); assert(receive(pair[1], &copy, sizeof(copy)));
        assert(copy.event == 7 && copy.length == 11); assert(receive(pair[1], response, copy.length));
        uint64_t address; memcpy(&address, response + 3, 8);
        assert(response[0] == 1 && address == UINT64_C(0x100000001000003));
        h.length = 1; assert(!name_request(pair[0], &h, request));
        uint8_t args[64] = {0}, payload[128];
        put_int(args, 5000); args[5] = 34; args[6] = 200;
        memcpy(args + 14, (uint8_t[]){39, 4, 0, 0, 0, 0}, 6);
        hmi_address = UINT64_C(0x100000001000003);
        assert(persistence(pair[0], 1003, args, 14));
        assert(receive(pair[1], &copy, sizeof(copy)) && copy.length == 45);
        assert(receive(pair[1], payload, copy.length) && u32(payload + 41) == 2);
        assert(persistence(pair[0], 1002, args, 20));
        assert(receive(pair[1], &copy, sizeof(copy)) && copy.length == 39);
        assert(receive(pair[1], payload, copy.length) && u32(payload + 35) == 0);
        assert(persistence(pair[0], 1003, args, 14));
        assert(receive(pair[1], &copy, sizeof(copy)) && copy.length == 45);
        assert(receive(pair[1], payload, copy.length) && u32(payload + 41) == 0);
        assert(!memcmp(payload + 34, args + 14, 6));
        args[16] = 2;
        put_int(args + 20, (uint32_t)-3); args[20] = 9;
        put_int(args + 25, 12); args[25] = 9;
        assert(persistence(pair[0], 1002, args, 30));
        assert(receive(pair[1], &copy, sizeof(copy)) && copy.length == 39);
        assert(receive(pair[1], payload, copy.length) && u32(payload + 35) == 0);
        assert(persistence(pair[0], 1003, args, 14));
        assert(receive(pair[1], &copy, sizeof(copy)) && copy.length == 55);
        assert(receive(pair[1], payload, copy.length) && u32(payload + 51) == 0);
        assert(!memcmp(payload + 34, args + 14, 16));
        assert(!persistence(pair[0], 1003, args, 13));
        args[16] = 255; assert(!persistence(pair[0], 1002, args, 20));
        uint8_t invalid[20] = {42}; put_int(invalid + 5, 33796096);
        put_int(invalid + 10, 1504078919); put_int(invalid + 15, 1003);
        h = (struct header){.type = 3, .event = 33796096, .length = 20,
                            .sender = hmi_address, .destination = dsi_address};
        assert(!dsi_request(pair[0], &h, invalid));
        put_int(args, 43); put_int(args + 5, 75);
        assert(display(pair[0], 1012, args, 10));
        assert(display(pair[0], 1013, args, 5));
        assert(receive(pair[1], &copy, sizeof(copy)) && copy.length == 30);
        assert(receive(pair[1], payload, copy.length) && u32(payload + 26) == 75);
        char key[] = "tecla 13 78 1", gesture[] = "gesto 1 300 150", bad[] = "gesto 1 999999999999999999999 1";
        assert(input_command(pair[0], key));
        assert(receive(pair[1], &copy, sizeof(copy)) && copy.length == 45);
        assert(receive(pair[1], payload, copy.length) && u32(payload + 16) == 25 && u32(payload + 26) == 78);
        assert(input_command(pair[0], gesture));
        assert(receive(pair[1], &copy, sizeof(copy)) && copy.length == 66);
        assert(receive(pair[1], payload, copy.length) && payload[35] == 2 && u32(payload + 37) == 300);
        assert(!input_command(pair[0], bad));
        close(pair[0]); assert(!receive(pair[1], &copy, sizeof(copy))); close(pair[1]);
        puts("PASS framing, names, persistence missing/write/read bytes, malformed payloads, key/gesture frames"); return 0;
    }
    int listener = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    const char name[] = "tsd.communication.commgr";
    memcpy(address.sun_path + 1, name, sizeof(name) - 1);
    if (listener < 0 || bind(listener, (void *)&address,
        offsetof(struct sockaddr_un, sun_path) + sizeof(name)) || listen(listener, 8)) {
        perror("commgr listen"); return 1;
    }
    puts("{\"action\":\"ready\",\"socket\":\"@tsd.communication.commgr\"}");
    FILE *input = fopen("/run/display/dsi-input", "w+");
    if (!input) { perror("input file"); return 1; }
    char line[128]; size_t line_size = 0;
    struct pollfd sockets[17];
    for (unsigned i = 0; i < 17; ++i) sockets[i] = (struct pollfd){.fd = -1, .events = POLLIN};
    sockets[0].fd = listener;
    unsigned next_id = 1;
    while (poll(sockets, 17, 20) >= 0) {
        clearerr(input);
        for (size_t i = 0; i < 4096; ++i) {
            int ch = fgetc(input);
            if (ch == EOF) break;
            if (ch == '\n') {
                if (line_size < sizeof(line) && hmi_fd >= 0) {
                    line[line_size] = 0;
                    if (!input_command(hmi_fd, line)) puts("{\"action\":\"input_rejected\"}");
                }
                line_size = 0;
            } else if (line_size < sizeof(line)) line[line_size++] = (char)ch;
        }
        if (sockets[0].revents) {
            int fd = accept4(listener, NULL, NULL, SOCK_CLOEXEC);
            if (fd < 0) { perror("accept"); return 1; }
            if (next_id > 16 || !hello(fd, next_id)) close(fd);
            else sockets[next_id++].fd = fd;
        }
        for (unsigned id = 1; id < next_id; ++id) {
            if (sockets[id].revents && !client_message(sockets[id].fd, id)) {
                if (sockets[id].fd == hmi_fd) hmi_fd = -1;
                close(sockets[id].fd); sockets[id].fd = -1;
                printf("{\"action\":\"disconnect\",\"client\":%u}\n", id);
            }
        }
    }
    perror("poll"); return 1;
}

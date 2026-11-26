#!/usr/bin/env python3
"""mock_proxy.py: a local HTTP + SOCKS5 proxy that misbehaves on request.

Use it to test how your code handles proxy logins, proxy errors, latency and
idle cuts without a real proxy. Python 3.8+, standard library only.

Maintained by Proxy302 (https://proxy302.com). MIT licensed.
"""
import argparse
import asyncio
import base64
import binascii
import errno
import random
import socket
import struct
import sys
import time
from urllib.parse import urlsplit

FAULTS = ("407", "403", "502", "503", "504", "reset", "hang")
SOCKS_FAULT_CODE = {"403": 0x02, "502": 0x05, "503": 0x01, "504": 0x06}
SOCKS_CODE_TEXT = {
    0x00: "succeeded", 0x01: "general failure", 0x02: "not allowed by ruleset",
    0x03: "network unreachable", 0x04: "host unreachable", 0x05: "connection refused",
    0x06: "TTL expired (timeout)", 0x07: "command not supported", 0x08: "address type not supported",
}
STATUS_TEXT = {200: "Connection established", 400: "Bad Request", 403: "Forbidden",
               405: "Method Not Allowed", 407: "Proxy Authentication Required",
               502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"}
HOP_BY_HOP = {"proxy-authorization", "proxy-connection", "connection", "keep-alive"}


# ---------------------------------------------------------------- helpers

def stamp():
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d" % int((t % 1) * 1000)


def log(conf, *parts):
    if not conf.quiet:
        print(stamp(), *parts, flush=True)


def peer_of(writer):
    p = writer.get_extra_info("peername")
    if not p:
        return "?"
    return ("[%s]:%d" % (p[0], p[1])) if ":" in p[0] else "%s:%d" % (p[0], p[1])


def human(n):
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1000.0


def pick_fault(conf):
    if conf.fail and conf.rng.random() < conf.fail_rate:
        return conf.fail
    return None


async def send(conf, writer, data):
    """Everything the proxy sends to the client goes through here (latency applies)."""
    if conf.latency:
        await asyncio.sleep(conf.latency)
    writer.write(data)
    await writer.drain()


def hard_reset(writer):
    """Close with a TCP RST instead of a normal FIN."""
    sock = writer.get_extra_info("socket")
    try:
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except OSError:
        pass
    writer.transport.abort()


async def close(writer):
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


async def hang(reader):
    """Say nothing until the client gives up."""
    try:
        while await reader.read(65536):
            pass
    except Exception:
        pass


async def open_target(conf, host, port):
    """Connect to the destination. Returns (reader, writer, None) or (None, None, reason)."""
    real = conf.map.get(host.lower(), host)
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(real, port), conf.connect_timeout)
        return r, w, None
    except asyncio.TimeoutError:
        return None, None, "timeout"
    except socket.gaierror:
        return None, None, "dns"
    except ConnectionRefusedError:
        return None, None, "refused"
    except OSError as e:
        if e.errno in (errno.EHOSTUNREACH,):
            return None, None, "host-unreachable"
        if e.errno in (errno.ENETUNREACH,):
            return None, None, "net-unreachable"
        return None, None, "error: %s" % e


async def relay(conf, label, c_reader, c_writer, t_reader, t_writer, first_line=None):
    """Copy bytes both ways until the target side ends, the client goes, or the idle limit hits."""
    started = time.monotonic()
    last = [started]
    count = {"up": 0, "down": 0}

    async def up():
        try:
            while True:
                data = await c_reader.read(65536)
                if not data:
                    break
                last[0] = time.monotonic()
                t_writer.write(data)
                await t_writer.drain()
                count["up"] += len(data)
            if t_writer.can_write_eof():
                t_writer.write_eof()
        except Exception:
            pass
        await asyncio.sleep(3600)  # client finished sending; wait for the answer

    async def down():
        try:
            while True:
                data = await t_reader.read(65536)
                if not data:
                    break
                last[0] = time.monotonic()
                if first_line is not None and not first_line:
                    first_line.append(data.split(b"\r\n", 1)[0].decode("latin-1", "replace"))
                if conf.latency:
                    await asyncio.sleep(conf.latency)
                c_writer.write(data)
                await c_writer.drain()
                count["down"] += len(data)
        except Exception:
            pass

    async def idle_watch():
        while True:
            await asyncio.sleep(0.1)
            if time.monotonic() - last[0] >= conf.idle_timeout:
                return

    up_task = asyncio.ensure_future(up())
    down_task = asyncio.ensure_future(down())
    watch = asyncio.ensure_future(idle_watch()) if conf.idle_timeout else None
    done, _ = await asyncio.wait([down_task] + ([watch] if watch else []),
                                 return_when=asyncio.FIRST_COMPLETED)
    why = ("idle limit (%g s without data), closed by proxy" % conf.idle_timeout) if watch in done else "ended"
    for t in (up_task, down_task, watch):
        if t:
            t.cancel()
    await close(t_writer)
    await close(c_writer)
    extra = ("; target answered: %s" % first_line[0]) if first_line else ""
    log(conf, label, "closed:", why + ",", "up", human(count["up"]) + ",", "down", human(count["down"]) + ",",
        "%.2f s" % (time.monotonic() - started) + extra)


# ---------------------------------------------------------------- HTTP side

def check_basic(conf, value):
    if conf.user is None:
        return "not required"
    if not value:
        return "missing"
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "basic":
        return "wrong scheme (%s)" % scheme
    try:
        user, _, pw = base64.b64decode(token.strip()).decode("utf-8", "replace").partition(":")
    except (binascii.Error, ValueError):
        return "unreadable"
    if user == conf.user and pw == conf.password:
        return "ok"
    if user != conf.user:
        return "wrong user (%r)" % user
    return "wrong password"


async def http_reply(conf, writer, code, extra_headers="", body=None, keep_alive=False):
    if body is None:
        body = "%d %s (generated by mock-proxy)\n" % (code, STATUS_TEXT.get(code, ""))
    raw = body.encode()
    head = ("HTTP/1.1 %d %s\r\nServer: mock-proxy\r\nContent-Type: text/plain\r\n"
            "Content-Length: %d\r\nConnection: %s\r\n%s\r\n"
            % (code, STATUS_TEXT.get(code, ""), len(raw), "keep-alive" if keep_alive else "close", extra_headers))
    await send(conf, writer, head.encode() + raw)


async def handle_http(conf, reader, writer):
    peer = peer_of(writer)
    label = "http   %s" % peer
    while True:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 60)
        except Exception:
            await close(writer)
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, _version = lines[0].split(" ", 2)
        except ValueError:
            log(conf, label, repr(lines[0][:60]), "-> 400 (not an HTTP proxy request)")
            await http_reply(conf, writer, 400)
            await close(writer)
            return
        headers = []
        for line in lines[1:]:
            if line:
                k, _, v = line.partition(":")
                headers.append((k.strip(), v.strip()))

        def hget(name):
            return next((v for k, v in headers if k.lower() == name), None)

        what = "%s %s" % (method, target)
        auth = check_basic(conf, hget("proxy-authorization"))
        fault = pick_fault(conf)
        if fault == "407" and auth in ("ok", "not required"):
            auth = "rejected (injected 407)"
        if auth not in ("ok", "not required"):
            body_len = int(hget("content-length") or 0)
            log(conf, label, what, "auth=" + auth, "-> 407")
            await http_reply(conf, writer, 407, 'Proxy-Authenticate: Basic realm="mock-proxy"\r\n',
                             keep_alive=(body_len == 0))
            if body_len:
                await close(writer)
                return
            continue  # the client may retry with credentials on the same connection
        if fault in ("403", "502", "503", "504"):
            log(conf, label, what, "auth=" + auth, "-> %s (injected)" % fault)
            await http_reply(conf, writer, int(fault))
            await close(writer)
            return
        if fault == "reset":
            log(conf, label, what, "auth=" + auth, "-> connection reset (injected)")
            hard_reset(writer)
            return
        if fault == "hang":
            log(conf, label, what, "auth=" + auth, "-> no answer (injected hang)")
            await hang(reader)
            await close(writer)
            return

        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            host = host.strip("[]")
            if not host or not port.isdigit():
                log(conf, label, what, "-> 400 (CONNECT needs host:port)")
                await http_reply(conf, writer, 400)
                await close(writer)
                return
            t_reader, t_writer, err = await open_target(conf, host, int(port))
            if err:
                code = 504 if err == "timeout" else 502
                log(conf, label, what, "auth=" + auth, "-> %d (target %s)" % (code, err))
                await http_reply(conf, writer, code, body="%d: mock-proxy could not reach %s (%s)\n" % (code, target, err))
                await close(writer)
                return
            log(conf, label, what, "auth=" + auth, "-> 200 tunnel")
            await send(conf, writer, b"HTTP/1.1 200 Connection established\r\n\r\n")
            await relay(conf, label, reader, writer, t_reader, t_writer)
            return

        if target.lower().startswith("http://"):
            u = urlsplit(target)
            host, port = u.hostname or "", u.port or 80
            path = (u.path or "/") + ("?" + u.query if u.query else "")
            t_reader, t_writer, err = await open_target(conf, host, port)
            if err:
                code = 504 if err == "timeout" else 502
                log(conf, label, what, "auth=" + auth, "-> %d (target %s)" % (code, err))
                await http_reply(conf, writer, code, body="%d: mock-proxy could not reach %s (%s)\n" % (code, host, err))
                await close(writer)
                return
            out = ["%s %s HTTP/1.1" % (method, path)]
            out += ["%s: %s" % (k, v) for k, v in headers if k.lower() not in HOP_BY_HOP]
            out += ["Connection: close", "", ""]
            t_writer.write("\r\n".join(out).encode("latin-1"))
            await t_writer.drain()
            log(conf, label, what, "auth=" + auth, "-> forwarded")
            await relay(conf, label, reader, writer, t_reader, t_writer, first_line=[])
            return

        log(conf, label, what, "-> 400 (expected CONNECT or an absolute http:// URL)")
        await http_reply(conf, writer, 400)
        await close(writer)
        return


# ---------------------------------------------------------------- SOCKS5 side

async def handle_socks(conf, reader, writer):
    peer = peer_of(writer)
    label = "socks5 %s" % peer
    try:
        ver, n = await asyncio.wait_for(reader.readexactly(2), 60)
        if ver != 5:
            log(conf, label, "first byte 0x%02x is not SOCKS5 -> closed" % ver)
            await close(writer)
            return
        methods = await reader.readexactly(n)
        fault = pick_fault(conf)
        if conf.user is not None:
            if 0x02 not in methods:
                log(conf, label, "client offered no username/password method -> 0xff")
                await send(conf, writer, b"\x05\xff")
                await close(writer)
                return
            await send(conf, writer, b"\x05\x02")
            _v, ulen = await reader.readexactly(2)
            uname = (await reader.readexactly(ulen)).decode("utf-8", "replace")
            plen = (await reader.readexactly(1))[0]
            pw = (await reader.readexactly(plen)).decode("utf-8", "replace")
            if fault == "407":
                auth = "rejected (injected)"
            elif uname != conf.user:
                auth = "wrong user (%r)" % uname
            elif pw != conf.password:
                auth = "wrong password"
            else:
                auth = "ok"
            if auth != "ok":
                log(conf, label, "login", "auth=" + auth, "-> 0x01 (login failed)")
                await send(conf, writer, b"\x01\x01")
                await close(writer)
                return
            await send(conf, writer, b"\x01\x00")
        else:
            auth = "not required"
            if 0x00 not in methods:
                log(conf, label, "client offered no no-login method -> 0xff")
                await send(conf, writer, b"\x05\xff")
                await close(writer)
                return
            await send(conf, writer, b"\x05\x00")

        _ver, cmd, _rsv, atyp = await reader.readexactly(4)
        if atyp == 1:
            host, kind = socket.inet_ntoa(await reader.readexactly(4)), "IPv4, resolved by the client"
        elif atyp == 3:
            ln = (await reader.readexactly(1))[0]
            host, kind = (await reader.readexactly(ln)).decode("ascii", "replace"), "hostname, resolved by the proxy"
        elif atyp == 4:
            host, kind = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16)), "IPv6, resolved by the client"
        else:
            log(conf, label, "address type %d -> 0x08" % atyp)
            await send(conf, writer, b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
            await close(writer)
            return
        port = struct.unpack("!H", await reader.readexactly(2))[0]
        shown = ("[%s]:%d" % (host, port)) if ":" in host else "%s:%d" % (host, port)
        what = "CONNECT %s (%s)" % (shown, kind)
        if cmd != 1:
            name = {2: "BIND", 3: "UDP ASSOCIATE"}.get(cmd, "command %d" % cmd)
            log(conf, label, name, shown, "-> 0x07 (only CONNECT is supported)")
            await send(conf, writer, b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            await close(writer)
            return
        if fault in SOCKS_FAULT_CODE:
            code = SOCKS_FAULT_CODE[fault]
            log(conf, label, what, "auth=" + auth, "-> 0x%02x %s (injected)" % (code, SOCKS_CODE_TEXT[code]))
            await send(conf, writer, bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0]))
            await close(writer)
            return
        if fault == "reset":
            log(conf, label, what, "auth=" + auth, "-> connection reset (injected)")
            hard_reset(writer)
            return
        if fault == "hang":
            log(conf, label, what, "auth=" + auth, "-> no answer (injected hang)")
            await hang(reader)
            await close(writer)
            return
        t_reader, t_writer, err = await open_target(conf, host, port)
        if err:
            code = {"timeout": 0x06, "dns": 0x04, "refused": 0x05, "host-unreachable": 0x04,
                    "net-unreachable": 0x03}.get(err, 0x01)
            log(conf, label, what, "auth=" + auth, "-> 0x%02x %s (target %s)" % (code, SOCKS_CODE_TEXT[code], err))
            await send(conf, writer, bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0]))
            await close(writer)
            return
        log(conf, label, what, "auth=" + auth, "-> 0x00 tunnel")
        await send(conf, writer, b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        await relay(conf, label, reader, writer, t_reader, t_writer)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        await close(writer)


# ---------------------------------------------------------------- echo target

async def handle_echo(conf, reader, writer):
    """A tiny HTTP server to point requests at: it answers with what it received."""
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
    except Exception:
        await close(writer)
        return
    body = ("echo: you are %s\n%s" % (peer_of(writer), head.decode("latin-1"))).encode()
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n"
                 b"Connection: close\r\n\r\n" % len(body) + body)
    try:
        await writer.drain()
    except Exception:
        pass
    await close(writer)


# ---------------------------------------------------------------- main

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Local HTTP + SOCKS5 proxy with logins, logging and injected faults, for testing proxy handling.")
    p.add_argument("--bind", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    p.add_argument("--http", type=int, default=3128, metavar="PORT", help="HTTP proxy port, 0 to disable (default 3128)")
    p.add_argument("--socks", type=int, default=1080, metavar="PORT", help="SOCKS5 port, 0 to disable (default 1080)")
    p.add_argument("--echo", type=int, default=0, metavar="PORT", help="also run an HTTP echo server on this port")
    p.add_argument("--user", help="require this username (HTTP Basic and SOCKS5 login)")
    p.add_argument("--password", default="", help="password that goes with --user")
    p.add_argument("--latency", type=float, default=0, metavar="MS",
                   help="wait MS milliseconds before everything sent back to the client")
    p.add_argument("--fail", choices=FAULTS, help="inject this fault")
    p.add_argument("--fail-rate", type=float, default=1.0, metavar="R", help="share of requests that get the fault, 0..1 (default 1)")
    p.add_argument("--seed", type=int, help="random seed, so --fail-rate picks the same requests every run")
    p.add_argument("--idle-timeout", type=float, default=0, metavar="S", help="close tunnels after S seconds without data")
    p.add_argument("--connect-timeout", type=float, default=10, metavar="S", help="timeout for reaching the target (default 10)")
    p.add_argument("--map", action="append", default=[], metavar="NAME=IP",
                   help="resolve NAME to IP at the proxy (repeatable), e.g. --map demo.test=127.0.0.1")
    p.add_argument("--quiet", action="store_true", help="no per-request log")
    a = p.parse_args(argv)
    if not a.http and not a.socks:
        p.error("enable at least one of --http and --socks")
    if not 0 <= a.fail_rate <= 1:
        p.error("--fail-rate must be between 0 and 1")
    a.latency = a.latency / 1000.0
    mapping = {}
    for item in a.map:
        name, sep, ip = item.partition("=")
        if not sep or not name or not ip:
            p.error("--map expects NAME=IP, got %r" % item)
        mapping[name.lower()] = ip
    a.map = mapping
    a.rng = random.Random(a.seed)
    return a


async def main(conf):
    servers = []

    def wrap(handler):
        return lambda r, w: handler(conf, r, w)

    lines = []
    if conf.http:
        servers.append(await asyncio.start_server(wrap(handle_http), conf.bind, conf.http))
        lines.append("HTTP proxy    http://%s:%d" % (conf.bind, conf.http))
    if conf.socks:
        servers.append(await asyncio.start_server(wrap(handle_socks), conf.bind, conf.socks))
        lines.append("SOCKS5 proxy  socks5h://%s:%d" % (conf.bind, conf.socks))
    if conf.echo:
        servers.append(await asyncio.start_server(wrap(handle_echo), conf.bind, conf.echo))
        lines.append("echo target   http://%s:%d/" % (conf.bind, conf.echo))
    print("mock-proxy listening")
    for line in lines:
        print("  " + line)
    print("  login         %s" % ("user %r, password set" % conf.user if conf.user is not None else "none"))
    extras = []
    if conf.latency:
        extras.append("latency %g ms" % (conf.latency * 1000))
    if conf.fail:
        extras.append("fault %s on %g%% of requests" % (conf.fail, conf.fail_rate * 100))
    if conf.idle_timeout:
        extras.append("idle limit %g s" % conf.idle_timeout)
    if conf.map:
        extras.append("map " + ", ".join("%s=%s" % kv for kv in conf.map.items()))
    print("  behaviour     %s" % ("; ".join(extras) if extras else "normal"), flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    try:
        asyncio.run(main(parse_args(sys.argv[1:])))
    except KeyboardInterrupt:
        pass

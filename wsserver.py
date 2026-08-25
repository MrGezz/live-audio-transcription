"""
wsserver.py - the control panel's listener: static HTTP and WebSocket on one
port, on nothing but the standard library.

Why this exists
---------------
The obvious answer is the websockets package. This project ships to Windows
users who run one pip install from a .cmd launcher, and that package changed
its serve() handler signature between 10/11 and 12+ - code written against one
does not run on the other. The framing this app needs is trivial (JSON text
frames and binary PCM), so implement RFC 6455 on stdlib asyncio and keep
requirements.txt untouched.

What the app gets
-----------------
    server = WSServer("127.0.0.1", 8770, "web", token="",
                      on_open=..., on_message=..., on_close=..., on_log=...)
    server.start()              # returns once the port is really bound
    server.broadcast({"type": "caption", "text": "..."})
    server.stop()

One port, because the page and its socket have to come from the same origin
for the browser to connect without a second host, port and CORS story.

Threading
---------
start() runs an asyncio loop in a daemon thread. on_open, on_message and
on_close are called ON THAT THREAD and MUST NOT BLOCK: whatever they do -
inference, disk, a lock held by the capture thread - stops every other socket
for exactly that long, including the pings that keep dead clients from piling
up. Hand work to a queue and return.

Everything the app calls in the other direction (broadcast, send, stop, and
WSClient.send / send_bytes / close) is safe from any thread and never raises
when the socket has already gone; a client that vanished is normal, not
an error the caller should have to handle.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import struct
import threading
import time
import urllib.parse
import uuid

# RFC 6455 section 1.3. Concatenated with Sec-WebSocket-Key, sha1'd and
# base64'd to prove to the client that this is a websocket endpoint and not an
# HTTP server that happened to answer 101.
#
# Copy this constant, never retype it. A single transposed character still
# produces a well-formed base64 accept header, so the server looks completely
# healthy from its own side - it answers 101, logs a connection, and every
# browser silently refuses to finish the handshake.
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

# A laptop that sleeps mid-session does not send a FIN, so the socket stays
# "open" forever and its client entry leaks - the panel then shows listeners
# that are not there. Ping after 20 s of silence, drop 20 s after that.
PING_INTERVAL = 20.0
PONG_TIMEOUT = 20.0
KEEPALIVE_TICK = 1.0

MAX_MESSAGE = 8 * 1024 * 1024
# A tab that has stopped reading - throttled in the background, or on a machine
# that slept - never applies backpressure to write(); asyncio just queues. This
# cap means one stuck viewer costs 4 MiB and a dropped connection instead of
# growing server memory for as long as the session lasts.
MAX_SEND_BUFFER = 4 * 1024 * 1024
# Long enough for a slow LAN client, short enough that a socket which connects
# and then says nothing cannot hold a slot.
HEADER_TIMEOUT = 10.0
MAX_HEADERS = 64

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".map": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
}

_STATUS_TEXT = {
    200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
    500: "Internal Server Error",
}


class _WSError(Exception):
    """A protocol violation, carrying the close code to answer it with."""

    def __init__(self, code, reason):
        Exception.__init__(self, reason)
        self.code = code
        self.reason = reason


# -------------------------------
# Framing
# -------------------------------
def _encode_frame(opcode, payload, fin=True):
    """One server-to-client frame. Never masked - RFC 6455 5.1 forbids it."""
    header = bytearray()
    header.append((0x80 if fin else 0x00) | opcode)
    size = len(payload)
    if size < 126:
        header.append(size)
    elif size < 65536:
        header.append(126)
        header += struct.pack(">H", size)
    else:
        header.append(127)
        header += struct.pack(">Q", size)
    return bytes(header) + payload


def _close_payload(code, reason):
    """
    A close body the peer is allowed to receive.

    Two things the obvious struct.pack(">H", code) + reason[:123] gets wrong.
    A code outside 0..65535 - anything an app passes to WSClient.close by
    mistake - made struct raise inside a call_soon_threadsafe callback, where
    on_log never sees it and _finish never runs, so the socket and its entry in
    _clients both leaked. And RFC 6455 5.5.1 requires the reason to be valid
    utf-8: cutting at 123 bytes lands mid-character for any non-ascii reason,
    and a strict client drops the connection over the half character rather
    than reading the reason it was sent.
    """
    if not 1000 <= code <= 4999 or code in (1004, 1005, 1006):
        # 1005 and 1006 mean "no close frame arrived", so putting either on the
        # wire is itself the violation it is meant to describe.
        code = 1000
    body = reason.encode("utf-8")[:123].decode("utf-8", "ignore")
    return struct.pack(">H", code) + body.encode("utf-8")


def _unmask(data, key):
    """
    XOR `data` against the 4-byte masking key.

    One big-integer XOR rather than the obvious per-byte loop because this runs
    on the event loop, in front of every other socket. Measured on a 200 KB
    binary frame, the size a browser sends when it hands over a few seconds of
    PCM: 28.08 ms byte by byte, 1.32 ms this way. 28 ms is a quarter of the
    audio chunk interval spent doing nothing but XOR.
    """
    size = len(data)
    pad = key * (size // 4) + key[:size % 4]
    return (int.from_bytes(data, "big")
            ^ int.from_bytes(pad, "big")).to_bytes(size, "big")


async def _read_frame(reader, budget):
    """
    (fin, opcode, payload) for one client frame, or raises _WSError.

    `budget` is how many more payload bytes this message may still take.
    """
    head = await reader.readexactly(2)
    first, second = head[0], head[1]
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    if first & 0x70:
        raise _WSError(1002, "reserved bits set")
    if not second & 0x80:
        # RFC 6455 5.1: every client-to-server frame is masked. An unmasked one
        # means the sender is not speaking this protocol, and guessing at where
        # its payload ends desynchronises the stream for good.
        raise _WSError(1002, "client frame was not masked")

    length = second & 0x7F
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]

    if opcode >= 0x8:
        if length > 125 or not fin:
            raise _WSError(1002, "oversized or fragmented control frame")
    elif length > budget:
        # Refused from the DECLARED length, before a byte of it is read. A cap
        # applied after reassembly is not a cap: the allocation it is meant to
        # prevent has already happened by then.
        raise _WSError(1009, "message over the {0} byte limit".format(
            MAX_MESSAGE))

    key = await reader.readexactly(4)
    if length:
        payload = _unmask(await reader.readexactly(length), key)
    else:
        payload = b""
    return fin, opcode, payload


# -------------------------------
# HTTP
# -------------------------------
async def _read_request(reader):
    """(method, path, query, headers) or None if the peer said nothing."""
    line = await reader.readline()
    if not line:
        return None
    parts = line.decode("latin-1").strip().split()
    if len(parts) < 2:
        return None
    method, target = parts[0].upper(), parts[1]
    headers = {}
    done = False
    # MAX_HEADERS + 1 reads, because the blank line that ends them is a read
    # too: at range(MAX_HEADERS) a request with exactly MAX_HEADERS headers
    # never reaches its own terminator and gets refused as if it had too many.
    for _ in range(MAX_HEADERS + 1):
        raw = await reader.readline()
        if raw in (b"\r\n", b"\n", b""):
            done = True
            break
        name, sep, value = raw.decode("latin-1").partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    if not done:
        # Giving up in the middle of the headers and handshaking anyway leaves
        # the rest of them on the stream, where the frame parser reads
        # "X-Pad-64: 64" as a frame header: measured with 80 headers, the
        # socket got its 101 and then died on an invented 1002 instead of on
        # the real fault. A client with more than 64 headers is broken; say so
        # by hanging up rather than by desynchronising.
        return None
    path, _, query = target.partition("?")
    return method, path, query, headers


async def _send_http(writer, status, body, ctype="text/plain; charset=utf-8"):
    """Write one complete HTTP response. Connection: close - no keep-alive."""
    head = ("HTTP/1.1 {0} {1}\r\n"
            "Content-Type: {2}\r\n"
            "Content-Length: {3}\r\n"
            # The panel draws itself from settings.schema_json(), so a cached
            # index.html or app.js from a previous version renders controls
            # that no longer exist and silently drops the ones that do.
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n"
            "\r\n").format(status, _STATUS_TEXT.get(status, "OK"), ctype,
                           len(body))
    writer.write(head.encode("ascii") + body)
    await writer.drain()


# -------------------------------
# Clients
# -------------------------------
class WSClient(object):
    """
    One connected socket.

    `role` is the app's own label for what this socket is doing ("panel",
    "audio", ...). This module never reads it; it only carries it so the app
    can tell its own sockets apart in on_message and in clients.
    """

    __slots__ = ("id", "remote", "role", "opened", "_server", "_writer",
                 "_closed", "_last_rx", "_pong_deadline", "_drop_reason")

    def __init__(self, server, writer, remote):
        # Short, because it is printed in every log line and shown in the
        # panel; a full uuid is 36 characters of noise for a handful of tabs.
        self.id = uuid.uuid4().hex[:8]
        self.remote = remote
        self.role = ""
        self.opened = time.time()
        self._server = server
        self._writer = writer
        self._closed = False
        self._last_rx = time.monotonic()
        self._pong_deadline = None
        self._drop_reason = None

    # -- app-facing, safe from any thread -----------------------------------
    def send(self, obj):
        """json.dumps `obj` to this client as a text frame."""
        try:
            text = json.dumps(obj)
        except (TypeError, ValueError) as e:
            self._server._log(
                "error",
                "dropped a message for {0} that will not encode: {1}".format(
                    self.id, e))
            return False
        return self._dispatch(OP_TEXT, text.encode("utf-8"))

    def send_bytes(self, data):
        """Send raw bytes as a binary frame."""
        return self._dispatch(OP_BINARY, bytes(data))

    def close(self, code=1000, reason=""):
        """Close politely: a close frame, then the transport."""
        loop = self._server._loop
        if loop is None or loop.is_closed():
            return False
        try:
            loop.call_soon_threadsafe(self._close_now, code, reason)
        except RuntimeError:
            return False
        return True

    def info(self):
        return {"id": self.id, "remote": self.remote, "role": self.role,
                "opened": self.opened,
                "age": round(time.time() - self.opened, 1)}

    # -- loop thread only ---------------------------------------------------
    def _dispatch(self, opcode, payload):
        """Hand a frame to the loop thread, from wherever the caller is."""
        loop = self._server._loop
        if self._closed or loop is None or loop.is_closed():
            return False
        try:
            loop.call_soon_threadsafe(self._write_frame, opcode, payload)
        except RuntimeError:
            # The loop stopped between the check above and here.
            return False
        return True

    def _write_frame(self, opcode, payload, fin=True):
        if self._closed:
            return
        try:
            if self._writer.is_closing():
                self._closed = True
                return
            self._writer.write(_encode_frame(opcode, payload, fin))
            queued = self._writer.transport.get_write_buffer_size()
            if queued > MAX_SEND_BUFFER:
                self._server._log(
                    "warn",
                    "{0} is not reading ({1} bytes queued) - dropping "
                    "it".format(self.id, queued))
                self._finish(hard=True)
        except Exception as e:
            # Writing to a socket the far end already dropped is the normal
            # way a browser tab closes. It is not the app's problem.
            self._server._log("info",
                              "write to {0} failed: {1}".format(self.id, e))
            self._finish(hard=True)

    def _close_now(self, code, reason):
        if self._closed:
            return
        self._write_frame(OP_CLOSE, _close_payload(code, reason))
        self._finish()

    def _finish(self, hard=False):
        """Mark closed and let go of the transport. Safe to call twice."""
        self._closed = True
        try:
            if hard:
                # abort() drops what is queued; close() flushes it first. Only
                # the graceful path can afford to wait for a peer that may
                # never read again.
                self._writer.transport.abort()
            else:
                self._writer.close()
        except Exception:
            pass


# -------------------------------
# Server
# -------------------------------
class WSServer(object):
    """
    HTTP + WebSocket on one port, in a daemon thread.

    Callbacks (all optional):
        on_open(client)             a socket finished its handshake
        on_message(client, data)    data is str for text frames, bytes for
                                    binary frames
        on_close(client, reason)    fired exactly once per opened client,
                                    whatever the connection died of
        on_log(level, text)         one line of diagnostics; level is "info",
                                    "warn" or "error", so a caller can print
                                    the ones that matter and drop the rest.
                                    Printed with a [web] prefix when not
                                    supplied

    on_open, on_message and on_close run on the server's event-loop thread and
    MUST NOT BLOCK - see the module docstring. An exception out of any of them
    is logged and swallowed; it never takes the connection or the server down.
    """

    def __init__(self, host, port, static_dir, token="",
                 on_open=None, on_message=None, on_close=None, on_log=None):
        self._host = host
        self._port = int(port)
        self._static_dir = static_dir
        # Resolved once, at construction: every request compares against it,
        # and re-resolving per request would let a symlink swapped underneath
        # the directory change the answer mid-session.
        self._static_root = os.path.realpath(static_dir)
        self._token = token or ""
        self._on_open = on_open
        self._on_message = on_message
        self._on_close = on_close
        self._on_log = on_log
        self._max_message = MAX_MESSAGE

        self._loop = None
        self._thread = None
        self._server = None
        self._clients = {}
        self._ready = threading.Event()
        self._error = None

    # -- properties ---------------------------------------------------------
    @property
    def host(self):
        return self._host

    @property
    def port(self):
        """The bound port. Real one, so port=0 is usable."""
        return self._port

    @property
    def url(self):
        host = self._host
        # 0.0.0.0 is a bind address, not a place a browser can go. The launcher
        # opens this string, so hand it something that resolves.
        if host in ("", "0.0.0.0", "::", "*"):
            host = "127.0.0.1"
        text = "http://{0}:{1}/".format(host, self._port)
        if self._token:
            text += "?token=" + urllib.parse.quote(self._token, safe="")
        return text

    @property
    def clients(self):
        """One info dict per connected socket, newest state at call time."""
        return [c.info() for c in list(self._clients.values())]

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        """
        Start the loop thread and return once the port is bound, or raise.

        Returning before the bind would make "address already in use" surface
        later, on some unrelated thread, as a UI that simply never loads.
        """
        if self._thread is not None:
            return
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="wsserver",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(20.0):
            self._thread = None
            raise RuntimeError("web listener did not come up within 20 s")
        if self._error is not None:
            error, self._error = self._error, None
            self._thread = None
            raise error

    def stop(self):
        """Close clients, stop the loop, join the thread. Idempotent."""
        thread, loop = self._thread, self._loop
        self._thread = None
        if thread is None:
            return
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._shutdown)
            except RuntimeError:
                pass
        # A callback calling stop() is running ON this thread; joining it would
        # deadlock. The shutdown above is already scheduled, so let it happen.
        if thread is not threading.current_thread():
            thread.join(timeout=10.0)
            if thread.is_alive():
                self._log("warn",
                          "event loop did not stop within 10 s - leaving "
                          "it to the daemon thread")

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._bind())
        except Exception as e:
            self._error = e
            self._ready.set()
            loop.close()
            return
        self._ready.set()
        try:
            loop.run_forever()
        except Exception as e:
            self._log("error", "event loop stopped: {0}".format(e))
        finally:
            self._drain(loop)

    async def _bind(self):
        self._server = await asyncio.start_server(
            self._handle, self._host, self._port)
        socks = self._server.sockets or []
        if socks:
            self._port = socks[0].getsockname()[1]

    def _shutdown(self):
        if self._server is not None:
            self._server.close()
        for client in list(self._clients.values()):
            client._close_now(1001, "server shutting down")
        # A tick before stopping, so those close frames actually reach the
        # transports. Stopping the loop in this callback would discard them and
        # every browser would report a 1006 abnormal closure instead.
        self._loop.call_later(0.15, self._loop.stop)

    def _drain(self, loop):
        """Cancel what is still pending, then close the loop."""
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass

    # -- app-facing sends ---------------------------------------------------
    def broadcast(self, obj, exclude=None):
        """
        json.dumps `obj` to every client. Safe from any thread.

        `exclude` may be a client id or a WSClient - the sender of a message
        usually does not want its own echo back.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return 0
        try:
            payload = json.dumps(obj).encode("utf-8")
        except (TypeError, ValueError) as e:
            self._log("error",
                      "broadcast dropped, will not encode: {0}".format(e))
            return 0
        skip = getattr(exclude, "id", exclude)
        targets = [c for c in list(self._clients.values()) if c.id != skip]
        for client in targets:
            client._dispatch(OP_TEXT, payload)
        return len(targets)

    def send(self, client_id, obj):
        """json.dumps `obj` to one client id. False if it is already gone."""
        client = self._clients.get(client_id)
        if client is None:
            return False
        return client.send(obj)

    # -- logging ------------------------------------------------------------
    def _log(self, level, text):
        # Two arguments, level first, because that is what app.py hands over:
        # on_log=lambda level, msg: ... printing only "warn" and "error". A
        # one-argument call turned every diagnostic line in this module into
        # "on_log itself failed (missing 1 required positional argument)" and
        # threw the line it was carrying away with it.
        if self._on_log is None:
            print("[web] {0}".format(text))
            return
        try:
            self._on_log(level, text)
        except Exception as e:
            print("[web] on_log itself failed ({0}): {1}".format(e, text))

    def _fire(self, name, callback, *args):
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as e:
            # One handler misbehaving must cost that handler, not the socket
            # and not the other clients.
            self._log("error", "{0} handler failed: {1}".format(name, e))

    # -- connections --------------------------------------------------------
    async def _handle(self, reader, writer):
        remote = "?"
        peer = writer.get_extra_info("peername")
        if peer:
            remote = "{0}:{1}".format(peer[0], peer[1])
        client = None
        reason = "closed"
        try:
            request = await asyncio.wait_for(_read_request(reader),
                                             HEADER_TIMEOUT)
            if request is None:
                return
            method, path, query, headers = request
            if not self._authorized(query, headers):
                self._log("warn", "403 for {0} {1} from {2}".format(
                    method, path, remote))
                await _send_http(writer, 403, b"forbidden: token required")
                return
            if "websocket" in headers.get("upgrade", "").lower():
                client = await self._upgrade(reader, writer, headers, remote)
                if client is None:
                    return
                reason = await self._pump(client, reader)
            else:
                await self._serve_http(writer, method, path)
        except (asyncio.IncompleteReadError, asyncio.CancelledError,
                ConnectionError, asyncio.TimeoutError):
            reason = "disconnected"
        except Exception as e:
            # Whatever one client managed to do, the other sockets and the
            # accept loop carry on.
            reason = "error: {0}".format(e)
            self._log("warn",
                          "connection from {0} failed: {1}".format(remote, e))
        finally:
            if client is not None:
                self._clients.pop(client.id, None)
                client._finish()
                self._fire("on_close", self._on_close, client, reason)
            try:
                writer.close()
            except Exception:
                pass

    def _authorized(self, query, headers):
        # No token means no check at all, which is the documented default for
        # a listener bound to 127.0.0.1.
        if not self._token:
            return True
        supplied = ""
        for pair in query.split("&"):
            name, _, value = pair.partition("=")
            if name == "token":
                supplied = urllib.parse.unquote_plus(value)
                break
        if not supplied:
            auth = headers.get("authorization", "")
            if auth[:7].lower() == "bearer ":
                # Header lines come off the wire decoded latin-1, because HTTP
                # gives them no encoding. Put the bytes back and read them as
                # the utf-8 they were sent as, or a non-ascii token - which the
                # compare below is deliberately written in bytes to support -
                # is mojibake here and only ever matches through ?token=.
                supplied = auth[7:].strip().encode(
                    "latin-1").decode("utf-8", "replace")
        # compare_digest, so a wrong token cannot be found one character at a
        # time by timing the answer. Bytes, because the token may be non-ASCII
        # and the str form of compare_digest raises on that.
        return hmac.compare_digest(supplied.encode("utf-8"),
                                   self._token.encode("utf-8"))

    async def _upgrade(self, reader, writer, headers, remote):
        key = headers.get("sec-websocket-key", "")
        try:
            raw = base64.b64decode(key, validate=True)
        except Exception:
            raw = b""
        if len(raw) != 16:
            self._log("warn", "bad websocket key from {0}".format(remote))
            await _send_http(writer, 400, b"bad Sec-WebSocket-Key")
            return None
        # If an Origin header is present, its host:port must match the Host
        # header. Scheme is deliberately not compared - Host carries none, so
        # there is nothing to compare it against.
        # Non-browser clients send no Origin and are allowed;
        # browsers always send one, so this closes a same-origin attack vector
        # in the already-warned case (non-loopback bind with no token).
        origin = headers.get("origin", "").lower()
        if origin:
            host = headers.get("host", "").lower()
            # Origin looks like "http://example.com:8770"; Host is "example.com:8770".
            # Extract scheme://host:port from Origin and compare to Host.
            try:
                parsed_origin = urllib.parse.urlparse(origin)
                origin_netloc = parsed_origin.netloc.lower()
                if origin_netloc != host:
                    self._log("warn", "403 for websocket from {0} (origin {1} "
                              "does not match host {2})".format(
                                  remote, origin, host))
                    await _send_http(
                        writer, 403,
                        b"forbidden: origin mismatch")
                    return None
            except Exception:
                # A malformed Origin, and the fail-secure path is to reject.
                self._log("warn", "bad origin from {0}".format(remote))
                await _send_http(writer, 403, b"forbidden: invalid origin")
                return None
        accept = base64.b64encode(hashlib.sha1(
            (key + _GUID).encode("ascii")).digest()).decode("ascii")
        writer.write(("HTTP/1.1 101 Switching Protocols\r\n"
                      "Upgrade: websocket\r\n"
                      "Connection: Upgrade\r\n"
                      "Sec-WebSocket-Accept: {0}\r\n"
                      "\r\n").format(accept).encode("ascii"))
        await writer.drain()
        client = WSClient(self, writer, remote)
        self._clients[client.id] = client
        self._fire("on_open", self._on_open, client)
        return client

    async def _pump(self, client, reader):
        """Read frames until the socket ends. Returns why it ended."""
        keepalive = asyncio.ensure_future(self._keepalive(client))
        frag_op = 0
        frag = bytearray()
        try:
            while True:
                fin, opcode, payload = await _read_frame(
                    reader, self._max_message - len(frag))
                client._last_rx = time.monotonic()

                # Control frames are answered HERE, before anything touches
                # `frag`. Routing them through the same path as data is the
                # classic bug in a hand-rolled RFC 6455: a browser's keepalive
                # ping arriving between two fragments of one message gets
                # concatenated into it, and the message is delivered corrupt
                # with nothing anywhere reporting an error.
                if opcode == OP_PING:
                    client._write_frame(OP_PONG, payload)
                    continue
                if opcode == OP_PONG:
                    client._pong_deadline = None
                    continue
                if opcode == OP_CLOSE:
                    if len(payload) == 1:
                        # RFC 6455 5.5.1: a close body is 0 bytes or at least
                        # 2. Echoing the single byte straight back, as this
                        # did, answers a malformed frame with another one.
                        raise _WSError(1002, "close frame with a 1-byte body")
                    code = 1000
                    if len(payload) >= 2:
                        code = struct.unpack(">H", payload[:2])[0]
                    # Through _close_payload, so a peer that sent 1005 or a
                    # code outside the allowed range does not get it handed
                    # back as this server's own protocol violation.
                    client._write_frame(OP_CLOSE, _close_payload(code, ""))
                    client._finish()
                    return "client closed ({0})".format(code)

                if opcode in (OP_TEXT, OP_BINARY):
                    if frag_op:
                        raise _WSError(1002, "data frame inside a fragmented "
                                             "message")
                    if not fin:
                        frag_op = opcode
                        frag.extend(payload)
                        continue
                    self._deliver(client, opcode, payload)
                elif opcode == OP_CONT:
                    if not frag_op:
                        raise _WSError(1002, "continuation with nothing to "
                                             "continue")
                    frag.extend(payload)
                    if fin:
                        self._deliver(client, frag_op, bytes(frag))
                        frag_op = 0
                        frag = bytearray()
                else:
                    raise _WSError(1002,
                                   "reserved opcode 0x{0:x}".format(opcode))
        except _WSError as e:
            self._log("warn", "{0}: {1} - closing {2}".format(
                client.id, e.reason, e.code))
            client._close_now(e.code, e.reason)
            return "protocol error {0}: {1}".format(e.code, e.reason)
        except asyncio.IncompleteReadError:
            return client._drop_reason or "disconnected"
        finally:
            keepalive.cancel()

    def _deliver(self, client, opcode, payload):
        if opcode == OP_TEXT:
            try:
                data = payload.decode("utf-8")
            except UnicodeDecodeError:
                # Closed with the code the RFC has for exactly this, rather
                # than letting the exception unwind into the accept loop.
                raise _WSError(1007, "text frame was not valid utf-8")
        else:
            data = bytes(payload)
        self._fire("on_message", self._on_message, client, data)

    async def _keepalive(self, client):
        """
        Ping an idle client, drop one that stops answering.

        Separate from the read loop on purpose: timing out the read itself
        would cancel a readexactly() in the middle of a frame, and the bytes
        already taken off the stream would be gone - every later frame then
        parses from the wrong offset.
        """
        try:
            while not client._closed:
                await asyncio.sleep(KEEPALIVE_TICK)
                now = time.monotonic()
                if client._pong_deadline is not None:
                    if now > client._pong_deadline:
                        self._log("info", "{0} stopped answering pings - "
                                  "dropping".format(client.id))
                        # Recorded before the abort, because aborting makes the
                        # read loop fail with a plain IncompleteReadError and
                        # on_close would otherwise report this identically to a
                        # tab the user simply closed.
                        client._drop_reason = ("no pong within {0:.0f} s of a "
                                               "ping".format(PONG_TIMEOUT))
                        client._finish(hard=True)
                        return
                elif now - client._last_rx >= PING_INTERVAL:
                    client._write_frame(OP_PING, b"live")
                    client._pong_deadline = now + PONG_TIMEOUT
        except asyncio.CancelledError:
            pass

    # -- static files -------------------------------------------------------
    def _resolve(self, path):
        """Absolute path for a request, or None if it is not ours to serve."""
        rel = urllib.parse.unquote(path)
        if rel in ("", "/"):
            rel = "/index.html"
        rel = rel.lstrip("/")
        # Rejected before the filesystem is touched. Percent-decoding happens
        # first, so %2e%2e%2f is caught here and not after os.path.join has
        # already turned it into a parent directory.
        if "\\" in rel or ":" in rel:
            return None
        if any(part in ("..", ".", "") for part in rel.split("/")):
            return None
        full = os.path.realpath(os.path.join(self._static_root, rel))
        # realpath on both sides, so a symlink or a junction inside the static
        # directory cannot point out of it either.
        if full != self._static_root \
                and not full.startswith(self._static_root + os.sep):
            return None
        if not os.path.isfile(full):
            return None
        return full

    async def _serve_http(self, writer, method, path):
        if method != "GET":
            await _send_http(writer, 404, b"not found")
            return
        if path == "/healthz":
            await _send_http(writer, 200, b'{"status":"ok"}',
                             "application/json; charset=utf-8")
            return
        full = self._resolve(path)
        if full is None:
            await _send_http(writer, 404, b"not found")
            return
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError as e:
            self._log("warn", "could not read {0}: {1}".format(full, e))
            await _send_http(writer, 404, b"not found")
            return
        ext = os.path.splitext(full)[1].lower()
        await _send_http(writer, 200, body,
                         CONTENT_TYPES.get(ext, "application/octet-stream"))

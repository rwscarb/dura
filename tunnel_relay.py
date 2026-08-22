#!/usr/bin/env python3
"""
NAT-traversal relay. A host with no reachable inbound port registers here
over one persistent outbound CONTROL connection; a downloader connects
here too and gets paired with a fresh DATA connection the host dials back
on demand (rathole/frp's rendezvous shape, hand-rolled to match this
repo's own wire protocol instead of pulling in an external tunnel tool).
From the moment a DATA connection is paired with a downloader connection,
this relay does nothing but shovel raw bytes between the two sockets — it
never parses node.py's INFO/LEAVES/FETCH protocol riding inside the
tunnel, same "dumb relay, no opinion on the payload" design as
discovery_relay.py, just for bytes instead of signed JSON events.

Rendezvous protocol — one plain-text first line per connection, then the
socket either stays line-based (REGISTER's control channel) or becomes a
raw pipe (CONNECT/DATA, once paired):

  REGISTER <token>          host -> relay, persistent control connection.
                             token is whatever the host wants to be found
                             under (node.py uses the archive's content_hash).
  CONNECT <token>           downloader -> relay, wants to reach that host.
                             Everything sent after this line IS the tunneled
                             protocol, not a relay command.
  NEWSTREAM <stream_id>     relay -> host, over the control connection:
                             "a downloader is waiting, dial back."
  DATA <stream_id>          host -> relay, the dial-back this relay pairs
                             with the waiting CONNECT. Everything after
                             this line is tunneled protocol too.

This relay has zero idea what `token` means — it's purely a lookup key,
same as content_hash is everywhere else in this repo.
"""
import secrets
import socket
import socketserver
import sys
import threading

_registrations = {}    # token -> control socket
_pending_streams = {}  # stream_id -> _Pairing (defined below)
_lock = threading.Lock()


def recv_line(sock):
    """Byte-at-a-time on purpose: CONNECT/DATA connections become a raw
    byte pipe immediately after this first line, and the real client
    (HostConnection.connect_via_tunnel) sends its next protocol command
    without waiting for any ack — a chunked recv() here could swallow
    those bytes along with the handshake line, leaving nothing for pipe()
    to forward. One byte at a time guarantees this stops exactly at the
    newline and never reads into what comes after. Only runs once per
    connection (a ~20-40 byte line), not on the hot path."""
    buf = bytearray()
    while True:
        b = sock.recv(1)
        if not b or b == b'\n':
            break
        buf += b
    return buf.decode().strip()


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle_register(conn, token, quiet):
    with _lock:
        _registrations[token] = conn
    if not quiet:
        print(f"[tunnel] {token[:16]}... registered", flush=True)
    try:
        while True:
            # otherwise-idle control connection — block here purely to
            # detect the host disconnecting, so a dead registration
            # doesn't linger and get handed out to future downloaders
            data = conn.recv(4096)
            if not data:
                break
    finally:
        with _lock:
            if _registrations.get(token) is conn:
                del _registrations[token]
        if not quiet:
            print(f"[tunnel] {token[:16]}... unregistered", flush=True)


class _Pairing:
    """Handshake state for one CONNECT waiting to be matched with the
    host's dial-back DATA connection. `ready` fires once the DATA side
    has arrived; `done` fires once piping has fully finished in both
    directions — both CONNECT's and DATA's handler threads block on the
    relevant event instead of returning early. That's required, not just
    tidy: socketserver's ThreadingMixIn calls shutdown()+close() on a
    request's socket the instant its handle() method returns, so a
    handler that spawns a pipe thread and returns immediately gets its
    own socket killed out from under that thread mid-transfer."""

    def __init__(self):
        self.data_conn = None
        self.ready = threading.Event()
        self.done = threading.Event()


def handle_connect(conn, token):
    with _lock:
        ctrl = _registrations.get(token)
    if ctrl is None:
        try:
            conn.sendall(b'ERR no such host\n')
        except OSError:
            pass
        return

    stream_id = secrets.token_hex(8)
    pairing = _Pairing()
    with _lock:
        _pending_streams[stream_id] = pairing
    try:
        ctrl.sendall(f'NEWSTREAM {stream_id}\n'.encode())
    except OSError:
        with _lock:
            _pending_streams.pop(stream_id, None)
        return

    if not pairing.ready.wait(timeout=15):
        with _lock:
            _pending_streams.pop(stream_id, None)
        return

    # one direction runs inline (blocking this handler thread, which is
    # the whole point), the other runs on a plain thread of our own —
    # NOT a socketserver-managed one, so it's free to keep running past
    # any single handle() call
    data_conn = pairing.data_conn
    t = threading.Thread(target=pipe, args=(data_conn, conn), daemon=True)
    t.start()
    pipe(conn, data_conn)
    t.join()
    pairing.done.set()


def handle_data(conn, stream_id):
    with _lock:
        pairing = _pending_streams.pop(stream_id, None)
    if pairing is None:
        return
    pairing.data_conn = conn
    pairing.ready.set()
    # block here until the CONNECT side has fully finished piping both
    # directions on `conn` — see _Pairing's docstring for why
    pairing.done.wait()


class TunnelHandler(socketserver.BaseRequestHandler):
    def handle(self):
        line = recv_line(self.request)
        parts = line.split()
        if len(parts) < 2:
            self.request.close()
            return
        cmd, token = parts[0], parts[1]
        if cmd == 'REGISTER':
            handle_register(self.request, token, getattr(self.server, 'quiet', False))
        elif cmd == 'CONNECT':
            handle_connect(self.request, token)
        elif cmd == 'DATA':
            handle_data(self.request, token)
        else:
            self.request.close()


class TunnelServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_tunnel_relay(port, bind_host='0.0.0.0', quiet=False):
    srv = TunnelServer((bind_host, port), TunnelHandler)
    srv.quiet = quiet
    if not quiet:
        print(f"[tunnel:{port}] up, no opinion on the payload, just pairs sockets and shovels bytes",
              flush=True)
    srv.serve_forever()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9199
    run_tunnel_relay(port)


if __name__ == '__main__':
    main()

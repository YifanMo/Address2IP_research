#!/usr/bin/env python3
import argparse
import json
import socket
import socketserver
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Optional
from urllib.parse import parse_qs, urlsplit


QTYPE_NAMES = {
    1: "A",
    2: "NS",
    5: "CNAME",
    12: "PTR",
    15: "MX",
    16: "TXT",
    28: "AAAA",
    33: "SRV",
    65: "HTTPS",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def qtype_name(value: int) -> str:
    return QTYPE_NAMES.get(value, str(value))


def domain_matches(domain: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    domain = domain.rstrip(".").lower()
    for pattern in patterns:
        needle = pattern.rstrip(".").lower()
        if domain == needle or domain.endswith("." + needle):
            return True
    return False


def parse_qname(packet: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped = False
    original_next = offset
    seen_offsets: set[int] = set()

    while True:
        if offset >= len(packet):
            raise ValueError("DNS name exceeds packet length")
        length = packet[offset]

        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise ValueError("truncated compressed DNS name")
            pointer = ((length & 0x3F) << 8) | packet[offset + 1]
            if pointer in seen_offsets:
                raise ValueError("DNS compression loop detected")
            seen_offsets.add(pointer)
            if not jumped:
                original_next = offset + 2
                jumped = True
            offset = pointer
            continue

        if length == 0:
            offset += 1
            if not jumped:
                original_next = offset
            break

        if length & 0xC0:
            raise ValueError("invalid DNS label length")
        offset += 1
        end = offset + length
        if end > len(packet):
            raise ValueError("truncated DNS label")
        labels.append(packet[offset:end].decode("ascii", errors="replace"))
        offset = end

    return ".".join(labels), original_next


def parse_questions(packet: bytes) -> list[dict]:
    if len(packet) < 12:
        raise ValueError("DNS packet is too short")
    qdcount = int.from_bytes(packet[4:6], "big")
    offset = 12
    questions: list[dict] = []

    for _ in range(qdcount):
        domain, offset = parse_qname(packet, offset)
        if offset + 4 > len(packet):
            raise ValueError("truncated DNS question")
        qtype = int.from_bytes(packet[offset : offset + 2], "big")
        qclass = int.from_bytes(packet[offset + 2 : offset + 4], "big")
        offset += 4
        questions.append(
            {
                "domain": domain,
                "qtype": qtype_name(qtype),
                "qtype_code": qtype,
                "qclass": qclass,
            }
        )

    return questions


def response_rcode(packet: bytes) -> Optional[int]:
    if len(packet) < 4:
        return None
    return packet[3] & 0x0F


def answer_count(packet: bytes) -> Optional[int]:
    if len(packet) < 8:
        return None
    return int.from_bytes(packet[6:8], "big")


def build_servfail(query: bytes) -> bytes:
    if len(query) < 12:
        return b""
    response = bytearray(query)
    response[2] = 0x81
    response[3] = 0x82
    response[6:8] = b"\x00\x00"
    response[8:10] = b"\x00\x00"
    response[10:12] = b"\x00\x00"
    return bytes(response)


class DnsHitStore:
    def __init__(self, max_records: int) -> None:
        self.max_records = max_records
        self._lock = Lock()
        self._next_id = 1
        self._records: list[dict] = []

    def add(self, record: dict) -> dict:
        with self._lock:
            saved = dict(record)
            saved["id"] = self._next_id
            self._next_id += 1
            self._records.append(saved)
            if len(self._records) > self.max_records:
                self._records = self._records[-self.max_records :]
            return dict(saved)

    def clear(self) -> None:
        with self._lock:
            self._records = []

    def list(
        self,
        client_ip: str = "",
        domain: str = "",
        qtype: str = "",
        text: str = "",
        limit: int = 200,
    ) -> list[dict]:
        with self._lock:
            records = list(self._records)

        client_ip = client_ip.strip()
        domain = domain.strip().lower()
        qtype = qtype.strip().upper()
        text = text.strip().lower()

        if client_ip:
            records = [item for item in records if item.get("client_ip") == client_ip]
        if domain:
            records = [item for item in records if domain in item.get("domain", "").lower()]
        if qtype:
            records = [item for item in records if item.get("qtype", "").upper() == qtype]
        if text:
            filtered = []
            for item in records:
                haystack = json.dumps(item, ensure_ascii=False).lower()
                if text in haystack:
                    filtered.append(item)
            records = filtered

        return list(reversed(records[-max(limit, 1) :]))

    def summary(self) -> dict:
        with self._lock:
            records = list(self._records)

        unique_ips: set[str] = set()
        domains: dict[str, int] = {}
        qtypes: dict[str, int] = {}
        latest = None

        for item in records:
            if item.get("client_ip"):
                unique_ips.add(item["client_ip"])
            if item.get("domain"):
                domains[item["domain"]] = domains.get(item["domain"], 0) + 1
            if item.get("qtype"):
                qtypes[item["qtype"]] = qtypes.get(item["qtype"], 0) + 1
            latest = item.get("timestamp")

        top_domains = sorted(domains.items(), key=lambda pair: (-pair[1], pair[0]))[:8]
        return {
            "total_queries": len(records),
            "unique_client_ips": len(unique_ips),
            "unique_domains": len(domains),
            "latest_timestamp": latest,
            "qtypes": qtypes,
            "top_domains": top_domains,
            "max_records": self.max_records,
        }


class DnsMonitorMixin:
    store: DnsHitStore
    upstreams: list[tuple[str, int]]
    match_domains: list[str]
    timeout: float
    log_path: Optional[Path]
    log_lock: Lock

    def forward_udp(self, packet: bytes) -> tuple[bytes, str, float]:
        errors: list[str] = []
        for upstream_host, upstream_port in self.upstreams:
            started_at = time.time()
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(self.timeout)
                    sock.sendto(packet, (upstream_host, upstream_port))
                    response, _ = sock.recvfrom(4096)
                    return response, f"{upstream_host}:{upstream_port}", time.time() - started_at
            except OSError as exc:
                errors.append(f"{upstream_host}:{upstream_port} {exc}")
        raise OSError("; ".join(errors) or "all upstream DNS servers failed")

    def forward_tcp(self, packet: bytes) -> tuple[bytes, str, float]:
        errors: list[str] = []
        payload = len(packet).to_bytes(2, "big") + packet
        for upstream_host, upstream_port in self.upstreams:
            started_at = time.time()
            try:
                with socket.create_connection((upstream_host, upstream_port), timeout=self.timeout) as sock:
                    sock.settimeout(self.timeout)
                    sock.sendall(payload)
                    length_raw = recv_exact(sock, 2)
                    response_len = int.from_bytes(length_raw, "big")
                    response = recv_exact(sock, response_len)
                    return response, f"{upstream_host}:{upstream_port}", time.time() - started_at
            except OSError as exc:
                errors.append(f"{upstream_host}:{upstream_port} {exc}")
        raise OSError("; ".join(errors) or "all upstream DNS servers failed")

    def emit_record(self, record: dict) -> None:
        saved = self.store.add(record)
        line = json.dumps(saved, ensure_ascii=False)
        with self.log_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            if self.log_path is not None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")

    def log_packet(
        self,
        packet: bytes,
        response: bytes,
        client_ip: str,
        client_port: int,
        protocol: str,
        upstream: str,
        duration: float,
        error: str = "",
    ) -> None:
        try:
            questions = parse_questions(packet)
        except ValueError as exc:
            questions = [{"domain": "", "qtype": "INVALID", "qtype_code": 0, "qclass": 0, "error": str(exc)}]

        for question in questions:
            domain = question.get("domain", "")
            if not domain_matches(domain, self.match_domains):
                continue
            self.emit_record(
                {
                    "timestamp": now_iso(),
                    "client_ip": client_ip,
                    "client_port": client_port,
                    "protocol": protocol,
                    "domain": domain,
                    "qtype": question.get("qtype"),
                    "qtype_code": question.get("qtype_code"),
                    "qclass": question.get("qclass"),
                    "matched": bool(self.match_domains),
                    "upstream": upstream,
                    "rcode": response_rcode(response),
                    "answer_count": answer_count(response),
                    "duration_ms": int(duration * 1000),
                    "error": error,
                }
            )


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise OSError("connection closed while reading DNS TCP response")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class ThreadingUDPServerWithConfig(DnsMonitorMixin, socketserver.ThreadingUDPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        store: DnsHitStore,
        upstreams: list[tuple[str, int]],
        match_domains: list[str],
        timeout: float,
        log_path: Optional[Path],
        log_lock: Lock,
    ):
        super().__init__(server_address, DnsUDPHandler)
        self.store = store
        self.upstreams = upstreams
        self.match_domains = match_domains
        self.timeout = timeout
        self.log_path = log_path
        self.log_lock = log_lock


class ThreadingTCPServerWithConfig(DnsMonitorMixin, socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        store: DnsHitStore,
        upstreams: list[tuple[str, int]],
        match_domains: list[str],
        timeout: float,
        log_path: Optional[Path],
        log_lock: Lock,
    ):
        super().__init__(server_address, DnsTCPHandler)
        self.store = store
        self.upstreams = upstreams
        self.match_domains = match_domains
        self.timeout = timeout
        self.log_path = log_path
        self.log_lock = log_lock


class DnsUDPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        packet, sock = self.request
        client_ip, client_port = self.client_address
        started_at = time.time()
        upstream = ""
        error = ""
        try:
            response, upstream, duration = self.server.forward_udp(packet)
        except OSError as exc:
            response = build_servfail(packet)
            duration = time.time() - started_at
            error = str(exc)
        if response:
            sock.sendto(response, self.client_address)
        self.server.log_packet(
            packet=packet,
            response=response,
            client_ip=client_ip,
            client_port=client_port,
            protocol="udp",
            upstream=upstream,
            duration=duration,
            error=error,
        )


class DnsTCPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client_ip, client_port = self.client_address
        started_at = time.time()
        upstream = ""
        error = ""
        response = b""
        try:
            length_raw = recv_exact(self.request, 2)
            packet_len = int.from_bytes(length_raw, "big")
            packet = recv_exact(self.request, packet_len)
            response, upstream, duration = self.server.forward_tcp(packet)
        except OSError as exc:
            packet = b""
            response = b""
            duration = time.time() - started_at
            error = str(exc)
        if response:
            self.request.sendall(len(response).to_bytes(2, "big") + response)
        if packet:
            self.server.log_packet(
                packet=packet,
                response=response,
                client_ip=client_ip,
                client_port=client_port,
                protocol="tcp",
                upstream=upstream,
                duration=duration,
                error=error,
            )


class DnsDashboardServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        static_dir: Path,
        store: DnsHitStore,
        dns_host: str,
        dns_port: int,
        match_domains: list[str],
    ):
        super().__init__(server_address, DnsDashboardHandler)
        self.static_dir = static_dir
        self.store = store
        self.dns_host = dns_host
        self.dns_port = dns_port
        self.match_domains = match_domains


class DnsDashboardHandler(BaseHTTPRequestHandler):
    server_version = "DnsMonitorDashboard/1.0"

    def _write_bytes(self, status_code: int, content_type: str, content: bytes) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _write_json(self, status_code: int, payload: dict) -> None:
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._write_bytes(status_code, "application/json; charset=utf-8", content)

    def _serve_static(self, filename: str, content_type: str) -> None:
        target = self.server.static_dir / filename
        if not target.exists():
            self._write_json(404, {"ok": False, "error": "static file not found"})
            return
        self._write_bytes(200, content_type, target.read_bytes())

    def _serve_queries_api(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        client_ip = query.get("ip", [""])[0]
        domain = query.get("domain", [""])[0]
        qtype = query.get("qtype", [""])[0]
        text = query.get("text", [""])[0]
        try:
            limit = int(query.get("limit", ["200"])[0])
        except ValueError:
            limit = 200

        self._write_json(
            200,
            {
                "ok": True,
                "items": self.server.store.list(
                    client_ip=client_ip,
                    domain=domain,
                    qtype=qtype,
                    text=text,
                    limit=limit,
                ),
                "summary": self.server.store.summary(),
            },
        )

    def _handle_ui_routes(self) -> bool:
        route = urlsplit(self.path).path
        if route == "/":
            self._serve_static("dns-monitor.html", "text/html; charset=utf-8")
            return True
        if route == "/dns-monitor.js":
            self._serve_static("dns-monitor.js", "application/javascript; charset=utf-8")
            return True
        if route == "/styles.css":
            self._serve_static("styles.css", "text/css; charset=utf-8")
            return True
        if route == "/api/queries":
            self._serve_queries_api()
            return True
        if route == "/api/config":
            self._write_json(
                200,
                {
                    "ok": True,
                    "config": {
                        "dns_host": self.server.dns_host,
                        "dns_port": self.server.dns_port,
                        "match_domains": self.server.match_domains,
                    },
                },
            )
            return True
        if route == "/favicon.ico":
            self._write_bytes(204, "image/x-icon", b"")
            return True
        return False

    def do_GET(self) -> None:
        if self._handle_ui_routes():
            return
        self._write_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        route = urlsplit(self.path).path
        if route == "/api/clear":
            self.server.store.clear()
            self._write_json(200, {"ok": True, "summary": self.server.store.summary()})
            return
        self._write_json(404, {"ok": False, "error": "not found"})

    def log_message(self, format: str, *args) -> None:
        return


def parse_upstream(value: str) -> tuple[str, int]:
    if ":" in value and value.rsplit(":", 1)[1].isdigit():
        host, port = value.rsplit(":", 1)
        return host, int(port)
    return value, 53


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor DNS queries by acting as a DNS forwarding server."
    )
    parser.add_argument("--listen-host", default="0.0.0.0", help="DNS bind host. Default: 0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=53, help="DNS bind port. Default: 53")
    parser.add_argument(
        "--upstream",
        action="append",
        default=[],
        help="Upstream DNS server, for example 10.8.8.8 or 223.5.5.5:53. Repeatable.",
    )
    parser.add_argument(
        "--match-domain",
        action="append",
        default=[],
        help="Only log this domain and subdomains. Repeatable. Omit to log all DNS queries.",
    )
    parser.add_argument("--timeout", type=float, default=3.0, help="Upstream timeout seconds. Default: 3")
    parser.add_argument("--log-file", help="Optional JSONL output file.")
    parser.add_argument("--max-records", type=int, default=2000, help="Dashboard memory limit. Default: 2000")
    parser.add_argument(
        "--dashboard-host",
        default="127.0.0.1",
        help="Dashboard bind host. Use 0.0.0.0 to open it from LAN devices.",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=18092,
        help="Dashboard bind port. Set to 0 to disable. Default: 18092",
    )
    parser.add_argument(
        "--udp-only",
        action="store_true",
        help="Only start UDP DNS. By default both UDP and TCP DNS are started.",
    )
    return parser.parse_args()


def start_threaded_server(server) -> Thread:
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def main() -> int:
    args = parse_args()
    upstreams = [parse_upstream(item) for item in args.upstream] or [("10.8.8.8", 53), ("10.8.4.4", 53)]
    store = DnsHitStore(max_records=max(args.max_records, 1))
    log_lock = Lock()
    log_path = Path(args.log_file) if args.log_file else None
    base_dir = Path(__file__).resolve().parent
    static_dir = base_dir / "web"

    servers = []
    threads = []
    try:
        udp_server = ThreadingUDPServerWithConfig(
            server_address=(args.listen_host, args.listen_port),
            store=store,
            upstreams=upstreams,
            match_domains=args.match_domain,
            timeout=max(args.timeout, 0.5),
            log_path=log_path,
            log_lock=log_lock,
        )
        servers.append(udp_server)
        threads.append(start_threaded_server(udp_server))

        if not args.udp_only:
            tcp_server = ThreadingTCPServerWithConfig(
                server_address=(args.listen_host, args.listen_port),
                store=store,
                upstreams=upstreams,
                match_domains=args.match_domain,
                timeout=max(args.timeout, 0.5),
                log_path=log_path,
                log_lock=log_lock,
            )
            servers.append(tcp_server)
            threads.append(start_threaded_server(tcp_server))

        if args.dashboard_port > 0:
            dashboard_server = DnsDashboardServer(
                server_address=(args.dashboard_host, args.dashboard_port),
                static_dir=static_dir,
                store=store,
                dns_host=args.listen_host,
                dns_port=args.listen_port,
                match_domains=args.match_domain,
            )
            servers.append(dashboard_server)
            threads.append(start_threaded_server(dashboard_server))
    except OSError as exc:
        for server in servers:
            server.server_close()
        print(f"failed to start DNS monitor: {exc}", file=sys.stderr)
        return 2

    upstream_text = ", ".join(f"{host}:{port}" for host, port in upstreams)
    match_text = ", ".join(args.match_domain) if args.match_domain else "all domains"
    print(f"DNS monitor listening on {args.listen_host}:{args.listen_port} UDP{' only' if args.udp_only else '/TCP'}")
    print(f"upstream DNS: {upstream_text}")
    print(f"logging: {match_text}")
    if args.dashboard_port > 0:
        print(f"dashboard: http://{args.dashboard_host}:{args.dashboard_port}/")
    sys.stdout.flush()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 130
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=1.0)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

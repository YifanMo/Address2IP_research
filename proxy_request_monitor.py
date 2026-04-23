#!/usr/bin/env python3
import argparse
import http.client
import json
import select
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlsplit


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def split_host_port(value: str, default_port: int) -> Tuple[str, int]:
    raw = value.strip()
    if not raw:
        raise ValueError("host is empty")

    if raw.startswith("[") and "]" in raw:
        host, _, tail = raw[1:].partition("]")
        if tail.startswith(":"):
            return host, int(tail[1:])
        return host, default_port

    if raw.count(":") == 1:
        host, port_text = raw.rsplit(":", 1)
        if port_text.isdigit():
            return host, int(port_text)
    return raw, default_port


def read_body(handler: BaseHTTPRequestHandler) -> bytes:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return b""
    return handler.rfile.read(length)


class HitStore:
    def __init__(self, max_records: int) -> None:
        self.max_records = max_records
        self._lock = Lock()
        self._next_id = 1
        self._records: List[dict] = []

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
        host: str = "",
        mode: str = "",
        text: str = "",
        limit: int = 200,
    ) -> List[dict]:
        with self._lock:
            records = list(self._records)

        client_ip = client_ip.strip()
        host = host.strip().lower()
        mode = mode.strip().lower()
        text = text.strip().lower()

        if client_ip:
            records = [item for item in records if item.get("client_ip", "") == client_ip]
        if host:
            records = [item for item in records if host in item.get("host", "").lower()]
        if mode:
            records = [item for item in records if item.get("mode", "").lower() == mode]
        if text:
            filtered = []
            for item in records:
                haystack = json.dumps(
                    {
                        "client_ip": item.get("client_ip"),
                        "host": item.get("host"),
                        "url": item.get("url"),
                        "path": item.get("path"),
                        "method": item.get("method"),
                        "mode": item.get("mode"),
                        "note": item.get("note"),
                    },
                    ensure_ascii=False,
                ).lower()
                if text in haystack:
                    filtered.append(item)
            records = filtered

        return list(reversed(records[-max(limit, 1) :]))

    def summary(self) -> dict:
        with self._lock:
            records = list(self._records)

        modes: Dict[str, int] = {}
        hosts: Dict[str, int] = {}
        unique_ips: Set[str] = set()
        latest = None

        for item in records:
            mode = item.get("mode", "unknown")
            host = item.get("host", "")
            modes[mode] = modes.get(mode, 0) + 1
            if host:
                hosts[host] = hosts.get(host, 0) + 1
            if item.get("client_ip"):
                unique_ips.add(item["client_ip"])
            latest = item.get("timestamp")

        top_hosts = sorted(hosts.items(), key=lambda pair: (-pair[1], pair[0]))[:5]
        return {
            "total_hits": len(records),
            "unique_client_ips": len(unique_ips),
            "unique_hosts": len(hosts),
            "latest_timestamp": latest,
            "modes": modes,
            "top_hosts": top_hosts,
            "max_records": self.max_records,
        }


@dataclass
class MatchRules:
    hosts: List[str]
    path_keywords: List[str]
    url_keywords: List[str]

    def host_matches(self, host: str) -> bool:
        host = host.lower()
        for item in self.hosts:
            needle = item.lower()
            if host == needle or host.endswith("." + needle):
                return True
        return False

    def should_log_http(self, host: str, path: str, full_url: str) -> bool:
        checks: List[bool] = []
        if self.hosts:
            checks.append(self.host_matches(host))
        if self.path_keywords:
            checks.append(any(item in path for item in self.path_keywords))
        if self.url_keywords:
            checks.append(any(item in full_url for item in self.url_keywords))
        return all(checks) if checks else True

    def should_log_connect(self, host: str) -> bool:
        return self.host_matches(host) if self.hosts else True


class ProxyMonitorServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: Tuple[str, int],
        match_rules: MatchRules,
        timeout: float,
        insecure_upstream: bool,
        log_path: Optional[Path],
        store: HitStore,
    ):
        super().__init__(server_address, ProxyMonitorHandler)
        self.match_rules = match_rules
        self.timeout = timeout
        self.insecure_upstream = insecure_upstream
        self.log_path = log_path
        self.store = store
        self._log_lock = Lock()

    def emit_record(self, record: dict) -> None:
        saved = self.store.add(record)
        line = json.dumps(saved, ensure_ascii=False)
        with self._log_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            if self.log_path is not None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")


class ProxyDashboardServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: Tuple[str, int],
        static_dir: Path,
        store: HitStore,
        proxy_host: str,
        proxy_port: int,
    ):
        super().__init__(server_address, ProxyDashboardHandler)
        self.static_dir = static_dir
        self.store = store
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port


class ProxyDashboardHandler(BaseHTTPRequestHandler):
    server_version = "ProxyMonitorDashboard/1.0"

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

    def _serve_hits_api(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        client_ip = query.get("ip", [""])[0]
        host = query.get("host", [""])[0]
        mode = query.get("mode", [""])[0]
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
                    host=host,
                    mode=mode,
                    text=text,
                    limit=limit,
                ),
                "summary": self.server.store.summary(),
            },
        )

    def _handle_ui_routes(self) -> bool:
        route = urlsplit(self.path).path
        if route == "/":
            self._serve_static("proxy-monitor.html", "text/html; charset=utf-8")
            return True
        if route == "/proxy-monitor.js":
            self._serve_static("proxy-monitor.js", "application/javascript; charset=utf-8")
            return True
        if route == "/styles.css":
            self._serve_static("styles.css", "text/css; charset=utf-8")
            return True
        if route == "/api/hits":
            self._serve_hits_api()
            return True
        if route == "/api/summary":
            self._write_json(200, {"ok": True, "summary": self.server.store.summary()})
            return True
        if route == "/api/config":
            self._write_json(
                200,
                {
                    "ok": True,
                    "config": {
                        "proxy_host": self.server.proxy_host,
                        "proxy_port": self.server.proxy_port,
                        "dashboard_origin": f"http://{self.headers.get('Host', '')}",
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
            read_body(self)
            self.server.store.clear()
            self._write_json(200, {"ok": True, "summary": self.server.store.summary()})
            return
        self._write_json(404, {"ok": False, "error": "not found"})

    def do_DELETE(self) -> None:
        route = urlsplit(self.path).path
        if route == "/api/clear":
            self.server.store.clear()
            self._write_json(200, {"ok": True, "summary": self.server.store.summary()})
            return
        self._write_json(404, {"ok": False, "error": "not found"})

    def log_message(self, format: str, *args) -> None:
        return


class ProxyMonitorHandler(BaseHTTPRequestHandler):
    server_version = "ProxyRequestMonitor/1.0"
    protocol_version = "HTTP/1.1"

    def _build_http_target(self) -> Tuple[str, str, int, str, str]:
        parsed = urlsplit(self.path)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            scheme = parsed.scheme.lower()
            host = parsed.hostname
            port = parsed.port or (443 if scheme == "https" else 80)
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            return scheme, host, port, path, self.path

        host_header = self.headers.get("Host", "")
        if not host_header:
            raise ValueError("missing Host header")
        host, port = split_host_port(host_header, 80)
        path = self.path or "/"
        if not path.startswith("/"):
            path = "/" + path
        full_url = f"http://{host_header}{path}"
        return "http", host, port, path, full_url

    def _upstream_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        for name, value in self.headers.items():
            if name.lower() in HOP_BY_HOP_HEADERS:
                continue
            headers[name] = value
        return headers

    def _forward_http(self) -> None:
        started_at = time.time()
        client_ip, client_port = self.client_address
        body = read_body(self)
        try:
            scheme, host, port, path, full_url = self._build_http_target()
        except ValueError as exc:
            self.send_error(400, explain=str(exc))
            return

        headers = self._upstream_headers()
        connection_kwargs = {"timeout": self.server.timeout}
        if scheme == "https":
            if self.server.insecure_upstream:
                connection_kwargs["context"] = ssl._create_unverified_context()
            conn = http.client.HTTPSConnection(host, port, **connection_kwargs)
        else:
            conn = http.client.HTTPConnection(host, port, **connection_kwargs)

        try:
            conn.request(self.command, path, body=body, headers=headers)
            response = conn.getresponse()
            response_body = response.read()
        except OSError as exc:
            self.send_error(502, explain=str(exc))
            return
        finally:
            conn.close()

        self.send_response(response.status, response.reason)
        for name, value in response.getheaders():
            lower_name = name.lower()
            if lower_name in HOP_BY_HOP_HEADERS or lower_name == "content-length":
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(response_body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response_body)

        if self.server.match_rules.should_log_http(host=host, path=path, full_url=full_url):
            self.server.emit_record(
                {
                    "timestamp": now_iso(),
                    "mode": "http-proxy",
                    "client_ip": client_ip,
                    "client_port": client_port,
                    "method": self.command,
                    "scheme": scheme,
                    "host": host,
                    "port": port,
                    "path": path,
                    "url": full_url,
                    "request_body_bytes": len(body),
                    "response_status": response.status,
                    "response_body_bytes": len(response_body),
                    "duration_ms": int((time.time() - started_at) * 1000),
                }
            )

    def _tunnel(self, upstream: socket.socket) -> Tuple[int, int]:
        sockets = [self.connection, upstream]
        client_to_server_bytes = 0
        server_to_client_bytes = 0

        self.connection.setblocking(False)
        upstream.setblocking(False)

        while True:
            readable, _, exceptional = select.select(sockets, [], sockets, self.server.timeout)
            if exceptional:
                break
            if not readable:
                continue

            for current in readable:
                try:
                    chunk = current.recv(65536)
                except OSError:
                    return client_to_server_bytes, server_to_client_bytes
                if not chunk:
                    return client_to_server_bytes, server_to_client_bytes

                target = upstream if current is self.connection else self.connection
                target.sendall(chunk)
                if current is self.connection:
                    client_to_server_bytes += len(chunk)
                else:
                    server_to_client_bytes += len(chunk)

        return client_to_server_bytes, server_to_client_bytes

    def do_CONNECT(self) -> None:
        started_at = time.time()
        client_ip, client_port = self.client_address
        try:
            host, port = split_host_port(self.path, 443)
            upstream = socket.create_connection((host, port), timeout=self.server.timeout)
        except OSError as exc:
            self.send_error(502, explain=str(exc))
            return

        self.send_response(200, "Connection Established")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            req_bytes, resp_bytes = self._tunnel(upstream)
        finally:
            upstream.close()

        if self.server.match_rules.should_log_connect(host):
            self.server.emit_record(
                {
                    "timestamp": now_iso(),
                    "mode": "https-connect",
                    "client_ip": client_ip,
                    "client_port": client_port,
                    "method": "CONNECT",
                    "host": host,
                    "port": port,
                    "note": "HTTPS CONNECT can log target host, but not the encrypted URL path.",
                    "client_to_server_bytes": req_bytes,
                    "server_to_client_bytes": resp_bytes,
                    "duration_ms": int((time.time() - started_at) * 1000),
                }
            )

    def do_GET(self) -> None:
        self._forward_http()

    def do_POST(self) -> None:
        self._forward_http()

    def do_PUT(self) -> None:
        self._forward_http()

    def do_DELETE(self) -> None:
        self._forward_http()

    def do_PATCH(self) -> None:
        self._forward_http()

    def do_HEAD(self) -> None:
        self._forward_http()

    def do_OPTIONS(self) -> None:
        self._forward_http()

    def log_message(self, format: str, *args) -> None:
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run an explicit HTTP/HTTPS proxy that forwards traffic and logs which LAN client IP "
            "requested a target host or URL."
        )
    )
    parser.add_argument("--listen-host", default="0.0.0.0", help="Bind host. Default: 0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=18090, help="Bind port. Default: 18090")
    parser.add_argument(
        "--dashboard-host",
        default="127.0.0.1",
        help="Dashboard bind host. Use 0.0.0.0 if you want to open the panel from other devices.",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=18091,
        help="Dashboard bind port. Set to 0 to disable the web panel. Default: 18091",
    )
    parser.add_argument(
        "--match-host",
        action="append",
        default=[],
        help="Only log requests whose target host matches this domain. Repeatable.",
    )
    parser.add_argument(
        "--match-path-contains",
        action="append",
        default=[],
        help="Only log plain HTTP requests whose path contains this text. Repeatable.",
    )
    parser.add_argument(
        "--match-url-contains",
        action="append",
        default=[],
        help="Only log plain HTTP requests whose full URL contains this text. Repeatable.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Upstream socket timeout seconds. Default: 30",
    )
    parser.add_argument(
        "--log-file",
        help="Optional JSONL output file. Matching records are still printed to stdout.",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=1000,
        help="Maximum number of recent matched records kept in memory for the dashboard. Default: 1000",
    )
    parser.add_argument(
        "--insecure-upstream",
        action="store_true",
        help="Disable certificate verification when the proxy itself makes HTTPS upstream requests.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    static_dir = base_dir / "web"
    store = HitStore(max_records=max(args.max_records, 1))

    try:
        proxy_server = ProxyMonitorServer(
            server_address=(args.listen_host, args.listen_port),
            match_rules=MatchRules(
                hosts=args.match_host,
                path_keywords=args.match_path_contains,
                url_keywords=args.match_url_contains,
            ),
            timeout=max(args.timeout, 1.0),
            insecure_upstream=args.insecure_upstream,
            log_path=Path(args.log_file) if args.log_file else None,
            store=store,
        )
    except OSError as exc:
        print(f"failed to start proxy server: {exc}", file=sys.stderr)
        return 2

    dashboard_server = None
    dashboard_thread = None
    if args.dashboard_port > 0:
        try:
            dashboard_server = ProxyDashboardServer(
                server_address=(args.dashboard_host, args.dashboard_port),
                static_dir=static_dir,
                store=store,
                proxy_host=args.listen_host,
                proxy_port=args.listen_port,
            )
        except OSError as exc:
            proxy_server.server_close()
            print(f"failed to start dashboard server: {exc}", file=sys.stderr)
            return 2

        dashboard_thread = Thread(target=dashboard_server.serve_forever, daemon=True)
        dashboard_thread.start()

    sys.stdout.write(
        f"Proxy request monitor listening on {args.listen_host}:{args.listen_port}\n"
    )
    if dashboard_server is not None:
        sys.stdout.write(
            f"dashboard: http://{args.dashboard_host}:{args.dashboard_port}/\n"
        )
    sys.stdout.flush()
    try:
        proxy_server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        proxy_server.server_close()
        if dashboard_server is not None:
            dashboard_server.shutdown()
            dashboard_server.server_close()
        if dashboard_thread is not None:
            dashboard_thread.join(timeout=1.0)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

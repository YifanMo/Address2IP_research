#!/usr/bin/env python3
import argparse
import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Dict, List, Set, Tuple
from urllib.parse import parse_qs, urlsplit


class RequestStore:
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

    def list(self, ip: str = "", method: str = "", text: str = "", limit: int = 200) -> List[dict]:
        with self._lock:
            records = list(self._records)

        ip = ip.strip()
        method = method.strip().upper()
        text = text.strip().lower()

        if ip:
            records = [item for item in records if item["client_ip"] == ip]
        if method:
            records = [item for item in records if item["method"] == method]
        if text:
            filtered = []
            for item in records:
                haystack = json.dumps(
                    {
                        "client_ip": item["client_ip"],
                        "method": item["method"],
                        "raw_path": item["raw_path"],
                        "headers": item["headers"],
                        "body": item["body"],
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

        methods: Dict[str, int] = {}
        unique_ips: Set[str] = set()
        latest = None
        for item in records:
            methods[item["method"]] = methods.get(item["method"], 0) + 1
            unique_ips.add(item["client_ip"])
            latest = item["timestamp"]

        return {
            "total_requests": len(records),
            "unique_client_ips": len(unique_ips),
            "methods": methods,
            "latest_timestamp": latest,
            "max_records": self.max_records,
        }


class RequestRecorderServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address: Tuple[str, int], static_dir: Path, store: RequestStore):
        super().__init__(server_address, RequestRecorderHandler)
        self.static_dir = static_dir
        self.store = store


class RequestRecorderHandler(BaseHTTPRequestHandler):
    server_version = "RequestRecorder/2.0"

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _build_payload(self, body: bytes) -> dict:
        client_ip, client_port = self.client_address
        request_url = urlsplit(self.path)
        try:
            body_text = body.decode("utf-8")
        except UnicodeDecodeError:
            body_text = body.hex()

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "client_ip": client_ip,
            "client_port": client_port,
            "method": self.command,
            "path": request_url.path,
            "query": request_url.query,
            "raw_path": self.path,
            "headers": dict(self.headers.items()),
            "body": body_text,
            "body_length": len(body),
        }

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

    def _serve_requests_api(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        ip = query.get("ip", [""])[0]
        method = query.get("method", [""])[0]
        text = query.get("text", [""])[0]
        try:
            limit = int(query.get("limit", ["200"])[0])
        except ValueError:
            limit = 200

        self._write_json(
            200,
            {
                "ok": True,
                "items": self.server.store.list(ip=ip, method=method, text=text, limit=limit),
                "summary": self.server.store.summary(),
            },
        )

    def _record_request(self) -> None:
        body = self._read_body()
        payload = self._build_payload(body)
        saved = self.server.store.add(payload)
        sys.stdout.write(json.dumps(saved, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        self._write_json(200, {"ok": True, "received": saved})

    def _handle_ui_routes(self) -> bool:
        route = urlsplit(self.path).path
        if route == "/":
            self._serve_static("index.html", "text/html; charset=utf-8")
            return True
        if route == "/app.js":
            self._serve_static("app.js", "application/javascript; charset=utf-8")
            return True
        if route == "/styles.css":
            self._serve_static("styles.css", "text/css; charset=utf-8")
            return True
        if route == "/api/requests":
            self._serve_requests_api()
            return True
        if route == "/api/summary":
            self._write_json(200, {"ok": True, "summary": self.server.store.summary()})
            return True
        if route == "/favicon.ico":
            self._write_bytes(204, "image/x-icon", b"")
            return True
        return False

    def do_GET(self) -> None:
        if self._handle_ui_routes():
            return
        self._record_request()

    def do_POST(self) -> None:
        route = urlsplit(self.path).path
        if route == "/api/clear":
            self._read_body()
            self.server.store.clear()
            self._write_json(200, {"ok": True, "summary": self.server.store.summary()})
            return
        self._record_request()

    def do_PUT(self) -> None:
        self._record_request()

    def do_DELETE(self) -> None:
        route = urlsplit(self.path).path
        if route == "/api/clear":
            self.server.store.clear()
            self._write_json(200, {"ok": True, "summary": self.server.store.summary()})
            return
        self._record_request()

    def log_message(self, format: str, *args) -> None:
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start a local HTTP service with a browser dashboard for request IP inspection."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=18080, help="Bind port. Default: 18080")
    parser.add_argument(
        "--max-records",
        type=int,
        default=500,
        help="Maximum number of recent requests kept in memory. Default: 500",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    static_dir = base_dir / "web"
    store = RequestStore(max_records=max(args.max_records, 1))
    server = RequestRecorderServer((args.host, args.port), static_dir=static_dir, store=store)
    print(f"dashboard: http://{args.host}:{args.port}/", flush=True)
    print(f"capture endpoint example: http://{args.host}:{args.port}/demo", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

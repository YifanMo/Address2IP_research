#!/usr/bin/env python3
import argparse
import http.client
import json
import sys
import time
import urllib.parse
from typing import Dict, Tuple


def parse_header(value: str) -> Tuple[str, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError(f"invalid header format: {value!r}")
    name, raw = value.split(":", 1)
    return name.strip(), raw.strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Send HTTP requests while binding each connection to a specific local source IP."
        )
    )
    parser.add_argument("url", help="Target URL, for example: http://127.0.0.1:18080/demo")
    parser.add_argument(
        "--source-ip",
        action="append",
        required=True,
        help="Local source IP to bind. Repeat this flag to send from multiple addresses.",
    )
    parser.add_argument("--method", default="GET", help="HTTP method. Default: GET")
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        type=parse_header,
        help='Custom header, for example: --header "X-Test: demo"',
    )
    parser.add_argument("--data", default="", help="Request body for POST/PUT/etc.")
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="Number of requests to send per source IP. Default: 1",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="Sleep seconds between requests. Default: 0",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Socket timeout seconds. Default: 5",
    )
    return parser.parse_args()


def make_connection(parsed: urllib.parse.SplitResult, source_ip: str, timeout: float):
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"unsupported scheme: {scheme}")

    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80

    kwargs = {"timeout": timeout, "source_address": (source_ip, 0)}
    if scheme == "https":
        return http.client.HTTPSConnection(parsed.hostname, port, **kwargs)
    return http.client.HTTPConnection(parsed.hostname, port, **kwargs)


def build_path(parsed: urllib.parse.SplitResult) -> str:
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path


def format_os_error(exc: OSError) -> str:
    message = str(exc)
    if exc.errno in {49, 99}:
        return (
            f"{message}. The source IP is not configured on any local interface "
            "or the OS cannot route it from this host."
        )
    return message


def send_once(
    parsed: urllib.parse.SplitResult,
    method: str,
    headers: Dict[str, str],
    body: str,
    timeout: float,
    source_ip: str,
) -> dict:
    conn = make_connection(parsed, source_ip, timeout)
    try:
        conn.request(method=method, url=build_path(parsed), body=body.encode("utf-8"), headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()

    try:
        response_body = raw.decode("utf-8")
    except UnicodeDecodeError:
        response_body = raw.hex()

    return {
        "status": resp.status,
        "reason": resp.reason,
        "response_headers": dict(resp.getheaders()),
        "response_body": response_body,
    }


def main() -> int:
    args = parse_args()
    parsed = urllib.parse.urlsplit(args.url)
    if not parsed.scheme or not parsed.hostname:
        print("invalid URL", file=sys.stderr)
        return 2

    headers = dict(args.header)
    method = args.method.upper()

    for source_ip in args.source_ip:
        for index in range(args.count):
            try:
                result = send_once(
                    parsed=parsed,
                    method=method,
                    headers=headers,
                    body=args.data,
                    timeout=args.timeout,
                    source_ip=source_ip,
                )
                print(
                    json.dumps(
                        {
                            "source_ip": source_ip,
                            "request_index": index + 1,
                            "result": result,
                        },
                        ensure_ascii=False,
                    )
                )
            except OSError as exc:
                print(
                    json.dumps(
                        {
                            "source_ip": source_ip,
                            "request_index": index + 1,
                            "error": format_os_error(exc),
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
            if args.interval > 0 and index + 1 < args.count:
                time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
import argparse
import csv
import http.client
import json
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

RATE_UNITS = [
    "Bps",
    "KBps",
    "KiBps",
    "MBps",
    "MiBps",
    "bps",
    "Kbps",
    "Mbps",
]


def parse_header(value: str) -> Tuple[str, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError(f"invalid header format: {value!r}")
    name, raw = value.split(":", 1)
    name = name.strip()
    if not name:
        raise argparse.ArgumentTypeError(f"header name is empty: {value!r}")
    return name, raw.strip()


def parse_cookie(value: str) -> str:
    value = value.strip()
    if "=" not in value:
        raise argparse.ArgumentTypeError(f"invalid cookie format: {value!r}")
    name, raw = value.split("=", 1)
    if not name.strip():
        raise argparse.ArgumentTypeError(f"cookie name is empty: {value!r}")
    return f"{name.strip()}={raw.strip()}"


def parse_counter(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid counter")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if cleaned.isdigit():
            return int(cleaned)
    raise ValueError(f"counter value is not numeric: {value!r}")


def tokenize_path(path: str) -> List[Union[str, int]]:
    if not path.strip():
        raise ValueError("path is empty")

    tokens: List[Union[str, int]] = []
    index = 0
    while index < len(path):
        char = path[index]
        if char == ".":
            index += 1
            continue
        if char == "[":
            end = path.find("]", index)
            if end < 0:
                raise ValueError(f"invalid path segment: {path!r}")
            raw = path[index + 1 : end].strip()
            if not raw.isdigit():
                raise ValueError(f"list index must be numeric in path: {path!r}")
            tokens.append(int(raw))
            index = end + 1
            continue

        end = index
        while end < len(path) and path[end] not in ".[":
            end += 1
        tokens.append(path[index:end])
        index = end

    return tokens


def extract_path(payload: Any, path: str) -> Any:
    current = payload
    for token in tokenize_path(path):
        if isinstance(token, int):
            if not isinstance(current, list):
                raise KeyError(f"path segment [{token}] expects a list")
            current = current[token]
            continue
        if not isinstance(current, dict):
            raise KeyError(f"path segment {token!r} expects an object")
        current = current[token]
    return current


def build_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid URL: {url!r}")
    return url


def format_bytes(value: int) -> str:
    number = float(value)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    unit_index = 0
    while number >= 1024 and unit_index < len(units) - 1:
        number /= 1024
        unit_index += 1
    return f"{number:.2f} {units[unit_index]}"


def format_mbps(byte_rate: float) -> str:
    return f"{byte_rate * 8 / 1_000_000:.2f} Mbps"


def convert_rate_to_bytes_per_second(value: float, unit: str) -> float:
    if unit == "Bps":
        return value
    if unit == "KBps":
        return value * 1_000
    if unit == "KiBps":
        return value * 1_024
    if unit == "MBps":
        return value * 1_000_000
    if unit == "MiBps":
        return value * 1_048_576
    if unit == "bps":
        return value / 8
    if unit == "Kbps":
        return value * 1_000 / 8
    if unit == "Mbps":
        return value * 1_000_000 / 8
    raise ValueError(f"unsupported rate unit: {unit}")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class Sample:
    timestamp: float
    rx_bytes: int
    tx_bytes: int


def fetch_payload(
    transport: str,
    url: str,
    method: str,
    headers: Dict[str, str],
    body_text: str,
    body: Optional[bytes],
    timeout: float,
    insecure: bool,
) -> Tuple[Dict[str, Any], str]:
    if transport in {"curl", "auto"}:
        try:
            return fetch_payload_with_curl(
                url=url,
                method=method,
                headers=headers,
                body_text=body_text,
                timeout=timeout,
                insecure=insecure,
            )
        except (FileNotFoundError, RuntimeError, ValueError):
            if transport == "curl":
                raise

    request = urllib.request.Request(url=url, data=body, method=method)
    for name, value in headers.items():
        request.add_header(name, value)

    context = ssl._create_unverified_context() if insecure else None
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        text = raw.decode(charset, errors="replace")
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("JSON payload must be an object at the top level")
        return payload, text


def fetch_payload_with_curl(
    url: str,
    method: str,
    headers: Dict[str, str],
    body_text: str,
    timeout: float,
    insecure: bool,
) -> Tuple[Dict[str, Any], str]:
    command = ["curl", "-fsSL", "--max-time", str(timeout), "-X", method]
    if insecure:
        command.append("-k")
    for name, value in headers.items():
        command.extend(["-H", f"{name}: {value}"])
    if body_text or method not in {"GET", "HEAD"}:
        command.extend(["--data-binary", body_text])
    command.append(url)

    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        raise RuntimeError(f"curl failed: {detail}")

    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise ValueError("JSON payload must be an object at the top level")
    return payload, completed.stdout


def build_headers(header_items: List[Tuple[str, str]], cookie_items: List[str]) -> Dict[str, str]:
    headers = dict(header_items)
    if cookie_items:
        cookie_value = "; ".join(cookie_items)
        if "Cookie" in headers:
            headers["Cookie"] = f"{headers['Cookie']}; {cookie_value}"
        else:
            headers["Cookie"] = cookie_value
    return headers


def write_csv_row(
    writer: csv.DictWriter,
    rx_total_bytes: Optional[int],
    tx_total_bytes: Optional[int],
    rx_rate: Optional[float],
    tx_rate: Optional[float],
    status: str,
    mode: str,
    rx_raw_value: Optional[float],
    tx_raw_value: Optional[float],
    raw_unit: str,
) -> None:
    writer.writerow(
        {
            "timestamp": now_iso(),
            "rx_bytes_total": "" if rx_total_bytes is None else rx_total_bytes,
            "tx_bytes_total": "" if tx_total_bytes is None else tx_total_bytes,
            "rx_rate_Bps": "" if rx_rate is None else f"{rx_rate:.2f}",
            "tx_rate_Bps": "" if tx_rate is None else f"{tx_rate:.2f}",
            "status": status,
            "mode": mode,
            "rx_raw_value": "" if rx_raw_value is None else rx_raw_value,
            "tx_raw_value": "" if tx_raw_value is None else tx_raw_value,
            "raw_unit": raw_unit,
        }
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Poll a router or gateway HTTP JSON endpoint that exposes cumulative WAN counters "
            "and convert them into inbound/outbound traffic rates."
        )
    )
    parser.add_argument(
        "url",
        help=(
            "HTTP/HTTPS JSON endpoint that returns cumulative WAN counters, "
            "for example http://192.168.3.1/api/traffic"
        ),
    )
    parser.add_argument(
        "--in-path",
        required=True,
        help='JSON path of the inbound/WAN-down value, for example "data.wan.rx_bytes"',
    )
    parser.add_argument(
        "--out-path",
        required=True,
        help='JSON path of the outbound/WAN-up value, for example "data.wan.tx_bytes"',
    )
    parser.add_argument("--method", default="GET", help="HTTP method. Default: GET")
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        type=parse_header,
        help='Custom header, for example: --header "X-Requested-With: XMLHttpRequest"',
    )
    parser.add_argument(
        "--cookie",
        action="append",
        default=[],
        type=parse_cookie,
        help='Cookie item, for example: --cookie "SessionID=abc123"',
    )
    parser.add_argument(
        "--body",
        default="",
        help="Optional request body for POST/PUT endpoints. Default: empty",
    )
    parser.add_argument(
        "--body-file",
        help="Read request body from a UTF-8 text file instead of --body",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=3.0,
        help="Polling interval seconds. Default: 3",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="HTTP timeout seconds. Default: 5",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="How many samples to collect. 0 means loop forever. Default: 0",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable HTTPS certificate verification for local self-signed router certificates.",
    )
    parser.add_argument(
        "--csv",
        help="Append samples to a CSV file for later charting.",
    )
    parser.add_argument(
        "--print-json-once",
        action="store_true",
        help="Print the first raw JSON response to stderr to help confirm in/out paths.",
    )
    parser.add_argument(
        "--value-mode",
        choices=["counter", "rate"],
        default="counter",
        help=(
            "How to interpret the values found at --in-path/--out-path. "
            "counter means cumulative bytes; rate means current bandwidth. Default: counter"
        ),
    )
    parser.add_argument(
        "--rate-unit",
        choices=RATE_UNITS,
        default="Bps",
        help="Unit of the values when --value-mode=rate. Default: Bps",
    )
    parser.add_argument(
        "--transport",
        choices=["auto", "urllib", "curl"],
        default="auto",
        help="HTTP client transport. Default: auto",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        url = build_url(args.url)
        headers = build_headers(args.header, args.cookie)
        body_text = Path(args.body_file).read_text(encoding="utf-8") if args.body_file else args.body
        body = body_text.encode("utf-8") if body_text or args.method.upper() not in {"GET", "HEAD"} else None
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    csv_writer = None
    csv_handle = None
    if args.csv:
        csv_handle = open(args.csv, "a", newline="", encoding="utf-8")
        csv_writer = csv.DictWriter(
            csv_handle,
                fieldnames=[
                    "timestamp",
                    "rx_bytes_total",
                    "tx_bytes_total",
                    "rx_rate_Bps",
                    "tx_rate_Bps",
                    "status",
                    "mode",
                    "rx_raw_value",
                    "tx_raw_value",
                    "raw_unit",
                ],
            )
        if csv_handle.tell() == 0:
            csv_writer.writeheader()
            csv_handle.flush()

    previous: Optional[Sample] = None
    sample_index = 0
    try:
        while True:
            sample_index += 1
            try:
                payload, raw_text = fetch_payload(
                    transport=args.transport,
                    url=url,
                    method=args.method.upper(),
                    headers=headers,
                    body_text=body_text,
                    body=body,
                    timeout=args.timeout,
                    insecure=args.insecure,
                )
                if args.print_json_once and sample_index == 1:
                    print(raw_text, file=sys.stderr)

                current = Sample(
                    timestamp=time.time(),
                    rx_bytes=parse_counter(extract_path(payload, args.in_path)),
                    tx_bytes=parse_counter(extract_path(payload, args.out_path)),
                )

                if args.value_mode == "rate":
                    status = "ok"
                    rx_rate = convert_rate_to_bytes_per_second(current.rx_bytes, args.rate_unit)
                    tx_rate = convert_rate_to_bytes_per_second(current.tx_bytes, args.rate_unit)
                    line = (
                        f"[{now_iso()}] "
                        f"rx_rate={format_mbps(rx_rate)} ({format_bytes(int(rx_rate))}/s) "
                        f"tx_rate={format_mbps(tx_rate)} ({format_bytes(int(tx_rate))}/s) "
                        f"raw_rx={current.rx_bytes} {args.rate_unit} "
                        f"raw_tx={current.tx_bytes} {args.rate_unit}"
                    )
                else:
                    if previous is None:
                        status = "baseline"
                        rx_rate = None
                        tx_rate = None
                        line = (
                            f"[{now_iso()}] baseline "
                            f"rx_total={format_bytes(current.rx_bytes)} "
                            f"tx_total={format_bytes(current.tx_bytes)}"
                        )
                    else:
                        elapsed = max(current.timestamp - previous.timestamp, 0.001)
                        delta_rx = current.rx_bytes - previous.rx_bytes
                        delta_tx = current.tx_bytes - previous.tx_bytes

                        if delta_rx < 0 or delta_tx < 0:
                            status = "counter_reset"
                            rx_rate = None
                            tx_rate = None
                            line = (
                                f"[{now_iso()}] counter reset detected "
                                f"rx_total={format_bytes(current.rx_bytes)} "
                                f"tx_total={format_bytes(current.tx_bytes)}"
                            )
                        else:
                            status = "ok"
                            rx_rate = delta_rx / elapsed
                            tx_rate = delta_tx / elapsed
                            line = (
                                f"[{now_iso()}] "
                                f"rx_rate={format_mbps(rx_rate)} ({format_bytes(int(rx_rate))}/s) "
                                f"tx_rate={format_mbps(tx_rate)} ({format_bytes(int(tx_rate))}/s) "
                                f"rx_total={format_bytes(current.rx_bytes)} "
                                f"tx_total={format_bytes(current.tx_bytes)}"
                            )

                print(line)
                if csv_writer is not None:
                    write_csv_row(
                        csv_writer,
                        rx_total_bytes=current.rx_bytes if args.value_mode == "counter" else None,
                        tx_total_bytes=current.tx_bytes if args.value_mode == "counter" else None,
                        rx_rate=rx_rate,
                        tx_rate=tx_rate,
                        status=status,
                        mode=args.value_mode,
                        rx_raw_value=current.rx_bytes if args.value_mode == "rate" else None,
                        tx_raw_value=current.tx_bytes if args.value_mode == "rate" else None,
                        raw_unit=args.rate_unit if args.value_mode == "rate" else "bytes",
                    )
                    csv_handle.flush()
                if args.value_mode == "counter":
                    previous = current
            except (
                http.client.RemoteDisconnected,
                KeyError,
                IndexError,
                ValueError,
                urllib.error.HTTPError,
                urllib.error.URLError,
                RuntimeError,
            ) as exc:
                message = f"[{now_iso()}] error: {exc}"
                print(message, file=sys.stderr)
                if csv_writer is not None:
                    write_csv_row(
                        csv_writer,
                        rx_total_bytes=previous.rx_bytes if previous is not None and args.value_mode == "counter" else None,
                        tx_total_bytes=previous.tx_bytes if previous is not None and args.value_mode == "counter" else None,
                        rx_rate=None,
                        tx_rate=None,
                        status=f"error:{exc}",
                        mode=args.value_mode,
                        rx_raw_value=None,
                        tx_raw_value=None,
                        raw_unit=args.rate_unit if args.value_mode == "rate" else "bytes",
                    )
                    csv_handle.flush()

            if args.count > 0 and sample_index >= args.count:
                break
            time.sleep(max(args.interval, 0.1))
    except KeyboardInterrupt:
        return 130
    finally:
        if csv_handle is not None:
            csv_handle.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

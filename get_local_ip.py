#!/usr/bin/env python3
import argparse
import ipaddress
import json
import shutil
import socket
import subprocess
import sys


FAMILY_MAP = {
    "ipv4": [socket.AF_INET],
    "ipv6": [socket.AF_INET6],
    "all": [socket.AF_INET, socket.AF_INET6],
}

PROBE_TARGETS = {
    socket.AF_INET: [("192.0.2.1", 80), ("8.8.8.8", 80)],
    socket.AF_INET6: [("2001:db8::1", 80, 0, 0), ("2001:4860:4860::8888", 80, 0, 0)],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print the preferred local IP address for this machine."
    )
    parser.add_argument(
        "--family",
        choices=sorted(FAMILY_MAP),
        default="ipv4",
        help="Address family to inspect. Default: ipv4",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Print all detected addresses for the selected family.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the result as JSON.",
    )
    parser.add_argument(
        "--include-loopback",
        action="store_true",
        help="Include loopback addresses such as 127.0.0.1 or ::1.",
    )
    return parser.parse_args()


def dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def is_usable_ip(value: str, include_loopback: bool) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False

    if address.is_unspecified:
        return False
    if address.is_loopback and not include_loopback:
        return False
    return True


def detect_preferred_ip(family: int) -> str:
    for target in PROBE_TARGETS.get(family, []):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.connect(target)
                return sock.getsockname()[0]
        except OSError:
            continue
    return ""


def run_command(args: list[str]) -> str:
    command = shutil.which(args[0])
    if command is None:
        return ""

    try:
        completed = subprocess.run(
            [command, *args[1:]],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""

    if completed.returncode != 0:
        return ""
    return completed.stdout


def collect_ifconfig_ips(family: int) -> list[str]:
    output = run_command(["ifconfig"])
    if not output:
        return []

    active: list[str] = []
    others: list[str] = []
    blocks: list[list[str]] = []
    current: list[str] = []

    for line in output.splitlines():
        if line and not line[0].isspace():
            if current:
                blocks.append(current)
            current = [line]
            continue
        if current:
            current.append(line)

    if current:
        blocks.append(current)

    for lines in blocks:
        interface = lines[0].split(":", 1)[0]
        text = "\n".join(lines)
        addresses: list[str] = []

        for raw_line in lines[1:]:
            stripped = raw_line.strip()
            if family == socket.AF_INET and stripped.startswith("inet "):
                parts = stripped.split()
                if len(parts) >= 2:
                    addresses.append(parts[1])
            if family == socket.AF_INET6 and stripped.startswith("inet6 "):
                parts = stripped.split()
                if len(parts) >= 2:
                    addresses.append(parts[1].split("%", 1)[0])

        if not addresses:
            continue

        if "status: active" in text and not interface.startswith("lo"):
            active.extend(addresses)
        else:
            others.extend(addresses)

    return dedupe(active + others)


def collect_candidate_ips(family: int) -> list[str]:
    candidates: list[str] = []
    for host in (socket.gethostname(), socket.getfqdn(), "localhost"):
        try:
            infos = socket.getaddrinfo(host, None, family=family, type=socket.SOCK_DGRAM)
        except socket.gaierror:
            continue
        for info in infos:
            address = info[4][0]
            if address:
                candidates.append(address)
    return dedupe(candidates)


def collect_ips(families: list[int], include_loopback: bool) -> list[str]:
    addresses: list[str] = []
    for family in families:
        preferred = detect_preferred_ip(family)
        if preferred:
            addresses.append(preferred)
        addresses.extend(collect_ifconfig_ips(family))
        addresses.extend(collect_candidate_ips(family))

    return [
        value
        for value in dedupe(addresses)
        if is_usable_ip(value, include_loopback=include_loopback)
    ]


def main() -> int:
    args = parse_args()
    families = FAMILY_MAP[args.family]

    addresses = collect_ips(families, include_loopback=args.include_loopback)
    if not addresses and not args.include_loopback:
        addresses = collect_ips(families, include_loopback=True)

    if not addresses:
        print("failed to detect a local IP address", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "family": args.family,
                    "preferred_ip": addresses[0],
                    "all_ips": addresses,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.all:
        for address in addresses:
            print(address)
        return 0

    print(addresses[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

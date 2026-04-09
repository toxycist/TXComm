#!/usr/bin/env python3
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

INSTALLER_VERSION = "installer"
INSTALLER_HANDLE = "INSTALLER"
WIDTH = 72


class Colors:
    RESET = '\033[0m'
    BOLD = '\033[1m'
    BRIGHT_TEAL = '\033[96m'
    GRAY = '\033[37m'
    DARK_GRAY = '\033[90m'
    BRIGHT_RED = '\033[91m'


def clear_screen():
    subprocess.run('clear' if os.name == 'posix' else 'cls')


def rule(char: str = '-', color: str = Colors.BRIGHT_TEAL) -> str:
    return f"{color}{char * WIDTH}{Colors.RESET}"


def print_system(text: str):
    print(f"{Colors.BRIGHT_TEAL}-- {text} --{Colors.RESET}")


def print_error(text: str):
    print(f"{Colors.BRIGHT_RED}-- {text} --{Colors.RESET}")


def recv_line(sock: socket.socket) -> Optional[str]:
    buffer = bytearray()
    while True:
        chunk = sock.recv(1)
        if not chunk:
            return None
        if chunk == b"\n":
            break
        buffer.extend(chunk)
    return buffer.decode("utf-8").strip()


def recv_exact(sock: socket.socket, size: int) -> Optional[bytes]:
    chunks = []
    received = 0
    while received < size:
        chunk = sock.recv(min(4096, size - received))
        if not chunk:
            return None
        chunks.append(chunk)
        received += len(chunk)
    return b"".join(chunks)


def install_binary(payload: bytes, install_dir: Path, binary_name: str) -> Path:
    target = install_dir / binary_name
    temp = target.with_suffix(target.suffix + ".new")
    backup = target.with_suffix(target.suffix + ".bak")

    with open(temp, "wb") as f:
        f.write(payload)
    os.chmod(temp, 0o755)

    if target.exists():
        os.replace(target, backup)
    os.replace(temp, target)
    return target


def prompt_settings():
    clear_screen()
    print()
    print(rule('=', Colors.BOLD + Colors.BRIGHT_TEAL))
    print(
        f"{Colors.BOLD}{Colors.BRIGHT_TEAL}"
        f"  TXComm  --  INSTALLER"
        f"{Colors.RESET}"
    )
    print(rule('=', Colors.BOLD + Colors.BRIGHT_TEAL))
    print()
    print(f"{Colors.DARK_GRAY}-- Enter installation settings --{Colors.RESET}")
    print()

    host = input(
        f"  {Colors.BRIGHT_TEAL}server address{Colors.RESET} "
        f"{Colors.DARK_GRAY}[localhost]{Colors.RESET} {Colors.BRIGHT_TEAL}: {Colors.RESET}"
    ).strip() or "localhost"
    port_raw = input(
        f"  {Colors.BRIGHT_TEAL}server port{Colors.RESET}    "
        f"{Colors.DARK_GRAY}[1717]{Colors.RESET}      {Colors.BRIGHT_TEAL}: {Colors.RESET}"
    ).strip()
    default_install = str(Path.home() / "Desktop")
    install_raw = input(
        f"  {Colors.BRIGHT_TEAL}install dir{Colors.RESET}    "
        f"{Colors.DARK_GRAY}[{default_install}]{Colors.RESET} {Colors.BRIGHT_TEAL}: {Colors.RESET}"
    ).strip()
    launch_now_raw = input(
        f"  {Colors.BRIGHT_TEAL}launch after install{Colors.RESET} "
        f"{Colors.DARK_GRAY}[y/n]{Colors.RESET} {Colors.BRIGHT_TEAL}: {Colors.RESET}"
    ).strip().lower()

    try:
        port = int(port_raw) if port_raw else 1717
    except ValueError:
        port = 1717

    install_dir = Path(install_raw or default_install).expanduser().resolve()
    launch_now = launch_now_raw not in ("n", "no")
    return host, port, install_dir, launch_now


def main():
    host, port, install_dir, launch_now = prompt_settings()
    if not install_dir.exists():
        print()
        print_error(f"Install directory does not exist: {install_dir}")
        return
    if not install_dir.is_dir():
        print()
        print_error(f"Install path is not a directory: {install_dir}")
        return
    if not os.access(install_dir, os.W_OK):
        print()
        print_error(f"No write permission for install directory: {install_dir}")
        return
    if not os.access(install_dir, os.X_OK):
        print()
        print_error(f"No execute permission for install directory: {install_dir}")
        return
    print()
    print_system(f"Connecting to {host}:{port}")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect((host, port))
        sock.send(f"LOGIN|{INSTALLER_HANDLE}|{INSTALLER_VERSION}".encode("utf-8"))

        first = recv_line(sock)
        if not first:
            print_error("Server closed connection")
            return

        parts = first.split("|", 3)
        msg_type = parts[0]

        if msg_type == "MISMATCH":
            print_system("Initiating the update sequence")
            time.sleep(2)
            second = recv_line(sock)
            if not second:
                print_error("Server closed connection during update")
                return
            parts = second.split("|", 3)
            msg_type = parts[0]

        if msg_type == "UPDATE_BIN" and len(parts) == 4:
            latest_version = parts[1]
            binary_name = parts[2]
            try:
                payload_size = int(parts[3])
            except ValueError:
                print_error("Invalid update size from server")
                return

            payload = recv_exact(sock, payload_size)
            if payload is None:
                print_error("Failed to receive update payload")
                return

            print_system(f"Installing TXComm {latest_version}")
            time.sleep(2)
            target = install_binary(payload, install_dir, binary_name)
            print_system(f"Installed: {target}")
            time.sleep(2)

            if launch_now:
                print_system("Launching TXComm")
                time.sleep(2)
                subprocess.run([str(target)])
            return

        if msg_type == "READY":
            print_system("Server reported no update payload")
            return

        if msg_type == "ERROR":
            print_error(f"Server error: {parts[1] if len(parts) > 1 else 'unknown'}")
            return

        print_error(f"Unexpected server response: {first}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print_error("Interrupted by user")
        raise SystemExit(0)

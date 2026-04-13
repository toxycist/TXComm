#!/usr/bin/env python3
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional
import hashlib

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
    with open(temp, "wb") as f:
        f.write(payload)
    os.chmod(temp, 0o755)

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
    print(f"{Colors.DARK_GRAY}-- enter installation settings --{Colors.RESET}")
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
        f"  {Colors.BRIGHT_TEAL}launch after install(y/n){Colors.RESET} "
        f"{Colors.DARK_GRAY}[n]{Colors.RESET} {Colors.BRIGHT_TEAL}: {Colors.RESET}"
    ).strip().lower()

    try:
        port = int(port_raw) if port_raw else 1717
    except ValueError:
        port = 1717

    install_dir = Path(install_raw or default_install).expanduser().resolve()
    launch_now = launch_now_raw in ("y", "yes")
    return host, port, install_dir, launch_now


def main():
    host, port, install_dir, launch_now = prompt_settings()
    if not install_dir.exists():
        print()
        print_error(f"install directory does not exist: {install_dir}")
        return
    if not install_dir.is_dir():
        print()
        print_error(f"install path is not a directory: {install_dir}")
        return
    if not os.access(install_dir, os.W_OK):
        print()
        print_error(f"no write permission for install directory: {install_dir}")
        return
    if not os.access(install_dir, os.X_OK):
        print()
        print_error(f"no execute permission for install directory: {install_dir}")
        return
    print()
    print_system(f"connecting to {host}:{port}")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect((host, port))
        sock.send(f"LOGIN|{INSTALLER_HANDLE}|{INSTALLER_VERSION}\n".encode("utf-8"))

        first = recv_line(sock)
        if not first:
            print_error("server closed connection")
            return

        parts = first.split("|", 3)
        msg_type = parts[0]

        if msg_type == "MISMATCH":
            print_system("initiating the update sequence")
            time.sleep(2)
            second = recv_line(sock)
            if not second:
                print_error("server closed connection during update")
                return
            parts = second.split("|", 3)
            msg_type = parts[0]

        if msg_type == "UPDATE_BIN":
            latest_version = parts[1]
            binary_name = parts[2]

            try:
                update_size = int(parts[3])
            except ValueError:
                print(f"{Colors.BRIGHT_RED}-- failed receiving update size from the server --{Colors.RESET}")
                return False
            
            update_payload = recv_exact(sock, update_size)
            if update_payload is None:
                print(f"{Colors.BRIGHT_RED}-- failed to download update payload --{Colors.RESET}")
                return False
            else:
                print(f"{Colors.BRIGHT_TEAL}-- update payload downloaded --{Colors.RESET}")

            try:
                checksum_size = int(recv_line(sock))
            except ValueError:
                print(f"{Colors.BRIGHT_RED}-- failed receiving size of file's checksum from the server --{Colors.RESET}")
                print(checksum_size)
                return False

            checksum = recv_exact(sock, checksum_size)
            if checksum is None:
                print(f"{Colors.BRIGHT_RED}-- failed receiving file checksum from the server --{Colors.RESET}")
                return False
            else:
                print(f"{Colors.BRIGHT_TEAL}-- file checksum received --{Colors.RESET}")

            print(f"{Colors.BRIGHT_TEAL}-- comparing checksums... --{Colors.RESET}")
            real_cheksum = hashlib.sha256(update_payload).digest()
            if checksum == real_cheksum:
                print(f"{Colors.BRIGHT_TEAL}-- checksums match --{Colors.RESET}")
            else:
                print(f"{Colors.BRIGHT_RED}-- checksums do not match --{Colors.RESET}")
                return False

            print_system(f"installing txcomm {latest_version}...")
            time.sleep(2)
            target = install_binary(update_payload, install_dir, binary_name)
            print_system(f"installed: {target}")
            time.sleep(2)

            if launch_now:
                print_system("launching txcomm...")
                time.sleep(2)
                subprocess.run([str(target)])
            return

        if msg_type == "READY":
            print_system("server reported no update payload")
            return

        if msg_type == "ERROR":
            print_error(f"server error: {parts[1] if len(parts) > 1 else 'unknown'}")
            return

        print_error(f"unexpected server response: {first}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print_error("interrupted by user")
        raise SystemExit(0)

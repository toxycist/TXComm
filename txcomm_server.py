#!/usr/bin/env python3
import socket
import select
import threading
import json
import os
import re
import sys
import shutil
import subprocess
import textwrap
from urllib.parse import quote, unquote
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Optional
import time
import hashlib
import string

class Colors:
    RESET = '\033[0m'
    BOLD = '\033[1m'
    BRIGHT_TEAL = '\033[96m'
    DARK_GRAY = '\033[90m'
    GRAY = '\033[37m'
    BRIGHT_GREEN = '\033[92m'
    BRIGHT_YELLOW = '\033[93m'
    BRIGHT_RED = '\033[91m'

WIDTH = 84
ALLOWED_CHARACTERS = set(string.ascii_letters + string.digits + "_-")

def validate_name(name: str) -> bool | str:
    for c in name:
        if c not in ALLOWED_CHARACTERS:
            return c
        
    return True

def checksum(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(4096):
            h.update(chunk)
    return h.digest()

def rule(char: str = '-', color: str = Colors.BRIGHT_TEAL) -> str:
    return f"{color}{char * WIDTH}{Colors.RESET}"

def clear_screen():
    subprocess.run('clear' if os.name == 'posix' else 'cls')

def encode_field(value: str) -> str:
    return quote(value or "", safe="")

def decode_field(value: str) -> str:
    return unquote(value or "")

def recv_command(sock: socket.socket, buffer: str, timeout: float = 0.15) -> tuple[str, str]:
    while True:
        if '\n' in buffer:
            line, buffer = buffer.split('\n', 1)
            return line.strip(), buffer

        ready, _, _ = select.select([sock], [], [], timeout)
        if not ready:
            return None, buffer

        chunk = sock.recv(1024)
        if not chunk:
            if buffer:
                line, buffer = buffer, ""
                return line.strip(), buffer
            return '', ''
        buffer += chunk.decode('utf-8', errors='replace')

class Message:
    def __init__(
        self,
        handle: str,
        text: str,
        timestamp: Optional[float] = None,
        color: str = "bright_teal",
        is_system: bool = False
    ):
        self.handle = handle
        self.text = text
        self.timestamp = timestamp or time.time()
        self.color = color or "bright_teal"
        self.is_system = is_system

    def to_dict(self):
        return {
            "handle": self.handle,
            "text": self.text,
            "timestamp": self.timestamp,
            "color": self.color,
            "is_system": self.is_system
        }

    @staticmethod
    def from_dict(d):
        return Message(
            d["handle"],
            d["text"],
            d["timestamp"],
            d.get("color", "bright_teal"),
            d.get("is_system", d.get("handle") == "SYSTEM")
        )


MAX_MEMO_MESSAGES = 200


class Memo:
    def __init__(self, name: str, data_dir: str = "./memos"):
        self.name = name
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
        self.messages: List[Message] = []
        self.users: Set[str] = set()
        self.file_path = self.data_dir / f"{name}.jsonl"
        self.load_messages()

    def load_messages(self):
        """Load chat history from JSONL file, compacting if oversized."""
        if not self.file_path.exists():
            return
        with open(self.file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        self.messages.append(Message.from_dict(json.loads(line)))
                    except Exception:
                        pass  # skip corrupt lines
        if len(self.messages) > MAX_MEMO_MESSAGES:
            self.messages = self.messages[-MAX_MEMO_MESSAGES:]
            self._compact()

    def _compact(self):
        """Rewrite the file keeping only the last MAX_MEMO_MESSAGES messages."""
        self.messages = self.messages[-MAX_MEMO_MESSAGES:]
        tmp = self.file_path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            for msg in self.messages:
                f.write(json.dumps(msg.to_dict()) + '\n')
        os.replace(tmp, self.file_path)

    def add_message(self, handle: str, text: str, color: str = "bright_teal", is_system: bool = False) -> Message:
        """Append a message to the memo, compacting if the file gets too large."""
        msg = Message(handle, text, color=color, is_system=is_system)
        self.messages.append(msg)
        with open(self.file_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(msg.to_dict()) + '\n')
        if len(self.messages) > MAX_MEMO_MESSAGES * 1.5:
            self._compact()
        return msg

    def add_user(self, handle: str):
        """Register a user in this memo"""
        self.users.add(handle)

    def remove_user(self, handle: str):
        """Unregister a user from this memo"""
        self.users.discard(handle)

    def get_recent_messages(self, count: int = 50) -> List[Message]:
        """Get the last N messages"""
        return self.messages[-count:] if self.messages else []


class TXCommServer:
    def __init__(self, host: str = '0.0.0.0', port: int = 1717):
        self.host = host
        self.port = port
        self.memos: Dict[str, Memo] = {}
        self.active_connections: Dict[str, socket.socket] = {}
        self.sessions: Dict[str, Dict[str, Optional[str]]] = {}
        self.lock = threading.Lock()
        self.running = False
        self.base_dir = Path(__file__).resolve().parent
        self.server_client_source_path = self.base_dir / "txcomm_client.py"
        self.memos_dir = self.base_dir / "memos"
        self.log_file_path = self.base_dir / "log.txt"
        self.update_build_dir = self.base_dir / "clients_updating"
        self.server_client_binary_path = self.update_build_dir / "dist" / "txcomm_client"
        self.events: List[str] = []
        self.max_events = 20
        self.server_version = self.get_latest_client_version()
        self.allowed_user_colors = {
            "red", "green", "blue", "yellow", "pink"
        }

    def get_or_create_memo(self, name: str) -> Memo:
        """Get a memo or create it if it doesn't exist"""
        created = False
        with self.lock:
            if name not in self.memos:
                self.memos[name] = Memo(name, data_dir=self.memos_dir)
                created = True
            memo = self.memos[name]
        if created:
            self.log_event(f"created memo: {name}")
        return memo

    def get_session_color(self, client_id: str) -> str:
        with self.lock:
            session = self.sessions.get(client_id)
            if session and session.get("color"):
                return session["color"]
        return "red"

    def colorize_handle(self, handle: str, color_name: str, base_color = Colors.RESET) -> str:
        color_map = {
            "pink": '\033[38;5;213m',
            "yellow": '\033[33m',
            "green": '\033[32m',
            "red": '\033[31m',
            "blue": '\033[34m',
        }
        return f"{color_map.get(color_name, "red")}{handle}{Colors.RESET}{base_color}"

    def normalize_user_color(self, color_name: str) -> str:
        return color_name if color_name in self.allowed_user_colors else "red"

    def resolve_unique_handle(self, requested_handle: str) -> str:
        max_len = 25
        base_handle = requested_handle.strip()[:25] if requested_handle else "user"
        with self.lock:
            used_handles = {
                (session.get("handle") or "").lower()
                for session in self.sessions.values()
                if isinstance(session, dict)
            }

        if base_handle not in used_handles:
            return base_handle

        suffix = 1
        while True:
            suffix_text = f"-{suffix}"
            trimmed_base = base_handle[:max_len - len(suffix_text)]
            candidate = f"{trimmed_base}{suffix_text}"
            if candidate not in used_handles:
                return candidate
            suffix += 1

    def log_event(self, text: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            event_line = f"[{timestamp}] {text}"
            self.events.append(event_line)
            if len(self.events) > self.max_events:
                self.events = self.events[-self.max_events:]
            with self.log_file_path.open('a', encoding='utf-8') as log_file:
                log_file.write(f"{event_line}\n")
        self.draw_dashboard()

    def draw_dashboard(self):
        with self.lock:
            connections = len(self.active_connections)
            in_memo = sum(1 for session in self.sessions.values() if session.get("memo"))
            in_lobby = max(0, connections - in_memo)
            active_memos = sorted({session.get("memo") for session in self.sessions.values() if session.get("memo")})
            latest_events = list(self.events)

        clear_screen()
        print()
        print(rule('=', Colors.BOLD + Colors.BRIGHT_TEAL))
        title_left = "  TXComm Server  --  LIVE OPERATIONS DASHBOARD"
        title_right = f"v{self.server_version}"
        spacer = " " * max(1, WIDTH - len(title_left) - len(title_right))
        print(f"{Colors.BOLD}{Colors.BRIGHT_TEAL}{title_left}{spacer}{title_right}{Colors.RESET}")
        print(rule('=', Colors.BOLD + Colors.BRIGHT_TEAL))
        print(f"{Colors.BRIGHT_TEAL}  host:{Colors.RESET} {self.host}:{self.port}    "
              f"{Colors.BRIGHT_TEAL}connections:{Colors.RESET} {connections}    "
              f"{Colors.BRIGHT_TEAL}in memo:{Colors.RESET} {in_memo}    "
              f"{Colors.BRIGHT_TEAL}in lobby:{Colors.RESET} {in_lobby}")
        if active_memos:
            print(f"{Colors.BRIGHT_TEAL}  active memos:{Colors.RESET} {', '.join(active_memos)}")
        else:
            print(f"{Colors.BRIGHT_TEAL}  active memos:{Colors.RESET} (none)")
        print(rule('-', Colors.BRIGHT_TEAL))
        print(f"{Colors.BOLD}{Colors.BRIGHT_TEAL}  recent events{Colors.RESET}")
        for event in latest_events:
            timestamp_end = event.find(']')
            if event.startswith('[') and timestamp_end != -1:
                timestamp = event[:timestamp_end + 1]
                message = event[timestamp_end + 1:].strip()
                wrapped = textwrap.wrap(
                    message,
                    width=max(10, WIDTH - 16),
                    break_long_words=False,
                    break_on_hyphens=False
                ) or [""]
                print(
                    f"  {Colors.DARK_GRAY}{timestamp}{Colors.RESET} "
                    f"{Colors.BRIGHT_TEAL}{wrapped[0]}{Colors.RESET}"
                )
                for line in wrapped[1:]:
                    print(f"            {Colors.BRIGHT_TEAL}{line}{Colors.RESET}")
            else:
                wrapped = textwrap.wrap(
                    event,
                    width=max(10, WIDTH - 2),
                    break_long_words=False,
                    break_on_hyphens=False
                ) or [""]
                for line in wrapped:
                    print(f"  {Colors.BRIGHT_TEAL}{line}{Colors.RESET}")
        print(rule('-', Colors.BRIGHT_TEAL))
        print(f"{Colors.BRIGHT_TEAL}  press ctrl+c to stop server{Colors.RESET}")

    def list_memos(self) -> List[str]:
        """Return known memos from memory and persisted files."""
        names = set(self.memos.keys())
        if self.memos_dir.exists():
            for file_path in self.memos_dir.glob("*.json"):
                names.add(file_path.stem)
        return sorted(names)

    def emit_system_event(
        self,
        memo_name: str,
        actor_handle: str,
        actor_color: str,
        text: str,
        exclude_client_id: Optional[str] = None
    ):
        """Persist and broadcast a system message inside a memo."""
        memo = self.get_or_create_memo(memo_name)
        memo.add_message(actor_handle, text, actor_color, is_system=True)
        self.broadcast_message(
            memo_name,
            actor_handle,
            text,
            sender_color=actor_color,
            is_system=True,
            exclude_client_id=exclude_client_id
        )

    def broadcast_users_list(
        self,
        memo_name: Optional[str] = None,
        mode: str = "header",
        target_client_id: Optional[str] = None
    ):
        with self.lock:
            users = sorted(
                [
                    (session.get("handle") or "UNKNOWN", session.get("color") or "red")
                    for session in self.sessions.values()
                    if isinstance(session, dict) and session.get("memo") == memo_name
                ],
                key=lambda item: item[0].lower()
            )
            if target_client_id:
                targets = [self.active_connections[target_client_id]] if target_client_id in self.active_connections else []
            else:
                targets = [
                    sock for cid, sock in self.active_connections.items()
                    if cid in self.sessions and self.sessions[cid].get("memo") == memo_name
                ]

        if mode == "header":
            payload = ";".join(f"{handle}:{color}" for handle, color in users)
            packet = f"HERE|{payload}\n".encode('utf-8')
        elif mode == "message":
            room_label = memo_name if memo_name else "lobby"
            if users:
                users_text = ", ".join(
                    f"{self.colorize_handle(handle, color)}{Colors.DARK_GRAY}"
                    for handle, color in users
                )
            else:
                users_text = "(none)"
            text = f"users in {room_label}: {users_text}"
            packet = f"MSG|SYSTEM|{text}|{time.time()}|dark_gray|1\n".encode('utf-8')
        else:
            return

        for sock in targets:
            try:
                sock.send(packet)
            except Exception:
                pass

    def get_latest_client_version(self) -> str:
        if not self.server_client_source_path.exists():
            return "0.0.0"
        try:
            content = self.server_client_source_path.read_text(encoding='utf-8')
            match = re.search(r'CLIENT_VERSION\s*=\s*"([^"]+)"', content)
            return match.group(1) if match else "0.0.0"
        except Exception:
            return "0.0.0"

    def is_version_mismatch(self, client_version: str, latest_version: str) -> bool:
        return (client_version or "").strip() != (latest_version or "").strip()

    def build_client_binary(self) -> bool:
        self.update_build_dir.mkdir(parents=True, exist_ok=True)
        dist_path = self.update_build_dir / "dist"
        work_path = self.update_build_dir / "build"
        spec_path = self.update_build_dir / "spec"
        dist_path.mkdir(parents=True, exist_ok=True)
        work_path.mkdir(parents=True, exist_ok=True)
        spec_path.mkdir(parents=True, exist_ok=True)

        if not self.server_client_source_path.exists():
            self.log_event("build failed: txcomm_client.py not found")
            return False

        venv_python = self.base_dir / ".venv" / "bin" / "python"
        pyinstaller_runner = venv_python if venv_python.exists() else Path(sys.executable)
        module_probe = subprocess.run(
            [str(pyinstaller_runner), "-m", "PyInstaller", "--version"],
            capture_output=True,
            text=True,
            cwd=str(self.base_dir)
        )
        if module_probe.returncode != 0:
            fallback = shutil.which("pyinstaller")
            if fallback:
                build_cmd = [fallback]
            else:
                self.log_event("build failed: pyinstaller is not available")
                return False
        else:
            build_cmd = [str(pyinstaller_runner), "-m", "PyInstaller"]

        build_cmd.extend([
            "--onefile",
            "--name", "txcomm_client",
            "--distpath", str(dist_path),
            "--workpath", str(work_path),
            "--specpath", str(spec_path),
            "--noconfirm",
            str(self.server_client_source_path)
        ])

        self.log_event("building linux client executable for update")
        build_result = subprocess.run(
            build_cmd,
            capture_output=True,
            text=True,
            cwd=str(self.base_dir)
        )
        if build_result.returncode != 0:
            err_line = (build_result.stderr or build_result.stdout or "unknown error").splitlines()
            self.log_event(f"build failed: {err_line[0] if err_line else 'unknown error'}")
            return False

        if not self.server_client_binary_path.exists():
            self.log_event("build failed: executable was not produced")
            return False

        os.chmod(self.server_client_binary_path, 0o755)
        return True
    
    def find_user_by_handle(self, handle):
        for cid, sock in self.active_connections.items():
            session = self.sessions.get(cid)
            if session and session.get("handle") == handle:
                return cid, sock, session
        return None, None, None

    def handle_client(self, client_socket: socket.socket, addr: tuple):
        """Handle a single client connection"""
        client_id = f"{addr[0]}:{addr[1]}"
        user_handle = None
        user_color = None
        authenticated = False
        recv_buffer = ""

        try:
            # Wait for initial handshake: LOGIN|handle|version|color
            data, recv_buffer = recv_command(client_socket, recv_buffer)

            if data == None:
                client_socket.send(b"ERROR|invalid login handshake - no data received\n")
                self.log_event(f"rejected invalid login from {client_id}: no data received")
                return

            parts = data.split('|', 3)

            if parts[0] != 'LOGIN' or len(parts) < 3 or len(parts) > 4 or not parts[1]:
                client_socket.send(b"ERROR|invalid login handshake - malformed login packet\n")
                self.log_event(f"rejected invalid login from {client_id}: malformed login packet")
                return

            requested_handle = parts[1]

            validation_status = validate_name(requested_handle)
            invalid_character_in_handle_error = None
            if validation_status != True:
                requested_handle = "user"
                invalid_character_in_handle_error = f"ERROR|character {validation_status} is not allowed in the handle. your handle was auto-assigned\n"

            client_version = parts[2]
            user_color = self.normalize_user_color(parts[3] if len(parts) == 4 and parts[3] else "red")
            latest_version = self.get_latest_client_version()
            user_handle = self.resolve_unique_handle(requested_handle)

            if self.is_version_mismatch(client_version, latest_version):
                client_socket.send(f"MISMATCH|{client_version}|{latest_version}\n".encode('utf-8'))
                self.log_event(
                    f"version mismatch for {client_id}: {client_version} instead of {latest_version}"
                )
                if not self.build_client_binary():
                    client_socket.send(b"ERROR|server failed to build txcomm_client executable for update\n")
                    self.log_event(f"update failed for {client_id}: build failed")
                    return
                payload = self.server_client_binary_path.read_bytes()
                header = (
                    f"UPDATE_BIN|{latest_version}|{self.server_client_binary_path.name}|{len(payload)}\n"
                ).encode('utf-8')
                binary_checksum = checksum(self.server_client_binary_path)
                binary_checksum_size = (f"{len(binary_checksum)}\n").encode('utf-8')
                client_socket.send(header)
                client_socket.send(payload)
                client_socket.send(binary_checksum_size)
                client_socket.send(binary_checksum)
                self.log_event(f"sent client update to {client_id} ({client_version} -> {latest_version})")
                return

            with self.lock:
                self.active_connections[client_id] = client_socket
                self.sessions[client_id] = {"handle": user_handle, "memo": None, "color": user_color}
            authenticated = True
            self.log_event(f"{client_id} authenticated as {self.colorize_handle(user_handle, user_color)}")

            client_socket.send(f"READY|{user_handle}|logged in as {user_handle}. you are in the lobby\n".encode('utf-8'))
            if invalid_character_in_handle_error:
                client_socket.send(invalid_character_in_handle_error.encode('utf-8'))
            self.broadcast_users_list(None)

            while True:
                data, recv_buffer = recv_command(client_socket, recv_buffer)
                if not data:
                    continue

                if data.startswith('JOIN|'):
                    requested_memo = data[5:].strip().lower()[:30]
                    if not requested_memo:
                        client_socket.send(b"ERROR|memo name cannot be empty\n")
                        continue
                    validation_status = validate_name(requested_memo)
                    if validation_status != True:
                        error_string = f"ERROR|character {validation_status} is not allowed in memo name\n"
                        client_socket.send(error_string.encode('utf-8'))
                        continue
                    if requested_memo == "lobby":
                        client_socket.send(b"ERROR|memo name 'lobby' is reserved\n")
                        continue

                    previous_memo = None
                    with self.lock:
                        session = self.sessions.get(client_id)
                        if session:
                            previous_memo = session.get("memo")

                    if previous_memo == requested_memo:
                        client_socket.send(f"INFO|you are already in memo: {requested_memo}\n".encode('utf-8'))
                        continue

                    memo = self.get_or_create_memo(requested_memo)
                    memo.add_user(user_handle)

                    with self.lock:
                        session = self.sessions.get(client_id)
                        if session:
                            session["memo"] = requested_memo

                    if previous_memo:
                        old_memo = self.memos.get(previous_memo)
                        if old_memo:
                            old_memo.remove_user(user_handle)
                        self.emit_system_event(
                            previous_memo,
                            user_handle,
                            user_color,
                            f"{user_handle} left the memo",
                            exclude_client_id=client_id
                        )
                        self.log_event(
                            f"{self.colorize_handle(user_handle, user_color, base_color = Colors.BRIGHT_TEAL)} left memo {previous_memo}"
                        )

                    self.broadcast_users_list(previous_memo)

                    recent = memo.get_recent_messages(20)
                    for msg in recent:
                        system_bit = 1 if msg.is_system else 0
                        enc_handle = encode_field(msg.handle)
                        enc_text = encode_field(msg.text)
                        enc_color = encode_field(msg.color)
                        client_socket.send(
                            f"MSG|{enc_handle}|{enc_text}|{msg.timestamp}|{enc_color}|{system_bit}\n".encode('utf-8')
                        )
                    client_socket.send(f"JOINED|{requested_memo}\n".encode('utf-8'))
                    self.emit_system_event(requested_memo, user_handle, user_color, f"{user_handle} joined the memo")
                    self.broadcast_users_list(requested_memo)
                    self.log_event(
                        f"{self.colorize_handle(user_handle, user_color, base_color = Colors.BRIGHT_TEAL)} joined memo {requested_memo}"
                    )

                elif data == 'LEAVE':
                    active_memo = None
                    with self.lock:
                        session = self.sessions.get(client_id)
                        if session:
                            active_memo = session.get("memo")
                            session["memo"] = None

                    if active_memo:
                        memo = self.memos.get(active_memo)
                        if memo:
                            memo.remove_user(user_handle)
                        self.emit_system_event(active_memo, user_handle, user_color, f"{user_handle} left the memo")
                        client_socket.send(b"LEFT|lobby\n")
                        self.broadcast_users_list(active_memo)
                        self.broadcast_users_list(None)
                        self.log_event(
                            f"{self.colorize_handle(user_handle, user_color, base_color = Colors.BRIGHT_TEAL)} returned to lobby from {active_memo}"
                        )
                    else:
                        client_socket.send(b"INFO|you are already in the lobby\n")

                elif data == 'MEMOS':
                    rooms = self.list_memos()
                    payload = ",".join(rooms)
                    client_socket.send(f"MEMOS|{payload}\n".encode('utf-8'))

                elif data == 'HERE':
                    active_memo = None
                    with self.lock:
                        session = self.sessions.get(client_id)
                        if session:
                            active_memo = session.get("memo")
                    self.broadcast_users_list(
                        active_memo,
                        mode="message",
                        target_client_id=client_id
                    )

                elif data.startswith("MENTION_COLOR|"):
                    requested = data[14:].strip()

                    if not requested:
                        client_socket.send(b"ERROR|invalid mention color request, could not parse the handle\n")
                        continue

                    with self.lock:
                        cid, sock, session = self.find_user_by_handle(requested)

                        if session:
                            color = session.get("color") or "red"
                            client_socket.send(f"MENTION_COLOR|{requested}|{color}\n".encode())
                        else:
                            client_socket.send(f"MENTION_COLOR|{requested}|\n".encode())


                elif data.startswith("MENTION|"):
                    requested = data[8:].strip()

                    if not requested:
                        client_socket.send(b"ERROR|invalid mention request, could not parse the handle\n")
                        continue

                    with self.lock:
                        cid, sock, session = self.find_user_by_handle(requested)

                        if session:
                            mentioner_session = self.sessions.get(client_id)
                            mentioner_memo = mentioner_session["memo"]

                            color = session.get("color") or "red"

                            text = (
                                f"you were mentioned by "
                                f"{self.colorize_handle(user_handle, user_color, base_color=Colors.DARK_GRAY)} "
                                f"in memo {mentioner_memo}"
                            )

                            packet = f"MSG|SYSTEM|{text}|{time.time()}|dark_gray|1\n".encode()

                            try:
                                sock.send(packet)
                            except:
                                pass

                            client_socket.send(f"MENTION_COLOR|{requested}|{color}\n".encode())
                        else:
                            client_socket.send(f"MENTION_COLOR|{requested}|\n".encode())

                elif data.startswith('ONLINE|'):
                    requested = data[7:].strip()
                    if not requested:
                        client_socket.send(b"ERROR|usage: /online <handle>\n")
                        continue
                    status_message = None
                    status_color = "dark_gray"
                    with self.lock:
                        for session in self.sessions.values():
                            if not isinstance(session, dict):
                                continue
                            handle = session.get("handle") or ""
                            if handle == requested:
                                status_message = f"{handle} is online"
                                status_color = session.get("color") or "red"
                                break
                    if status_message:
                        client_socket.send(f"INFO|{status_message}|{status_color}\n".encode('utf-8'))
                    else:
                        client_socket.send(f"INFO|{requested} is offline\n".encode('utf-8'))

                elif data.startswith('SAY|'):
                    message_text = decode_field(data[4:])
                    active_memo = None
                    with self.lock:
                        session = self.sessions.get(client_id)
                        if session:
                            active_memo = session.get("memo")

                    if not active_memo:
                        client_socket.send(b"ERROR|join a memo before sending messages\n")
                        continue

                    memo = self.get_or_create_memo(active_memo)
                    memo.add_message(user_handle, message_text, user_color, is_system=False)
                    self.broadcast_message(
                        active_memo,
                        user_handle,
                        message_text,
                        sender_color=user_color,
                        is_system=False
                    )
                    self.log_event(
                        f"{self.colorize_handle(user_handle, user_color, base_color = Colors.BRIGHT_TEAL)}@{active_memo}: {message_text}"
                    )

                elif data == 'QUIT':
                    break
                else:
                    client_socket.send(b"ERROR|unknown command\n")

        except Exception as e:
            if user_handle:
                self.log_event(
                    f"Error for {self.colorize_handle(user_handle, self.get_session_color(client_id), base_color = Colors.BRIGHT_TEAL)}({client_id}): {e}"
                )
            else:
                self.log_event(f"error for {client_id}: {e}")

        finally:
            # Cleanup
            disconnect_color = self.get_session_color(client_id)
            if user_handle:
                active_memo = None
                with self.lock:
                    session = self.sessions.get(client_id)
                    if session:
                        active_memo = session.get("memo")
                        session["memo"] = None

                memo = self.memos.get(active_memo) if active_memo else None
                if memo:
                    memo.remove_user(user_handle)
                    self.emit_system_event(active_memo, user_handle, user_color, f"{user_handle} left the memo")
                    self.broadcast_users_list(active_memo)

            with self.lock:
                if client_id in self.active_connections:
                    del self.active_connections[client_id]
                if client_id in self.sessions:
                    del self.sessions[client_id]
            self.broadcast_users_list(None)

            try:
                client_socket.close()
            except Exception:
                pass

            if authenticated and user_handle:
                self.log_event(
                    f"{self.colorize_handle(user_handle, disconnect_color, base_color = Colors.BRIGHT_TEAL)} disconnected"
                )
            else:
                self.log_event(f"{client_id} disconnected")

    def broadcast_message(
        self,
        memo_name: str,
        sender_handle: str,
        text: str,
        sender_color: str = "bright_teal",
        is_system: bool = False,
        exclude_client_id: Optional[str] = None
    ):
        """Broadcast a message to all clients in a memo"""
        with self.lock:
            connections_to_notify = [
                (cid, sock) for cid, sock in self.active_connections.items()
                if (
                    cid in self.sessions
                    and self.sessions[cid].get("memo") == memo_name
                    and cid != exclude_client_id
                )
            ]

        system_bit = 1 if is_system else 0
        enc_handle = encode_field(sender_handle)
        enc_text = encode_field(text)
        enc_color = encode_field(sender_color)
        message = f"MSG|{enc_handle}|{enc_text}|{time.time()}|{enc_color}|{system_bit}\n"
        for client_id, sock in connections_to_notify:
            try:
                sock.send(message.encode('utf-8'))
            except Exception:
                pass

    def start(self):
        """Start the server"""
        self.running = True
        self.server_version = self.get_latest_client_version()
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((self.host, self.port))
        server_socket.listen(5)
        self.log_event(f"server started on {self.host}:{self.port}")

        try:
            while self.running:
                try:
                    client_socket, addr = server_socket.accept()
                    self.log_event(f"new connection from {addr[0]}:{addr[1]}")
                    client_thread = threading.Thread(
                        target=self.handle_client,
                        args=(client_socket, addr),
                        daemon=True
                    )
                    client_thread.start()
                except KeyboardInterrupt:
                    break
        finally:
            server_socket.close()
            self.running = False
            self.log_event("server stopped")


if __name__ == "__main__":
    server = TXCommServer()
    server.start()
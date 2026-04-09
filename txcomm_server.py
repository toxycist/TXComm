#!/usr/bin/env python3
import socket
import threading
import json
import os
import re
import sys
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Optional
import time

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

def rule(char: str = '-', color: str = Colors.BRIGHT_TEAL) -> str:
    return f"{color}{char * WIDTH}{Colors.RESET}"

def clear_screen():
    os.system('clear' if os.name == 'posix' else 'cls')

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


class Chatroom:
    def __init__(self, name: str, data_dir: str = "./chatrooms"):
        self.name = name
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
        self.messages: List[Message] = []
        self.users: Set[str] = set()
        self.file_path = self.data_dir / f"{name}.json"
        self.load_messages()

    def load_messages(self):
        """Load chat history from file"""
        if self.file_path.exists():
            try:
                with open(self.file_path, 'r') as f:
                    data = json.load(f)
                    self.messages = [Message.from_dict(m) for m in data.get("messages", [])]
            except:
                self.messages = []

    def save_messages(self):
        """Save chat history to file"""
        with open(self.file_path, 'w') as f:
            json.dump({
                "messages": [m.to_dict() for m in self.messages],
                "created": self.file_path.stat().st_ctime if self.file_path.exists() else time.time()
            }, f, indent=2)

    def add_message(self, handle: str, text: str, color: str = "bright_teal", is_system: bool = False) -> Message:
        """Add a message to the chatroom"""
        msg = Message(handle, text, color=color, is_system=is_system)
        self.messages.append(msg)
        self.save_messages()
        return msg

    def add_user(self, handle: str):
        """Register a user in this chatroom"""
        self.users.add(handle)

    def remove_user(self, handle: str):
        """Unregister a user from this chatroom"""
        self.users.discard(handle)

    def get_recent_messages(self, count: int = 50) -> List[Message]:
        """Get the last N messages"""
        return self.messages[-count:] if self.messages else []


class TXCommServer:
    def __init__(self, host: str = 'localhost', port: int = 1717):
        self.host = host
        self.port = port
        self.chatrooms: Dict[str, Chatroom] = {}
        self.active_connections: Dict[str, socket.socket] = {}
        self.sessions: Dict[str, Dict[str, Optional[str]]] = {}
        self.lock = threading.Lock()
        self.running = False
        self.base_dir = Path(__file__).resolve().parent
        self.server_client_source_path = self.base_dir / "txcomm_client.py"
        self.chatrooms_dir = self.base_dir / "chatrooms"
        self.update_build_dir = self.base_dir / "clients_updating"
        self.server_client_binary_path = self.update_build_dir / "dist" / "txcomm_client"
        self.events: List[str] = []
        self.max_events = 20
        self.server_version = self.get_latest_client_version()
        self.allowed_user_colors = {
            "red", "green", "blue", "yellow", "pink"
        }

    def get_or_create_chatroom(self, name: str) -> Chatroom:
        """Get a chatroom or create it if it doesn't exist"""
        created = False
        with self.lock:
            if name not in self.chatrooms:
                self.chatrooms[name] = Chatroom(name, data_dir=self.chatrooms_dir)
                created = True
            chatroom = self.chatrooms[name]
        if created:
            self.log_event(f"Created memo: {name}")
        return chatroom

    def get_session_color(self, client_id: str) -> str:
        with self.lock:
            session = self.sessions.get(client_id)
            if session and session.get("color"):
                return session["color"]
        return "blue"

    def colorize_handle(self, handle: str, color_name: str) -> str:
        color_map = {
            "pink": '\033[38;5;213m',
            "yellow": '\033[33m',
            "green": '\033[32m',
            "red": '\033[31m',
            "blue": '\033[34m',
        }
        return f"{color_map.get(color_name, '\033[34m')}{handle}{Colors.BRIGHT_TEAL}"

    def normalize_user_color(self, color_name: str) -> str:
        return color_name if color_name in self.allowed_user_colors else "blue"

    def resolve_unique_handle(self, requested_handle: str) -> str:
        with self.lock:
            used_handles = {
                (session.get("handle") or "").lower()
                for session in self.sessions.values()
                if isinstance(session, dict)
            }

        if requested_handle.lower() not in used_handles:
            return requested_handle

        suffix = 1
        while True:
            candidate = f"{requested_handle}({suffix})"
            if candidate.lower() not in used_handles:
                return candidate
            suffix += 1

    def log_event(self, text: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.events.append(f"[{timestamp}] {text}")
            if len(self.events) > self.max_events:
                self.events = self.events[-self.max_events:]
        self.draw_dashboard()

    def draw_dashboard(self):
        with self.lock:
            connections = len(self.active_connections)
            in_memo = sum(1 for session in self.sessions.values() if session.get("chatroom"))
            in_lobby = max(0, connections - in_memo)
            active_memos = sorted({session.get("chatroom") for session in self.sessions.values() if session.get("chatroom")})
            latest_events = list(self.events)

        clear_screen()
        print()
        print(rule('=', Colors.BOLD + Colors.BRIGHT_TEAL))
        title_left = "  TXComm Server  --  LIVE OPERATIONS DASHBOARD"
        title_right = f"v{self.server_version}"
        spacer = " " * max(1, WIDTH - len(title_left) - len(title_right))
        print(f"{Colors.BOLD}{Colors.BRIGHT_TEAL}{title_left}{spacer}{title_right}{Colors.RESET}")
        print(rule('=', Colors.BOLD + Colors.BRIGHT_TEAL))
        print(f"{Colors.BRIGHT_TEAL}  Host:{Colors.RESET} {self.host}:{self.port}    "
              f"{Colors.BRIGHT_TEAL}Connections:{Colors.RESET} {connections}    "
              f"{Colors.BRIGHT_TEAL}In memo:{Colors.RESET} {in_memo}    "
              f"{Colors.BRIGHT_TEAL}Lobby:{Colors.RESET} {in_lobby}")
        if active_memos:
            print(f"{Colors.BRIGHT_TEAL}  Active memos:{Colors.RESET} {', '.join(active_memos)}")
        else:
            print(f"{Colors.BRIGHT_TEAL}  Active memos:{Colors.RESET} (none)")
        print(rule('-', Colors.BRIGHT_TEAL))
        print(f"{Colors.BOLD}{Colors.BRIGHT_TEAL}  Recent events{Colors.RESET}")
        for event in latest_events:
            timestamp_end = event.find(']')
            if event.startswith('[') and timestamp_end != -1:
                timestamp = event[:timestamp_end + 1]
                message = event[timestamp_end + 1:].strip()
                print(
                    f"  {Colors.DARK_GRAY}{timestamp}{Colors.RESET} "
                    f"{Colors.BRIGHT_TEAL}{message}{Colors.RESET}"
                )
            else:
                print(f"  {Colors.BRIGHT_TEAL}{event}{Colors.RESET}")
        print(rule('-', Colors.BRIGHT_TEAL))
        print(f"{Colors.BRIGHT_TEAL}  Press Ctrl+C to stop server{Colors.RESET}")

    def list_chatrooms(self) -> List[str]:
        """Return known chatrooms from memory and persisted files."""
        names = set(self.chatrooms.keys())
        if self.chatrooms_dir.exists():
            for file_path in self.chatrooms_dir.glob("*.json"):
                names.add(file_path.stem)
        return sorted(names)

    def emit_system_event(
        self,
        chatroom_name: str,
        actor_handle: str,
        actor_color: str,
        text: str,
        exclude_client_id: Optional[str] = None
    ):
        """Persist and broadcast a system message inside a chatroom."""
        chatroom = self.get_or_create_chatroom(chatroom_name)
        chatroom.add_message(actor_handle, text, actor_color, is_system=True)
        self.broadcast_message(
            chatroom_name,
            actor_handle,
            text,
            sender_color=actor_color,
            is_system=True,
            exclude_client_id=exclude_client_id
        )

    def broadcast_users_list(self, chatroom_name: str):
        with self.lock:
            users = sorted(
                [
                    (session.get("handle") or "UNKNOWN", session.get("color") or "red")
                    for session in self.sessions.values()
                    if isinstance(session, dict) and session.get("chatroom") == chatroom_name
                ],
                key=lambda item: item[0].lower()
            )
            targets = [
                sock for cid, sock in self.active_connections.items()
                if cid in self.sessions and self.sessions[cid].get("chatroom") == chatroom_name
            ]

        payload = ";".join(f"{handle}:{color}" for handle, color in users)
        packet = f"USERS|{payload}\n".encode('utf-8')
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
            self.log_event("Build failed: txcomm_client.py not found")
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
                self.log_event("Build failed: PyInstaller is not available")
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

        self.log_event("Building Linux client executable for update")
        build_result = subprocess.run(
            build_cmd,
            capture_output=True,
            text=True,
            cwd=str(self.base_dir)
        )
        if build_result.returncode != 0:
            err_line = (build_result.stderr or build_result.stdout or "unknown error").splitlines()
            self.log_event(f"Build failed: {err_line[0] if err_line else 'unknown error'}")
            return False

        if not self.server_client_binary_path.exists():
            self.log_event("Build failed: executable was not produced")
            return False

        os.chmod(self.server_client_binary_path, 0o755)
        return True

    def handle_client(self, client_socket: socket.socket, addr: tuple):
        """Handle a single client connection"""
        client_id = f"{addr[0]}:{addr[1]}"
        user_handle = None
        authenticated = False
        chatroom_name = None

        try:
            # Wait for initial handshake: LOGIN|handle|version|color
            data = client_socket.recv(1024).decode('utf-8').strip()
            parts = data.split('|', 3)

            if parts[0] != 'LOGIN' or len(parts) < 3 or len(parts) > 4 or not parts[1]:
                client_socket.send(b"ERROR|Invalid login handshake\n")
                self.log_event(f"Rejected invalid login from {client_id}")
                return

            requested_handle = parts[1]
            client_version = parts[2]
            user_color = self.normalize_user_color(parts[3] if len(parts) == 4 and parts[3] else "blue")
            latest_version = self.get_latest_client_version()
            user_handle = self.resolve_unique_handle(requested_handle)

            if self.is_version_mismatch(client_version, latest_version):
                client_socket.send(f"MISMATCH|{client_version}|{latest_version}\n".encode('utf-8'))
                self.log_event(
                    f"Version mismatch for {client_id}: {client_version} instead of {latest_version}"
                )
                if not self.build_client_binary():
                    client_socket.send(b"ERROR|Server failed to build txcomm_client executable for update\n")
                    self.log_event(f"Update failed for {client_id}: build failed")
                    return
                payload = self.server_client_binary_path.read_bytes()
                header = (
                    f"UPDATE_BIN|{latest_version}|{self.server_client_binary_path.name}|{len(payload)}\n"
                ).encode('utf-8')
                client_socket.send(header)
                client_socket.send(payload)
                self.log_event(f"Sent client update to {client_id} ({client_version} -> {latest_version})")
                return

            with self.lock:
                self.active_connections[client_id] = client_socket
                self.sessions[client_id] = {"handle": user_handle, "chatroom": None, "color": user_color}
            authenticated = True
            self.log_event(f"{client_id} authenticated as {self.colorize_handle(user_handle, user_color)}")

            client_socket.send(f"READY|{user_handle}|Logged in as {user_handle}. You are in the lobby.\n".encode('utf-8'))

            while True:
                data = client_socket.recv(1024).decode('utf-8').strip()
                if not data:
                    break

                if data.startswith('JOIN|'):
                    requested_chatroom = data[5:].strip()
                    if not requested_chatroom:
                        client_socket.send(b"ERROR|Memo name cannot be empty\n")
                        continue

                    previous_chatroom = None
                    with self.lock:
                        session = self.sessions.get(client_id)
                        if session:
                            previous_chatroom = session.get("chatroom")

                    if previous_chatroom == requested_chatroom:
                        client_socket.send(f"INFO|You are already in memo: {requested_chatroom}\n".encode('utf-8'))
                        continue

                    if previous_chatroom:
                        old_chatroom = self.chatrooms.get(previous_chatroom)
                        if old_chatroom:
                            old_chatroom.remove_user(user_handle)
                        self.emit_system_event(
                            previous_chatroom,
                            user_handle,
                            user_color,
                            f"{user_handle} left the memo",
                            exclude_client_id=client_id
                        )
                        self.log_event(
                            f"{self.colorize_handle(user_handle, user_color)} left memo {previous_chatroom}"
                        )
                        self.broadcast_users_list(previous_chatroom)

                    chatroom = self.get_or_create_chatroom(requested_chatroom)
                    chatroom.add_user(user_handle)
                    chatroom_name = requested_chatroom

                    with self.lock:
                        session = self.sessions.get(client_id)
                        if session:
                            session["chatroom"] = chatroom_name

                    recent = chatroom.get_recent_messages(20)
                    for msg in recent:
                        system_bit = 1 if msg.is_system else 0
                        client_socket.send(
                            f"MSG|{msg.handle}|{msg.text}|{msg.timestamp}|{msg.color}|{system_bit}\n".encode('utf-8')
                        )
                    client_socket.send(f"JOINED|{chatroom_name}\n".encode('utf-8'))
                    self.emit_system_event(chatroom_name, user_handle, user_color, f"{user_handle} joined the memo")
                    self.broadcast_users_list(chatroom_name)
                    self.log_event(
                        f"{self.colorize_handle(user_handle, user_color)} joined memo {chatroom_name}"
                    )

                elif data == 'LEAVE':
                    active_chatroom = None
                    with self.lock:
                        session = self.sessions.get(client_id)
                        if session:
                            active_chatroom = session.get("chatroom")
                            session["chatroom"] = None

                    if active_chatroom:
                        chatroom = self.chatrooms.get(active_chatroom)
                        if chatroom:
                            chatroom.remove_user(user_handle)
                        chatroom_name = None
                        self.emit_system_event(active_chatroom, user_handle, user_color, f"{user_handle} left the memo")
                        client_socket.send(b"LEFT|Lobby\n")
                        self.broadcast_users_list(active_chatroom)
                        self.log_event(
                            f"{self.colorize_handle(user_handle, user_color)} returned to lobby from {active_chatroom}"
                        )
                    else:
                        client_socket.send(b"INFO|You are already in the lobby\n")

                elif data == 'LIST':
                    rooms = self.list_chatrooms()
                    payload = ",".join(rooms)
                    client_socket.send(f"LIST|{payload}\n".encode('utf-8'))

                elif data.startswith('SAY|'):
                    message_text = data[4:]
                    active_chatroom = None
                    with self.lock:
                        session = self.sessions.get(client_id)
                        if session:
                            active_chatroom = session.get("chatroom")

                    if not active_chatroom:
                        client_socket.send(b"ERROR|Join a memo before sending messages\n")
                        continue

                    chatroom = self.get_or_create_chatroom(active_chatroom)
                    chatroom.add_message(user_handle, message_text, user_color, is_system=False)
                    self.broadcast_message(
                        active_chatroom,
                        user_handle,
                        message_text,
                        sender_color=user_color,
                        is_system=False
                    )
                    preview = message_text if len(message_text) <= 40 else f"{message_text[:37]}..."
                    self.log_event(
                        f"{self.colorize_handle(user_handle, user_color)}@{active_chatroom}: {preview}"
                    )

                elif data == 'QUIT':
                    break
                else:
                    client_socket.send(b"ERROR|Unknown command\n")

        except Exception as e:
            if user_handle:
                self.log_event(
                    f"Error for {self.colorize_handle(user_handle, self.get_session_color(client_id))}({client_id}): {e}"
                )
            else:
                self.log_event(f"Error for {client_id}: {e}")

        finally:
            # Cleanup
            disconnect_color = self.get_session_color(client_id)
            if user_handle:
                active_chatroom = None
                with self.lock:
                    session = self.sessions.get(client_id)
                    if session:
                        active_chatroom = session.get("chatroom")

                chatroom = self.chatrooms.get(active_chatroom) if active_chatroom else None
                if chatroom:
                    chatroom.remove_user(user_handle)
                    user_color = "red"
                    with self.lock:
                        session = self.sessions.get(client_id)
                        if session and session.get("color"):
                            user_color = session["color"]
                    self.emit_system_event(active_chatroom, user_handle, user_color, f"{user_handle} left the memo")
                    self.broadcast_users_list(active_chatroom)

            with self.lock:
                if client_id in self.active_connections:
                    del self.active_connections[client_id]
                if client_id in self.sessions:
                    del self.sessions[client_id]

            try:
                client_socket.close()
            except:
                pass

            if authenticated and user_handle:
                self.log_event(
                    f"{self.colorize_handle(user_handle, disconnect_color)} disconnected"
                )
            else:
                self.log_event(f"{client_id} disconnected")

    def broadcast_message(
        self,
        chatroom_name: str,
        sender_handle: str,
        text: str,
        sender_color: str = "bright_teal",
        is_system: bool = False,
        exclude_client_id: Optional[str] = None
    ):
        """Broadcast a message to all clients in a chatroom"""
        with self.lock:
            connections_to_notify = [
                (cid, sock) for cid, sock in self.active_connections.items()
                if (
                    cid in self.sessions
                    and self.sessions[cid].get("chatroom") == chatroom_name
                    and cid != exclude_client_id
                )
            ]

        system_bit = 1 if is_system else 0
        message = f"MSG|{sender_handle}|{text}|{time.time()}|{sender_color}|{system_bit}\n"
        for client_id, sock in connections_to_notify:
            try:
                sock.send(message.encode('utf-8'))
            except:
                pass

    def start(self):
        """Start the server"""
        self.running = True
        self.server_version = self.get_latest_client_version()
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((self.host, self.port))
        server_socket.listen(5)
        self.log_event(f"Server started on {self.host}:{self.port}")

        try:
            while self.running:
                try:
                    client_socket, addr = server_socket.accept()
                    self.log_event(f"New connection from {addr[0]}:{addr[1]}")
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
            self.log_event("Server stopped")


if __name__ == "__main__":
    server = TXCommServer()
    server.start()

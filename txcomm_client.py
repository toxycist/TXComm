#!/usr/bin/env python3
import socket
import subprocess
import threading
import sys
import time
from urllib.parse import quote, unquote
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Tuple, Dict
from queue import Queue
import os

# ============================================================
# ANSI color palette
# ============================================================
class Colors:
    RESET      = '\033[0m'
    BOLD       = '\033[1m'

    # UI chrome  (teal/slate)
    BRIGHT_TEAL = '\033[96m'

    # System/status messages
    GRAY       = '\033[37m'
    DARK_GRAY  = '\033[90m'

    # Handle colors
    PINK        = '\033[38;5;213m'
    YELLOW      = '\033[33m'
    GREEN       = '\033[32m'
    RED         = '\033[31m'
    BLUE        = '\033[34m'
    BRIGHT_RED  = '\033[91m'

COLOR_BY_NAME: Dict[str, str] = {
    "pink": Colors.PINK,
    "yellow": Colors.YELLOW,
    "green": Colors.GREEN,
    "red": Colors.RED,
    "blue": Colors.BLUE,
    "bright_teal": Colors.BRIGHT_TEAL,
    "gray": Colors.GRAY,
    "dark_gray": Colors.DARK_GRAY,
}

USER_PICKABLE_COLORS = {
    "red", "green", "blue", "yellow", "pink"
}

CLIENT_VERSION = "1.0.8"

def get_color_by_name(color_name: str, fallback_handle: str = "") -> str:
    if color_name in COLOR_BY_NAME:
        return COLOR_BY_NAME[color_name]
    if fallback_handle:
        return Colors.BLUE
    return Colors.BRIGHT_TEAL

def format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%H:%M")

def clear_screen():
    subprocess.run('clear' if os.name == 'posix' else 'cls')

def decode_field(value: str) -> str:
    return unquote(value or "")

def encode_field(value: str) -> str:
    return quote(value or "", safe="")

# ============================================================
# Layout constants
# ============================================================
WIDTH = 72   # total terminal width for the UI

def rule(char: str = '-', color: str = Colors.DARK_GRAY) -> str:
    """A horizontal rule across the full width."""
    return f"{color}{char * WIDTH}{Colors.RESET}"

def center(text: str, width: int = WIDTH) -> str:
    """Center plain text (strips ANSI for length calc isn't needed here)."""
    return text.center(width)


# ============================================================
# Main client class
# ============================================================
class TXCommClient:
    def __init__(self, host: str = 'localhost', port: int = 1717,
                 handle: str = 'user', chatroom: str = '', color_name: str = 'blue'):
        self.host = host
        self.port = port
        self.handle = handle
        self.chatroom = chatroom
        self.color_name = color_name if color_name in USER_PICKABLE_COLORS else 'blue'
        self.socket: Optional[socket.socket] = None
        self.running = False
        self.connected = False
        self.in_lobby = True
        self.pending_join_memo = chatroom.strip() if chatroom else ''
        self.online_users: List[Tuple[str, str]] = []
        self.messages: List[Tuple[str, str, float, str, bool]] = []
        self.message_queue = Queue()
        self.lock = threading.Lock()

    # ----------------------------------------------------------
    # Networking
    # ----------------------------------------------------------
    def connect(self) -> bool:
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((self.host, self.port))
            self.socket.send(f"LOGIN|{self.handle}|{CLIENT_VERSION}|{self.color_name}\n".encode('utf-8'))

            server_msg = self._recv_line()
            if not server_msg:
                print(f"{Colors.BRIGHT_RED}-- Server closed connection during login --{Colors.RESET}")
                return False

            parts = server_msg.split('|', 3)
            msg_type = parts[0]

            if msg_type == 'MISMATCH':
                print(f"{Colors.BRIGHT_TEAL}-- Client version mismatch. Initiating the update sequence --{Colors.RESET}")
                server_msg = self._recv_line()
                if not server_msg:
                    print(f"{Colors.BRIGHT_RED}-- Server closed connection during update sequence --{Colors.RESET}")
                    return False
                parts = server_msg.split('|', 3)
                msg_type = parts[0]

            if msg_type == 'READY':
                assigned_handle = parts[1] if len(parts) > 2 else self.handle
                ready_text = parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else "")
                self.message_queue.put(('ready', assigned_handle, ready_text))
            elif msg_type in ('UPDATE', 'UPDATE_BIN'):
                if msg_type == 'UPDATE_BIN' and len(parts) != 4:
                    print(f"{Colors.BRIGHT_RED}-- Invalid update payload from server --{Colors.RESET}")
                    return False
                if msg_type == 'UPDATE' and len(parts) != 3:
                    print(f"{Colors.BRIGHT_RED}-- Invalid update payload from server --{Colors.RESET}")
                    return False
                latest_version = parts[1]
                try:
                    update_size = int(parts[2] if msg_type == 'UPDATE' else parts[3])
                except ValueError:
                    print(f"{Colors.BRIGHT_RED}-- Invalid update size from server --{Colors.RESET}")
                    return False
                update_payload = self._recv_exact(update_size)
                if update_payload is None:
                    print(f"{Colors.BRIGHT_RED}-- Failed to download update payload --{Colors.RESET}")
                    return False
                binary_name = "txcomm_client" if msg_type == 'UPDATE' else parts[2]
                return self._apply_update_and_restart(latest_version, update_payload, binary_name)
            elif msg_type == 'ERROR':
                print(f"{Colors.BRIGHT_RED}-- {parts[1] if len(parts) > 1 else 'Server error'} --{Colors.RESET}")
                return False
            else:
                print(f"{Colors.BRIGHT_RED}-- Unexpected server response: {msg_type} --{Colors.RESET}")
                return False

            self.connected = True
            return True
        except Exception as e:
            print(f"{Colors.BRIGHT_RED}-- Error connecting to server: {e} --{Colors.RESET}")
            return False

    def _recv_line(self) -> Optional[str]:
        if not self.socket:
            return None
        buffer = bytearray()
        while True:
            chunk = self.socket.recv(1)
            if not chunk:
                return None
            if chunk == b'\n':
                break
            buffer.extend(chunk)
        return buffer.decode('utf-8').strip()

    def _recv_exact(self, expected_size: int) -> Optional[bytes]:
        if not self.socket:
            return None
        chunks: List[bytes] = []
        received = 0
        while received < expected_size:
            chunk = self.socket.recv(min(4096, expected_size - received))
            if not chunk:
                return None
            chunks.append(chunk)
            received += len(chunk)
        return b''.join(chunks)

    def _apply_update_and_restart(self, latest_version: str, payload: bytes, binary_name: str) -> bool:
        if getattr(sys, "frozen", False):
            target_path = Path(sys.executable).resolve()
        else:
            target_path = Path(__file__).resolve().with_name(binary_name)
        temp_path = target_path.with_suffix(target_path.suffix + ".new")
        try:
            print(f"{Colors.BRIGHT_TEAL}-- Updating client {CLIENT_VERSION} -> {latest_version} --{Colors.RESET}")
            time.sleep(2)
            with open(temp_path, 'wb') as f:
                f.write(payload)
            os.chmod(temp_path, 0o755)
            os.replace(temp_path, target_path)
            print(f"{Colors.BRIGHT_TEAL}-- Update installed. Restarting client... --{Colors.RESET}")
            time.sleep(2)
            os.execv(str(target_path), [str(target_path)] + sys.argv[1:])
            return False
        except Exception as e:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass
            print(f"{Colors.BRIGHT_RED}-- Failed to apply update: {e}{Colors.RESET}")
            return False

    def receive_loop(self):
        recv_buffer = ""
        while self.running and self.connected:
            try:
                chunk = self.socket.recv(1024)
                if not chunk:
                    break
                recv_buffer += chunk.decode('utf-8', errors='replace')
                while '\n' in recv_buffer:
                    line, recv_buffer = recv_buffer.split('\n', 1)
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split('|', 5)
                    msg_type = parts[0]
                    if msg_type == 'MSG':
                        sender    = decode_field(parts[1]) if len(parts) > 1 else "UNKNOWN"
                        message   = decode_field(parts[2]) if len(parts) > 2 else ""
                        timestamp = float(parts[3]) if len(parts) > 3 else time.time()
                        sender_color = decode_field(parts[4]) if len(parts) > 4 and parts[4] else ""
                        is_system = parts[5] == '1' if len(parts) > 5 else sender == "SYSTEM"
                        with self.lock:
                            self.messages.append((sender, message, timestamp, sender_color, is_system))
                        self.message_queue.put(('refresh',))
                    elif msg_type == 'READY':
                        assigned_handle = parts[1] if len(parts) > 1 else self.handle
                        info_message = parts[2] if len(parts) > 2 else ""
                        self.message_queue.put(('ready', assigned_handle, info_message))
                    elif msg_type == 'JOINED':
                        memo_name = parts[1] if len(parts) > 1 else self.chatroom
                        self.message_queue.put(('joined', memo_name))
                    elif msg_type == 'LEFT':
                        self.message_queue.put(('left',))
                    elif msg_type == 'USERS':
                        users: List[Tuple[str, str]] = []
                        if len(parts) > 1 and parts[1]:
                            for item in parts[1].split(';'):
                                if not item:
                                    continue
                                handle, _, color = item.partition(':')
                                users.append((handle, color or "red"))
                        self.message_queue.put(('users', users))
                    elif msg_type == 'MEMOS':
                        rooms = parts[1].split(',') if len(parts) > 1 and parts[1] else []
                        self.message_queue.put(('memos', rooms))
                    elif msg_type == 'ERROR':
                        self.message_queue.put(('error', parts[1] if len(parts) > 1 else "Unknown error"))
                    elif msg_type == 'INFO':
                        self.message_queue.put(('info', parts[1] if len(parts) > 1 else ""))
            except ConnectionResetError:
                self.connected = False
                break
            except Exception:
                if self.running:
                    break
        
        return

    def input_loop(self):
        while self.running:
            try:
                user_input = input(f" ")
                print(Colors.RESET, end="")
                self.message_queue.put(('input', user_input))
            except EOFError:
                self.message_queue.put(('quit',))
                break
            except KeyboardInterrupt:
                self.message_queue.put(('quit',))
                break
        
        return

    # ----------------------------------------------------------
    # Drawing
    # ----------------------------------------------------------
    def draw_screen(self):
        try:
            import readline
            current_input = readline.get_line_buffer()
        except ImportError:
            current_input = ''

        clear_screen()

        my_color = get_color_by_name(self.color_name, self.handle)

        # ── Header ───────────────────────────────────────────────
        print()
        print(rule('=', Colors.BOLD + Colors.BRIGHT_TEAL))
        if self.in_lobby:
            title_line = (
                f"{Colors.BOLD}{Colors.BRIGHT_TEAL}"
                f"  LOBBY  --  logged in as "
                f"{my_color}{self.handle}{Colors.BRIGHT_TEAL}"
                f"{Colors.RESET}"
            )
        else:
            title_line = (
                f"{Colors.BOLD}{Colors.BRIGHT_TEAL}"
                f"  {self.chatroom.upper()}  --  logged in as "
                f"{my_color}{self.handle}{Colors.BRIGHT_TEAL}"
                f"{Colors.RESET}"
            )
        print(title_line)
        print(rule('=', Colors.BOLD + Colors.BRIGHT_TEAL))

        if not self.in_lobby:
            users_count = len(self.online_users)
            users_preview = ", ".join(
                f"{get_color_by_name(color, handle)}{handle}{Colors.BRIGHT_TEAL}"
                for handle, color in self.online_users[:8]
            )
            if users_count > 8:
                users_preview += f"{Colors.BRIGHT_TEAL}, +{users_count - 8}{Colors.BRIGHT_TEAL}"
            print(
                f"{Colors.BRIGHT_TEAL}"
                f"  online users ({users_count}): {users_preview if users_preview else '(none)'}"
                f"{Colors.RESET}"
            )

        if self.in_lobby:
            print(f"{Colors.BRIGHT_TEAL}  commands: /join <memo>, /memos, /users, /quit{Colors.RESET}")
        else:
            print(f"{Colors.BRIGHT_TEAL}  commands: /leave, /join <memo>, /users, /quit{Colors.RESET}")
        print()

        # ── Message log ──────────────────────────────────────────
        with self.lock:
            for sender, text, timestamp, sender_color_name, is_system in self.messages:
                sender_color = get_color_by_name(sender_color_name, sender)
                if is_system:
                    if sender != "SYSTEM" and sender and sender in text:
                        formatted = text.replace(
                            sender,
                            f"{sender_color}{sender}{Colors.DARK_GRAY}"
                        )
                        print(f"{Colors.DARK_GRAY}-- {formatted} --{Colors.RESET}")
                    else:
                        base_color = get_color_by_name(sender_color_name)
                        print(f"{base_color}-- {text} --{Colors.RESET}")
                    continue

                handle_color = sender_color
                time_str = format_time(timestamp)

                # [HH:MM] handleName: message text
                handle_tag = (
                    f"{handle_color}{Colors.BOLD}{sender}:"
                )
                print(f"{Colors.DARK_GRAY}[{time_str}]{Colors.RESET} {handle_tag} {text}{Colors.RESET}")

        # ── Input prompt ─────────────────────────────────────────
        print(rule('-', Colors.BOLD + Colors.BRIGHT_TEAL))
        print(f"{my_color}{Colors.BOLD}{self.handle}: {current_input}", end="")
        sys.stdout.flush()

    # ----------------------------------------------------------
    # Sending / quitting
    # ----------------------------------------------------------
    def send_message(self, text: str):
        if self.connected:
            try:
                self.socket.send(f"SAY|{encode_field(text)}\n".encode('utf-8'))
            except Exception:
                pass

    def join_memo(self, memo_name: str):
        if self.connected and memo_name:
            try:
                self.socket.send(f"JOIN|{memo_name}\n".encode('utf-8'))
            except Exception:
                pass

    def leave_memo(self):
        if self.connected:
            try:
                self.socket.send(b"LEAVE\n")
            except Exception:
                pass

    def request_memos(self):
        if self.connected:
            try:
                self.socket.send(b"MEMOS\n")
            except Exception:
                pass

    def request_users(self):
        if self.connected:
            try:
                self.socket.send(b"USERS\n")
            except Exception:
                pass

    def quit(self):
        try:
            self.socket.send(b"QUIT\n")
        except Exception:
            pass
        finally:
            try:
                self.socket.close()
            except Exception:
                pass
            self.connected = False

    # ----------------------------------------------------------
    # Main loop
    # ----------------------------------------------------------
    def run(self):
        if not self.connect():
            return

        self.running = True
        self.draw_screen()

        receive_thread = threading.Thread(target=self.receive_loop, daemon=True)
        receive_thread.start()

        input_thread = threading.Thread(target=self.input_loop, daemon=True)
        input_thread.start()

        try:
            while self.running and self.connected:
                try:
                    msg = self.message_queue.get(timeout=0.5)

                    if msg[0] == 'refresh':
                        self.draw_screen()

                    elif msg[0] == 'ready':
                        assigned_handle = msg[1]
                        info = msg[2]
                        self.handle = assigned_handle
                        if info:
                            with self.lock:
                                self.messages.append((self.handle, info, time.time(), self.color_name, True))
                        if self.pending_join_memo:
                            if self.in_lobby or self.pending_join_memo != self.chatroom:
                                with self.lock:
                                    self.messages = []
                            self.join_memo(self.pending_join_memo)
                            self.pending_join_memo = ''
                        self.draw_screen()

                    elif msg[0] == 'joined':
                        self.chatroom = msg[1]
                        self.in_lobby = False
                        self.draw_screen()

                    elif msg[0] == 'left':
                        self.in_lobby = True
                        self.chatroom = ''
                        self.online_users = []
                        with self.lock:
                            self.messages = [("SYSTEM", "Returned to lobby", time.time(), "dark_gray", True)]
                        self.draw_screen()

                    elif msg[0] == 'users':
                        self.online_users = msg[1]
                        self.draw_screen()

                    elif msg[0] == 'memos':
                        rooms = msg[1]
                        room_text = ", ".join(rooms) if rooms else "(none yet)"
                        with self.lock:
                            self.messages.append(("SYSTEM", f"Memos: {room_text}", time.time(), "dark_gray", True))
                        self.draw_screen()

                    elif msg[0] == 'error':
                        with self.lock:
                            self.messages.append(("SYSTEM", f"Error: {msg[1]}", time.time(), "red", True))
                        self.draw_screen()

                    elif msg[0] == 'info':
                        with self.lock:
                            self.messages.append(("SYSTEM", msg[1], time.time(), "dark_gray", True))
                        self.draw_screen()

                    elif msg[0] == 'input':
                        user_input = msg[1]

                        if user_input.lower() in ['/quit', '/exit', '/q']:
                            break

                        if user_input.lower() == '/memos':
                            self.request_memos()
                            continue

                        if user_input.lower() == '/users':
                            self.request_users()
                            continue

                        if user_input.lower() in ['/leave', '/l']:
                            self.leave_memo()
                            continue

                        if user_input.lower().startswith('/join '):
                            memo_name = user_input[6:].strip()
                            if memo_name:
                                if memo_name.lower() == "lobby":
                                    with self.lock:
                                        self.messages.append(("SYSTEM", "Error: Memo name 'lobby' is reserved", time.time(), "red", True))
                                    self.draw_screen()
                                    continue
                                if self.in_lobby or memo_name != self.chatroom:
                                    with self.lock:
                                        self.messages = []
                                self.join_memo(memo_name)
                            else:
                                with self.lock:
                                    self.messages.append(("SYSTEM", "Usage: /join <memo>", time.time(), "dark_gray", True))
                                self.draw_screen()
                            continue

                        if user_input and not self.in_lobby:
                            self.send_message(user_input)
                        elif user_input and self.in_lobby:
                            with self.lock:
                                self.messages.append(("SYSTEM", "Join a memo first: /join <memo>", time.time(), "dark_gray", True))

                        self.draw_screen()

                    elif msg[0] == 'quit':
                        break

                except Exception:
                    pass

        finally:
            self.running = False
            self.quit()
            print(
                f"\n{Colors.DARK_GRAY}-- "
                f"{get_color_by_name(self.color_name, self.handle)}{self.handle}"
                f"{Colors.DARK_GRAY} has disconnected. --{Colors.RESET}\n"
            )
            subprocess.run(["stty", "sane"])


# ============================================================
# Entry point / connection splash
# ============================================================
def main():
    clear_screen()

    print()
    print(rule('=', Colors.BOLD + Colors.BRIGHT_TEAL))
    header_left = "  TXComm  --  DELIVERING YOUR MESSAGES SINCE 2009"
    header_right = f"v{CLIENT_VERSION}"
    spacer = " " * max(1, WIDTH - len(header_left) - len(header_right))
    print(
        f"{Colors.BOLD}{Colors.BRIGHT_TEAL}"
        f"{header_left}{spacer}{header_right}"
        f"{Colors.RESET}"
    )
    print(rule('=', Colors.BOLD + Colors.BRIGHT_TEAL))
    print()
    print(f"{Colors.DARK_GRAY}-- Enter connection settings --{Colors.RESET}")
    print()

    host_input = input(
        f"  {Colors.BRIGHT_TEAL}server address{Colors.RESET} "
        f"{Colors.DARK_GRAY}[localhost]{Colors.RESET} {Colors.BRIGHT_TEAL}: {Colors.RESET}"
    ).strip()
    host = host_input or 'localhost'

    port_raw = input(
        f"  {Colors.BRIGHT_TEAL}server port{Colors.RESET}    "
        f"{Colors.DARK_GRAY}[1717]{Colors.RESET}      {Colors.BRIGHT_TEAL}: {Colors.RESET}"
    ).strip()
    try:
        port = int(port_raw) if port_raw else 1717
    except ValueError:
        port = 1717

    handle = (
        input(
            f"  {Colors.BRIGHT_TEAL}handle{Colors.RESET}         "
            f"{Colors.DARK_GRAY}[user]{Colors.RESET}      {Colors.BRIGHT_TEAL}: {Colors.RESET}"
        ).strip()
        or 'user'
    )

    chatroom = (
        input(
            f"  {Colors.BRIGHT_TEAL}memo{Colors.RESET}           "
            f"{Colors.DARK_GRAY}[leave blank for lobby]{Colors.RESET} {Colors.BRIGHT_TEAL}: {Colors.RESET}"
        ).strip()
    )

    print()
    print(f"{Colors.DARK_GRAY}-- Choose handle color --{Colors.RESET}")
    color_options = [
        "red", "green", "blue", "yellow", "pink"
    ]
    colored_options = f"{Colors.DARK_GRAY}, {Colors.RESET}".join(
        f"{get_color_by_name(opt)}{opt}{Colors.RESET}" for opt in color_options
    )
    print(f"  {colored_options}")
    color_name = input(
        f"  {Colors.BRIGHT_TEAL}color{Colors.RESET}          "
        f"{Colors.DARK_GRAY}[red]{Colors.RESET} {Colors.BRIGHT_TEAL}: {Colors.RESET}"
    ).strip().lower() or "red"
    if color_name not in USER_PICKABLE_COLORS:
        color_name = "red"

    print()

    client = TXCommClient(host=host, port=port, handle=handle, chatroom=chatroom, color_name=color_name)
    client.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{Colors.BRIGHT_RED}-- Interrupted by user --{Colors.RESET}")
        subprocess.run(["stty", "sane"])
        raise SystemExit(0)

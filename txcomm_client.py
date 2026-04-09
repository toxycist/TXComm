#!/usr/bin/env python3
import socket
import subprocess
import threading
import sys
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Tuple
from queue import Queue
import os

# ============================================================
# ANSI color palette
# ============================================================
class Colors:
    RESET      = '\033[0m'
    BOLD       = '\033[1m'
    DIM        = '\033[2m'

    # UI chrome  (teal/slate)
    TEAL       = '\033[36m'
    BRIGHT_TEAL = '\033[96m'
    SLATE      = '\033[34m'
    BRIGHT_SLATE = '\033[94m'

    # System/status messages
    GRAY       = '\033[37m'
    DARK_GRAY  = '\033[90m'

    # Handle colors
    CYAN        = '\033[36m'
    MAGENTA     = '\033[35m'
    YELLOW      = '\033[33m'
    GREEN       = '\033[32m'
    RED         = '\033[31m'
    BLUE        = '\033[34m'
    BRIGHT_CYAN    = '\033[96m'
    BRIGHT_MAGENTA = '\033[95m'
    BRIGHT_YELLOW  = '\033[93m'
    BRIGHT_GREEN   = '\033[92m'
    BRIGHT_RED     = '\033[91m'
    BRIGHT_BLUE    = '\033[94m'

HANDLE_COLORS = [
    Colors.CYAN,         Colors.MAGENTA,        Colors.YELLOW,
    Colors.GREEN,        Colors.RED,            Colors.BLUE,
    Colors.BRIGHT_CYAN,  Colors.BRIGHT_MAGENTA, Colors.BRIGHT_YELLOW,
    Colors.BRIGHT_GREEN, Colors.BRIGHT_RED,     Colors.BRIGHT_BLUE,
]

CLIENT_VERSION = "1.0.0"

def get_color_for_handle(handle: str) -> str:
    return HANDLE_COLORS[hash(handle) % len(HANDLE_COLORS)]

def format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%H:%M")

def clear_screen():
    subprocess.run('clear' if os.name == 'posix' else 'cls')

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
                 handle: str = 'USER', chatroom: str = 'main'):
        self.host = host
        self.port = port
        self.handle = handle
        self.chatroom = chatroom
        self.socket: Optional[socket.socket] = None
        self.running = False
        self.connected = False
        self.in_lobby = True
        self.pending_join_memo = chatroom.strip() if chatroom else ''
        self.messages: List[Tuple[str, str, float]] = []
        self.message_queue = Queue()
        self.lock = threading.Lock()

    # ----------------------------------------------------------
    # Networking
    # ----------------------------------------------------------
    def connect(self) -> bool:
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((self.host, self.port))
            self.socket.send(f"LOGIN|{self.handle}|{CLIENT_VERSION}".encode('utf-8'))

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
                self.message_queue.put(('ready', parts[1] if len(parts) > 1 else ""))
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
        while self.running and self.connected:
            try:
                data = self.socket.recv(1024).decode('utf-8').strip()
                if not data:
                    break
                for line in data.split('\n'):
                    if not line:
                        continue
                    parts = line.split('|', 3)
                    msg_type = parts[0]
                    if msg_type == 'MSG':
                        sender    = parts[1] if len(parts) > 1 else "UNKNOWN"
                        message   = parts[2] if len(parts) > 2 else ""
                        timestamp = float(parts[3]) if len(parts) > 3 else time.time()
                        with self.lock:
                            self.messages.append((sender, message, timestamp))
                        self.message_queue.put(('refresh',))
                    elif msg_type == 'READY':
                        self.message_queue.put(('ready', parts[1] if len(parts) > 1 else ""))
                    elif msg_type == 'JOINED':
                        memo_name = parts[1] if len(parts) > 1 else self.chatroom
                        self.message_queue.put(('joined', memo_name))
                    elif msg_type == 'LEFT':
                        self.message_queue.put(('left',))
                    elif msg_type == 'LIST':
                        rooms = parts[1].split(',') if len(parts) > 1 and parts[1] else []
                        self.message_queue.put(('list', rooms))
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

        # ── Header ───────────────────────────────────────────────
        print()
        print(rule('=', Colors.BOLD + Colors.BRIGHT_TEAL))
        if self.in_lobby:
            title_line = (
                f"{Colors.BOLD}{Colors.BRIGHT_TEAL}"
                f"  LOBBY  --  logged in as {self.handle}"
                f"{Colors.RESET}"
            )
        else:
            title_line = (
                f"{Colors.BOLD}{Colors.BRIGHT_TEAL}"
                f"  {self.chatroom.upper()} --  logged in as {self.handle}"
                f"{Colors.RESET}"
            )
        print(title_line)
        print(rule('=', Colors.BOLD + Colors.BRIGHT_TEAL))
        if self.in_lobby:
            print(f"{Colors.BOLD}{Colors.BRIGHT_TEAL}  Commands: /join <memo>, /list, /quit{Colors.RESET}")
        else:
            print(f"{Colors.BOLD}{Colors.BRIGHT_TEAL}  Commands: /leave, /join <memo>, /quit{Colors.RESET}")
        print()

        # ── Message log ──────────────────────────────────────────
        with self.lock:
            for sender, text, timestamp in self.messages:
                if sender == "SYSTEM":
                    print(f"{Colors.DARK_GRAY}-- {text} --{Colors.RESET}")
                    continue

                handle_color = get_color_for_handle(sender)
                time_str = format_time(timestamp)

                # [HH:MM] handleName: message text
                handle_tag = (
                    f"{handle_color}{Colors.BOLD}{sender}:"
                )
                print(f"{Colors.DARK_GRAY}[{time_str}]{Colors.RESET} {handle_tag} {text}{Colors.RESET}")

        # ── Input prompt ─────────────────────────────────────────
        print(rule('-', Colors.BOLD + Colors.BRIGHT_TEAL))
        my_color = get_color_for_handle(self.handle)
        print(f"{my_color}{Colors.BOLD}{self.handle}: {current_input}", end="")
        sys.stdout.flush()

    # ----------------------------------------------------------
    # Sending / quitting
    # ----------------------------------------------------------
    def send_message(self, text: str):
        if self.connected:
            try:
                self.socket.send(f"SAY|{text}".encode('utf-8'))
            except Exception:
                pass

    def join_memo(self, memo_name: str):
        if self.connected and memo_name:
            try:
                self.socket.send(f"JOIN|{memo_name}".encode('utf-8'))
            except Exception:
                pass

    def leave_memo(self):
        if self.connected:
            try:
                self.socket.send(b"LEAVE")
            except Exception:
                pass

    def request_memo_list(self):
        if self.connected:
            try:
                self.socket.send(b"LIST")
            except Exception:
                pass

    def quit(self):
        try:
            self.socket.send(b"QUIT")
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
                        info = msg[1]
                        if info:
                            with self.lock:
                                self.messages.append(("SYSTEM", info, time.time()))
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
                        with self.lock:
                            self.messages = [("SYSTEM", "Returned to lobby", time.time())]
                        self.draw_screen()

                    elif msg[0] == 'list':
                        rooms = msg[1]
                        room_text = ", ".join(rooms) if rooms else "(none yet)"
                        with self.lock:
                            self.messages.append(("SYSTEM", f"Memos: {room_text}", time.time()))
                        self.draw_screen()

                    elif msg[0] == 'error':
                        with self.lock:
                            self.messages.append(("SYSTEM", f"Error: {msg[1]}", time.time()))
                        self.draw_screen()

                    elif msg[0] == 'info':
                        with self.lock:
                            self.messages.append(("SYSTEM", msg[1], time.time()))
                        self.draw_screen()

                    elif msg[0] == 'input':
                        user_input = msg[1]

                        if user_input.lower() in ['/quit', '/exit', '/q']:
                            break

                        if user_input.lower() == '/list':
                            self.request_memo_list()
                            continue

                        if user_input.lower() == '/leave':
                            self.leave_memo()
                            continue

                        if user_input.lower().startswith('/join '):
                            memo_name = user_input[6:].strip()
                            if memo_name:
                                if self.in_lobby or memo_name != self.chatroom:
                                    with self.lock:
                                        self.messages = []
                                self.join_memo(memo_name)
                            else:
                                with self.lock:
                                    self.messages.append(("SYSTEM", "Usage: /join <memo>", time.time()))
                                self.draw_screen()
                            continue

                        if user_input and not self.in_lobby:
                            self.send_message(user_input)
                        elif user_input and self.in_lobby:
                            with self.lock:
                                self.messages.append(("SYSTEM", "Join a memo first: /join <memo>", time.time()))

                        self.draw_screen()

                    elif msg[0] == 'quit':
                        break

                except Exception:
                    pass

        finally:
            self.running = False
            self.quit()
            print(
                f"\n{Colors.DARK_GRAY}-- {self.handle} "
                f"has disconnected. --{Colors.RESET}\n"
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
            f"{Colors.DARK_GRAY}[USER]{Colors.RESET}      {Colors.BRIGHT_TEAL}: {Colors.RESET}"
        ).strip()
        or 'USER'
    )

    chatroom = (
        input(
            f"  {Colors.BRIGHT_TEAL}memo{Colors.RESET}           "
            f"{Colors.DARK_GRAY}[leave blank for lobby]{Colors.RESET} {Colors.BRIGHT_TEAL}: {Colors.RESET}"
        ).strip()
    )

    print()

    client = TXCommClient(host=host, port=port, handle=handle, chatroom=chatroom)
    client.run()


if __name__ == "__main__":
    main()

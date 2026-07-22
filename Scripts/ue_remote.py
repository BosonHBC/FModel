#!/usr/bin/env python3
"""UE Python Remote Execution client.

Implements the exact protocol from UE's remote_execution.py:
  - UDP multicast (239.0.0.1:6766) for node discovery (ping/pong)
  - TCP (127.0.0.1:6776) for command execution (UE connects to us)
  - JSON messages with version=1, magic='ue_py'

Reference: Engine/Plugins/Experimental/PythonScriptPlugin/Content/Python/remote_execution.py
"""

import socket
import json
import uuid
import time
import threading
import traceback

# Protocol constants (must match UE's PythonScriptRemoteExecution.cpp)
_PROTOCOL_VERSION = 1
_PROTOCOL_MAGIC = 'ue_py'
_TYPE_PING = 'ping'
_TYPE_PONG = 'pong'
_TYPE_OPEN_CONNECTION = 'open_connection'
_TYPE_CLOSE_CONNECTION = 'close_connection'
_TYPE_COMMAND = 'command'
_TYPE_COMMAND_RESULT = 'command_result'

_NODE_PING_SECONDS = 1
_NODE_TIMEOUT_SECONDS = 5

DEFAULT_MULTICAST_GROUP = '239.0.0.1'
DEFAULT_MULTICAST_PORT = 6766
DEFAULT_BIND_ADDRESS = '127.0.0.1'
DEFAULT_COMMAND_HOST = '127.0.0.1'
DEFAULT_COMMAND_PORT = 6776
DEFAULT_RECEIVE_BUFFER_SIZE = 8192

# Execution modes
MODE_EXEC_FILE = 'ExecuteFile'
MODE_EXEC_STATEMENT = 'ExecuteStatement'
MODE_EVAL_STATEMENT = 'EvaluateStatement'

_MODE_MAP = {
    'exec': MODE_EXEC_FILE,
    'statement': MODE_EXEC_STATEMENT,
    'eval': MODE_EVAL_STATEMENT,
}


class DebugLog:
    """Collects debug messages for diagnostics."""
    def __init__(self):
        self._lines = []
        self._lock = threading.Lock()

    def log(self, msg):
        ts = time.strftime('%H:%M:%S')
        entry = f"[{ts}] {msg}"
        with self._lock:
            self._lines.append(entry)

    def get_lines(self):
        with self._lock:
            return list(self._lines)

    def clear(self):
        with self._lock:
            self._lines.clear()


# Global debug log for test_connection
_debug_log = DebugLog()


def _make_message(msg_type, source, dest=None, data=None):
    """Build a protocol message dict."""
    obj = {
        'version': _PROTOCOL_VERSION,
        'magic': _PROTOCOL_MAGIC,
        'type': msg_type,
        'source': source,
    }
    if dest:
        obj['dest'] = dest
    if data:
        obj['data'] = data
    return obj


def _msg_to_bytes(msg):
    return json.dumps(msg, ensure_ascii=False).encode('utf-8')


def _msg_from_bytes(raw):
    """Parse a protocol message. Returns dict or None on failure."""
    try:
        obj = json.loads(raw.decode('utf-8'))
        if obj.get('version') != _PROTOCOL_VERSION:
            return None
        if obj.get('magic') != _PROTOCOL_MAGIC:
            return None
        return obj
    except Exception:
        return None


class UERemoteExec:
    """UE Python Remote Execution client.

    Args:
        host: Multicast group address (default 239.0.0.1)
        port: Multicast port (default 6766)
        bind_addr: Adapter to bind multicast socket (default 127.0.0.1)
        cmd_host: TCP command listen host (default 127.0.0.1)
        cmd_port: TCP command listen port (default 6776)
        dbg: DebugLog instance (optional)
    """

    def __init__(self, host=DEFAULT_MULTICAST_GROUP, port=DEFAULT_MULTICAST_PORT,
                 bind_addr=DEFAULT_BIND_ADDRESS,
                 cmd_host=DEFAULT_COMMAND_HOST, cmd_port=DEFAULT_COMMAND_PORT,
                 dbg=None):
        self.multicast_group = (host, port)
        self.bind_addr = bind_addr
        self.cmd_endpoint = (cmd_host, cmd_port)
        self._node_id = str(uuid.uuid4())
        self._sock = None
        self._listen_thread = None
        self._running = False
        # _remote_nodes: {node_id: (node_data_dict, timestamp)}
        self._remote_nodes = {}
        self._nodes_lock = threading.Lock()
        self._connected = False
        self._cmd_sock = None
        self._remote_node_id = None
        self._dbg = dbg or DebugLog()
        self._ping_count = 0
        self._pong_count = 0
        self._udp_recv_count = 0

    @property
    def connected(self):
        return self._connected

    @property
    def remote_nodes(self):
        """Return list of discovered node dicts, each with 'node_id' key."""
        with self._nodes_lock:
            result = []
            for nid, val in self._remote_nodes.items():
                # val is (node_data_dict, timestamp)
                node_dict = dict(val[0])  # copy the data dict
                node_dict['node_id'] = nid
                result.append(node_dict)
            return result

    def connect(self, timeout=10):
        """Discover UE nodes via UDP multicast and open a command connection.

        Returns True on success.
        """
        self._dbg.log(f"connect() called: multicast={self.multicast_group}, "
                      f"bind={self.bind_addr}, cmd_endpoint={self.cmd_endpoint}, "
                      f"timeout={timeout}s")
        try:
            self._dbg.log("Initializing broadcast socket...")
            self._init_broadcast_socket()
            self._dbg.log("Broadcast socket OK, starting listen thread...")
            self._running = True
            self._listen_thread = threading.Thread(target=self._listen_loop, daemon=True)
            self._listen_thread.start()

            # Wait for UE to be discovered
            self._dbg.log("Waiting for UE pong response...")
            deadline = time.time() + timeout
            poll_count = 0
            while time.time() < deadline:
                nodes = self.remote_nodes
                if nodes:
                    self._dbg.log(f"Discovered {len(nodes)} UE node(s): "
                                  f"{[n.get('node_id', '?')[:8] for n in nodes]}")
                    break
                poll_count += 1
                if poll_count % 10 == 0:  # every ~2 seconds
                    self._dbg.log(f"  Still waiting... (pings sent={self._ping_count}, "
                                  f"udps recv={self._udp_recv_count}, pongs={self._pong_count})")
                time.sleep(0.2)
            else:
                self._dbg.log(f"TIMEOUT: No UE node discovered after {timeout}s. "
                              f"Stats: pings={self._ping_count}, udps={self._udp_recv_count}, "
                              f"pongs={self._pong_count}")
                self._cleanup()
                return False

            # Open command connection to first node
            self._remote_node_id = nodes[0]['node_id']
            self._dbg.log(f"Opening command connection to node {self._remote_node_id[:8]}...")
            return self._open_command_connection(timeout=timeout)
        except Exception as e:
            self._dbg.log(f"EXCEPTION in connect(): {e}")
            self._dbg.log(traceback.format_exc())
            self._cleanup()
            return False

    def run_command(self, cmd_str, mode='exec', timeout=120):
        """Execute a Python command in UE. Returns dict with success/result/error/output."""
        if not self._connected:
            return {'success': False, 'error': 'Not connected to UE'}
        exec_mode = _MODE_MAP.get(mode, MODE_EXEC_FILE)
        try:
            msg = _make_message(_TYPE_COMMAND, self._node_id, self._remote_node_id, {
                'command': cmd_str,
                'unattended': True,
                'exec_mode': exec_mode,
            })
            self._cmd_sock.settimeout(timeout)
            self._cmd_sock.sendall(_msg_to_bytes(msg))

            # Receive response
            raw = self._recv_tcp()
            if not raw:
                return {'success': False, 'error': 'No response from UE'}
            resp = _msg_from_bytes(raw)
            if not resp or resp.get('type') != _TYPE_COMMAND_RESULT:
                return {'success': False, 'error': 'Invalid response from UE'}
            data = resp.get('data', {})
            return {
                'success': data.get('success', False),
                'result': data.get('result', ''),
                'error': data.get('error', ''),
                'output': data.get('output', ''),
            }
        except socket.timeout:
            return {'success': False, 'error': 'Command timed out'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def disconnect(self):
        """Disconnect from UE."""
        if self._cmd_sock and self._remote_node_id:
            try:
                msg = _make_message(_TYPE_CLOSE_CONNECTION, self._node_id, self._remote_node_id)
                self._sock.sendto(_msg_to_bytes(msg), self.multicast_group)
            except Exception:
                pass
        self._cleanup()

    # ---- Internal implementation ----

    def _init_broadcast_socket(self):
        """Create and configure the UDP multicast socket."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._dbg.log(f"  Socket created: AF_INET/DGRAM/UDP")

        if hasattr(socket, 'SO_REUSEPORT'):
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            self._dbg.log("  Set SO_REUSEPORT")
        else:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._dbg.log("  Set SO_REUSEADDR")

        bind_tuple = (self.bind_addr, self.multicast_group[1])
        self._dbg.log(f"  Binding to {bind_tuple}...")
        self._sock.bind(bind_tuple)
        self._dbg.log(f"  Bind OK")

        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        self._dbg.log("  Set IP_MULTICAST_LOOP=1")

        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
        self._dbg.log("  Set IP_MULTICAST_TTL=0 (localhost only)")

        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                              socket.inet_aton(self.bind_addr))
        self._dbg.log(f"  Set IP_MULTICAST_IF={self.bind_addr}")

        mc_group = self.multicast_group[0]
        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                              socket.inet_aton(mc_group) + socket.inet_aton(self.bind_addr))
        self._dbg.log(f"  Joined multicast group {mc_group} on {self.bind_addr}")

        self._sock.settimeout(0.1)
        self._dbg.log("  Socket timeout set to 0.1s")

    def _listen_loop(self):
        """Background thread: send pings, receive pongs, timeout stale nodes."""
        last_ping = 0
        self._dbg.log("Listen thread started")
        while self._running:
            # Receive all pending data
            try:
                while True:
                    try:
                        data, addr = self._sock.recvfrom(DEFAULT_RECEIVE_BUFFER_SIZE)
                        self._udp_recv_count += 1
                        if data:
                            self._dbg.log(f"  UDP recv {len(data)} bytes from {addr}")
                            self._handle_udp_data(data, addr)
                    except socket.timeout:
                        break
            except OSError as e:
                if self._running:
                    self._dbg.log(f"  UDP recv error: {e}")

            # Send ping periodically
            now = time.time()
            if now - last_ping >= _NODE_PING_SECONDS:
                last_ping = now
                self._ping_count += 1
                try:
                    msg = _make_message(_TYPE_PING, self._node_id)
                    self._sock.sendto(_msg_to_bytes(msg), self.multicast_group)
                    if self._ping_count <= 3 or self._ping_count % 10 == 0:
                        self._dbg.log(f"  Sent ping #{self._ping_count} to {self.multicast_group}")
                except Exception as e:
                    self._dbg.log(f"  Ping send error: {e}")

            # Timeout stale nodes
            with self._nodes_lock:
                stale = [nid for nid, val in self._remote_nodes.items()
                         if now - val[1] > _NODE_TIMEOUT_SECONDS]
                for nid in stale:
                    self._dbg.log(f"  Node {nid[:8]} timed out")
                    del self._remote_nodes[nid]

            time.sleep(0.1)

        self._dbg.log("Listen thread exited")

    def _handle_udp_data(self, data, addr):
        """Process a UDP datagram."""
        msg = _msg_from_bytes(data)
        if not msg:
            # Log raw data for debugging if it's not a valid protocol message
            try:
                raw_str = data.decode('utf-8', errors='replace')[:200]
                self._dbg.log(f"  Non-protocol UDP data from {addr}: {raw_str}")
            except Exception:
                self._dbg.log(f"  Non-protocol UDP data from {addr}: {len(data)} bytes (binary)")
            return

        msg_type = msg.get('type', '?')
        msg_source = msg.get('source', '?')

        if msg_source == self._node_id:
            return  # Skip our own messages

        if msg.get('dest') and msg['dest'] != self._node_id:
            return  # Not for us

        if msg_type == _TYPE_PONG:
            self._pong_count += 1
            node_data = msg.get('data', {})
            self._dbg.log(f"  PONG received from {msg_source[:8]} (node data keys: "
                          f"{list(node_data.keys()) if node_data else 'empty'})")
            with self._nodes_lock:
                self._remote_nodes[msg_source] = (node_data, time.time())
        else:
            self._dbg.log(f"  UDP message type={msg_type} from {msg_source[:8]}")

    def _open_command_connection(self, timeout=10):
        """Open TCP command channel: we listen, UE connects to us."""
        self._dbg.log(f"Opening TCP listen socket on {self.cmd_endpoint}...")
        listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listen_sock.bind(self.cmd_endpoint)
            self._dbg.log(f"  TCP bind OK on {self.cmd_endpoint}")
        except OSError as e:
            self._dbg.log(f"  TCP bind failed on {self.cmd_endpoint}: {e}, trying random port...")
            listen_sock.bind((self.cmd_endpoint[0], 0))
            self.cmd_endpoint = listen_sock.getsockname()
            self._dbg.log(f"  TCP bound to random port: {self.cmd_endpoint}")
        listen_sock.listen(1)
        listen_sock.settimeout(5)

        # Tell UE to connect to our TCP endpoint via UDP
        for attempt in range(6):
            try:
                msg = _make_message(_TYPE_OPEN_CONNECTION, self._node_id, self._remote_node_id, {
                    'command_ip': self.cmd_endpoint[0],
                    'command_port': self.cmd_endpoint[1],
                })
                self._sock.sendto(_msg_to_bytes(msg), self.multicast_group)
                self._dbg.log(f"  Sent open_connection attempt {attempt+1}/6 "
                              f"(ip={self.cmd_endpoint[0]}, port={self.cmd_endpoint[1]})")
                self._cmd_sock = listen_sock.accept()[0]
                self._cmd_sock.setblocking(True)
                self._connected = True
                self._dbg.log(f"  TCP connection accepted from UE!")
                listen_sock.close()
                return True
            except socket.timeout:
                self._dbg.log(f"  TCP accept timeout on attempt {attempt+1}/6")
                continue
        self._dbg.log("  All 6 TCP accept attempts failed")
        listen_sock.close()
        return False

    def _recv_tcp(self):
        """Receive a complete message from TCP socket."""
        data = b''
        while True:
            try:
                part = self._cmd_sock.recv(DEFAULT_RECEIVE_BUFFER_SIZE)
                data += part
                if len(part) < DEFAULT_RECEIVE_BUFFER_SIZE:
                    break
            except socket.timeout:
                break
        return data if data else None

    def _cleanup(self):
        """Clean up all sockets and state."""
        self._running = False
        if self._listen_thread and self._listen_thread.is_alive():
            self._listen_thread.join(timeout=2)
        if self._cmd_sock:
            try:
                self._cmd_sock.close()
            except Exception:
                pass
            self._cmd_sock = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._connected = False
        self._remote_nodes.clear()


def test_connection(host=DEFAULT_MULTICAST_GROUP, port=DEFAULT_MULTICAST_PORT):
    """Quick connection test. Returns (success, message)."""
    _debug_log.clear()
    dbg = _debug_log
    dbg.log(f"=== test_connection(host={host}, port={port}) ===")

    client = UERemoteExec(host, port, dbg=dbg)
    if not client.connect(timeout=10):
        lines = dbg.get_lines()
        detail = "\n".join(lines)
        return False, (f"Cannot discover UE at multicast {host}:{port}.\n\n"
                       f"--- Debug Log ---\n{detail}")
    dbg.log("connect() succeeded, sending test command...")
    resp = client.run_command("__import__('unreal').SystemLibrary.get_engine_version()",
                              mode='eval', timeout=10)
    dbg.log(f"Test command result: success={resp.get('success')}, "
            f"result={resp.get('result', '')[:100]}, error={resp.get('error', '')[:100]}")
    client.disconnect()
    if resp.get('success'):
        ver = (resp.get('result', '') or '').strip()
        return True, f"Connected! UE version: {ver}"
    lines = dbg.get_lines()
    detail = "\n".join(lines)
    return False, (f"Connected but command failed: {resp.get('error', 'unknown')}\n\n"
                   f"--- Debug Log ---\n{detail}")

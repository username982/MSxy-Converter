# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mark Stuen
"""
network.py
----------
Raw UDP sACN (E1.31) receiver and sacn-library sender.

The receiver bypasses the sacn library entirely and opens its own UDP
socket with SO_REUSEADDR + SO_REUSEPORT so it can coexist with other
sACN listeners (e.g. sACNview) on the same machine and port.
"""

import socket
import struct
import threading
import traceback

# E1.31 protocol constants
SACN_PORT         = 5568
SACN_ACN_ID       = b"ASC-E1.17\x00\x00\x00"
SACN_ROOT_VECTOR  = 0x00000004   # VECTOR_ROOT_E131_DATA
SACN_FRAME_VECTOR = 0x00000002   # VECTOR_E131_DATA_PACKET
SACN_DMP_VECTOR   = 0x02         # VECTOR_DMP_SET_PROPERTY

# Byte offsets within a UDP payload
OFF_ACN_ID     =   4   # 12 bytes
OFF_ROOT_VEC   =  18   # 4 bytes  big-endian uint32
OFF_FRAME_VEC  =  40   # 4 bytes  big-endian uint32
OFF_UNIVERSE   = 113   # 2 bytes  big-endian uint16
OFF_DMP_VEC    = 117   # 1 byte
OFF_START_CODE = 125   # 1 byte   (0x00 = standard DMX)
OFF_DMX_DATA   = 126   # up to 512 bytes


def universe_to_multicast(universe: int) -> str:
    """E1.31 §9.3.1 — universe number → multicast IP 239.255.hi.lo."""
    return f"239.255.{(universe >> 8) & 0xFF}.{universe & 0xFF}"


def parse_sacn_packet(data: bytes):
    """
    Validate and parse a raw UDP E1.31 payload.

    Returns
    -------
    (dmx_bytes, universe)  on success — dmx_bytes is always 512 bytes
    (None, None)           if the packet is not a valid E1.31 DMX frame
    """
    if len(data) < OFF_DMX_DATA + 1:
        return None, None
    if data[OFF_ACN_ID : OFF_ACN_ID + 12] != SACN_ACN_ID:
        return None, None
    if struct.unpack_from(">I", data, OFF_ROOT_VEC)[0]  != SACN_ROOT_VECTOR:
        return None, None
    if struct.unpack_from(">I", data, OFF_FRAME_VEC)[0] != SACN_FRAME_VECTOR:
        return None, None
    if data[OFF_DMP_VEC]    != SACN_DMP_VECTOR:
        return None, None
    if data[OFF_START_CODE] != 0x00:
        return None, None

    universe  = struct.unpack_from(">H", data, OFF_UNIVERSE)[0]
    dmx_bytes = data[OFF_DMX_DATA:]
    if len(dmx_bytes) < 512:
        dmx_bytes = dmx_bytes + bytes(512 - len(dmx_bytes))
    else:
        dmx_bytes = dmx_bytes[:512]
    return dmx_bytes, universe


def build_rx_socket(bind_ip: str, universe: int) -> socket.socket:
    """
    Create and configure a UDP socket for receiving one sACN universe.

    SO_REUSEADDR + SO_REUSEPORT allow multiple applications (e.g. this
    converter and sACNview) to bind port 5568 simultaneously.

    For loopback addresses (127.x.x.x) the multicast group join is
    skipped — loopback interfaces do not support multicast.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass

    sock.bind(("", SACN_PORT))
    sock.settimeout(1.0)

    if bind_ip.startswith("127."):
        print(f"  RX bound to loopback {bind_ip}:{SACN_PORT}")
    else:
        mcast_addr = universe_to_multicast(universe)
        mreq = socket.inet_aton(mcast_addr) + socket.inet_aton(bind_ip)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        print(f"  RX joined multicast {mcast_addr} on {bind_ip}")

    return sock


def receiver_loop(bind_ip: str, universe: int, converter,
                  stop_event: threading.Event) -> None:
    """
    Thread target: receive sACN packets and pass DMX payloads to the
    converter.  Filters to the requested universe; ignores all others.
    """
    sock = None
    try:
        sock = build_rx_socket(bind_ip, universe)
        print(f"  RX listening for universe {universe}...", flush=True)
        while not stop_event.is_set():
            try:
                data, _ = sock.recvfrom(638)   # max valid E1.31 UDP payload
                dmx, pkt_uni = parse_sacn_packet(data)
                if dmx is None or pkt_uni != universe:
                    continue
                converter.process(dmx)
            except socket.timeout:
                continue
            except Exception as e:
                if not stop_event.is_set():
                    print(f"\n  RX ERROR: {e}", flush=True)
    except Exception as e:
        print(f"\n  FATAL RX SETUP ERROR: {e}", flush=True)
        traceback.print_exc()
    finally:
        if sock:
            sock.close()


def sender_loop(sender, universe: int, converter, fps: float = 44.0) -> None:
    """
    Thread target: write the converter's output buffer to the sacn sender
    at a steady fps regardless of input arrival rate.

    The sacn library's fps= parameter is only a ceiling on retransmission,
    not a heartbeat.  Without this dedicated loop the output rate falls back
    to the library's ~1 Hz keepalive when nothing has changed.
    """
    import time
    interval  = 1.0 / fps
    next_tick = time.monotonic()
    while True:
        buf, _ = converter.get_frame()
        try:
            sender[universe].dmx_data = tuple(buf)
        except Exception as e:
            print(f"\n  SENDER ERROR: {e}", flush=True)
        next_tick += interval
        sleep_for  = next_tick - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            next_tick = time.monotonic()

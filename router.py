#!/usr/bin/env python3
"""
Distance-Vector Router (DV-JSON over UDP, port 5000).
Bellman-Ford updates + Split Horizon on advertisements.
"""

import json
import os
import socket
import threading
import time

# --- Configuration (Docker-friendly) ---
MY_IP = os.getenv("MY_IP", "127.0.0.1")
NEIGHBORS = [n.strip() for n in os.getenv("NEIGHBORS", "").split(",") if n.strip()]
# Comma-separated CIDRs this router is directly connected to (distance 0).
LOCAL_SUBNETS = [s.strip() for s in os.getenv("LOCAL_SUBNETS", "").split(",") if s.strip()]
PORT = int(os.getenv("PORT", "5000"))
BROADCAST_INTERVAL = float(os.getenv("BROADCAST_INTERVAL", "5"))
NEIGHBOR_TIMEOUT = float(os.getenv("NEIGHBOR_TIMEOUT", "15"))

# routing_table[subnet] = [distance, next_hop_ip]
# next_hop "0.0.0.0" means directly connected (no forwarding hop).
routing_table = {}
neighbor_last_seen = {}
lock = threading.Lock()

# Track what we installed in Linux so we can delete stale entries.
_installed_routes = {}  # subnet -> next_hop


def _local_set():
    return set(LOCAL_SUBNETS)


def _ip_route_ok(cmd: str) -> bool:
    """Run `ip route ...` via shell; return True if exit status 0."""
    return os.system(cmd) == 0


def sync_linux_routes():
    """
    Push routing_table into the kernel: non-local routes use
    'ip route replace <subnet> via <next_hop>'.
    Directly connected subnets are left to Docker/kernel defaults.
    """
    global _installed_routes
    desired = {}
    with lock:
        local = _local_set()
        for subnet, (dist, nh) in routing_table.items():
            if subnet in local:
                continue
            if dist > 0 and nh and nh != "0.0.0.0":
                desired[subnet] = nh

    # Remove routes that are no longer desired
    for subnet, nh in list(_installed_routes.items()):
        if subnet not in desired or desired[subnet] != nh:
            _ip_route_ok(f"ip route del {subnet} via {nh}")
            del _installed_routes[subnet]

    # Add or replace desired routes
    for subnet, nh in desired.items():
        if _installed_routes.get(subnet) == nh:
            continue
        if subnet in _installed_routes:
            old_nh = _installed_routes[subnet]
            _ip_route_ok(f"ip route del {subnet} via {old_nh}")
        if _ip_route_ok(f"ip route replace {subnet} via {nh}"):
            _installed_routes[subnet] = nh


def init_routing_table():
    """Directly connected subnets: distance 0, next hop 0.0.0.0."""
    with lock:
        for s in LOCAL_SUBNETS:
            routing_table[s] = [0, "0.0.0.0"]


def build_dv_packet_for_neighbor(neighbor_ip: str) -> dict:
    """
    Split Horizon: do not advertise subnet S to neighbor N if we use N as
    next_hop to reach S (prevents feeding a route back to its source).
    """
    routes = []
    with lock:
        for subnet, (dist, nh) in routing_table.items():
            if nh == neighbor_ip:
                continue
            routes.append({"subnet": subnet, "distance": dist})
    return {"router_id": MY_IP, "version": 1.0, "routes": routes}


def broadcast_updates():
    """Periodically send DV-JSON to each neighbor (split horizon per neighbor)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        while True:
            for n in NEIGHBORS:
                pkt = build_dv_packet_for_neighbor(n)
                data = json.dumps(pkt).encode("utf-8")
                sock.sendto(data, (n, PORT))
            time.sleep(BROADCAST_INTERVAL)
    finally:
        sock.close()


def update_logic(neighbor_ip: str, routes_from_neighbor: list):
    """
    Bellman-Ford: for each advertised subnet, cost = neighbor_metric + 1.
    Replace if strictly better, or refresh if current next_hop is this neighbor.
    """
    if neighbor_ip not in NEIGHBORS:
        return

    changed = False
    local = _local_set()

    with lock:
        neighbor_last_seen[neighbor_ip] = time.time()

        for entry in routes_from_neighbor:
            subnet = entry.get("subnet")
            if not subnet:
                continue
            if subnet in local:
                # Our own links stay at cost 0; ignore neighbor claims.
                continue

            try:
                nd = float(entry.get("distance", 1e9))
            except (TypeError, ValueError):
                continue

            new_cost = int(nd) + 1
            if new_cost > 255:
                new_cost = 255

            if subnet not in routing_table:
                routing_table[subnet] = [new_cost, neighbor_ip]
                changed = True
                print(
                    f"[update] {subnet} -> metric {new_cost} nh {neighbor_ip}",
                    flush=True,
                )
            else:
                cur_cost, cur_nh = routing_table[subnet]
                if new_cost < cur_cost or cur_nh == neighbor_ip:
                    if routing_table[subnet] != [new_cost, neighbor_ip]:
                        routing_table[subnet] = [new_cost, neighbor_ip]
                        changed = True
                        print(
                            f"[update] {subnet} -> metric {new_cost} nh {neighbor_ip}",
                            flush=True,
                        )

    if changed:
        sync_linux_routes()


def expire_stale_routes():
    """
    If a neighbor stops sending (e.g. container stopped), remove routes
    that depended on that neighbor so the table can reconverge elsewhere.
    """
    now = time.time()
    changed = False
    with lock:
        dead = [n for n, t in neighbor_last_seen.items() if now - t > NEIGHBOR_TIMEOUT]
        for n in dead:
            neighbor_last_seen.pop(n, None)
        to_del = []
        for subnet, (dist, nh) in routing_table.items():
            if subnet in _local_set():
                continue
            if nh != "0.0.0.0" and nh in dead:
                to_del.append(subnet)
        for subnet in to_del:
            routing_table.pop(subnet, None)
            changed = True
            print(f"[timeout] dropped {subnet} (neighbor silent)", flush=True)

    if changed:
        sync_linux_routes()


def listen_for_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", PORT))

    while True:
        data, addr = sock.recvfrom(65535)
        neighbor_ip = addr[0]
        try:
            pkt = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError):
            continue

        ver = pkt.get("version")
        if ver is None or float(ver) != 1.0:
            continue
        routes = pkt.get("routes", [])
        if not isinstance(routes, list):
            continue

        update_logic(neighbor_ip, routes)


def maintenance_loop():
    """Periodic expiry of routes through silent neighbors."""
    while True:
        time.sleep(2.0)
        expire_stale_routes()


def main():
    if not LOCAL_SUBNETS:
        print("ERROR: Set LOCAL_SUBNETS (comma-separated CIDRs), e.g. 10.0.1.0/24,10.0.3.0/24")
        return
    if not NEIGHBORS:
        print("WARNING: NEIGHBORS is empty; no peers to exchange with.")

    init_routing_table()
    sync_linux_routes()

    threading.Thread(target=broadcast_updates, daemon=True).start()
    threading.Thread(target=maintenance_loop, daemon=True).start()
    print(f"DV router {MY_IP} listening on UDP/{PORT}, neighbors={NEIGHBORS}, locals={LOCAL_SUBNETS}")
    listen_for_updates()


if __name__ == "__main__":
    main()

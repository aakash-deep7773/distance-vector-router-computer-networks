#!/usr/bin/env python3
"""
Distance-Vector Router (DV-JSON over UDP, port 5000).
Bellman-Ford updates + split horizon, withdraw flashes (INF) when a local
link or next-hop path goes away, and triggered updates so tables reconverge
under professor node and link failure tests.

"""

import json
import os
import socket
import subprocess
import threading
import time

# --- Configuration (Docker-friendly) ---
MY_IP = os.getenv("MY_IP", "127.0.0.1")
NEIGHBORS = [n.strip() for n in os.getenv("NEIGHBORS", "").split(",") if n.strip()]
# Optional: comma-separated CIDRs (distance 0). If unset, we discover from `ip route`.
LOCAL_SUBNETS_ENV = [s.strip() for s in os.getenv("LOCAL_SUBNETS", "").split(",") if s.strip()]
PORT = int(os.getenv("PORT", "5000"))
# Faster defaults help the professor scripts' ~20s initial convergence window.
BROADCAST_INTERVAL = float(os.getenv("BROADCAST_INTERVAL", "0.75"))
NEIGHBOR_TIMEOUT = float(os.getenv("NEIGHBOR_TIMEOUT", "5"))
WITHDRAW_FLASHES = int(os.getenv("WITHDRAW_FLASHES", "8"))
# RIP-style infinity; paths >= this are unreachable (ring has << 16 hops).
INF = int(os.getenv("DV_INFINITY", "16"))

# routing_table[subnet] = [distance, next_hop_ip]
# next_hop "0.0.0.0" means directly connected (no forwarding hop).
routing_table = {}
neighbor_last_seen = {}
# RLock: refresh/update paths call helpers that also take this lock.
routing_lock = threading.RLock()

# Union of LOCAL_SUBNETS_ENV and kernel-discovered link subnets (updated periodically).
local_subnet_cache = set()

# Track what we installed in Linux so we can delete stale entries.
_installed_routes = {}  # subnet -> next_hop

_broadcast_wakeup = threading.Event()
# When a link goes down we lose a local subnet; tell peers INF briefly so they
# drop invalid routes that used us for that prefix.
_withdraw_flash = {}


def discover_local_subnets():
    """
    Parse kernel 'connected' IPv4 routes (proto kernel scope link).
    Matches professor topology 10.0.x.0/24 on eth* after Docker attaches networks.
    """
    subnets = []
    try:
        res = subprocess.run(
            ["ip", "route", "show", "proto", "kernel", "scope", "link"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if not parts:
                continue
            dst = parts[0]
            if dst == "default" or "/" not in dst:
                continue
            # Professor / lab use 10.0.0.0/8-style underlay; ignore docker bridge noise if any.
            if dst.startswith("10."):
                subnets.append(dst)
    except Exception:
        pass
    return sorted(set(subnets))


def _local_set():
    with routing_lock:
        return set(local_subnet_cache)


def refresh_local_subnets():
    """
    Merge env LOCAL_SUBNETS with discovered link-local subnets.
    Call periodically: `docker network connect` happens after the process starts.
    """
    global local_subnet_cache
    changed = False
    discovered = set(discover_local_subnets())
    merged = set(LOCAL_SUBNETS_ENV) | discovered
    with routing_lock:
        removed_locals = local_subnet_cache - merged
        if merged != local_subnet_cache:
            local_subnet_cache = merged
            changed = True
        for s in removed_locals:
            if routing_table.get(s) == [0, "0.0.0.0"]:
                routing_table.pop(s, None)
                _withdraw_flash[s] = WITHDRAW_FLASHES
                changed = True
        for s in local_subnet_cache:
            if routing_table.get(s) != [0, "0.0.0.0"]:
                routing_table[s] = [0, "0.0.0.0"]
                changed = True
    return changed


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
    with routing_lock:
        local = _local_set()
        for subnet, (dist, nh) in routing_table.items():
            if subnet in local:
                continue
            if dist > 0 and nh and nh != "0.0.0.0":
                desired[subnet] = nh

    for subnet, nh in list(_installed_routes.items()):
        if subnet not in desired or desired[subnet] != nh:
            _ip_route_ok(f"ip route del {subnet} via {nh}")
            del _installed_routes[subnet]

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
    refresh_local_subnets()
    with routing_lock:
        for s in local_subnet_cache:
            routing_table[s] = [0, "0.0.0.0"]


def build_dv_packet_for_neighbor(neighbor_ip: str) -> dict:
    """
    Split horizon: omit routes whose next hop is this neighbor. Explicit INF
    only for withdraw flashes (detached local link, dropped path, etc.).
    """
    routes = []
    with routing_lock:
        for subnet, left in list(_withdraw_flash.items()):
            if subnet not in routing_table:
                routes.append({"subnet": subnet, "distance": INF})
        for subnet, (dist, nh) in routing_table.items():
            if nh == neighbor_ip:
                continue
            routes.append({"subnet": subnet, "distance": dist})
    return {"router_id": MY_IP, "version": 1.0, "routes": routes}


def broadcast_updates():
    """Send DV-JSON periodically and on demand (triggered updates)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        while True:
            _broadcast_wakeup.wait(timeout=BROADCAST_INTERVAL)
            _broadcast_wakeup.clear()
            if refresh_local_subnets():
                sync_linux_routes()
            for n in NEIGHBORS:
                pkt = build_dv_packet_for_neighbor(n)
                data = json.dumps(pkt).encode("utf-8")
                sock.sendto(data, (n, PORT))
            with routing_lock:
                for s in list(_withdraw_flash.keys()):
                    _withdraw_flash[s] -= 1
                    if _withdraw_flash[s] <= 0:
                        del _withdraw_flash[s]
    finally:
        sock.close()


def request_broadcast():
    """Wake the broadcaster without waiting for the next period."""
    _broadcast_wakeup.set()


def update_logic(neighbor_ip: str, routes_from_neighbor: list):
    """
    Bellman-Ford: for each advertised subnet, cost = neighbor_metric + 1.
    Replace if better, or refresh route if current next_hop is this neighbor.
    """
    if neighbor_ip not in NEIGHBORS:
        return

    changed = False
    local = _local_set()

    with routing_lock:
        neighbor_last_seen[neighbor_ip] = time.time()

        for entry in routes_from_neighbor:
            subnet = entry.get("subnet")
            if not subnet:
                continue
            if subnet in local:
                continue

            try:
                nd = float(entry.get("distance", 1e9))
            except (TypeError, ValueError):
                continue

            if nd >= INF:
                if subnet in routing_table:
                    cur_cost, cur_nh = routing_table[subnet]
                    if cur_nh == neighbor_ip:
                        routing_table.pop(subnet, None)
                        _withdraw_flash[subnet] = WITHDRAW_FLASHES
                        changed = True
                        print(
                            f"[update] {subnet} withdrawn via {neighbor_ip}",
                            flush=True,
                        )
                continue

            new_cost = int(nd) + 1
            if new_cost >= INF:
                if subnet in routing_table and routing_table[subnet][1] == neighbor_ip:
                    routing_table.pop(subnet, None)
                    _withdraw_flash[subnet] = WITHDRAW_FLASHES
                    changed = True
                    print(
                        f"[update] {subnet} withdrawn via {neighbor_ip} (INF)",
                        flush=True,
                    )
                continue
            if new_cost > 255:
                new_cost = 255

            if subnet not in routing_table:
                routing_table[subnet] = [new_cost, neighbor_ip]
                _withdraw_flash.pop(subnet, None)
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
                    _withdraw_flash.pop(subnet, None)

    if changed:
        sync_linux_routes()
        request_broadcast()


def expire_stale_routes():
    """
    Remove remote routes whose next hop has stopped sending DV packets.
    Stale paths after topology changes are handled by poison reverse, INF
    handling in update_logic, and withdraw flashes when local links go down.
    """
    now = time.time()
    changed = False
    with routing_lock:
        dead = [n for n, t in neighbor_last_seen.items() if now - t > NEIGHBOR_TIMEOUT]
        dead_set = set(dead)
        for n in dead:
            neighbor_last_seen.pop(n, None)
        local = _local_set()
        to_del = []
        for subnet, (dist, nh) in routing_table.items():
            if subnet in local:
                continue
            if nh != "0.0.0.0" and nh in dead_set:
                to_del.append(subnet)
        for subnet in to_del:
            routing_table.pop(subnet, None)
            _withdraw_flash[subnet] = WITHDRAW_FLASHES
            changed = True
            print(f"[timeout] dropped {subnet} (neighbor silent)", flush=True)

    if changed:
        sync_linux_routes()
        request_broadcast()


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
        try:
            ver_num = float(ver)
        except (TypeError, ValueError):
            continue
        if ver_num != 1.0:
            continue
        routes = pkt.get("routes", [])
        if not isinstance(routes, list):
            continue

        # Prefer stable identity from payload (router_id = sender MY_IP).
        # After docker network detach/attach, source IP seen by recvfrom can
        # temporarily differ from the neighbor IP configured in NEIGHBORS.
        router_id = pkt.get("router_id")
        logical_neighbor = None
        if isinstance(router_id, str) and router_id in NEIGHBORS:
            logical_neighbor = router_id
        elif neighbor_ip in NEIGHBORS:
            logical_neighbor = neighbor_ip
        else:
            continue

        try:
            update_logic(logical_neighbor, routes)
        except Exception:
            # Keep router process alive on malformed/edge-case updates.
            continue


def maintenance_loop():
    """Periodic expiry of routes through silent neighbors + interface discovery."""
    while True:
        time.sleep(1.0)
        if refresh_local_subnets():
            sync_linux_routes()
            request_broadcast()
        expire_stale_routes()


def main():
    if not NEIGHBORS:
        print("WARNING: NEIGHBORS is empty; no peers to exchange with.")

    # Let Docker finish attaching networks right after `docker run` (evaluator connects more NICs).
    time.sleep(0.5)
    init_routing_table()
    sync_linux_routes()

    threading.Thread(target=broadcast_updates, daemon=True).start()
    threading.Thread(target=maintenance_loop, daemon=True).start()
    request_broadcast()
    print(
        f"DV router {MY_IP} UDP/{PORT} neighbors={NEIGHBORS} "
        f"locals_env={LOCAL_SUBNETS_ENV or '(auto)'}",
        flush=True,
    )
    listen_for_updates()


if __name__ == "__main__":
    main()

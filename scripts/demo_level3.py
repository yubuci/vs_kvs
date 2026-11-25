"""
Level 3 demo: sharding with consistent hashing, live, on your own machine.

Run (from the repository root):
    python -m scripts.demo_level3                          # servers_level3.json, 600 keys
    python -m scripts.demo_level3 --vnodes 1               # 1 virtual node per server: unbalanced, some server fills up
    python -m scripts.demo_level3 --placement modn         # hash mod N instead of the ring

What it shows, in order:
  1. Three servers hold more than 256 records together (one server alone cannot).
  2. Two independent clients agree on where every key lives (placement is deterministic).
  3. When one server stops, exactly ITS keys become unreadable and all others still work.
     (Level 3 has no replication and no failover: this is the reason for Level 4.)

The servers are started by scripts/start_cluster.py and are always stopped at the end.
"""

import argparse
import sys

from client.client import KVError, ShardedClient, format_stats_report
from common.config import ConfigError, load_shard_config
from common.protocol import MAX_KEYS
from common.ring import make_placement, node_id
from scripts.start_cluster import ClusterError, start_cluster, stop_cluster

KEY_COUNT = 600


def key_name(i):
    return f"key{i}"            # at most 16 characters (protocol limit) for any i < 10**12


def write_keys(client, indexes):
    """SET every key; return (stored, refused): the indexes answered OK and answered ERR FULL."""
    stored, refused = [], []
    for i in indexes:
        response = client.send_command(f"SET {key_name(i)} value-{i}")
        if response == "OK":
            stored.append(i)
        elif response == "ERR FULL":
            refused.append(i)
        else:
            raise KVError(f"unexpected answer to SET {key_name(i)}: {response}")
    return stored, refused


def read_keys(client, indexes):
    """GET every key; return (readable, unreadable). Readable = the stored value came back."""
    readable, unreadable = [], []
    for i in indexes:
        try:
            ok = client.send_command(f"GET {key_name(i)}") == f"VALUE value-{i}"
        except KVError:
            ok = False
        (readable if ok else unreadable).append(i)
    return readable, unreadable


def run_demo(config, key_count=KEY_COUNT, out=print):
    """Run the whole scenario; `config` is the dict from load_shard_config (placement/vnodes may be overridden)."""
    servers, placement, vnodes = config["servers"], config["placement"], config["vnodes"]
    ids = [node_id(s["host"], s["port"]) for s in servers]
    owner_of = make_placement(placement, ids, vnodes)      # the same function the clients use

    out(f"Placement: {placement}" + (f", vnodes={vnodes}" if placement == "ring" else ""))
    out(f"Servers:   {', '.join(ids)}   (capacity {MAX_KEYS} keys each)\n")

    running = start_cluster(servers, replication=False, quiet=True, cluster_file=config["path"],
                            placement=placement, vnodes=vnodes)
    try:
        half = key_count // 2
        with ShardedClient(servers, placement, vnodes) as client_a, \
                ShardedClient(servers, placement, vnodes) as client_b:

            # --- 1. fill the cluster from two clients -----------------------------------------
            out(f"1. Two clients write {key_count} keys (A: first {half}, B: the rest)")
            stored_a, refused_a = write_keys(client_a, range(half))
            stored_b, refused_b = write_keys(client_b, range(half, key_count))
            stored, refused = len(stored_a) + len(stored_b), set(refused_a + refused_b)
            out(format_stats_report(client_a.stats_per_server()))
            out(f"   stored {stored}, refused with ERR FULL: {len(refused)}")
            if stored > MAX_KEYS:
                out(f"   -> {stored} > {MAX_KEYS}: one server alone could never hold this.\n")
            else:
                out("   -> a server filled up before the others (try more virtual nodes).\n")

            # --- 2. the clients agree --------------------------------------------------------
            out("2. Client B reads every key written by client A, and the other way round")
            read_a, bad_a = read_keys(client_b, range(half))
            read_b, bad_b = read_keys(client_a, range(half, key_count))
            missing = set(bad_a + bad_b)
            out(f"   readable: {len(read_a) + len(read_b)}, not readable: {len(missing)}"
                f"   (only the {len(refused)} keys refused with ERR FULL may be missing)")
            out(f"   the clients agree on every key that was stored: {missing == refused}\n")

            # --- 3. one server dies -----------------------------------------------------------
            victim_server, victim_proc = running[0]
            victim = node_id(victim_server["host"], victim_server["port"])
            out(f"3. Stopping {victim} ...")
            stop_cluster([(victim_server, victim_proc)])
            readable, unreadable = read_keys(client_a, range(key_count))
            victims_keys = {i for i in range(key_count) if owner_of(key_name(i)) == victim}
            out(f"   readable: {len(readable)}, not readable: {len(unreadable)}")
            out(f"   keys that belong to {victim}: {len(victims_keys)}")
            # Expected misses: the dead server's keys, plus keys that were never stored (ERR FULL)
            out(f"   the unreadable keys are exactly {victim}'s (plus any ERR FULL keys): "
                f"{set(unreadable) == victims_keys | refused}")
            out(format_stats_report(client_a.stats_per_server()))
            out("\n   No replication, no failover: the dead server's data is unreachable.")
            out("   That is the reason for Level 4.")
            return {"stored": stored, "refused": len(refused), "missing_before_stop": len(missing),
                    "unreadable_after_stop": len(unreadable), "victim_keys": len(victims_keys)}
    finally:
        stop_cluster(running)


def main():
    parser = argparse.ArgumentParser(description="Level 3 demo: sharding with consistent hashing")
    parser.add_argument("--config", default="servers_level3.json", help="sharding file (default: servers_level3.json)")
    parser.add_argument("--placement", choices=("ring", "modn"), help="override the file's placement")
    parser.add_argument("--vnodes", type=int, help="override the file's vnodes (ring only)")
    parser.add_argument("--keys", type=int, default=KEY_COUNT, help=f"number of keys (default {KEY_COUNT})")
    args = parser.parse_args()
    if args.keys < 2 or args.vnodes is not None and args.vnodes < 1:
        parser.error("--keys must be at least 2 and --vnodes at least 1")

    try:
        config = load_shard_config(args.config)
        config["path"] = args.config
        config["placement"] = args.placement or config["placement"]
        config["vnodes"] = args.vnodes or config["vnodes"]
        run_demo(config, args.keys)
    except (ConfigError, ClusterError, KVError) as e:
        print(f"Hata: {e}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
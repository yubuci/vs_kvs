"""
Placement of keys on servers (Level 3): consistent hashing and hash mod N.

The one place that decides which server owns a key. Client, server and the
measurement scripts all call make_placement(); nobody computes placement by hand.
"""

import bisect
import hashlib

DEFAULT_VNODES = 50


def node_id(host, port):
    """The one and only spelling of a node: "127.0.0.1:5000"."""
    return f"{host}:{port}"


def stable_hash(text):
    """md5 of the UTF-8 text; first 8 bytes as an unsigned big-endian 64-bit integer.

    Python's built-in hash() is NOT used: it gives different results in every process.
    """
    digest = hashlib.md5(text.encode("utf-8")).digest()  # 16 bytes
    return int.from_bytes(digest[:8], "big")


class HashRing:
    """Consistent hashing ring with virtual nodes."""

    def __init__(self, nodes, vnodes=DEFAULT_VNODES):
        nodes = list(nodes)
        if not nodes or vnodes < 1:                                       
            raise ValueError("a ring needs at least one node and vnodes >= 1")
        if len(set(nodes)) != len(nodes):
            raise ValueError("duplicate node in the list")   # would make nodes_for() loop forever
        self._nodes = nodes
        self.vnodes = vnodes

        # Every node puts `vnodes` points on the ring: the hash of "node#0", "node#1", ...
        points = []
        for node in nodes:
            for i in range(vnodes):
                points.append((stable_hash(f"{node}#{i}"),node))                        
        points.sort()                                     # by position; ties broken by node name

        # Two parallel lists in ring order: position i belongs to owner i
        self._hashes = [position for position, _owner in points]                             
        self._owners = [owner for position, owner in points]

    @property
    def nodes(self):
        """The node list, in the order it was given."""
        return list(self._nodes)

    def _index(self, key):
        """Index of the first point clockwise from the key's hash (wraps around at the end)."""
        i = bisect.bisect_right(self._hashes, stable_hash(key))        
        return i % len(self._hashes)                                    

    def node_for(self, key):
        """The node that owns the key."""
        return self._owners[self._index(key)]                                      

    def nodes_for(self, key, n):
        """The first n DIFFERENT physical nodes clockwise from the key (for Level 4)."""
        wanted = min(n, len(self._nodes))                 # n larger than the node count -> all nodes
        result = []
        i = self._index(key)
        while len(result) < wanted:
            node = self._owners[i]
            if node not in result:                                   
                result.append(node)
            i = (i + 1) % len(self._owners)
        return result


def modn_node_for(key, nodes):
    """Placement by hash mod N (for comparison with the ring)."""
    return nodes[stable_hash(key) % len(nodes)]


def make_placement(mode, nodes, vnodes=DEFAULT_VNODES):
    """Return a function key -> node_id. mode is "ring" or "modn"."""
    nodes = list(nodes)
    if mode == "ring":
        ring = HashRing(nodes, vnodes)
        return ring.node_for
    if mode == "modn":
        if not nodes:
            raise ValueError("modn needs at least one node")
        return lambda key: modn_node_for(key, nodes)
    raise ValueError(f"unknown placement mode: {mode!r}")
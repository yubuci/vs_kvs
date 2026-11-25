"""Unit tests for common/config.py.

Every test writes its own small file into a temporary directory, so the tests
never touch the real servers.json.

Run (from the repository root):
    python -m unittest tests.test_config -v
"""

import json
import os
import tempfile
import unittest

from common.config import (
    ConfigError, VALID_PLACEMENTS, backups_of, load_servers, load_shard_config, primary_of,
)
from common.ring import DEFAULT_VNODES, make_placement

# A valid cluster; tests copy it and break one thing at a time
PRIMARY = {"host": "127.0.0.1", "port": 5000, "role": "primary"}
BACKUP1 = {"host": "127.0.0.1", "port": 5001, "role": "backup"}
BACKUP2 = {"host": "127.0.0.1", "port": 5002, "role": "backup"}

# Level 3: the same three addresses without a role
NODE0 = {"host": "127.0.0.1", "port": 5000}
NODE1 = {"host": "127.0.0.1", "port": 5001}
NODE2 = {"host": "127.0.0.1", "port": 5002}


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)                  # the directory is deleted when the test ends
        self.dir = tmp.name

    def write(self, content, name="servers.json"):
        """Write content (a dict/list is turned into JSON, a str/bytes is written as it is)."""
        path = os.path.join(self.dir, name)
        if isinstance(content, bytes):
            with open(path, "wb") as f:
                f.write(content)
        else:
            text = content if isinstance(content, str) else json.dumps(content)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return path

    def assertConfigError(self, content, fragment=""):
        """load_servers must raise ConfigError; fragment (optional) must appear in the message."""
        path = self.write(content)
        with self.assertRaises(ConfigError) as ctx:
            load_servers(path)
        self.assertIn(fragment, str(ctx.exception))

    def assertShardError(self, content, fragment=""):
        """Same as assertConfigError, but for load_shard_config (Level 3)."""
        path = self.write(content, "servers_level3.json")
        with self.assertRaises(ConfigError) as ctx:
            load_shard_config(path)
        self.assertIn(fragment, str(ctx.exception))


class TestLoadServers(ConfigTestCase):
    def test_valid_file(self):
        path = self.write({"servers": [PRIMARY, BACKUP1, BACKUP2]})
        self.assertEqual(load_servers(path), [PRIMARY, BACKUP1, BACKUP2])   # file order is kept

    def test_the_real_servers_json_is_valid(self):
        # The file in the repository root is part of the contract: it must always load
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        servers = load_servers(os.path.join(root, "servers.json"))
        self.assertEqual(len(servers), 3)

    def test_unknown_extra_fields_are_dropped(self):
        path = self.write({"servers": [dict(PRIMARY, comment="main node")]})
        self.assertEqual(load_servers(path), [PRIMARY])

    def test_file_missing(self):
        with self.assertRaises(ConfigError) as ctx:
            load_servers(os.path.join(self.dir, "yok.json"))
        self.assertIn("not found", str(ctx.exception))

    def test_path_is_a_directory(self):
        with self.assertRaises(ConfigError):
            load_servers(self.dir)

    def test_invalid_json(self):
        self.assertConfigError('{"servers": [', "not valid JSON")

    def test_not_utf8(self):
        self.assertConfigError(b"\xff\xfe\x00\x00 bozuk", "not valid JSON")

    def test_servers_list_missing(self):
        self.assertConfigError({"nodes": [PRIMARY]}, "servers")
        self.assertConfigError([PRIMARY], "servers")                 # top level is a list, not an object
        self.assertConfigError({"servers": "5000"}, "servers")       # "servers" is not a list

    def test_empty_servers_list(self):
        self.assertConfigError({"servers": []}, "empty")

    def test_entry_is_not_an_object(self):
        self.assertConfigError({"servers": [PRIMARY, "127.0.0.1:5001"]}, "server #2")

    def test_entry_missing_field(self):
        for field in ("host", "port", "role"):
            with self.subTest(field=field):
                broken = {k: v for k, v in BACKUP1.items() if k != field}
                self.assertConfigError({"servers": [PRIMARY, broken]}, f'"{field}"')

    def test_wrong_types(self):
        bad_entries = [
            dict(BACKUP1, port="5001"),        # port as text
            dict(BACKUP1, port=True),          # bool counts as int in Python: must be rejected
            dict(BACKUP1, port=5001.0),        # float
            dict(BACKUP1, host=127),           # host not a string
            dict(BACKUP1, host=""),            # empty host
        ]
        for entry in bad_entries:
            with self.subTest(entry=entry):
                self.assertConfigError({"servers": [PRIMARY, entry]})

    def test_port_range(self):
        for port in (0, -1, 65536):
            with self.subTest(port=port):
                self.assertConfigError({"servers": [PRIMARY, dict(BACKUP1, port=port)]}, "port")
        for port in (1, 65535):                                       # the limits themselves are valid
            with self.subTest(port=port):
                path = self.write({"servers": [PRIMARY, dict(BACKUP1, port=port)]})
                self.assertEqual(len(load_servers(path)), 2)

    def test_unknown_role(self):
        self.assertConfigError({"servers": [PRIMARY, dict(BACKUP1, role="leader")]}, "role")
        self.assertConfigError({"servers": [PRIMARY, dict(BACKUP1, role="Backup")]}, "role")   # case matters

    def test_duplicate_address(self):
        self.assertConfigError({"servers": [PRIMARY, BACKUP1, dict(BACKUP2, port=5001)]}, "twice")

    def test_no_primary(self):
        self.assertConfigError({"servers": [BACKUP1, BACKUP2]}, "exactly one primary")

    def test_two_primaries(self):
        self.assertConfigError({"servers": [PRIMARY, dict(BACKUP1, role="primary")]}, "exactly one primary")


class TestHelpers(unittest.TestCase):
    SERVERS = [BACKUP1, PRIMARY, BACKUP2]        # primary is NOT first: the helpers must look at the role

    def test_primary_of(self):
        self.assertEqual(primary_of(self.SERVERS), PRIMARY)

    def test_backups_of_keeps_file_order(self):
        self.assertEqual(backups_of(self.SERVERS), [BACKUP1, BACKUP2])

    def test_primary_of_without_primary_raises(self):
        with self.assertRaises(ConfigError):
            primary_of([BACKUP1, BACKUP2])

    def test_backups_of_without_backups_is_empty(self):
        self.assertEqual(backups_of([PRIMARY]), [])


class TestLoadShardConfig(ConfigTestCase):
    """Level 3: servers_level3.json. Mirrors TestLoadServers, one broken thing per test."""

    def load(self, content):
        return load_shard_config(self.write(content, "servers_level3.json"))

    def test_valid_file(self):
        config = self.load({"placement": "modn", "vnodes": 10, "servers": [NODE0, NODE1, NODE2]})
        self.assertEqual(config, {"placement": "modn", "vnodes": 10, "servers": [NODE0, NODE1, NODE2]})

    def test_defaults(self):
        # placement missing -> "ring"; vnodes missing -> the ring's own default (50)
        config = self.load({"servers": [NODE0, NODE1]})
        self.assertEqual(config["placement"], "ring")
        self.assertEqual(config["vnodes"], DEFAULT_VNODES)
        self.assertEqual(DEFAULT_VNODES, 50)                 # the value the contract promises

    def test_server_order_is_kept(self):
        config = self.load({"servers": [NODE2, NODE0, NODE1]})
        self.assertEqual(config["servers"], [NODE2, NODE0, NODE1])

    def test_single_server_is_valid(self):
        self.assertEqual(len(self.load({"servers": [NODE0]})["servers"]), 1)

    def test_unknown_extra_fields_are_dropped(self):
        config = self.load({"comment": "x", "servers": [dict(NODE0, label="main")]})
        self.assertEqual(config["servers"], [NODE0])

    def test_the_real_servers_level3_json_is_valid(self):
        # Like servers.json: the file in the repository root is part of the contract
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config = load_shard_config(os.path.join(root, "servers_level3.json"))
        self.assertEqual(len(config["servers"]), 3)
        self.assertIn(config["placement"], VALID_PLACEMENTS)

    def test_file_problems(self):
        with self.assertRaises(ConfigError) as ctx:                       # missing file
            load_shard_config(os.path.join(self.dir, "nope.json"))
        self.assertIn("not found", str(ctx.exception))
        with self.assertRaises(ConfigError):                              # a directory
            load_shard_config(self.dir)
        self.assertShardError("{ not json", "not valid JSON")
        self.assertShardError(b"\xff\xfe\x00bad", "not valid JSON")       # not UTF-8

    def test_structure_problems(self):
        self.assertShardError([NODE0], "servers")                          # top level is a list
        self.assertShardError({"placement": "ring"}, "servers")            # no servers key
        self.assertShardError({"servers": "127.0.0.1:5000"}, "servers")    # servers is not a list
        self.assertShardError({"servers": []}, "empty")
        self.assertShardError({"servers": [NODE0, "127.0.0.1:5001"]}, "server #2")   # entry not an object

    def test_unknown_placement(self):
        for placement in ("Ring", "hash", "", None, 3, ["ring"]):          # case matters, wrong types
            with self.subTest(placement=placement):
                self.assertShardError({"placement": placement, "servers": [NODE0]}, "placement")

    def test_bad_vnodes(self):
        # 0 and negatives make no ring; True is an int in Python (the same trap as the port); text, float, None
        for vnodes in (0, -1, True, False, "50", 50.0, None):
            with self.subTest(vnodes=vnodes):
                self.assertShardError({"vnodes": vnodes, "servers": [NODE0]}, "vnodes")

    def test_vnodes_is_checked_even_in_modn_mode(self):
        # modn ignores vnodes, but a nonsense value in the file is still a mistake worth reporting
        self.assertShardError({"placement": "modn", "vnodes": 0, "servers": [NODE0]}, "vnodes")

    def test_role_field_means_level2_file(self):
        self.assertShardError({"servers": [NODE0, dict(NODE1, role="backup")]}, "Level 2")

    def test_the_real_level2_servers_json_is_rejected(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with self.assertRaises(ConfigError) as ctx:
            load_shard_config(os.path.join(root, "servers.json"))
        self.assertIn("Level 2", str(ctx.exception))

    def test_missing_host_or_port(self):
        self.assertShardError({"servers": [{"port": 5000}]}, '"host"')
        self.assertShardError({"servers": [{"host": "127.0.0.1"}]}, '"port"')

    def test_wrong_types(self):
        bad_entries = [
            dict(NODE1, port="5001"), dict(NODE1, port=True), dict(NODE1, port=5001.0),
            dict(NODE1, host=127), dict(NODE1, host=""),
        ]
        for entry in bad_entries:
            with self.subTest(entry=entry):
                self.assertShardError({"servers": [NODE0, entry]}, "server #2")

    def test_port_range(self):
        for port in (0, -1, 65536):
            with self.subTest(port=port):
                self.assertShardError({"servers": [NODE0, dict(NODE1, port=port)]}, "port")
        for port in (1, 65535):
            with self.subTest(port=port):
                self.assertEqual(len(self.load({"servers": [NODE0, dict(NODE1, port=port)]})["servers"]), 2)

    def test_duplicate_address(self):
        self.assertShardError({"servers": [NODE0, NODE1, dict(NODE2, port=5001)]}, "twice")

    def test_localhost_and_127_are_different_addresses(self):
        # Documented limit: node ids are compared as text, so these count as TWO nodes (the plan's rule 4:
        # write every address the same way everywhere). If this test ever fails, the duplicate rule got smarter.
        config = self.load({"servers": [NODE0, dict(NODE0, host="localhost")]})
        self.assertEqual(len(config["servers"]), 2)

    def test_level3_file_is_rejected_by_load_servers(self):
        # The other direction: the Level 2 loader must not silently accept a sharding file
        path = self.write({"servers": [NODE0, NODE1]})
        with self.assertRaises(ConfigError):
            load_servers(path)


class TestPlacementContract(unittest.TestCase):
    """config.py lists the placement names; ring.py implements them. They must agree."""

    def test_every_listed_placement_is_implemented(self):
        for mode in VALID_PLACEMENTS:
            with self.subTest(mode=mode):
                place = make_placement(mode, ["127.0.0.1:5000", "127.0.0.1:5001"])
                self.assertIn(place("some-key"), ["127.0.0.1:5000", "127.0.0.1:5001"])

    def test_the_names_the_contract_promises(self):
        self.assertEqual(set(VALID_PLACEMENTS), {"ring", "modn"})


if __name__ == "__main__":
    unittest.main()
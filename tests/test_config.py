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

from common.config import ConfigError, backups_of, load_servers, primary_of

# A valid cluster; tests copy it and break one thing at a time
PRIMARY = {"host": "127.0.0.1", "port": 5000, "role": "primary"}
BACKUP1 = {"host": "127.0.0.1", "port": 5001, "role": "backup"}
BACKUP2 = {"host": "127.0.0.1", "port": 5002, "role": "backup"}


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


if __name__ == "__main__":
    unittest.main()

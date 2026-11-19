"""
Cluster configuration: reads servers.json (Level 2).

File format (see servers.json in the repository root):

    {"servers": [
        {"host": "127.0.0.1", "port": 5000, "role": "primary"},
        {"host": "127.0.0.1", "port": 5001, "role": "backup"}
    ]}

Design: ALL validation lives in load_servers(), in one place. A list that
load_servers() returned is therefore known to be valid, and primary_of() and
backups_of() can stay trivial. The alternative (checking again in every
function) would spread the same rules over three places.

Note: the default path "servers.json" is relative to the CURRENT directory,
so the client must be started from the repository root (like every other
command in the README). From another directory, pass --servers with a path.
"""

import json

ROLE_PRIMARY = "primary"
ROLE_BACKUP = "backup"
VALID_ROLES = (ROLE_PRIMARY, ROLE_BACKUP)

MAX_PORT = 65535


class ConfigError(Exception):
    """servers.json is missing or invalid. The client prints this message, not a traceback."""


def load_servers(path="servers.json"):
    """Read and validate the cluster file.

    Returns a list of {"host": str, "port": int, "role": "primary" | "backup"} dicts
    in file order (the order matters: the client asks servers in this order).
    Raises ConfigError (with a message that says what is wrong) for every problem.
    """
    # --- 1. Read the file ---------------------------------------------------
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        # "from None": hide the technical FileNotFoundError, the message is enough
        raise ConfigError(f"Config file not found: {path}") from None
    except OSError as e:                              # e.g. a directory, no permission
        raise ConfigError(f"Cannot read config file {path}: {e}") from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # JSONDecodeError: not valid JSON. UnicodeDecodeError: not UTF-8 at all.
        raise ConfigError(f"Config file {path} is not valid JSON: {e}") from e

    # --- 2. Check the structure ----------------------------------------------
    if not isinstance(data, dict) or not isinstance(data.get("servers"), list):
        raise ConfigError(f'Config file {path} must contain {{"servers": [...]}}')
    if not data["servers"]:
        raise ConfigError(f"Config file {path}: the servers list is empty")

    # --- 3. Check every entry and build clean copies ---------------------------
    servers = []
    seen = set()                                      # (host, port) pairs, to find duplicates
    for index, entry in enumerate(data["servers"]):
        where = f"server #{index + 1}"                # human-friendly position in error messages
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: must be an object with host, port and role")
        for field in ("host", "port", "role"):
            if field not in entry:
                raise ConfigError(f'{where}: missing field "{field}"')

        host, port, role = entry["host"], entry["port"], entry["role"]
        if not isinstance(host, str) or not host:
            raise ConfigError(f'{where}: "host" must be a non-empty string')
        # bool is a subclass of int in Python: isinstance(True, int) is True, so exclude it explicitly
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= MAX_PORT:
            raise ConfigError(f'{where}: "port" must be an integer from 1 to {MAX_PORT}, got {port!r}')
        if role not in VALID_ROLES:
            raise ConfigError(f'{where}: "role" must be "primary" or "backup", got {role!r}')
        if (host, port) in seen:
            raise ConfigError(f"{where}: {host}:{port} is listed twice")
        seen.add((host, port))

        # A fresh dict with exactly the three known fields: unknown extra fields are dropped,
        # and the caller cannot change our parsed data by accident.
        servers.append({"host": host, "port": port, "role": role})

    # --- 4. The cluster as a whole: exactly one primary ----------------------------
    primaries = [s for s in servers if s["role"] == ROLE_PRIMARY]
    if len(primaries) != 1:
        raise ConfigError(f"Config file {path}: exactly one primary is required, found {len(primaries)}")
    return servers


def primary_of(servers):
    """Return the entry whose role is "primary" (the list must come from load_servers)."""
    for server in servers:
        if server["role"] == ROLE_PRIMARY:
            return server
    raise ConfigError("No primary in the server list")   # only reachable with a hand-made list


def backups_of(servers):
    """Return the entries whose role is "backup", in file order."""
    return [server for server in servers if server["role"] == ROLE_BACKUP]

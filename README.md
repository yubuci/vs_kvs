# Verteilter Key-Value-Store

Projekt für *Verteilte Systeme* (INF511), WS 2026, Türkisch-Deutsche Universität.
Aufgabe: DTS-WS26-P01.

**Aktueller Stand:** Level 1 fertig (ein Server, mehrere gleichzeitige Clients); Level 2 (Primary-Backup-Replikation) in Arbeit.

## Voraussetzungen

- Python 3.11 oder neuer
- keine externen Bibliotheken

Alle Befehle werden im Wurzelverzeichnis des Projekts ausgeführt.

## Server starten

```
python -m server.server --port 5000
```

Beenden mit `Strg+C`. Mehrere Server können auf verschiedenen Ports gleichzeitig laufen:

```
python -m server.server --port 5001
```

## Client

Einzelner Befehl:

```
python -m client.client --port 5000 SET stadt1 Istanbul
python -m client.client --port 5000 GET stadt1
```

Interaktive Shell (ohne Befehl starten, beenden mit `exit`):

```
python -m client.client --port 5000
kvs> SET stadt2 Bursa und Umgebung
OK
kvs> GET stadt2
VALUE Bursa und Umgebung
kvs> STATS
STATS 2
```

| Befehl | Antwort |
|---|---|
| `SET <key> <value>` | `OK` oder `ERR KEY` / `ERR VALUE` / `ERR FULL` |
| `GET <key>` | `VALUE <value>` oder `NOT_FOUND` |
| `STATS` | `STATS <Anzahl der Einträge>` |

Grenzen: Key 1–16 Zeichen ohne Leerzeichen, Value 1–4080 Zeichen, höchstens 256 Einträge pro Server.
Das vollständige Protokoll steht in [docs/protokol.md](docs/protokol.md).

## Level 2: Primary-Backup-Cluster

Drei Server: ein Primary und zwei Backups. Die Adressen stehen in `servers.json` (Wurzelverzeichnis).

Cluster starten (drei Terminals, zuerst der Primary):

```
python -m server.server --port 5000 --role primary --peers 127.0.0.1:5001,127.0.0.1:5002
python -m server.server --port 5001 --role backup --primary 127.0.0.1:5000
python -m server.server --port 5002 --role backup --primary 127.0.0.1:5000
```

Client im Cluster-Modus (`--servers` statt `--port`; beides zusammen ist nicht erlaubt):

```
python -m client.client --servers servers.json SET stadt1 Istanbul
python -m client.client --servers servers.json GET stadt1
python -m client.client --servers servers.json
```

Regeln des Clients:

- `SET` geht an den Primary; `GET` und `STATS` gehen an den ersten erreichbaren Server der Liste.
- Antwortet ein Server mit `ERR NOT_PRIMARY <port>`, wiederholt der Client den Befehl einmal beim genannten Port.
- Ist der Primary nicht erreichbar, schlägt `SET` mit einer Fehlermeldung fehl (Exit-Code 2); `GET` wird weiter von einem Backup beantwortet. Einen neuen Primary gibt es erst in Level 5.
- Eine fehlerhafte `servers.json` ergibt eine verständliche Fehlermeldung statt eines Tracebacks.

## Tests

Unit-Tests (Protokoll und Store):

```
python -m unittest discover -s tests -t . -v
```

Lasttest über das Netzwerk (startet seine eigenen Server auf den Ports ab 5100):

```
python -m tests.load_test
```

- Test A: 300 Clients fügen gleichzeitig neue Keys ein → genau 256 `OK`, 44 `ERR FULL`
- Test B: 50 Clients schreiben gleichzeitig denselben Key → ein konsistenter Eintrag

## Projektstruktur

```
common/protocol.py    Konstanten, Validierung, Parsen, Framing (inkl. REPL/SYNC/ENTRY)
common/config.py      servers.json lesen und prüfen (Level 2)
servers.json          Adressen und Rollen der Cluster-Server (Level 2)
server/store.py       Speicher im RAM mit Lock (max. 256 Einträge)
server/server.py      TCP-Server, ein Thread pro Verbindung
client/client.py      Client (Einzelbefehl, Shell, Cluster-Modus mit Routing)
tests/                Unit-Tests und Lasttest
docs/protokol.md      Protokollbeschreibung
docs/notlar.md        Entscheidungen und Probleme (Projekttagebuch)
```

## Bekannte Einschränkungen (Level 1)

- Die Daten liegen nur im RAM: Beim Beenden des Servers gehen sie verloren.
- Replikation nur vom Primary zu den Backups (Level 2); kein automatischer Primary-Wechsel (Level 5) und keine Partitionierung (Level 3).
- Keine Authentifizierung und keine Verschlüsselung (laut Aufgabenstellung nicht verlangt).

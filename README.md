# Verteilter Key-Value-Store

Projekt für *Verteilte Systeme* (INF511), WS 2026, Türkisch-Deutsche Universität.
Aufgabe: DTS-WS26-P01.

**Aktueller Stand: Level 1** – ein Server, mehrere gleichzeitige Clients.

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
common/protocol.py    Konstanten, Validierung, Parsen, Framing
server/store.py       Speicher im RAM mit Lock (max. 256 Einträge)
server/server.py      TCP-Server, ein Thread pro Verbindung
client/client.py      Client (Einzelbefehl und Shell)
tests/                Unit-Tests und Lasttest
docs/protokol.md      Protokollbeschreibung
docs/notlar.md        Entscheidungen und Probleme (Projekttagebuch)
```

## Bekannte Einschränkungen (Level 1)

- Die Daten liegen nur im RAM: Beim Beenden des Servers gehen sie verloren.
- Server kennen einander nicht; es gibt noch keine Replikation und keine Partitionierung.
- Keine Authentifizierung und keine Verschlüsselung (laut Aufgabenstellung nicht verlangt).

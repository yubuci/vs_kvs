"""
Kümeyi tek komutla başlatır: 1 primary + N backup (varsayılan 3 sunucu).

Çalıştırma (depo kökünden):
    python -m scripts.start_cluster                       # servers.json ya da varsayılan küme
    python -m scripts.start_cluster --config servers.json
    python -m scripts.start_cluster --replication off     # herkes rolsüz (Level 1 davranışı, Level 3 için)
    python -m scripts.start_cluster --log-dir logs        # her sunucunun çıktısı ayrı dosyaya

Ctrl+C ile bütün sunucular kapatılır.

Diğer betikler (ör. tests/test_cluster.py) start_cluster() ve stop_cluster()
fonksiyonlarını doğrudan kullanabilir.
"""

import argparse
import json
import os
import subprocess
import sys
import time

from client.client import KVClient, KVError

# Depo kökü: betik nereden çağrılırsa çağrılsın sunucular buradan başlatılır
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# servers.json yoksa kullanılan küme (docs/level2_gorevler.md, bölüm 2.2 ile aynı)
DEFAULT_SERVERS = [
    {"host": "127.0.0.1", "port": 5000, "role": "primary"},
    {"host": "127.0.0.1", "port": 5001, "role": "backup"},
    {"host": "127.0.0.1", "port": 5002, "role": "backup"},
]

READY_TIMEOUT = 5.0   # saniye; bir sunucunun bağlantı kabul etmeye başlaması için


class ClusterError(Exception):
    """Küme başlatılamadı (port dolu, yapılandırma hatalı, sunucu cevap vermiyor)."""


# ---------------------------------------------------------------------------
# Yapılandırma
# ---------------------------------------------------------------------------

def load_config(path):
    """Sunucu listesini okur. Dosya yoksa varsayılan kümeyi döndürür.

    TODO: Öğrenci2'nin common/config.py'si main'e gelince load_servers() kullanılacak.
    """
    if not os.path.exists(path):
        return [dict(s) for s in DEFAULT_SERVERS], False
    try:
        with open(path, encoding="utf-8") as f:
            servers = json.load(f)["servers"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ClusterError(f"{path} okunamadı: {e}") from e
    return servers, True


def check_config(servers, replication):
    """Listenin başlatılabilir olduğunu kontrol eder; sorun varsa ClusterError."""
    if not servers:
        raise ClusterError("sunucu listesi boş")
    ports = [s["port"] for s in servers]
    if len(set(ports)) != len(ports):
        raise ClusterError(f"aynı port birden fazla kez kullanılmış: {ports}")
    if replication:
        primaries = [s for s in servers if s.get("role") == "primary"]
        if len(primaries) != 1:
            raise ClusterError(f"tam olarak 1 primary olmalı, {len(primaries)} bulundu")


def addr(server):
    return f"{server['host']}:{server['port']}"


def build_command(server, servers, replication):
    """Bir sunucuyu başlatacak komut satırını üretir (docs/level2_gorevler.md, bölüm 2.1)."""
    cmd = [sys.executable, "-m", "server.server", "--host", server["host"], "--port", str(server["port"])]
    if not replication:
        return cmd                                   # rolsüz: Level 1 davranışı

    primary = next(s for s in servers if s.get("role") == "primary")
    if server is primary:
        backups = [addr(s) for s in servers if s is not primary]
        cmd += ["--role", "primary"]
        if backups:
            cmd += ["--peers", ",".join(backups)]
    else:
        cmd += ["--role", "backup", "--primary", addr(primary)]
    return cmd


# ---------------------------------------------------------------------------
# Başlatma ve durdurma
# ---------------------------------------------------------------------------

def port_in_use(host, port):
    """Portta şu an cevap veren bir program var mı?

    Gerekli, çünkü wait_ready() yalnızca "bu portta biri STATS'a cevap veriyor mu"
    diye bakar. Port zaten başka bir sunucudaysa onun cevabını alıp bizimki
    başlamadan "hazır" sanırdı.
    """
    try:
        with KVClient(host, port, timeout=0.5) as c:
            c.connect()
        return True
    except KVError:
        return False


def wait_ready(proc, host, port, timeout=READY_TIMEOUT):
    """Sunucu STATS'a cevap verene kadar bekler. Süreç ölürse ya da süre dolarsa ClusterError."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise ClusterError(f"{host}:{port} hemen kapandı (çıkış kodu {proc.returncode}); port dolu olabilir")
        try:
            with KVClient(host, port, timeout=0.5) as c:
                c.send_command("STATS")
            return
        except KVError:
            time.sleep(0.1)                          # henüz dinlemiyor, biraz sonra tekrar dene
    raise ClusterError(f"{host}:{port} {timeout:g} sn içinde hazır olmadı")


def start_cluster(servers, replication=True, log_dir=None, quiet=False):
    """Sunucuları başlatır ve hepsi hazır olunca [(server, proc), ...] döndürür.

    Sıra: önce primary, sonra backup'lar. Bir backup açılırken primary'den SYNC
    çektiği için primary'nin o anda dinliyor olması gerekir.
    Bir sunucu başlamazsa o ana kadar başlatılanlar kapatılır ve ClusterError fırlatılır.
    """
    check_config(servers, replication)
    if replication:
        ordered = sorted(servers, key=lambda s: s.get("role") != "primary")   # primary başa
    else:
        ordered = list(servers)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    running = []
    try:
        for server in ordered:
            if port_in_use(server["host"], server["port"]):
                raise ClusterError(f"{addr(server)} zaten kullanılıyor (başka bir sunucu açık mı?)")
            if log_dir:
                out = open(os.path.join(log_dir, f"{server['port']}.log"), "w", encoding="utf-8")
            elif quiet:
                out = subprocess.DEVNULL
            else:
                out = None                           # sunucu logu bu terminale akar ([port] önekiyle)
            proc = subprocess.Popen(build_command(server, servers, replication),
                                    cwd=REPO_ROOT, stdout=out, stderr=out)
            running.append((server, proc))
            wait_ready(proc, server["host"], server["port"])
    except BaseException:
        stop_cluster(running)                        # yarım kalmış kümeyi geride bırakma
        raise
    return running


def stop_cluster(running, timeout=5.0):
    """Bütün sunucuları kapatır: önce nazikçe (terminate), cevap vermeyeni zorla (kill)."""
    for _server, proc in running:
        if proc.poll() is None:
            proc.terminate()
    for _server, proc in running:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# Komut satırı
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Key-Value-Store kümesini başlatır")
    parser.add_argument("--config", default=os.path.join(REPO_ROOT, "servers.json"),
                        help="sunucu listesi (varsayılan: depo kökündeki servers.json; yoksa 5000/5001/5002)")
    parser.add_argument("--replication", choices=["on", "off"], default="on",
                        help="off: bütün sunucular rolsüz başlar (Level 1 davranışı)")
    parser.add_argument("--log-dir", default=None,
                        help="her sunucunun çıktısını bu klasörde <port>.log dosyasına yaz")
    args = parser.parse_args()
    replication = args.replication == "on"

    try:
        servers, from_file = load_config(args.config)
        kaynak = args.config if from_file else "varsayılan küme (servers.json yok)"
        print(f"Yapılandırma: {kaynak}")
        running = start_cluster(servers, replication=replication, log_dir=args.log_dir)
    except ClusterError as e:
        print(f"Hata: {e}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)

    print("\nKüme hazır:")
    for server, proc in running:
        rol = server.get("role", "-") if replication else "rolsüz"
        print(f"  {addr(server):<17} {rol:<8} pid {proc.pid}")
    print("Durdurmak için Ctrl+C.\n")

    try:
        while True:
            time.sleep(0.5)
            # Bir sunucu beklenmedik şekilde kapanırsa haber ver (ör. elle kapatıldı ya da çöktü)
            for server, proc in running:
                if proc.poll() is not None and not server.get("_bildirildi"):
                    print(f"Uyarı: {addr(server)} kapandı (çıkış kodu {proc.returncode})", file=sys.stderr)
                    server["_bildirildi"] = True
    except KeyboardInterrupt:
        print("\nKüme kapatılıyor...")
    finally:
        stop_cluster(running)
        print("Bütün sunucular kapatıldı.")


if __name__ == "__main__":
    main()

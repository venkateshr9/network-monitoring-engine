#!/usr/bin/env python3
import os
import ipaddress
import subprocess
import logging
from datetime import datetime
from dotenv import load_dotenv
import mysql.connector

# ================= LOAD ENV =================
load_dotenv("/opt/netmon/.env")

DB_CFG = {
    "host": os.getenv("DB_HOST"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASS"),
    "database": os.getenv("DB_NAME"),
}

SNMP_COMMUNITIES = [c.strip() for c in os.getenv("SNMP_COMMUNITIES", "").split(",")]
NETWORKS = os.getenv("NETWORKS", "").split(";")

LOG_DIR = os.getenv("LOG_DIR", "/opt/netmon/logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    filename=f"{LOG_DIR}/step1_network_discovery.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger().addHandler(console)

# ================= DB =================
def db_connect():
    return mysql.connector.connect(**DB_CFG)

# ================= ICMP =================
def ping(ip):
    try:
        subprocess.check_output(
            ["ping", "-c", "1", "-W", "1", ip],
            stderr=subprocess.DEVNULL
        )
        return True
    except Exception:
        return False

# ================= SNMP =================
def snmp_get(ip, oid, community):
    try:
        out = subprocess.check_output(
            ["snmpget", "-v2c", "-c", community, "-t", "1", "-r", "0", ip, oid],
            stderr=subprocess.DEVNULL,
            timeout=2
        )
        return out.decode(errors="ignore")
    except Exception:
        return None

def detect_snmp_and_oem(ip):
    sysdescr_oid = "1.3.6.1.2.1.1.1.0"

    for community in SNMP_COMMUNITIES:
        out = snmp_get(ip, sysdescr_oid, community)
        if not out:
            continue

        text = out.lower()

        if "c9200l" in text:
            return "YES", "CISCO", community
        if "sg350-28p" in text:
            return "YES", "CISCO", community
        if "cisco" in text:
            return "YES", "CISCO", community
        if "ruckus" in text:
            return "YES", "RUCKUS", community
        if "gs1528" in text:
            return "YES", "DIGISOL", community
        if "digisol" in text:
            return "YES", "DIGISOL", community
        if "cubetel" in text:
            return "YES", "CUBETEL", community
        if "techroutes" in text:
            return "YES", "TECHROUTES", community
        if "tp-link" in text:
            return "YES", "TP-LINK", community
        if "jetstream" in text:
            return "YES", "TP-LINK", community
        if "ctl-282424" in text:
            return "YES", "CUBETEL", community
        if "d-link" in text:
            return "YES", "D-LINK", community
        if "dgs-1510-28x" in text:
            return "YES", "D-LINK", community
        if "tejas" in text:
            return "YES", "TEJAS", community
        if "tj1400p" in text:
            return "YES", "TEJAS", community
        if "nokia" in text:
            return "YES", "NOKIA", community
        if "alpha bridge" in text:
            return "YES", "ALPHA BRIDGE", community
        if "ewit" in text:
            return "YES", "EWIT", community
        if "azteca" in text:
            return "YES", "EWIT", community

        return "YES", "UNKNOWN", community

    return "NO", None, None

# ================= DISCOVERY =================
def discover():
    db = db_connect()
    cur = db.cursor()

    sql = """
    INSERT INTO devices
        (device_ip, icmp_ok, snmp_ok, oem, community, last_seen, updated_at)
    VALUES
        (%s, %s, %s, %s, %s, NOW(), NOW())
    ON DUPLICATE KEY UPDATE
        icmp_ok=VALUES(icmp_ok),
        snmp_ok=VALUES(snmp_ok),
        oem=VALUES(oem),
        community=VALUES(community),
        last_seen=NOW(),
        updated_at=NOW()
    """

    found = 0

    for net in NETWORKS:
        net = net.strip()
        if not net:
            continue

        logging.info(f"Scanning network {net}")

        for ip in ipaddress.ip_network(net, strict=False):
            ip = str(ip)

            if not ping(ip):
                continue

            icmp_ok = "YES"
            snmp_ok, oem, community = detect_snmp_and_oem(ip)

            cur.execute(sql, (
                ip,
                icmp_ok,
                snmp_ok,
                oem,
                community
            ))

            found += 1
            logging.info(f"DISCOVERED | {ip} | SNMP={snmp_ok} | OEM={oem}")

    db.commit()
    cur.close()
    db.close()

    logging.info(f"STEP-1 COMPLETE | Devices discovered: {found}")

# ================= RUN =================
if __name__ == "__main__":
    logging.info("STEP-1 NETWORK DISCOVERY STARTED")
    discover()
    logging.info("STEP-1 NETWORK DISCOVERY FINISHED")

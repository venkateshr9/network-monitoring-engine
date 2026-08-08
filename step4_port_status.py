#!/usr/bin/env python3
import os
import re
import subprocess
import logging
from datetime import datetime
from dotenv import load_dotenv
import mysql.connector
from snmp_utils import snmp_get
from netmiko import ConnectHandler 
from netmiko.exceptions import (
        NetmikoTimeoutException,
        NetmikoAuthenticationException,
)
#================= LOAD ENV ==================
load_dotenv("/opt/netmon/.env")
DEBUG = os.getenv("DEBUG", "FALSE") == "True"
DB_CFG = {
        "host": os.getenv("DB_HOST"),
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASS"),
        "database": os.getenv("DB_NAME")
}

COMMUNITIES = [c.strip() for c in os.getenv("SNMP_COMMUNITIES").split(",")]

LOG_DIR = os.getenv("LOG_DIR", "/opt/netmon/logs")
os.makedirs(LOG_DIR, exist_ok=True)
#===================== LOGGING ====================
logging.basicConfig(
        filename=os.path.join(LOG_DIR, "step4_port_status.log"),
        level=logging.DEBUG if DEBUG else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.getLogger().addHandler(console)
#==================== DB CONNECT ==========
def db_connect():
    return mysql.connector.connect(**DB_CFG)
#==================== DEVICE TYPE ======================
DEVICE_TYPE_MAP = {
    "CISCO": "cisco_ios",
    "TECHROUTES": "cisco_ios",
    "DIGISOL": "cisco_ios",
    "RUCKUS": "cisco_ios",
    "EWIT": "generic_termserver",
}
#=================== FETCH PORT INVENTORY ===========
def get_ports():
    db = mysql.connector.connect(**DB_CFG, autocommit=True)
    cur = db.cursor(dictionary=True)

    cur.execute("""
        SELECT device_ip, oem, port
        FROM switch_ports
        WHERE port IS NOT NULL
    """)

    rows = cur.fetchall()
    cur.close()
    db.close()

    logging.info(f"PORTS LOADED FROM INVENTORY: {len(rows)}")
    return rows
#================ PARSE ADMIN / OPER STATUS =============
def parse_admin_oper(output, port):
    for line in output.splitlines():
        if not line.lower().startswith(port.lower()):
            continue

        l = line.lower()

        if "disabled" in l or "shutdown" in l:
            return "DOWN", "DOWN"

        admin = "UP"
        oper = "UP" if ("up" in l or "connected" in l) else "DOWN"
        return admin, oper

    return None, None
#===================== SAVE TO DB =======================
def save_status(rows):
    if not rows:
        logging.warning("No rows to insert into DB")
        return
        
    db = mysql.connector.connect(**DB_CFG, autocommit=True)
    cur = db.cursor()

    sql = """
    INSERT INTO switch_port_status
    (device_ip, oem, port, admin_status, oper_status, collected_at)
    VALUES (%s,%s,%s,%s,%s,NOW())
    """

    inserted = 0
    for r in rows:
        if not r.get("admin_status") or not r.get("oper_status"):
            logging.error(
                f"SKIPPING INSERT | {r.get('device_ip')} {r.get('port')}"
                f"ADMIN={r.get('admin_status')} OPER={r.get('oper_status')}"
            )
            continue
        admin = str(r.get("admin_status") or "DOWN")
        oper = str(r.get("oper_status") or "DOWN")

        cur.execute(sql, (
            r["device_ip"],
            r["oem"],
            r["port"],
            admin,
            oper,
        ))

        logging.info(
            f"DB INSERT | {r['device_ip']} {r['port']} |"
            f"A={r['admin_status']} )={r['oper_status']}"
        )
        inserted +=1

        
    logging.info(f"DB INSERTED ROWS: {inserted}")
    
    cur.close()
    db.close()
#======================= PROCESS DEVICE ======================
def process_device(ip, oem, ports):
    device_type = DEVICE_TYPE_MAP.get(oem.upper())
    if not device_type:
        logging.warning(f"{ip} | UNSUPPORTED OEM {oem}")
        return []
    
    logging.info(f"{ip} | Processing {len(ports)} ports")

    try:
        conn = ConnectHandler(
            device_type=device_type,
            host=ip,
            username=os.getenv("SW_SSH_USER"),
            password=os.getenv("SW_SSH_PASS"),
            secret=os.getenv("SW_SSH_SECRET"),
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
        )
        conn.enable()
        conn.send_command("terminal length 0")
    
    except NetmikoTimeoutException:
        logging.error(f"{ip} | SSH TIMEOUT - skipping device")
        return []
    
    except NetmikoAuthenticationException:
        logging.error(f"{ip} | SSH AUTH FAILED - skipping device")
        return []

    except Exception as e:
        logging.error(f"{ip} | SSH ERROR: {e}")
        return []

    results = []
    
    for p in ports:
        try:
            port = p["port"]

            out = conn.send_command(f"show interface {port} status", read_timeout=8)

            admin, oper = parse_admin_oper(out, port)

            results.append({
                "device_ip": ip,
                "oem": oem,
                "port": port,
                "admin_status": admin or "DOWN",
                "oper_status": oper or "DOWN",
            })
        except Exception as e:
            logging.warning(f"{ip} | {port} | parse failed: {e}")
            continue

    conn.disconnect()
    return results
#======================= MAIN ==========================
def main():
    inventory = get_ports()

    devices = {}
    for r in inventory:
        key = (r["device_ip"], r["oem"])
        devices.setdefault(key, []).append(r)

    all_rows = []

    for (ip, oem), ports in devices.items():
        logging.info(f"{ip} | Processing {len(ports)} ports")
        rows = process_device(ip, oem, ports)
        all_rows.extend(rows)

    if all_rows:
        logging.info(f"Saving {len(all_rows)} port status rows")
        save_status(all_rows)
    else:
        logging.warning("NO PORT STATUS COLLECTED")

    logging.info("STEP4 FINISHED")

if __name__ == "__main__":
    main()

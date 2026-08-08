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
        filename=os.path.join(LOG_DIR, "step5_sfp_power.log"),
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
#=================== FETCH ELIGIBLE PORTS ===========
def get_fiber_up_ports():
    db = mysql.connector.connect(**DB_CFG, autocommit=True)
    cur = db.cursor(dictionary=True)

    cur.execute("""
        SELECT sps.device_ip, sps.oem, sps.port
        FROM switch_port_status sps
        JOIN (
            SELECT device_ip, port, MAX(collected_at) AS last_seen
            FROM switch_port_status
            GROUP BY device_ip, port
        ) latest
          ON sps.device_ip = latest.device_ip
         AND sps.port = latest.port
         AND sps.collected_at = latest.last_seen
        JOIN switch_ports sp
          ON sp.device_ip = sps.device_ip
         AND sp.port = sps.port
        WHERE 
          sp.port_type = 'FIBER'
    """)

    rows = cur.fetchall()
    cur.close()
    db.close()

    logging.info(f"PORTS LOADED FROM INVENTORY: {len(rows)}")
    return rows
#==================================================
#sps.admin_status = 'UP'
#AND sps.oper_status = 'UP'

#================ PARSE CISCO SFP POWER =============
def parse_cisco_sfp(output):
    for line in output.splitlines():
        line = line.strip()

        if not line:
            continue

        if any(x in line for x in (
            "Threshold", "Alarm", "Warning",
            "Temperature", "Voltage", "Current",
            "Transmit Fault", "Diagnostics"
        )):
            continue

        if not re.match(r"^[A-Za-z]+[\d/]+", line):
            continue

        numbers = re.findall(r"-?\d+\.\d+", line)


        if len(numbers) >= 2:
            try:
                tx = float(numbers[-2])
                rx = float(numbers[-1])
                return tx, rx
            
            except ValueError:
                return None, None

    return None, None
#==================== PARSE TECHROUTES SFP POWER =================
def parse_techroutes_sfp(output):
    
    for line in output.splitlines():
        line = line.strip().lower()

        if "tx power" in line and "rx power" in line:
            numbers = re.findall(r"-?\d+(?:\.\d+)?", line)

            if len(numbers) >= 2:
                try:
                    tx = float(numbers[0])
                    rx = float(numbers[1])
                    return tx, rx
                except ValueError:
                    return None, None

    return None, None
#===================== SAVE TO DB =======================
def update_sfp(device_ip, port, tx, rx):
        
    db = mysql.connector.connect(**DB_CFG, autocommit=True)
    cur = db.cursor()

    cur.execute("""
        UPDATE switch_port_status
        SET sfp_tx_power=%s,
            sfp_rx_power=%s,
            collected_at=NOW()
        WHERE device_ip=%s
          AND port=%s
        ORDER BY id DESC
        LIMIT 1
    """, (tx, rx, device_ip, port))

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

    for p in ports:
        port = p["port"]
        try:
            if oem == "CISCO":
                sfp_out = conn.send_command(
                    f"show interface {port} transceiver", 
                    read_timeout=8
                )
            
            elif oem == "TECHROUTES":
                sfp_out= conn.send_command(
                    f"show interface {port}",
                    read_timeout=8
                )

            else:
                logging.info("f{ip} {port} | NO SFP command for OEM {oem}")
                continue

            if oem == "CISCO":
                logging.debug(f"{ip} {port} RAW OUTPUT:\n{sfp_out}")
                tx, rx = parse_cisco_sfp(sfp_out)

            elif oem == "TECHROUTES":
                logging.debug(f"{ip} {port} RAW OUTPUT:\n{sfp_out}")
                tx, rx = parse_techroutes_sfp(sfp_out)
            
            else:
                logging.info(f"{ip} {port} | SFP Parsing not supported for OEM {oem}")
                tx, rx = None, None

            if tx is not None and rx is not None:
                
                if tx < -40 or rx < -40:
                    logging.warning(
                        f"{ip} {port} Suspicious SFP Values TX={tx} RX={rx}"
                    )

            update_sfp(ip, port, tx, rx)

            logging.info(
                f"SFP UPDATE | {ip} {port} TX={tx} RX={rx}"
            )
        except Exception as e:
            logging.warning(f"{ip} {port} | SFP READ FAILED: {e}")

    conn.disconnect()
#======================= MAIN ==========================
def main():
    ports = get_fiber_up_ports()
    
    devices = {}
    for p in ports:
        key = (p["device_ip"], p["oem"])
        devices.setdefault(key, []).append(p)

    for (ip, oem), p_list in devices.items():
        process_device(ip, oem, p_list)

    logging.info("STEP5 FINISHED")

if __name__ == "__main__":
    main()

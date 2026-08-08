#!/usr/bin/env python3
print("STEP2 SCRIPT LOADED")
import os
import re
import subprocess
import logging
from datetime import datetime
from dotenv import load_dotenv
import mysql.connector
from snmp_utils import snmp_get
from db_utils import db_connect
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
        filename=os.path.join(LOG_DIR, "step2_device_identify.log"),
        level=logging.DEBUG if DEBUG else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.getLogger().addHandler(console)
#==================== OIDs =========================
OID_SYSNAME = "1.3.6.1.2.1.1.5.0"
OID_SYSDESC = "1.3.6.1.2.1.1.1.0"
#===================== HELPERS =========================
def detect_oem(sysdescr):

    if not sysdescr or not isinstance(sysdescr, str):
        return "UNKNOWN"

    s = sysdescr.lower()
    if "cisco" in s:
        return "CISCO"
    if "aruba" in s or "hp" in s or "hewlett-packard" in s:
        return "HP"
    if "d-link" in s:
        return "D-LINK"
    if "digisol" in s:
        return "DIGISOL"
    if "techroutes" in s:
        return "TECHROUTES"
    if "tp-link" in s:
        return "TP-LINK"
    if "netgear" in s:
        return "NETGEAR"
    if "ruckus" in s:
        return "RUCKUS"
    if "alpha bridge technologies" in s:
        return "ALPHA BRIDGE"
    if "ctl-282424" in s:
        return "CUBETEL"
    if "jetstream" in s:
        return "TP-LINK"
    if "dgs-1510-28x" in s:
        return "D-LINK"
    if "tj1400p-m2-24sd-s" in s:
        return "TEJAS"
    if "tj1400p-m2-24hpd-s" in s:
        return "TEJAS"

    return "EWIT"
#====================== PARSE OEM VERSION ===============
def cli_parse_show_version(ip):
    try:
        device = {
                "device_type": "cisco_ios",
                "host": ip,
                "username": os.getenv("SW_SSH_USER"),
                "password": os.getenv("SW_SSH_PASS"),
                "secret": os.getenv("SW_SSH_SECRET", ""),
                "timeout": 5,
                "fast_cli": False,
        }

        conn = ConnectHandler(**device)

        if device["secret"]:
            conn.enable()

        output = conn.send_command(
            "show version",
            expect_string=r"#|>",
            strip_prompt=True,
            strip_command=True
        )

        conn.disconnect()

        if not output:
            return None

        patterns = [
            r"(WS-C\d+[A-Z0-9\-]+)",
            r"(C\d{4}[A-Z0-9\-]+)",
            r"(N\d{4}[A-Z0-9\-]+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, output)
            if m:
                return m.group(1)

        m = re.search(r"S\d{4,5}E-\d+AC)", output)
        if m:
            return f"TR-{m.group(1)}"

        return None

    except Exception as e:
        logging.debug(f"{ip} | CLI show version failed: {e}")
        return None
#======================= OEM =============================
def snmp_get_sysdescr(ip, community):
    cmd = [
        "/usr/bin/snmpget", "-v2c",
        "-c", community,
        "-Ovq", ip,
        OID_SYSDESC
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    if r.returncode == 0 and r.stdout:
        return r.stdout.strip()
    return None
#===================== SNMP SYSOBJECT ====================
def snmp_get_sysobjectid(ip, community):
    cmd = [
        "/usr/bin/snmpwalk", "-v2c",
        "-c", community,
        "-Ovq",
        ip,
        "1.3.6.1.2.1.1.2.0"
    ]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    if r.returncode == 0 and r.stdout:
        val = r.stdout.strip()

        if val.startswith("SNMPv2-SMI::enterprises."):
            val = "1.3.6.1.4.1." + val.split("enterprises.", 1)[1]

            return val

    return None

SYSOBJID_MODEL_MAP = {
    "1.3.6.1.4.1.11863": "TECHROUTES SWITCH",
    "1.3.6.1.4.1.171": "DIGISOL SWITCH",
    "1.3.6.1.4.1.17409": "EWIT SWITCH",
    "1.3.6.1.4.1.25053": "RUCKUS SWITCH",
}
#====================== MODEL FROM OSNMP OBJECT ID =============
def model_from_sysobjectid(sysobjid):
    if not sysobjid:
        return None

    if sysobjid.startswith("1.3.6.1.4.1.1991"):
        return "RUCKUS SWITCH"

    for oid, model in SYSOBJID_MODEL_MAP.items():
        if sysobjid.startswith(oid):
            return model
        
        return None
#===================== SNMP MODEL ========================
def snmp_get_model_entity(ip, community):
    cmd = [
        "/usr/bin/snmpwalk", "-v2c",
        "-c", community,
        "-Ovq",
        ip,
        "1.3.6.1.2.1.47.1.1.1.1.13"
    ]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=4)
    if r.returncode != 0 or not r.stdout:
        return None

    for line in r.stdout.splitlines():
        line = line.strip()

        if not line:
            continue

        if "no such" in line.lower():
            continue

        if any(x in line.lower() for x in ("sfp", "transceiver", "module")):
            continue
        return line[:64]

    return None
#====================== MODEL =============================
def extract_model_from_sysdescr(oem, sysdescr):
    if not sysdescr:
        return None

    s = sysdescr.strip()

    if oem == "CISCO":
        # Common Cisco patterns
        m = re.search(r"\bC\d{3,4}[A-Z0-9\-]*\b", s)
        if m:
            return m.group(0)

    if oem in ("HP", "ARUBA"):
        m = re.search(r"(J\d{4}[A-Z\-0-9]*)", s)
        if m:
            return m.group(1)

    if oem == "RUCKUS":
        m = re.search(r"(ICX\d{4,5}-[A-Z0-9]+)", s, re.IGNORECASE)
        if m:
            return m.group(1).upper()
    
    if oem == "DIGISOL":
        m = re.search(r"DG-[A-Z0-9\-]+)", s)
        if m:
            return m.group(1)

    if oem == "NOKIA":
        if "7750" in s:
            return "7750 SR"
        if "7250" in s:
            return "7250 IXR"

    # Fallback: first meaningful token
    return None
#======================= HOSTNAME ===========================
def normalize_hostname(hostname):
    if not hostname:
        return None
    h = hostname.strip().upper()
    h = h.replace("-", "_")
    return h.split(".")[0]
#======================== IDENTITY & ROLE ==============================
def ping_ip(ip):
    return subprocess.run(
        ["ping", "-c", "1", "-W", "1", ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    ).returncode == 0
#===================== SNMP HOSTNAME ====================
def snmp_get_hostname(ip, community):
    cmd = [
        "/usr/bin/snmpget", "-v2c",
        "-c", community,
        "-Ovq", ip,
        "1.3.6.1.2.1.1.5.0"
    ]
    r= subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    if r.returncode == 0 and r.stdout:
        return normalize_hostname(r.stdout.strip())
    return None
#===================== Parsing ================================
def parse_identity(hostname):
    location_wing_map = {
        "AW_": ("SENA BHAWAN", "A WING"),
        "BW_": ("SENA BHAWAN", "B WING"),
        "CW_": ("SENA BHAWAN", "C WING"),
        "D1W_": ("SENA BHAWAN", "D1 WING"),
        "D2W_": ("SENA BHAWAN", "D2 WING"),
        "KGAW_": ("KG MARG", "A WING"),
        "KGBW_": ("KG MARG", "B WING"),
        "AFAW_": ("AFRICA AVENUE", "A WING"),
        "AFBW_": ("AFRICA AVENUE", "B WING"),
        "KH_": ("KASHMIR HOUSE", "KH"),
        "SB_": ("SOUTH BLOCK", "SB"),
        "SE_": ("SIGNALS ENCLAVE", "SE"),
    }

    wing = None
    location = None

    for prefix, (loc, w) in location_wing_map.items():
        if hostname.startswith(prefix):
            location = loc
            wing = w
            break

    parts = hostname.split("_")

    room = None
    floor = None
    role = None

    if len(parts) >= 2:
        room = parts[1]

        if room:
            m = re.match(r"(\d)", room)
            if m:
                floor = m.group(1)
            else:
                floor = "GF"
    
    if hostname.endswith("_AS" ):
        role = "ACCESS"
    elif hostname.endswith("_CS"):
        role = "CORE"
    elif hostname.endswith("_DS"):
        role = "DISTRIBUTION"
    else:
        role = "UNKNOWN"

    return location, wing, floor, room, role
#======================= DATABASE =============================
def update_identity(ip, hostname, model, location, wing, floor, room, role):
    db = mysql.connector.connect(**DB_CFG)
    cursor = db.cursor()

    cursor.execute("""
        UPDATE devices
        SET 
            hostname = %s,
            model = %s,
            location = %s,
            wing = %s,
            floor = %s,
            room_no = %s,
            role = %s,
            updated_at = NOW()
        WHERE device_ip = %s AND hostname IS NULL 
    """, (
        hostname,
        model,
        location,
        wing,
        floor,
        room,
        role.lower(),
        ip
    ))

    db.commit()
    cursor.close()
    db.close()
#============================== PROCESS ===========================
def process_ip(ip):
    logging.debug(f"Scanning {ip}")

    if not ping_ip(ip):
        return

    snmp_ok, community, hostname = snmp_get(ip)

    if not snmp_ok or not hostname:
        return

    location, wing, floor, room, role = parse_identity(hostname)

    update_identity(
        ip,
        hostname,
        location,
        wing,
        floor,
        room,
        role
    )
    
    logging.info(f"{ip} | {hostname} | {location} | {role}")
#============================ DISCOVERED IP ==================
def get_discovered_devices(cursor):
    cursor.execute("""
        SELECT device_ip, community 
        FROM devices
        WHERE icmp_ok = 'YES'
          AND snmp_ok = 'YES'
    """)
    return cursor.fetchall()
#============================= main ==========================
def main():
    db = mysql.connector.connect(
        host=DB_CFG["host"],
        user=DB_CFG["user"],
        password=DB_CFG["password"],
        database=DB_CFG["database"]
    )
    cursor = db.cursor(dictionary=True)

    devices = get_discovered_devices(cursor)

    for device in devices:
        ip = device["device_ip"]
        community = device["community"]

        try:
            hostname = snmp_get_hostname(ip, community)
            if not hostname:
                continue
            sysdescr = snmp_get_sysdescr(ip, community)
            
            sysobjid = snmp_get_sysobjectid(ip, community)

            model = None
            model_source = "UNKNOWN"
            model_from_entity_val = None
            model_from_sysdescr_val = None
            model_from_sysobjectid_val= None

            oem = detect_oem(sysdescr) if sysdescr else "UNKNOWN OEM"
           
            model_from_entity_val = snmp_get_model_entity(ip, community)
            if model_from_entity_val and "no such" not in model_from_entity_val.lower():
                model = model_from_entity_val
                model_source = "ENTITY"

            if not model:
                model_from_sysobjectid_val = model_from_sysobjectid(sysobjid)
                if model_from_sysobjectid_val:
                    model = model_from_sysobjectid_val
                    model_source = "SYSOBJECTID"

            if not model:
                model_from_sysdescr_val = extract_model_from_sysdescr(oem, sysdescr)
                if model_from_sysdescr_val:
                    model = model_from_sysdescr_val
                    model_source = "SYSDESCR"

            if not model and oem in ("CISCO", "TECHROUTES"):
                logging.info(f"{ip} | CLI fallback")
                cli_model = cli_parse_show_version(ip)
                if cli_model:
                    model = cli_model
                    model_source = "CLI"
            
            if not model:
                model = "UNKNOWN"
                model_source = "UNKNOWN"


            location, wing, floor, room, role = parse_identity(hostname)

            update_identity(
                ip,
                hostname,
                model,
                location,
                wing,
                floor,
                room,
                role
            )

            logging.info(f"{ip} | {hostname} | {model} | {location} | {wing} | {role}  | IDENTIFIED")
            
        except Exception as e:
            logging.info(f"{ip} | ERROR: {e}")
            db.rollback()

    cursor.close()
    db.close()

if __name__ == "__main__":
    main()

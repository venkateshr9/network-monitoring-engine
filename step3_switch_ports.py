#!/usr/bin/env python3
import os
import re
import subprocess
import logging
from datetime import datetime
from dotenv import load_dotenv
import mysql.connector
from snmp_utils import snmp_get
from db_utils import db_connect
from netmiko import ConnectHandler
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
        filename=os.path.join(LOG_DIR, "step3_switch_ports.log"),
        level=logging.DEBUG if DEBUG else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.getLogger().addHandler(console)
#===================== PORT REGEX ========================
PORT_REGEX = re.compile(
        r"(?<!\S)("                     # Start REGEX Line
        r"gi\d+/\d+/\d+|"               # Gi1/0/1
        r"te\d+/\d+/\d+|"               # Te1/1/1
        r"twe\d+/\d+/\d+|"              # Twe1/0/1

        r"gi\d+/\d+|"                   # Gi0/1
        r"g\d+/\d+|"                    # g0/1
        r"te\d+/\d+|"                   # Te1/1
        r"tg\d+/\d+|"                   # tg0/1
        r"eth\d+/\d+|"                  # eth0/1
        r"fa\d+/\d+|"                   # fa0/1
        r"\d+/\d+|"                     # 1/0

        r"gi\d+|"                       # gi1
        r"g\d+|"                        # g1
        r"\d+/\d+/\d+|"                 # 1/0/1
        r"\d+/\d+"                      # cubetel

        r")(?!\S)",                     #End REGEX line
        re.IGNORECASE
)
#=================== COMMANDS ====================
INTERFACE_COMMANDS = {
    "CISCO": "show interface status",
    "TECHROUTES": [
        "show interface brief",
        "show interface ethernet status"
    ],

    "EWIT": "show interface gigabitethernet 1-28 status",
    "RUCKUS": "show interface brief",
    "ALPHA BRIDGE": "show interface brief",
    "DIGISOL": "show interface ethernet status",
    "D-LINK": "show interface status",
    "CUBETEL": "show port all",
}
#==================== DEVICE TYPE MAP ===============
DEVICE_TYPE_MAP = {
    "CISCO": "cisco_ios",
    "TECHROUTES": "cisco_ios",
    "DIGISOL": "cisco_ios",
    "CUBETEL": "cisco_ios",
    "ALPHA BRIDGE": "cisco_ios",
    "RUCKUS": "cisco_ios",
    "TPLINK": "generic_termserver",
    "EWIT": "generic_termserver",
}
#==================== ONLY WANTED PORT ==========
def is_valid_port(port: str) -> bool:
    p = port.lower()

    if p.startswith((
        "vlan", "vl", "po", "Ap", "lo", "Hu", "mgmt",
    )):
        return False
                    
    return True
#=================== ANSI COLOR ==========
ANSI_ESCAPE = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')

def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub('', text)
#==================== DB CONNECT ==========
def db_connect():
    return mysql.connector.connect(**DB_CFG)
#================== GET DEVICE =================
def get_devices():
    db = db_connect()
    cur = db.cursor(dictionary=True)
    cur.execute("""
        SELECT device_ip, oem, model
        FROM devices
        WHERE device_ip IS NOT NULL
    """)
    rows = cur.fetchall()
    cur.close()
    db.close()
    logging.info(f"Total devices fetched: {len(rows)}")
    return rows
#================= GET INTERFACE COMMANDS ==================
def get_interface_commands(oem):
    oem = (oem or "").upper()

    if oem in INTERFACE_COMMANDS:
        cmds = INTERFACE_COMMANDS[oem]
        return cmds if isinstance(cmds, list) else [cmds]

    return []
#=================== NATURAL PORT NUMBERS ========================
def natural_port_key(port: str):
    parts = re.split(r'(\d+)', port.lower())
    key = []
    for p in parts:
        if p.isdigit():
            key.append(int(p))
        else:
            key.append(p)
    return key
#===================== PORT TYPE ==========================
def detect_port_type(line: str, port: str):
    l = line.lower()
    p = port.lower()

    if "copper" in l:
        return "UTP"
    
    if "fiber" in l:
        return "FIBER"

    if any(k in l for k in (
        "giga-tx", "g-tx", "1000 full", "baset", "utp", "10/100basetx",
        "1000base-t"
    )):
        return "UTP"

    if p.startswith(("fa")):
        return "UTP"

    if any(k in l for k in (
        "sfp", "10g", "10g full", "sfpp", "lx", "1000baselx", "1000basesx",
        "10gbase", "sr", "lr", "er", "1000basesx sfp",
        "10gbase-lr", "1000base-sx", "10gbase-lr-s",
        "40gbase-active", "1000basesx sfp", "1000base-lh"
    )):
        return "FIBER"
    if p.startswith(("tg", "te", "twe")):
        return "FIBER"

    return None
#================== EXTRACT PPORTS =================
def extract_ports(output, oem):
    ports = []
    current = None

    for line in output.splitlines():
        line = line.rstrip()
        if not line:
            continue

        lower = line.lower()
        
        #---------------- HEADER JUNK FILTER ---------------
        if line.lower().startswith((
            "port",
            "name",
            "status",
            "vlan",
            "duplex",
            "speed",
            "type",
            "link",
            "alias",
            "interface",
            "codes",
            "---",
        )):
            continue
        #---------------- CONTINUATION LINE ---------------
        if current and not PORT_REGEX.search(line):

            ptype = detect_port_type(line, current["port"])
            if ptype:
                current["port_type"] = ptype
            continue
        #----------------- PORT LINE ---------------------
        match = PORT_REGEX.search(line)
        if not match:
            continue

        port = match.group(0)

        if not is_valid_port(port):
            continue
        
        port_type = detect_port_type(line, port)
        
        current = {
            "port": port,
            "port_type": port_type

        }
       
        ports.append(current)

    return ports
#======================= PROCESS DEVICE ==================
def process_device(device, db_connect):
    ip = device["device_ip"]
    oem = (device["oem"] or "").upper()

    device_type = DEVICE_TYPE_MAP.get(oem, "generic_termserver")

    logging.info(f"{ip} |OEM={oem} | device_type={device_type} | connecting")

    if device_type == "generic_termserver":
        try:
            conn = ConnectHandler(
                device_type="generic_termserver",
                host=ip,
                username="",
                password="",
                timeout=30,
                banner_timeout=30,
                auth_timeout=30,
                global_delay_factor=3,
                fast_cli=False,
                session_log=f"/tmp/ewit_{ip}.log",
            )
        except Exception as e:
            logging.error(f"{ip} | connection failed | {e}")
            return
    
    else:
        try:
            conn = ConnectHandler(
                device_type="cisco_ios",
                host=ip,
                username=os.getenv("SW_SSH_USER"),
                password=os.getenv("SW_SSH_PASS"),
                secret=os.getenv("SW_SSH_SECRET"),
                timeout=30,
                banner_timeout=30,
                auth_timeout=30,
                fast_cli=False,
            )
        except Exception as e:
            logging.error(f"{ip} | connection failed | {e}")
            return

    if device_type == "generic_termserver":
        import time

        conn.write_channel("\n")
        time.sleep(1)

        conn.write_channel(os.getenv("SW_SSH_USER") + "\n")
        time.sleep(2)

        conn.write_channel(os.getenv("SW_SSH_PASS") + "\n")
        time.sleep(5)

        output = ""
        for _ in range(5):
            time.sleep(1)
            output += conn.read_channel()

        logging.debug(f"{ip} | post-login output:\n{output}")

    if device_type == "cisco_ios":
        try:
            conn.enable()
            conn.send_command("terminal length 0")
        
        except Exception as e:
            logging.error(f"{ip} | enable failed (continuing): {e}")
    else:
        conn.send_command_timing("\n")
        conn.send_command_timing("terminal length 0\n")

    commands = get_interface_commands(oem)
    if not commands:
        logging.warning(f"{ip} | unsupported OEM")
        conn.disconnect()
        return

    PAGING_COMMANDS = {
        "CISCO": "terminal length 0",
        "TECHROUTES": "terminal length 0",
        "DIGISOL": "terminal length 0",
        "RUCKUS": "skip-page-display",
        "CUBETEL": "terminal length 0",
    }

    page_cmd = PAGING_COMMANDS.get(oem.upper())
    
    if page_cmd:
        conn.send_command_timing(page_cmd)

    all_ports = []

    for cmd in commands:
        logging.debug(f"{ip} | running: {cmd}")
        
        if device_type == "generic_termserver":
            conn.write_channel(cmd + "\n")
            time.sleep(2)
            out = ""
            MAX_MORE = 20
            count = 0

            while count < MAX_MORE:
                time.sleep(1)
                chunk = conn.read_channel()
                if not chunk:
                    break

                out += chunk

                if "--More--" in chunk or "\x03" in chunk or "More" in chunk:
                    conn.write_channel( " ")
                    count += 1
                    continue
                else:
                    break
        
        else:
            out = conn.send_command_timing(
                cmd,
                strip_prompt=True,
                strip_command=True,
            )
        if not out.strip():
            logging.warning(f"{ip} | EMPTY OUTPUT for command: {cmd}")
            continue 

        MAX_MORE = 20
        more_count = 0

        while (
            ("--More--" in out or "\x03" in out or "More" in out)
            and more_count < MAX_MORE
        ):
            more_count += 1
            out += conn.send_command_timing(
                " ",
                strip_prompt=False,
                strip_command=False,
                delay_factor=1
            )

        if more_count == MAX_MORE:
            logging.warning(f"{ip} | pagination limit hit for command: {cmd}")

        if not out.strip():
            logging.warning(f"{ip} | EMPTY OUTPUT for command: {cmd}")
            continue

        clean_out = strip_ansi(out)
        logging.debug(f"{ip} | CLEAN OUTPUT: \n{clean_out}")

        ports = extract_ports(clean_out, oem)
        all_ports.extend(ports)
        
    save_ports_to_db(ip, oem, all_ports, db_connect)

    conn.disconnect()

    logging.info(f"{ip} | TOTAL PORTS FOUND: {len(all_ports)}")

    for p in sorted(all_ports, key=lambda x: natural_port_key(x["port"])):
        logging.info(f"{ip} | PORT: {p['port']} | TYPE={p['port_type']}")
#================== SAVE DB ===================
def save_ports_to_db(device_ip, oem, ports, conn):
    sql = """
        INSERT INTO switch_ports (device_ip, oem, port, port_type, last_seen)
        VALUES(%s,%s,%s,%s,NOW())
        ON DUPLICATE KEY UPDATE
            oem = VALUES(oem),
            port_type = VALUES(port_type),
            last_seen = NOW()
    """
    with conn.cursor() as cur:
        for p in ports:
            cur.execute(
                sql,
                (
                    device_ip,
                    oem, 
                    p["port"],
                    p["port_type"],
                )
            )

    conn.commit()
# ====================== MAIN ==============================
def main():
    devices = get_devices()
    
    if not devices:
        logging.warning("No devices found in database")
        return

    db = db_connect()

    try:
        for dev in devices:
            try:
                process_device(dev, db)
            
            except Exception as e:
                logging.error(f"{dev['device_ip']} | ERROR | {e}")
        
    finally:
        db.close()

if __name__ == "__main__":
    main()

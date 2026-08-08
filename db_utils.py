import mysql.connector
from datetime import datetime

def db_connect(cfg):
    return mysql.connector.connect(
        host=cfg["host"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        autocommit=False
    )

def update_device_profile(cursor, ip, hostname, oem, role, floor, fiber_capable):
    cursor.execute("""
        UPDATE devices
        SET
            hostname=%s,
            oem=%s,
            role=%s,
            floor=%s,
            fiber_capable=%s,
            profiled='YES',
            last_seen=NOW(),
            updated_at=NOW()
        WHERE device_ip=%s    
    """, (
        hostname,
        oem,
        role,
        floor,
        fiber_capable,
        ip
    ))


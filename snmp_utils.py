import subprocess

SNMPGET = "/usr/bin/snmpget"
SNMPWALK = "/usr/bin/snmpwalk"

def snmp_get(ip, communities, oid):
    for community in communities:
        try:
            r = subprocess.run(
                [SNMPGET, "-v2c", "-c", community, "-Oqv", ip, oid],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
    return None

def snmp_walk(ip, communities, oid):
    for community in communities:
        try:
            cmd = [
                "snmpwalk", "-v2c",
                "-c", community,
                "-On",
                "-Oqv",
                ip, oid
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )
            if proc.returncode != 0:
                continue

            result = {}

            raw_lines = proc.stdout.strip().splitlines()

            cmd_idx = [
                "snmpwalk", "-v2c",
                "-c", community,
                "-On",
                ip, oid
            ]

            proc_idx = subprocess.run(
                cmd_idx,
                capture_output=True,
                text=True,
                timeout=10
            )

            idx_lines = proc_idx.stdout.strip().splitlines()

            for i in range(len(idx_lines)):
                oid_full = idx_lines[i].split(" = ")[0]

                idx = oid_full.split(".")[-1]
                val = raw_lines[i].strip('" ')
                result[idx] = val

            return result
        
        except Exception:
            continue

    return {}


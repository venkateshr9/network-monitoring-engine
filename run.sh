#!/bin/bash
set -e

LOG="/var/log/netmon/pipeline.log"
LOCK="/var/run/netmon_pipeline.lock"

exec >> "$LOG" 2>&1

# ------ PREVENT OVERLAPPING RUN ------------

exec 200>$LOCK
flock -n 200 || {
	echo "$(date) | Pipeline already running. Exiting."
	exit 0
}

echo "========== PIPELINE STARTED $(date) ========"

# ---------- STEP3 ---------------------------
start=$(date +%s)
python /opt/netmon/step3_switch_ports.py
end=$(date +%s)
echo "Step3 Switch Ports completed in $(( (end-start)/60 )) minutes"

# ------------ STEP4 ---------------------------
start=$(date +%s)
python /opt/netmon/step4_port_status.py
end=$(date +%s)
echo "Step4 Port Status Completed in $(( (end-start)/60 )) minutes"

# -------------- STEP5 -------------------------
start=$(date +%s)
python /opt/netmon/step5_sfp_power.py
end=$(date +%s)
echo "Step5 SFP Power Completed in $(( (end-start)/60 )) minutes"


echo "========== PIPELINE ENDED $(date) ========"


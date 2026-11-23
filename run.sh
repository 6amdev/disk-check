#!/usr/bin/env bash
# ตัวช่วยรัน diskcheck — ใช้: sudo ./run.sh [ตัวเลือก]
cd "$(dirname "$0")"
if ! command -v smartctl >/dev/null; then
  echo "⚠  ยังไม่มี smartmontools — ติดตั้งด้วย: sudo apt install smartmontools"
fi
exec python3 diskcheck.py "$@"

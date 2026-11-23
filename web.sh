#!/usr/bin/env bash
# เปิดเว็บ ดูผลตรวจ/อัปโหลดรูป/กรอกข้อมูลการขายจากมือถือ — ใช้: ./web.sh  (ไม่ต้องใช้ sudo)
cd "$(dirname "$0")"
exec python3 diskcheck.py --web "$@"

"""Buat ulang daftar_akun.txt dari data/akun_resmi.csv (base data akun).

Jalankan:  python tools/buat_daftar.py
Baris berstatus "Tidak ditemukan" (kolom Akun kosong) tidak dimasukkan.
"""
import csv
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "akun_resmi.csv")
DST = os.path.join(ROOT, "daftar_akun.txt")
ORDER = ["Instagram", "X", "TikTok", "YouTube", "Threads", "Facebook"]


def main():
    with open(SRC, encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r["URL"].strip()]
    out = ["# Dibuat otomatis dari data/akun_resmi.csv — ubah CSV-nya, lalu jalankan: python tools/buat_daftar.py",
           "# Format: URL | Nama unit. Baris diawali # diabaikan.", ""]
    for plat in ORDER:
        group = [r for r in rows if r["Platform"] == plat]
        if not group:
            continue
        out.append(f"# ===== {plat} =====")
        for status in ("Terverifikasi", "Kemungkinan", "Belum terverifikasi"):
            sub = [r for r in group if r["Status"] == status]
            if sub:
                out.append(f"# -- {status} --")
                out += [f"{r['URL']} | {r['Unit']}" for r in sub]
        out.append("")
    with open(DST, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"{len(rows)} akun ditulis ke {DST}")


if __name__ == "__main__":
    main()

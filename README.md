# Pengambil Link Unggahan (tanpa API)

Aplikasi lokal untuk mengambil **link unggahan** dari banyak akun **Instagram, X, TikTok, YouTube, Threads, dan Facebook**
dalam **periode tanggal tertentu**. Tidak memakai API berbayar, tidak memakai AI, dan tidak menarik data engagement:
hasilnya hanya daftar link beserta tanggal unggahnya.

## Cara kerja singkat

- Alat membuka **browser sungguhan di laptop kamu** (Chrome/Edge, atau Chromium kalau tidak ada) memakai sesi login kamu sendiri,
  membuka profil, scroll, lalu membaca link unggahan yang tampil.
- **Tanggal dibaca dari ID unggahan**, jadi tiap unggahan tidak perlu dibuka satu per satu:
  - Instagram & Threads: shortcode `/p/…`, `/reel/…`, `/post/…`
  - X: ID status (snowflake), plus pencarian `from:akun since:… until:…` agar langsung terfilter periodenya
  - TikTok: ID video/foto
  - YouTube: dibaca dengan `yt-dlp` (tab Videos, Shorts, dan Live). Tidak perlu login.
  - Facebook: dibaca dari data yang dimuat halaman saat scroll. Paling rapuh; anggap *best effort*.
- Scroll berhenti otomatis setelah melewati tanggal mulai. Unggahan yang di-pin (biasanya lama) tidak membuat proses berhenti terlalu cepat.

## Instalasi & menjalankan

1. Install **Python 3.10+**. Di Windows: https://www.python.org/downloads/ (centang **Add Python to PATH**).
   Di Mac: pakai installer .pkg dari python.org.
2. Download ZIP repo ini, lalu ekstrak.
3. Jalankan:
   - **Windows:** klik dua kali `start-windows.bat`
   - **Mac:** klik dua kali `start-mac.command` (pertama kali: klik kanan → Open → Open)
   - **Linux:** `./start-mac-linux.sh`
4. Saat pertama kali dijalankan, paket dan browser Chromium dipasang otomatis (beberapa menit). Setelah itu halaman
   http://127.0.0.1:8770 terbuka di browser. Jendela hitam (server) harus tetap terbuka selama dipakai.

## Pemakaian

1. **Login (sekali saja):** klik *Buka browser untuk login*. Login di tiap tab (Instagram, X, TikTok, Threads, Facebook),
   lalu **tutup jendela browser**. Sesi tersimpan di folder `browser_profile/` di laptop ini dan tidak ikut ke GitHub.
   Saran: pakai akun biasa, bukan akun utama kantor.
2. **Daftar akun:** sudah terisi dari `daftar_akun.txt`. Kamu boleh mengubahnya lalu klik *Simpan daftar*.
   Satu URL per baris, semua platform boleh dicampur, dan duplikat dibuang otomatis.
3. **Periode:** pilih tanggal mulai dan akhir, zona waktu (WIB/WITA/WIT), dan platform yang mau diambil.
4. Klik **Mulai ambil link**. Biarkan jendela browser yang dibuka alat tetap terbuka (boleh di belakang).
   Kalau muncul **captcha** (sering di TikTok), selesaikan di jendela itu; proses menunggu.
   Tombol **Stop** tetap menyimpan hasil yang sudah terkumpul.

## Hasil

Tersimpan di folder `outputs/`, dan bisa juga diunduh dari halaman:

- `link_unggahan_<periode>_<waktu>.xlsx`
  - **Link Unggahan**: No, Platform, Akun, Tanggal, Jam, Link (link bisa diklik)
  - **Status Akun**: status tiap akun, yaitu OK / Tidak ditemukan / Perlu login / Username tidak valid / Error, plus jumlah link
  - **Info**: periode dan waktu pembuatan
- File `.csv` berisi data yang sama.

File disimpan ulang setelah tiap akun selesai, jadi kalau proses terputus hasil sejauh itu tetap ada.

## Catatan penting soal daftar akun

Sebagian URL di daftar **tidak mungkin valid** karena melanggar aturan username platformnya. Akun seperti itu
otomatis dilewati dan ditandai *Username tidak valid* di sheet Status Akun:

- **X:** maksimal 15 karakter dan tanpa titik. Contoh yang tidak valid: `pu_sda_sumatera1`, `setwan.dsdan`.
- **Facebook:** tidak boleh ada underscore (`_`), jadi semua `facebook.com/pu_sda_...` tidak valid.
  Halaman Facebook balai kemungkinan memakai alamat lain; cari alamat aslinya dari situs atau bio Instagram masing-masing.
- **TikTok:** maksimal 24 karakter. Contoh yang tidak valid: `pu_sda_pompenganjeneberang`, `pu_sda_balaiteknikirigasi`.

Ganti URL tersebut dengan alamat yang benar di `daftar_akun.txt`, lalu jalankan lagi.

## Batasan

- Platform bisa mengubah tampilan halamannya kapan saja; kalau sebuah platform tiba-tiba menghasilkan 0 link untuk semua akun,
  kemungkinan tampilannya berubah.
- Jeda acak antar-scroll dan antar-akun sengaja dibuat agar akun login tidak dibatasi. Jangan dijalankan berkali-kali beruntun.
- Unggahan *collab* yang dibuat akun lain, story, dan unggahan yang sudah dihapus tidak ikut.

## Struktur

| File | Isi |
| --- | --- |
| `crawler/accounts.py` | Membaca daftar URL, deteksi platform, validasi username |
| `crawler/idtime.py` | Membaca tanggal dari ID unggahan (IG/Threads/X/TikTok) |
| `crawler/browser.py` | Membuka profil di browser, scroll, dan mengumpulkan link per platform |
| `crawler/youtube.py` | YouTube via yt-dlp |
| `crawler/job.py` | Proses login & pengambilan di background, simpan hasil |
| `crawler/output.py` | Menulis Excel/CSV |
| `crawler/server.py`, `crawler/static/index.html` | Tampilan web lokal (hanya bisa diakses dari laptop ini) |
| `daftar_akun.txt` | Daftar akun default |
| `tests/` | Tes (`python -m pytest tests`) |

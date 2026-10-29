# Nexora Server

Server kecil untuk mengekspor video 1080p/1440p dengan 30 atau 60 fps memakai FFmpeg.

## Jalankan lokal
    pip install -r requirements.txt
    python app.py
Buka http://localhost:8000

## Jalankan dengan Docker
    docker build -t nexora .
    docker run -p 8000:8000 nexora

## Pengaturan (environment variable)
- NEXORA_MAX_MB: ukuran upload maksimal (default 200)
- NEXORA_MAX_SECONDS: durasi maksimal (default 90)
- NEXORA_TTL: lama hasil disimpan dalam detik (default 1800)
- NEXORA_DIR: folder penyimpanan sementara (default /tmp/nexora)

## Catatan
- Pakai satu worker (-w 1) karena status job disimpan di memori.
- Hasil dihapus otomatis setelah TTL.
- Versi gratis (Render, Oracle Always Free) punya CPU dan RAM terbatas, jadi proses bisa lebih lama.

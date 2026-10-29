"""Nexora server: terima video, ekspor dengan FFmpeg, lalu sediakan hasilnya untuk diunduh.

Jalankan lokal:  python app.py
Produksi:        gunicorn -w 1 --threads 4 -b 0.0.0.0:$PORT --timeout 900 app:app
(Pakai -w 1 karena status job disimpan di memori proses yang sama.)
"""
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

BASE = Path(os.environ.get("NEXORA_DIR", "/tmp/nexora"))
UP = BASE / "uploads"
OUT = BASE / "outputs"
MAX_MB = int(os.environ.get("NEXORA_MAX_MB", "200"))          # ukuran upload maksimal
MAX_SEC = float(os.environ.get("NEXORA_MAX_SECONDS", "90"))   # durasi video maksimal
TTL = int(os.environ.get("NEXORA_TTL", "1800"))               # hasil dihapus setelah 30 menit

ALLOWED = {"mp4", "mov", "m4v", "webm"}
RES = {"1080": (1080, 1920, "30M", "35M", "50M"), "1440": (1440, 2560, "40M", "45M", "70M")}
FPS = {"30", "60"}

UP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024

jobs = {}
jobs_lock = threading.Lock()
queue_ = queue.Queue()


def set_job(jid, **kw):
    with jobs_lock:
        if jid in jobs:
            jobs[jid].update(kw)


def probe_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def build_cmd(src, dst, res, fps):
    w, h, bitrate, maxrate, bufsize = RES[res]
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p")
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
        "-b:v", bitrate, "-maxrate", maxrate, "-bufsize", bufsize,
        "-r", str(fps),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        str(dst),
    ]


def run_job(jid):
    with jobs_lock:
        job = dict(jobs[jid])
    src, dst = job["src"], job["dst"]
    dur = job["duration"] or 1.0
    set_job(jid, status="processing", progress=0)
    try:
        proc = subprocess.Popen(build_cmd(src, dst, job["res"], job["fps"]),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in proc.stdout:
            if line.startswith("out_time_us="):
                try:
                    t = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                set_job(jid, progress=min(99, int(t / dur * 100)))
        err = proc.stderr.read()
        rc = proc.wait()
        if rc != 0 or not dst.exists():
            raise RuntimeError((err or "FFmpeg gagal").strip()[-300:])
        set_job(jid, status="done", progress=100, size=dst.stat().st_size)
    except Exception as e:  # noqa: BLE001
        set_job(jid, status="error", error=str(e))
    finally:
        src.unlink(missing_ok=True)


def worker():
    while True:
        jid = queue_.get()
        try:
            run_job(jid)
        finally:
            queue_.task_done()


def cleaner():
    while True:
        time.sleep(60)
        now = time.time()
        with jobs_lock:
            expired = [j for j, v in jobs.items() if now - v["created"] > TTL]
            for j in expired:
                v = jobs.pop(j)
                v["dst"].unlink(missing_ok=True)
                v["src"].unlink(missing_ok=True)


threading.Thread(target=worker, daemon=True).start()
threading.Thread(target=cleaner, daemon=True).start()


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.post("/api/jobs")
def create_job():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="Pilih video dulu."), 400
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in ALLOWED:
        return jsonify(error="Format tidak didukung. Pakai MP4, MOV, M4V, atau WebM."), 400
    res = request.form.get("res", "1080")
    fps = request.form.get("fps", "60")
    if res not in RES or fps not in FPS:
        return jsonify(error="Pilihan resolusi atau fps tidak valid."), 400

    jid = uuid.uuid4().hex[:12]
    src = UP / f"{jid}.{ext}"
    f.save(src)

    dur = probe_duration(src)
    if dur <= 0:
        src.unlink(missing_ok=True)
        return jsonify(error="File tidak bisa dibaca sebagai video."), 400
    if dur > MAX_SEC:
        src.unlink(missing_ok=True)
        return jsonify(error=f"Durasi maksimal {int(MAX_SEC)} detik."), 400

    with jobs_lock:
        jobs[jid] = {
            "status": "queued", "progress": 0, "error": None,
            "res": res, "fps": int(fps), "duration": dur,
            "src": src, "dst": OUT / f"{jid}.mp4", "created": time.time(), "size": 0,
        }
    queue_.put(jid)
    return jsonify(id=jid), 202


@app.get("/api/jobs/<jid>")
def job_status(jid):
    with jobs_lock:
        job = jobs.get(jid)
        if not job:
            abort(404)
        return jsonify(status=job["status"], progress=job["progress"],
                       error=job["error"], size=job["size"])


@app.get("/api/jobs/<jid>/download")
def download(jid):
    with jobs_lock:
        job = jobs.get(jid)
        if not job or job["status"] != "done":
            abort(404)
        dst, res, fps = job["dst"], job["res"], job["fps"]
    return send_file(dst, as_attachment=True, mimetype="video/mp4",
                     download_name=f"nexora-{res}p-{fps}fps.mp4")


@app.errorhandler(413)
def too_large(_):
    return jsonify(error=f"Ukuran file maksimal {MAX_MB} MB."), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), threaded=True)

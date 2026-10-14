"""Media helpers: find ffmpeg (system PATH, else the one bundled by `pip install imageio-ffmpeg`), probe a video, grab
a poster frame, classify files. No GUI imports."""
import os
import re
import shutil
import subprocess

IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
VID_EXT = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
PAGE_EXT = {".html", ".htm", ".svg", ".md", ".txt", ".pdf"}
NOWINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}      # no console flash on Windows


def kind_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in IMG_EXT:
        return "image"
    if ext in VID_EXT:
        return "video"
    return "page" if ext in PAGE_EXT else "file"


def ffmpeg_path() -> str | None:
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def probe(path: str) -> dict:
    """duration (s), width, height, has_audio. Parsed from `ffmpeg -i` (works without ffprobe)."""
    ff = ffmpeg_path()
    info = {"duration": 0.0, "width": 0, "height": 0, "has_audio": False}
    if not ff:
        return info
    try:
        err = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, text=True, timeout=30, **NOWINDOW).stderr
    except (subprocess.SubprocessError, OSError):
        return info
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", err)
    if m:
        info["width"], info["height"] = int(m[1]), int(m[2])
    info["has_audio"] = "Audio:" in err
    return info


def poster(path: str, t: float = 1.0, width: int = 480) -> bytes | None:
    """One JPEG frame at time t (seconds)."""
    ff = ffmpeg_path()
    if not ff:
        return None
    for ts in (t, 0.0):                                   # very short clip: fall back to the first frame
        try:
            r = subprocess.run([ff, "-v", "error", "-ss", f"{ts:.2f}", "-i", path, "-frames:v", "1",
                                "-vf", f"scale='min({width},iw)':-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                               capture_output=True, timeout=30, **NOWINDOW)
        except (subprocess.SubprocessError, OSError):
            return None
        if r.returncode == 0 and r.stdout:
            return r.stdout
    return None


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_time(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 60}:{sec % 60:02d}"

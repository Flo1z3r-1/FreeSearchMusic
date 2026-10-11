import os
import re
import glob
import shutil
import subprocess
import tempfile
import threading
import logging

from dotenv import load_dotenv
import telebot
from telebot import apihelper

# Загружаем переменные окружения из файла .env
load_dotenv()

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

# -----------------------------------------------------------------------------
# НАСТРОЙКИ
# -----------------------------------------------------------------------------
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise SystemExit("❌ Ошибка: не найден токен бота! Создайте файл .env и укажите в нем BOT_TOKEN=ваш_токен")

MAX_DURATION_SEC = 900          
MIN_FULL_SEC = 60               
MAX_FILE_MB = 49                
MAX_PARALLEL_DOWNLOADS = 2      # Уменьшено до 2 для защиты от бана IP
CANDIDATES_PER_SOURCE = 4       

SOURCES = [
    ("SoundCloud", "scsearch6"),
    ("YouTube", "ytsearch6"),
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("musicbot")

apihelper.CONNECT_TIMEOUT = 10
apihelper.READ_TIMEOUT = 60

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=6)
download_slots = threading.Semaphore(MAX_PARALLEL_DOWNLOADS)

file_cache = {}
cache_lock = threading.Lock()

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _clear_dir(path: str):
    for f in glob.glob(os.path.join(path, "*")):
        try:
            os.remove(f)
        except OSError:
            pass


def real_duration(path: str):
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(path)
        length = getattr(getattr(mf, "info", None), "length", 0)
        if length and length > 0:
            return int(round(length))
    except Exception:
        pass

    if shutil.which("ffprobe"):
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", path],
                capture_output=True, text=True, timeout=15,
            ).stdout.strip()
            if out:
                return int(round(float(out)))
        except Exception:
            pass
    return None


def clean_track_metadata(title: str, performer: str):
    full_str = f"{title} - {performer}"
    full_str = re.sub(r'(?:тг|tg|канал|кнал)[\s:]*@?\w+', '', full_str, flags=re.IGNORECASE)
    full_str = re.sub(r'@[A-Za-z0-9_]+', '', full_str)
    full_str = re.sub(r'(?:сливы|слив музыки|премьера трека|exclusive|текст песни|lyrics).*', '', full_str, flags=re.IGNORECASE)
    full_str = re.sub(r'[\|\/\\#]', '-', full_str)
    
    parts = [p.strip() for p in full_str.split('-') if p.strip()]
    
    if len(parts) >= 2:
        clean_performer = parts[0]
        clean_title = " - ".join(parts[1:])
    elif len(parts) == 1:
        clean_performer = performer.strip()
        clean_title = parts[0]
    else:
        clean_title = "Трек"
        clean_performer = "Исполнитель"
        
    clean_title = re.sub(r'^[\s\-–—]+|[\s\-–—]+$', '', clean_title)
    clean_performer = re.sub(r'^[\s\-–—]+|[\s\-–—]+$', '', clean_performer)
    
    return clean_title or "Трек", clean_performer or "Исполнитель"


def calculate_relevance_score(query: str, title: str, uploader: str) -> int:
    q_words = set(re.findall(r'[а-яёa-z0-9]+', query.lower()))
    if not q_words:
        return 0
    
    target_text = f"{title} {uploader}".lower()
    score = 0
    
    for word in q_words:
        if word in target_text:
            score += 1
            
    if query.lower() in title.lower():
        score += 5
        
    return score


def try_source(name: str, prefix: str, query: str, tmpdir: str, errors: list):
    base = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }

    try:
        opts = {**base, "extract_flat": "in_playlist", "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            found = ydl.extract_info(f"{prefix}:{query}", download=False)
    except Exception as e:
        log.warning("%s: ошибка поиска: %s", name, e)
        errors.append(f"{name}: поиск — {str(e)[:120]}")
        return None

    candidates = []
    for e in (found or {}).get("entries") or []:
        if not e:
            continue
        url = e.get("webpage_url") or e.get("url")
        dur = e.get("duration")
        title = e.get("title") or ""
        uploader = e.get("uploader") or e.get("channel") or ""
        
        if not url or not str(url).startswith("http"):
            continue
        if dur is not None and not (MIN_FULL_SEC <= dur <= MAX_DURATION_SEC):
            continue
            
        score = calculate_relevance_score(query, title, uploader)
        candidates.append({"url": url, "score": score, "title": title})

    if not candidates:
        errors.append(f"{name}: ничего подходящего")
        return None

    candidates.sort(key=lambda x: x["score"], reverse=True)

    dl_opts = {
        **base,
        "format": "bestaudio/best" if HAS_FFMPEG
        else "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio",
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
    }
    if HAS_FFMPEG:
        dl_opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]

    for candidate in candidates[:CANDIDATES_PER_SOURCE]:
        url = candidate["url"]
        _clear_dir(tmpdir)
        try:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            log.warning("%s: ошибка загрузки %s: %s", name, url, e)
            continue

        files = [f for f in glob.glob(os.path.join(tmpdir, "*")) if os.path.isfile(f)]
        if not files:
            continue
        path = max(files, key=os.path.getsize)
        if os.path.getsize(path) > MAX_FILE_MB * 1024 * 1024:
            continue

        site_duration = (info or {}).get("duration")
        file_duration = real_duration(path)
        duration = file_duration or site_duration
        if duration is not None and duration < MIN_FULL_SEC:
            continue

        raw_title = info.get("track") or info.get("title") or "Трек"
        raw_performer = info.get("artist") or info.get("uploader") or info.get("channel") or "Исполнитель"
        
        clean_title, clean_performer = clean_track_metadata(raw_title, raw_performer)

        return {
            "path": path,
            "title": clean_title,
            "performer": clean_performer,
            "duration": int(duration) if duration else None,
            "source": name,
        }

    return None


def download_full_track(query: str, tmpdir: str, errors: list):
    for name, prefix in SOURCES:
        track = try_source(name, prefix, query, tmpdir, errors)
        if track:
            log.info("Найдено через %s: %s", name, track["title"])
            return track
    return None


@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    bot.reply_to(
        message,
        "Здравствуйте! 🎵 Отправьте название песни и автора, и я быстро пришлю точную аудиозапись.",
    )


def safe_edit(chat_id, message_id, text):
    try:
        bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


@bot.message_handler(content_types=["text"])
def handle_search(message):
    query = (message.text or "").strip()
    if query.startswith("/"):
        return
    if len(query) < 2:
        bot.reply_to(message, "Пожалуйста, введите более точный запрос.")
        return

    if yt_dlp is None:
        bot.reply_to(message, "❌ Модуль yt-dlp не установлен.")
        return

    chat_id = message.chat.id
    key = query.lower()

    with cache_lock:
        cached = file_cache.get(key)
    if cached:
        try:
            bot.send_audio(chat_id, cached["file_id"], caption=cached["caption"])
            return
        except Exception:
            with cache_lock:
                file_cache.pop(key, None)

    status = bot.reply_to(message, "⚡ Ищу нужный трек...")
    tmpdir = tempfile.mkdtemp(prefix="track_")
    errors = []

    try:
        with download_slots:
            track = download_full_track(query, tmpdir, errors)

        if not track:
            safe_edit(chat_id, status.message_id, "❌ Трек не найден. Возможно, сервис временно ограничивает запросы с этого сервера. Попробуйте чуть позже.")
            return

        caption = f"🎧 {track['performer']} — {track['title']}"

        with open(track["path"], "rb") as f:
            sent = bot.send_audio(
                chat_id,
                f,
                title=track["title"],
                performer=track["performer"],
                duration=track["duration"],
                caption=caption,
                timeout=120,
            )

        if sent and sent.audio:
            with cache_lock:
                if len(file_cache) > 2000:
                    file_cache.clear()
                file_cache[key] = {"file_id": sent.audio.file_id, "caption": caption}

        try:
            bot.delete_message(chat_id, status.message_id)
        except Exception:
            pass

    except Exception as e:
        log.exception("Ошибка обработки: %s", e)
        safe_edit(chat_id, status.message_id, "❌ Произошла ошибка при отправке аудио.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    log.info("Бот запущен. ffmpeg: %s, yt-dlp: %s", HAS_FFMPEG, yt_dlp is not None)
    bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)

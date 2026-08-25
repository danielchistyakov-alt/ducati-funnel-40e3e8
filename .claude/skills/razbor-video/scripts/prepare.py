#!/usr/bin/env python3
"""Готовит видео к разбору: звук, кадры смены слайдов, расшифровка.

    python3 prepare.py "https://vkvideo.ru/video-150593037_456240881"
    python3 prepare.py запись.mp4 --выход разбор/вебинар
    python3 prepare.py запись.mp4 --без-расшифровки      # только кадры и звук
    python3 prepare.py запись.mp4 --модель /путь/vosk-model-small-ru-0.22

На выходе в папке разбора:
    видео.mp4            исходник (если качали)
    звук.ogg             моно 16 кГц, opus 16 кбит/с — час речи ≈ 7 МБ
    кадры/NNN_ммсс.jpg   по кадру на смену слайда, таймкод в имени
    расшифровка.srt      речь с таймкодами, если распознавание доступно
    опись.json           что получилось: длительность, список кадров, пути

Кадры именованы таймкодом, поэтому кадр и кусок расшифровки сходятся по времени —
это и есть то, ради чего всё затевается: слайд плюс то, что под него говорили.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ffmpeg берём из пакета imageio-ffmpeg, если системного нет: он ставится с PyPI,
# а PyPI открыт даже в сессиях с урезанным доступом в сеть.
def ffmpeg() -> str:
    if (сист := shutil.which("ffmpeg")):
        return сист
    try:
        import imageio_ffmpeg
    except ImportError:
        поставить("imageio-ffmpeg")
        import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def поставить(пакет: str) -> None:
    print(f"ставлю {пакет}…", file=sys.stderr)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", пакет], check=True)


def прогнать(команда: list[str]) -> None:
    итог = subprocess.run(команда, capture_output=True, text=True)
    if итог.returncode:
        хвост = (итог.stderr or итог.stdout).strip().splitlines()[-15:]
        raise SystemExit("не выполнилось: " + " ".join(команда[:3]) + "\n" + "\n".join(хвост))


def скачать(ссылка: str, куда: Path) -> Path:
    if not shutil.which("yt-dlp"):
        поставить("yt-dlp")
    шаблон = str(куда / "видео.%(ext)s")
    прогнать(["yt-dlp", "--no-playlist", "--merge-output-format", "mp4", "-o", шаблон, ссылка])
    файлы = sorted(куда.glob("видео.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not файлы:
        raise SystemExit("yt-dlp отработал, но файла нет — проверь ссылку и доступ к домену")
    return файлы[0]


def длительность(видео: Path) -> float:
    итог = subprocess.run([ffmpeg(), "-i", str(видео)], capture_output=True, text=True)
    if (м := re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", итог.stderr)):
        ч, мин, сек = м.groups()
        return int(ч) * 3600 + int(мин) * 60 + float(сек)
    return 0.0


def звук(видео: Path, куда: Path) -> Path:
    цель = куда / "звук.ogg"
    прогнать([ffmpeg(), "-y", "-i", str(видео), "-vn", "-ac", "1", "-ar", "16000",
              "-c:a", "libopus", "-b:a", "16k", str(цель)])
    return цель


def кадры(видео: Path, куда: Path, порог: float, предел: int) -> list[dict]:
    """Кадры на смене сцены: для скринкаста со слайдами это и есть смена слайда."""
    папка = куда / "кадры"
    папка.mkdir(exist_ok=True)
    for старый in папка.glob("*.jpg"):
        старый.unlink()
    лог = куда / "кадры.log"
    # eq(n,0) — первый кадр: детектор смены сцены его пропускает, а это титульный слайд
    фильтр = (f"select='eq(n,0)+gt(scene,{порог})',metadata=print:file={лог},"
              "scale='min(1600,iw)':-2")
    прогнать([ffmpeg(), "-y", "-i", str(видео), "-vf", фильтр, "-vsync", "vfr",
              "-q:v", "3", str(папка / "врем_%04d.jpg")])

    времена = [float(м) for м in re.findall(r"pts_time:(\d+\.?\d*)", лог.read_text() or "")]
    снимки = sorted(папка.glob("врем_*.jpg"))
    лог.unlink(missing_ok=True)

    # Кадров может выйти сильно больше, чем нужно (анимации, курсор, перемотка).
    # Оставляем самые «сильные» по порядку времени, равномерно прореживая.
    шаг = max(1, len(снимки) // предел + (1 if len(снимки) % предел else 0))
    отобранные = []
    for номер, снимок in enumerate(снимки):
        if номер % шаг:
            снимок.unlink()
            continue
        сек = времена[номер] if номер < len(времена) else 0.0
        имя = f"{len(отобранные) + 1:03d}_{int(сек) // 60:02d}м{int(сек) % 60:02d}с.jpg"
        снимок.rename(папка / имя)
        отобранные.append({"файл": f"кадры/{имя}", "секунда": round(сек, 1)})
    return отобранные


def в_таймкод(сек: float) -> str:
    целиком = max(0.0, сек)
    ч, остаток = divmod(целиком, 3600)
    мин, сек_ = divmod(остаток, 60)
    return f"{int(ч):02d}:{int(мин):02d}:{сек_:06.3f}".replace(".", ",")


def расшифровать(звуковой: Path, куда: Path, модель: str | None) -> Path | None:
    """Сначала пробуем faster-whisper (веса с huggingface.co), потом vosk с локальной моделью.

    Если сеть урезана и веса не скачиваются, скрипт не падает: он честно говорит,
    что расшифровки не будет, — кадры и звук всё равно уже готовы.
    """
    цель = куда / "расшифровка.srt"
    if модель:
        return _вослк(звуковой, цель, модель)
    try:
        поставить("faster-whisper")
        from faster_whisper import WhisperModel
        сеть = WhisperModel("small", device="cpu", compute_type="int8")
        куски, _ = сеть.transcribe(str(звуковой), language="ru", vad_filter=True)
        строки = []
        for номер, кусок in enumerate(куски, 1):
            строки.append(f"{номер}\n{в_таймкод(кусок.start)} --> {в_таймкод(кусок.end)}\n{кусок.text.strip()}\n")
        цель.write_text("\n".join(строки), encoding="utf-8")
        return цель
    except Exception as беда:               # нет весов, нет сети, мало памяти
        print(f"расшифровка не вышла: {беда}", file=sys.stderr)
        print("варианты: открыть доступ к huggingface.co или дать --модель с моделью vosk",
              file=sys.stderr)
        return None


def _вослк(звуковой: Path, цель: Path, модель: str) -> Path | None:
    try:
        поставить("vosk")
        import wave
        from vosk import KaldiRecognizer, Model
        сырой = звуковой.with_suffix(".wav")
        прогнать([ffmpeg(), "-y", "-i", str(звуковой), "-ac", "1", "-ar", "16000", str(сырой)])
        with wave.open(str(сырой), "rb") as поток:
            распознаватель = KaldiRecognizer(Model(модель), поток.getframerate())
            распознаватель.SetWords(True)
            куски = []
            while (данные := поток.readframes(4000)):
                if распознаватель.AcceptWaveform(данные):
                    куски.append(json.loads(распознаватель.Result()))
            куски.append(json.loads(распознаватель.FinalResult()))
        строки = []
        for номер, кусок in enumerate([к for к in куски if к.get("text")], 1):
            слова = кусок.get("result") or []
            начало = слова[0]["start"] if слова else 0
            конец = слова[-1]["end"] if слова else начало
            строки.append(f"{номер}\n{в_таймкод(начало)} --> {в_таймкод(конец)}\n{кусок['text']}\n")
        цель.write_text("\n".join(строки), encoding="utf-8")
        сырой.unlink(missing_ok=True)
        return цель
    except Exception as беда:
        print(f"vosk не отработал: {беда}", file=sys.stderr)
        return None


def main() -> None:
    разбор = argparse.ArgumentParser(description="подготовка видео к разбору")
    разбор.add_argument("источник", help="ссылка на видео или путь к файлу")
    разбор.add_argument("--выход", default="разбор", help="папка с результатами")
    разбор.add_argument("--порог", type=float, default=0.15, help="чувствительность к смене кадра")
    разбор.add_argument("--макс-кадров", type=int, default=80, dest="макс")
    разбор.add_argument("--без-расшифровки", action="store_true")
    разбор.add_argument("--модель", help="папка модели vosk, если whisper недоступен")
    дано = разбор.parse_args()

    куда = Path(дано.выход)
    куда.mkdir(parents=True, exist_ok=True)

    видео = Path(дано.источник)
    if not видео.exists():
        видео = скачать(дано.источник, куда)

    итог = {"видео": str(видео), "длительность_сек": round(длительность(видео), 1)}
    итог["звук"] = str(звук(видео, куда))
    итог["кадры"] = кадры(видео, куда, дано.порог, дано.макс)
    if not дано.без_расшифровки:
        путь = расшифровать(Path(итог["звук"]), куда, дано.модель)
        итог["расшифровка"] = str(путь) if путь else None

    (куда / "опись.json").write_text(json.dumps(итог, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"готово: {len(итог['кадры'])} кадров, {итог['длительность_сек']} сек, папка {куда}")


if __name__ == "__main__":
    main()

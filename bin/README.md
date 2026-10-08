# bin/

Сюда `python transcribe.py --setup` скачивает и распаковывает whisper.cpp
(на Windows — `whisper-cli.exe` и его DLL, в подпапку `whisper/`).

Если скачивание автоматически не сработало, положите сюда архив вручную:
скачайте с https://github.com/ggml-org/whisper.cpp/releases/latest файл
`whisper-bin-x64.zip` (или `whisper-bin-win-cuda-12.4.0-x64.zip` для видеокарты NVIDIA)
и распакуйте в любую подпапку `bin/` — скрипт найдёт `whisper-cli.exe` сам.

Сюда же можно положить `ffmpeg.exe` и `ffprobe.exe`, если не хочется ставить ffmpeg
через winget.

Содержимое этой папки в репозиторий не попадает (см. `.gitignore`).

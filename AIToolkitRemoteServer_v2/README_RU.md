# AI Toolkit Remote Server 2.0

Отдельный Windows-сервер для iPhone-приложения **AI Toolkit Remote**.

## Что изменилось

Теперь **ComfyUIRemote / PCRemoteServer.exe вообще не нужен**.

Схема:

`iPhone → AIToolkitRemoteServer.exe → AI Toolkit на 127.0.0.1:8675`

Приложение iPhone из версии 1.0 совместимо с этим сервером: API сохранён под `/api/aitk/*`.

## Быстрый запуск

1. Собери `AIToolkitRemoteServer.exe` через GitHub Actions:
   - положи содержимое этого ZIP в отдельный GitHub-репозиторий;
   - открой **Actions → Build AI Toolkit Remote Server EXE → Run workflow**;
   - после сборки внизу Summary скачай artifact **AIToolkitRemoteServer-Windows**.
2. Запусти `AIToolkitRemoteServer.exe` на ПК.
3. Windows может спросить разрешение Firewall — разреши для **Private networks**.
4. В окне сервера:
   - `AI Toolkit URL` оставь `http://127.0.0.1:8675`;
   - выбери `.bat/.cmd/.exe`, которым запускаешь AI Toolkit;
   - выбери рабочую папку AI Toolkit;
   - выбери папку `datasets`, если она не определилась автоматически;
   - нажми **Сохранить**.
5. В блоке **Подключение iPhone** будут:
   - **Адрес** — например `http://192.168.1.50:8111` или Tailscale `http://100.x.x.x:8111`;
   - **Ключ** — генерируется автоматически.
6. На iPhone открой AI Toolkit Remote → **Настройки**:
   - Server URL = адрес из EXE;
   - Access key = ключ из EXE.
7. Нажми проверку подключения.

## Вне дома

Если ПК и iPhone подключены к Tailscale, сервер старается показать Tailscale-IP первым. Используй адрес вида:

`http://100.x.x.x:8111`

Порт AI Toolkit `8675` наружу открывать не нужно.

## Что сервер умеет

- статус AI Toolkit;
- запуск AI Toolkit с ПК-команды;
- GPU/VRAM;
- список job;
- создание/изменение job;
- Start / Stop / Save now / Delete;
- live log;
- loss;
- samples;
- список `.safetensors` и `optimizer.pt`;
- потоковое скачивание больших файлов;
- список датасетов;
- создание папки датасета;
- просмотр датасета;
- загрузка фото с iPhone без буферизации всего multipart-файла в RAM сервера.

## Конфиг

После первого запуска рядом с EXE появится:

`AIToolkitRemoteServer.json`

Пример:

```json
{
  "host": "0.0.0.0",
  "port": 8111,
  "access_key": "генерируется автоматически",
  "ai_toolkit_url": "http://127.0.0.1:8675",
  "start_command": "\"D:\\AI-Toolkit\\run_windows.bat\"",
  "start_cwd": "D:\\AI-Toolkit",
  "datasets_dir": "D:\\AI-Toolkit\\datasets",
  "request_timeout": 15.0,
  "max_upload_mb": 4096,
  "autostart_windows": false
}
```

## Безопасность

- AI Toolkit upstream разрешён только на localhost/127.0.0.1/::1.
- iPhone API защищён ключом `X-PCRemote-Key` / Bearer.
- Сервер не является универсальным HTTP proxy.
- Большие скачивания и загрузки стримятся.

## Если iPhone пишет Unauthorized

Скопируй ключ из окна EXE заново в поле `Access key`.

## Если AI Toolkit Offline

Проверь, открывается ли на ПК `http://127.0.0.1:8675`. Затем укажи правильную команду запуска в EXE и нажми **Запустить AI Toolkit**.

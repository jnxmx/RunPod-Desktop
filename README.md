# Comfy Course Deployer (`ComfyUI-RunPod-Desktop`)

Самодостаточный плагин для **Comfy Desktop (1.2.1+)** и ComfyUI, который превращает локальную инстанцию в полноценный пульт управления подами RunPod и прозрачный HTTP/WebSocket-прокси к удалённому ComfyUI.

## Основные возможности

- **100% локальный интерфейс:** не зависит от внешних сайтов, работает напрямую с `api.runpod.io`.
- **Безопасное хранение ключа:** RunPod API Secret Key хранится только на стороне Python-плагина в `config.json` (с правами `0600`) и никогда не попадает в `localStorage` браузера. В режиме подключения к поду (`remote`) доступ к настройкам ключа и прокси RunPod API заблокирован (`403`).
- **Баланс через GraphQL:** в левой верхней карточке отображается текущий баланс RunPod, расход в час (`$/ч`) и оценка оставшегося времени работы.
- **Двухколоночный интерфейс в стиле Comfy Desktop:**
  - **Левая колонка:** Баланс (GraphQL), API-ключ и секреты (`HF_TOKEN`, `CIVITAI_API_KEY`, `COMFYUI_BACKUP`), список активных подов с 5-этапным мониторингом, логами SSE, кнопкой **«Открыть»** (открывает удалённый ComfyUI прямо внутри окна Comfy Desktop) и внешними ссылками **«Fizgig ↗»** (`:7862`) / **«FileBrowser ↗»** (`:7861`).
  - **Правая колонка:** Настройка темплейта (`casls01eko` по умолчанию), параметры запуска (`NEW_MANAGER`, `RESTORE_BACKUP`, таймер автоудаления) и таблица GPU с фильтрами цены, RAM, Secure Cloud и авто-повтором/охотой.
- **Плавающая панель поверх удалённого ComfyUI (`topbar.js`):** Shadow DOM-пилюля для быстрого возврата в деплойер, открытия Fizgig/FileBrowser в системном браузере и удаления пода.
- **Поддержка ComfyUI Agent:** запросы `/api/agent` и `/api/workflows` обслуживаются локальным `comfy-agent` в Comfy Desktop, тогда как чтение каталога нод/моделей (`object_info`), запуск воркфлоу и стриминг результатов прозрачно идут на подключённый под RunPod. При подключении к поду и после перезапуска удалённого ComfyUI каталог локального агента автоматически обновляется.

## Установка через Snapshot в Comfy Desktop 1.2.1+

1. В **Comfy Desktop** нажмите **New Instance → Load Snapshot**.
2. Выберите или перетащите файл [`runpod-deployer.snapshot.json`](./runpod-deployer.snapshot.json).
3. Нажмите **Create** — Comfy Desktop создаст отдельную лёгкую инстанцию (`variant: cpu`, без лишних CUDA-колёс) и установит плагин.

> **Примечание для раздачи студентам (CNR vs Git):**
> Для тестового снапшота используется `"type": "git"` (требует установленного `git` в `PATH`). Для массовой раздачи студентам без `git` опубликуйте плагин в ComfyUI Registry (`comfy node publish`) и замените запись в `customNodes` файла `runpod-deployer.snapshot.json` на `"type": "cnr"`:
> ```json
> {
>   "dirName": "comfyui-runpod-desktop",
>   "id": "comfyui-runpod-desktop",
>   "type": "cnr",
>   "version": "0.1.0",
>   "enabled": true
> }
> ```

## Локальная отладка

Чтобы временно отключить перехват middleware без удаления папки плагина, запустите ComfyUI с переменной окружения:

```bash
RUNPOD_DESKTOP_DISABLE=1
```

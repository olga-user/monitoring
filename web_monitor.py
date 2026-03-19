import os
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from groq import Groq

import monitor


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "web"
ENV_FILE = BASE_DIR / ".env.local"
PROMPT_FILE = BASE_DIR / "spam_prompt.txt"

app = Flask(__name__, static_folder=str(STATIC_DIR))

# Разрешаем кросс-доменные запросы для встраивания фронта на другой сайт.
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


def load_local_env() -> None:
    if not ENV_FILE.exists():
        return
    with ENV_FILE.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and not os.environ.get(key):
                os.environ[key] = value


def save_local_env(values: dict[str, str]) -> None:
    existing: dict[str, str] = {}
    if ENV_FILE.exists():
        with ENV_FILE.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                existing[key.strip()] = value.strip()

    for key, value in values.items():
        if value:
            existing[key] = value

    lines = [f"{k}={v}" for k, v in existing.items()]
    with ENV_FILE.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


def load_prompt_override() -> None:
    if not PROMPT_FILE.exists():
        return
    text = PROMPT_FILE.read_text(encoding="utf-8").strip()
    if text:
        monitor.SPAM_PROMPT = text


def save_prompt_override(prompt_text: str) -> None:
    PROMPT_FILE.write_text(prompt_text.strip() + "\n", encoding="utf-8")
    monitor.SPAM_PROMPT = prompt_text.strip()


class MonitorController:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None

        self.client: Groq | None = None
        self.last_id: int = monitor.load_last_id()
        self.cycle_no: int = 1

        self.last_error: str | None = None
        self.last_cycle_at: float | None = None
        self.total_checked: int = 0
        self.cycle_running: bool = False
        self.cycle_started_at: float | None = None
        self.current_total: int = 0
        self.current_done: int = 0
        self.current_comment_id: str | None = None
        self.current_cycle_rows: list[dict] = []
        self.last_cycle_rows: list[dict] = []

    @property
    def is_running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _ensure_client(self) -> None:
        if self.client:
            return
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("Не задана переменная окружения GROQ_API_KEY.")
        self.client = Groq(api_key=api_key)

    def run_once(self) -> dict:
        with self.lock:
            self._ensure_client()
            sep = "═" * 50
            monitor.log.info("%s", sep)
            monitor.log.info(" Цикл #%d | %s", self.cycle_no, time.strftime("%Y-%m-%d %H:%M:%S"))
            monitor.log.info("%s", sep)

            self.cycle_running = True
            self.cycle_started_at = time.time()
            self.current_total = 0
            self.current_done = 0
            self.current_comment_id = None
            self.current_cycle_rows = []
            self.last_error = None

            try:
                comments = monitor.fetch_comments()
                new_comments = [c for c in comments if int(c["id"]) > self.last_id]
                self.current_total = len(new_comments)

                if not new_comments:
                    monitor.log.info("Новых комментариев нет.")
                    self.last_cycle_at = time.time()
                    self.cycle_no += 1
                    return {"checked_now": 0, "last_id": self.last_id, "cycle_no": self.cycle_no}

                monitor.log.info("Новых комментариев для проверки: %d", self.current_total)
                new_last_id = self.last_id

                for i, comment in enumerate(new_comments, start=1):
                    self.current_done = i
                    self.current_comment_id = comment["id"]
                    spam = monitor.is_spam(comment["text"], self.client)
                    new_last_id = max(new_last_id, int(comment["id"]))
                    self.current_cycle_rows.append({
                        "id": comment["id"],
                        "text": comment["text"],
                        "article_url": comment["article_url"],
                        "comment_url": comment.get("comment_url", ""),
                        "is_spam": spam,
                    })

                    if spam:
                        monitor.log.info("[%d/%d] id=%s | SPAM", i, self.current_total, comment["id"])
                        monitor.log.info("        Текст: %s", comment["text"][:200])
                        monitor.log.info("        Статья: %s", comment["article_url"] or "неизвестна")
                        monitor.log_spam(comment["text"], comment["article_url"])
                    else:
                        monitor.log.info("[%d/%d] id=%s | ok | %s", i, self.current_total, comment["id"], comment["text"][:100])

                    time.sleep(monitor.random.uniform(*monitor.GROQ_DELAY))

                monitor.save_last_id(new_last_id)
                self.last_id = new_last_id
                checked_now = len(new_comments)
                self.total_checked += checked_now
                self.last_cycle_at = time.time()
                self.last_cycle_rows = list(self.current_cycle_rows)
                self.cycle_no += 1
                return {"checked_now": checked_now, "last_id": self.last_id, "cycle_no": self.cycle_no}
            except Exception:
                self.last_error = "Ошибка в цикле, смотрите лог сервера."
                raise
            finally:
                self.cycle_running = False
                self.current_done = 0
                self.current_total = 0
                self.current_comment_id = None

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:
                self.last_error = str(exc)
                monitor.log.error("Ошибка в web-цикле: %s", exc, exc_info=True)

            # Проверяем stop_event раз в секунду, чтобы остановка была быстрой.
            for _ in range(monitor.INTERVAL):
                if self.stop_event.is_set():
                    break
                time.sleep(1)

    def start(self) -> None:
        if self.is_running:
            return
        self._ensure_client()
        self.stop_event.clear()
        self.worker = threading.Thread(target=self._loop, daemon=True, name="klerk-monitor-worker")
        self.worker.start()

    def stop(self) -> None:
        if not self.is_running:
            return
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=10)

    def status(self) -> dict:
        return {
            "running": self.is_running,
            "last_id": self.last_id,
            "cycle_no": self.cycle_no,
            "interval_sec": monitor.INTERVAL,
            "model": monitor.MODEL,
            "last_cycle_at": self.last_cycle_at,
            "total_checked": self.total_checked,
            "last_error": self.last_error,
            "has_groq_key": bool(os.environ.get("GROQ_API_KEY")),
            "has_bitrix": bool(os.environ.get("BITRIX_WEBHOOK")) and bool(os.environ.get("BITRIX_CHAT_ID")),
            "cycle_running": self.cycle_running,
            "cycle_started_at": self.cycle_started_at,
            "current_total": self.current_total,
            "current_done": self.current_done,
            "current_comment_id": self.current_comment_id,
        }

    def comments_view(self) -> dict:
        rows = self.current_cycle_rows if self.cycle_running else self.last_cycle_rows
        spam = [r for r in rows if r.get("is_spam")]
        return {
            "rows": rows[-1000:],
            "spam_rows": spam[-1000:],
            "total": len(rows),
            "spam_total": len(spam),
            "cycle_running": self.cycle_running,
        }


load_local_env()
load_prompt_override()
controller = MonitorController()


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/status")
def api_status():
    return jsonify({"ok": True, "status": controller.status()})


@app.get("/api/config")
def api_config_get():
    return jsonify({
        "ok": True,
        "config": {
            "groq_key_masked": "***" if os.environ.get("GROQ_API_KEY") else "",
            "bitrix_webhook_masked": "***" if os.environ.get("BITRIX_WEBHOOK") else "",
            "bitrix_chat_id": os.environ.get("BITRIX_CHAT_ID", ""),
        },
    })


@app.post("/api/config")
def api_config_set():
    data = request.get_json(silent=True) or {}
    groq_api_key = str(data.get("groq_api_key", "")).strip()
    bitrix_webhook = str(data.get("bitrix_webhook", "")).strip()
    bitrix_chat_id = str(data.get("bitrix_chat_id", "")).strip()

    updates: dict[str, str] = {}

    if groq_api_key:
        os.environ["GROQ_API_KEY"] = groq_api_key
        updates["GROQ_API_KEY"] = groq_api_key
        controller.client = None

    if bitrix_webhook:
        os.environ["BITRIX_WEBHOOK"] = bitrix_webhook
        updates["BITRIX_WEBHOOK"] = bitrix_webhook

    if bitrix_chat_id:
        os.environ["BITRIX_CHAT_ID"] = bitrix_chat_id
        updates["BITRIX_CHAT_ID"] = bitrix_chat_id

    if updates:
        save_local_env(updates)

    return jsonify({"ok": True, "status": controller.status(), "saved_keys": list(updates.keys())})


@app.get("/api/prompt")
def api_prompt_get():
    return jsonify({"ok": True, "prompt": monitor.SPAM_PROMPT})


@app.post("/api/prompt")
def api_prompt_set():
    data = request.get_json(silent=True) or {}
    prompt_text = str(data.get("prompt", "")).strip()
    if not prompt_text:
        return jsonify({"ok": False, "error": "Промпт не может быть пустым."}), 400
    if "{text}" not in prompt_text:
        return jsonify({"ok": False, "error": "Промпт должен содержать плейсхолдер {text}."}), 400

    save_prompt_override(prompt_text)
    return jsonify({"ok": True, "saved": True})


@app.post("/api/start")
def api_start():
    try:
        controller.start()
        return jsonify({"ok": True, "status": controller.status()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/stop")
def api_stop():
    controller.stop()
    return jsonify({"ok": True, "status": controller.status()})


@app.post("/api/run-once")
def api_run_once():
    if controller.is_running:
        return jsonify({"ok": False, "error": "Сначала остановите фоновый режим."}), 409
    try:
        result = controller.run_once()
        return jsonify({"ok": True, "result": result, "status": controller.status()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/api/spam-log")
def api_spam_log():
    limit = request.args.get("limit", default=200, type=int)
    limit = min(max(limit, 20), 2000)

    log_path = BASE_DIR / monitor.SPAM_LOG_FILE
    if not log_path.exists():
        return jsonify({"ok": True, "text": ""})

    with log_path.open("r", encoding="utf-8") as f:
        lines = f.readlines()
    return jsonify({"ok": True, "text": "".join(lines[-limit:])})


@app.get("/api/comments-view")
def api_comments_view():
    return jsonify({"ok": True, "data": controller.comments_view()})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False)

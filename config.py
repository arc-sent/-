"""Загрузка конфигурации из переменных окружения (.env)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _parse_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
API_URL: str = os.getenv("API_URL", "https://api.prmotion.me/v1").strip().rstrip("/")
ADMIN_IDS: set[int] = _parse_ids(os.getenv("ADMIN_IDS", ""))
DEFAULT_CURRENCY: str = os.getenv("DEFAULT_CURRENCY", "RUB").strip().upper() or "RUB"
DB_PATH: str = os.getenv("DB_PATH", "bot.db").strip() or "bot.db"

# --- TLS / проверка сертификата API ---
# VERIFY_SSL=false отключает проверку сертификата (крайний случай — например, если
# перехватывающий прокси/антивирус мешает даже с системным хранилищем).
VERIFY_SSL: bool = os.getenv("VERIFY_SSL", "true").strip().lower() not in (
    "0", "false", "no", "off",
)
# Необязательный путь к своему CA-бандлу (.pem), если нужен конкретный корень.
CA_BUNDLE: str = os.getenv("CA_BUNDLE", "").strip()

# Какие сервисы показывать и под каким названием — задаётся в catalog.py.

# За сколько единиц указана цена (rate) в API.
# Цена за 1 шт = rate / RATE_PER_UNITS (например, rate 0.09 -> 0.009 ₽/шт).
RATE_PER_UNITS: int = 10

# Капельная доставка (drip-feed).
DRIP_DAYS_CHOICES: tuple[int, ...] = (3, 4)  # варианты «на сколько дней растянуть»
# Свои варианты дней для конкретных групп (см. catalog.py / grouping.py).
DRIP_DAYS_BY_GROUP: dict[str, tuple[int, ...]] = {
    "subs": (1,),  # подписчиков растягиваем на сутки
}
DRIP_RUNS_PER_DAY: int = 6                    # сколько докруток в сутки (равномерно)
DRIP_TICK_SECONDS: int = 45                   # период проверки планировщика
DRIP_MAX_CONCURRENT: int = 10                 # максимум одновременно выполняющихся докруток


def drip_days_for(group: str | None) -> tuple[int, ...]:
    """Варианты «на сколько дней растянуть» для группы сервиса."""
    return DRIP_DAYS_BY_GROUP.get(group or "", DRIP_DAYS_CHOICES)

# Активные часы: докрутки идут только в это окно (по местному времени), ночью пауза.
# Формат (час_начала, час_конца), 24-часовой. None -> круглосуточно.
DRIP_ACTIVE_HOURS: tuple[int, int] | None = (9, 23)
DRIP_TZ_OFFSET: int = 3  # часовой пояс для активных часов (UTC+3 = Москва)

# «Живой» разброс: на столько минут случайно смещаются границы окна каждый день
# и моменты докруток (например, старт 8:12, конец 23:41 — по-разному в разные дни).
DRIP_JITTER_MINUTES: int = 55

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Заполни .env (см. .env.example).")

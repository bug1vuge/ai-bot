#!/usr/bin/env bash
# Установка бота на VPS (Ubuntu 22.04 / 24.04, Debian 12).
# Запуск из папки проекта:   sudo bash deploy/install.sh            — адрес вида 1-2-3-4.sslip.io
#                            sudo bash deploy/install.sh pay.mysite.ru — свой домен
# Повторный запуск безопасен: обновит код, сохранит .env и базу покупок.
# Если на сервере уже есть сайт на nginx — скрипт его не трогает: добавляет отдельный
# файл /etc/nginx/conf.d/guide-bot.conf только для адреса бота и получает для него сертификат.
# Свой домен в этом случае указывайте только НОВЫЙ (поддомен), не домен существующего сайта.
set -euo pipefail

APP_DIR=/opt/guide-bot
APP_USER=guidebot
SERVICE=guide-bot
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOMAIN="${1:-}"

say()  { echo -e "\n\033[1;35m==> $*\033[0m"; }
warn() { echo -e "\033[1;33m[!] $*\033[0m"; }
die()  { echo -e "\033[1;31m[x] $*\033[0m"; exit 1; }

[[ $EUID -eq 0 ]] || die "Запустите с правами администратора: sudo bash deploy/install.sh"
command -v apt-get >/dev/null || die "Скрипт рассчитан на Ubuntu/Debian (нужен apt-get)."

# все ли файлы на месте (частая причина ошибок — неполная загрузка или вложенная папка guide-bot/guide-bot)
for f in requirements.txt bot/main.py data/offer.md .env.example; do
  if [[ ! -e "$SRC_DIR/$f" ]]; then
    hint=""
    [[ -d "$SRC_DIR/guide-bot" ]] && hint="
    Внутри есть вложенная папка guide-bot — объедините её: cd $SRC_DIR && cp -an guide-bot/. ./ && rm -rf guide-bot"
    die "В папке $SRC_DIR нет «$f». Загрузите проект целиком (git clone или полный архив).$hint"
  fi
done
# сам гайд в git не хранится: его кладут в data/ отдельно (или он уже есть с прошлой установки)
if [[ ! -f "$SRC_DIR/data/guide.pdf" && ! -f "$APP_DIR/data/guide.pdf" ]]; then
  die "Нет файла гайда data/guide.pdf. Загрузите PDF в $SRC_DIR/data/guide.pdf (через FileZilla или scp) и запустите скрипт снова."
fi

# ---------------------------------------------------------------- пакеты
say "Устанавливаю системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv curl ca-certificates fonts-dejavu-core

# ---------------------------------------------------------------- кто занимает порты 80/443
WEB_MODE=caddy
for port in 80 443; do
  holder=$(ss -ltnp "sport = :$port" 2>/dev/null | awk 'NR>1 {print $NF}' | grep -v caddy || true)
  if [[ -n "$holder" ]]; then
    if grep -q nginx <<<"$holder"; then
      WEB_MODE=nginx
    else
      die "Порт $port занят программой: $holder
    Скрипт умеет работать рядом с nginx, но не с этой программой. Пришлите этот текст — подскажу, как быть."
    fi
  fi
done

if [[ "$WEB_MODE" == nginx ]]; then
  say "На сервере уже работает nginx — подключаю бота к нему, существующие сайты не трогаю"
  apt-get install -y certbot python3-certbot-nginx
elif ! command -v caddy >/dev/null; then
  say "Устанавливаю Caddy (веб-сервер с автоматическим HTTPS)"
  if ! apt-get install -y caddy; then
    apt-get install -y debian-keyring debian-archive-keyring apt-transport-https gnupg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' -o /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -y
    apt-get install -y caddy
  fi
fi

# ---------------------------------------------------------------- адрес
if [[ -z "$DOMAIN" ]]; then
  IP=$(curl -4 -fs --max-time 10 https://api.ipify.org || curl -4 -fs --max-time 10 https://ifconfig.me || true)
  [[ -n "$IP" ]] || die "Не удалось определить внешний IP сервера. Запустите так: sudo bash deploy/install.sh ваш.домен"
  DOMAIN="${IP//./-}.sslip.io"
fi
PUBLIC_URL="https://$DOMAIN"
say "Адрес бота для Робокассы: $PUBLIC_URL"

# ---------------------------------------------------------------- файлы
say "Копирую файлы в $APP_DIR"
id "$APP_USER" &>/dev/null || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR/data"
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
  # код обновляем, а .env, базу и venv не трогаем
  tar -C "$SRC_DIR" --exclude='./.git' --exclude='./.env' --exclude='./data/bot.db*' --exclude='./venv' \
      --exclude='*/__pycache__' --exclude='./wheels/*win_amd64*' -cf - . | tar -C "$APP_DIR" -xf -
  if [[ -f "$SRC_DIR/.env" && ! -f "$APP_DIR/.env" ]]; then cp "$SRC_DIR/.env" "$APP_DIR/.env"; fi
fi
[[ -f "$APP_DIR/.env" ]] || cp "$APP_DIR/.env.example" "$APP_DIR/.env"

ENVTOOL="python3 $APP_DIR/deploy/set_env.py $APP_DIR/.env"

# порт бота: 8080 или следующий свободный (на сервере может работать другой проект)
systemctl stop "$SERVICE" 2>/dev/null || true
port_busy() { ss -ltn "sport = :$1" 2>/dev/null | awk 'NR>1' | grep -q .; }
BOT_PORT=$($ENVTOOL WEB_PORT)
[[ "$BOT_PORT" =~ ^[0-9]+$ ]] || BOT_PORT=8080
while port_busy "$BOT_PORT"; do BOT_PORT=$((BOT_PORT + 1)); done
echo "Внутренний порт бота: $BOT_PORT"

$ENVTOOL WEB_ENABLED true
$ENVTOOL WEB_HOST 127.0.0.1
$ENVTOOL WEB_PORT "$BOT_PORT"
$ENVTOOL PUBLIC_URL "$PUBLIC_URL"

# ---------------------------------------------------------------- заполнение .env
ask() {  # ask КЛЮЧ "Вопрос" [секрет]
  local key="$1" prompt="$2" secret="${3:-}" cur val
  cur=$($ENVTOOL "$key")
  if [[ -n "$cur" ]]; then return; fi
  if [[ -n "$secret" ]]; then read -r -s -p "$prompt: " val; echo; else read -r -p "$prompt: " val; fi
  [[ -n "$val" ]] && $ENVTOOL "$key" "$val"
  return 0
}
if [[ -t 0 ]]; then
  say "Заполняем настройки (Enter — пропустить; потом можно поправить в $APP_DIR/.env)"
  ask BOT_TOKEN "Токен бота от @BotFather" secret
  ask ADMIN_IDS "Ваш Telegram ID (из @userinfobot)"
  ask SUPPORT_USERNAME "Ник для поддержки без @"
  ask SELLER_NAME "Ваши ФИО полностью (для оферты)"
  ask SELLER_INN "Ваш ИНН"
  ask SELLER_EMAIL "E-mail для связи с покупателями"
  ask ROBOKASSA_LOGIN "Идентификатор магазина в Робокассе"
  ask ROBOKASSA_PASSWORD1 "Робокасса: пароль #1 (тестовый, пока магазин не активирован)" secret
  ask ROBOKASSA_PASSWORD2 "Робокасса: пароль #2 (тестовый, пока магазин не активирован)" secret
fi
[[ -n "$($ENVTOOL BOT_TOKEN)" ]] || die "В $APP_DIR/.env не указан BOT_TOKEN. Впишите его: nano $APP_DIR/.env — и запустите скрипт снова."

# ---------------------------------------------------------------- Python
say "Готовлю окружение Python"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "Нужен Python 3.10 или новее."
[[ -x "$APP_DIR/venv/bin/python" ]] || python3 -m venv "$APP_DIR/venv"
PIP=("$APP_DIR/venv/bin/pip" install --disable-pip-version-check -q -r "$APP_DIR/requirements.txt")
# сначала из папки wheels (есть в полном архиве, работает без интернета), затем из PyPI
if ! { [[ -d "$APP_DIR/wheels" ]] && "${PIP[@]}" --no-index --find-links "$APP_DIR/wheels"; }; then
  "${PIP[@]}" || die "Не удалось установить библиотеки Python. Проверьте доступ сервера к pypi.org."
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

# ---------------------------------------------------------------- доступ к Telegram
say "Проверяю доступ сервера к Telegram"
if [[ -z "$($ENVTOOL TELEGRAM_PROXY)" ]] && ! curl -s -o /dev/null --max-time 10 https://api.telegram.org; then
  warn "Сервер не может напрямую подключиться к api.telegram.org (так бывает на российских VPS)."
  warn "Укажите прокси в $APP_DIR/.env (строка TELEGRAM_PROXY=socks5://логин:пароль@адрес:порт)"
  warn "и перезапустите бота: systemctl restart $SERVICE"
fi

# ---------------------------------------------------------------- systemd
say "Настраиваю автозапуск бота"
cat > /etc/systemd/system/$SERVICE.service <<UNIT
[Unit]
Description=Telegram bot «ИИ без тёмного леса»
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python -m bot.main
Restart=always
RestartSec=5
# время закончить начатые выдачи при остановке
TimeoutStopSec=30
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
# Ограничения: бот может писать только в папку data (база), остальная система только для чтения
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR/data
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

# ---------------------------------------------------------------- HTTPS для адресов Робокассы
if [[ "$WEB_MODE" == caddy ]]; then
  say "Настраиваю HTTPS для адресов Робокассы (Caddy)"
  if [[ -f /etc/caddy/Caddyfile ]] && ! grep -q "guide-bot" /etc/caddy/Caddyfile; then
    cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.backup.$(date +%s)"
  fi
  cat > /etc/caddy/Caddyfile <<CADDY
# guide-bot: адреса для Робокассы
$DOMAIN {
	handle /robokassa/* {
		reverse_proxy 127.0.0.1:$BOT_PORT
	}
	handle /health {
		reverse_proxy 127.0.0.1:$BOT_PORT
	}
	handle /offer {
		reverse_proxy 127.0.0.1:$BOT_PORT
	}
	handle {
		respond "OK" 200
	}
}
CADDY
  systemctl enable caddy >/dev/null
  systemctl restart caddy
else
  say "Настраиваю HTTPS для адресов Робокассы (nginx + certbot)"
  NGX=/etc/nginx/conf.d/guide-bot.conf
  if [[ -f "$NGX" ]] && ! grep -q "server_name $DOMAIN;" "$NGX"; then
    # адрес бота сменился (например, с sslip.io на свой домен) — настраиваем заново
    echo "Адрес бота изменился — заменяю настройки nginx для бота."
    mv "$NGX" "/root/guide-bot-nginx.backup.$(date +%s).conf"
  fi
  if [[ ! -f "$NGX" ]] && grep -rqs "server_name[^;]*\b$DOMAIN\b" /etc/nginx/; then
    die "Домен $DOMAIN уже используется другим сайтом в nginx. Запустите скрипт без домена
    (будет адрес вида 1-2-3-4.sslip.io) или укажите новый поддомен."
  fi
  if [[ -f "$NGX" ]] && grep -q "ssl_certificate" "$NGX"; then
    # повторный запуск: сертификат уже выпущен, обновляем только порт
    sed -i -E "s#proxy_pass http://127\.0\.0\.1:[0-9]+;#proxy_pass http://127.0.0.1:$BOT_PORT;#g" "$NGX"
    if ! grep -q "location = /offer" "$NGX"; then
      sed -i "s#^    location = /health {#    location = /offer {\n        proxy_pass http://127.0.0.1:$BOT_PORT;\n    }\n    location = /health {#" "$NGX"
    fi
  else
    cat > "$NGX" <<NGINX
# guide-bot: адреса для Робокассы (отдельный сайт, другие сайты не затрагивает)
server {
    listen 80;
    server_name $DOMAIN;

    location /robokassa/ {
        proxy_pass http://127.0.0.1:$BOT_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
    location = /health {
        proxy_pass http://127.0.0.1:$BOT_PORT;
    }
    location = /offer {
        proxy_pass http://127.0.0.1:$BOT_PORT;
    }
    location / {
        return 200 "OK";
    }
}
NGINX
  fi
  if ! nginx -t; then
    rm -f "$NGX"
    die "nginx не принял настройки бота — файл удалён, существующие сайты работают как раньше. Пришлите текст ошибки выше."
  fi
  systemctl reload nginx
  if ! grep -q "ssl_certificate" "$NGX"; then
    if certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect; then
      echo "Сертификат для $DOMAIN получен."
    else
      warn "Не удалось получить HTTPS-сертификат. Проверьте, что порт 80 открыт у хостера, и запустите скрипт ещё раз."
    fi
  fi
fi

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi

# ---------------------------------------------------------------- проверка
say "Проверяю запуск"
sleep 6
if systemctl is-active --quiet "$SERVICE"; then
  echo "Бот работает."
else
  warn "Бот не запустился. Последние строки журнала:"
  journalctl -u "$SERVICE" -n 25 --no-pager || true
fi
for _ in 1 2 3 4 5 6; do
  if curl -fs --max-time 10 "$PUBLIC_URL/health" >/dev/null; then HTTPS_OK=1; break; fi
  sleep 5
done
[[ "${HTTPS_OK:-}" == 1 ]] && echo "HTTPS работает: $PUBLIC_URL/health" \
  || warn "HTTPS пока не отвечает. Сертификат может выпускаться ещё минуту-две. Проверьте: curl $PUBLIC_URL/health"

cat <<DONE

==================================================================
 Готово! Впишите в Робокассе (Технические настройки магазина):

   Result URL:   $PUBLIC_URL/robokassa/result    метод POST
   Success URL:  $PUBLIC_URL/robokassa/success   метод GET
   Fail URL:     $PUBLIC_URL/robokassa/fail      метод GET
   Алгоритм расчёта хеша: MD5

 Оферта (проверьте ФИО, ИНН и контакты):  $PUBLIC_URL/offer

 Полезные команды:
   Журнал бота:        journalctl -u $SERVICE -f
   Перезапуск:         systemctl restart $SERVICE
   Настройки:          nano $APP_DIR/.env   (после правки — перезапуск)
==================================================================
DONE

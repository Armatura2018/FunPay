#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${PROJECT_DIR}/FunPayAPI"

if [ -d "${TARGET}/.git" ]; then
  echo "Обновляю FunPayAPI..."
  git -C "${TARGET}" pull --ff-only
elif [ -d "${TARGET}" ]; then
  echo "Папка ${TARGET} уже существует, но это не Git-клон."
  echo "Удалите/переименуйте её и запустите скрипт повторно."
  exit 1
else
  echo "Загружаю FunPayAPI..."
  git clone --depth 1 https://github.com/MorikTV/FunPayAPI.git "${TARGET}"
fi

echo "Готово. Пакет доступен для импорта из ${TARGET}."

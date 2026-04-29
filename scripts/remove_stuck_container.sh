#!/bin/bash

# Скрипт для удаления зависших Docker контейнеров
# Использование: ./remove_stuck_container.sh [container_id|container_name]
# Если ID/имя не указано, ищет posting_bot

CONTAINER_ID=$1

if [ -z "$CONTAINER_ID" ]; then
    echo "🔍 ID контейнера не указан, ищем posting_bot..."
    CONTAINER_ID=$(sudo docker ps -a --filter name=posting_bot --format "{{.ID}}" | head -1)
    if [ -z "$CONTAINER_ID" ]; then
        echo "❌ Контейнер posting_bot не найден"
        echo ""
        echo "Использование: $0 [container_id|container_name]"
        echo "Пример: $0 786b87116b67"
        exit 1
    fi
    echo "📦 Найден контейнер: $CONTAINER_ID"
fi

echo "=========================================="
echo "🔧 Удаление зависшего контейнера: $CONTAINER_ID"
echo "=========================================="

echo ""
echo "📊 Информация о контейнере:"
sudo docker ps -a --filter id=$CONTAINER_ID --format "ID: {{.ID}}\nName: {{.Names}}\nImage: {{.Image}}\nStatus: {{.Status}}" 2>/dev/null || echo "Контейнер не найден"
echo ""

echo "🛑 Шаг 1: Отключаем автоперезапуск..."
sudo docker update --restart=no $CONTAINER_ID 2>/dev/null || true

echo "⏹️  Шаг 2: Пытаемся docker stop (timeout 10s)..."
sudo docker stop --timeout 10 $CONTAINER_ID 2>/dev/null || true
sleep 2

CONTAINER_STATUS=$(sudo docker inspect $CONTAINER_ID --format '{{.State.Status}}' 2>/dev/null || echo "removed")

if [ "$CONTAINER_STATUS" = "removed" ] || [ "$CONTAINER_STATUS" = "exited" ]; then
    echo "✅ Контейнер остановлен через docker stop"
else
    echo "🔪 Шаг 3: docker stop не помог, используем docker kill..."
    sudo docker kill $CONTAINER_ID 2>/dev/null || true
    sleep 2
fi

echo "🗑️  Шаг 4: Удаляем контейнер..."
sudo docker rm -f $CONTAINER_ID 2>/dev/null || true
sleep 1

if sudo docker ps -a --format "{{.ID}}" | grep -q "^${CONTAINER_ID:0:12}"; then
    echo ""
    echo "⚠️  Контейнер всё ещё существует. Применяем экстренные меры..."

    echo "🔍 Шаг 5: Получаем PID контейнера..."
    CONTAINER_PID=$(sudo docker inspect $CONTAINER_ID --format '{{.State.Pid}}' 2>/dev/null || echo "0")

    if [ "$CONTAINER_PID" != "0" ] && [ -n "$CONTAINER_PID" ]; then
        echo "📌 PID контейнера: $CONTAINER_PID"
        echo "💀 Убиваем процесс $CONTAINER_PID через kill -9..."
        sudo kill -9 $CONTAINER_PID 2>/dev/null || true
        sleep 3

        echo "🗑️  Повторная попытка удаления..."
        sudo docker rm -f $CONTAINER_ID 2>/dev/null || true
        sleep 1
    fi

    if sudo docker ps -a --format "{{.ID}}" | grep -q "^${CONTAINER_ID:0:12}"; then
        echo ""
        echo "❌ КРИТИЧЕСКАЯ ОШИБКА: Контейнер-зомби не удаётся удалить!"
        echo ""
        echo "Попробуйте:"
        echo "  1. Перезапустить Docker daemon:  sudo systemctl restart docker"
        echo "  2. Перезагрузить сервер:         sudo reboot"
        echo ""
        exit 1
    fi
fi

echo ""
echo "✅ Контейнер успешно удалён!"
echo ""

echo "🧹 Очищаем неиспользуемые Docker ресурсы..."
sudo docker system prune -f 2>/dev/null || true

echo ""
echo "✅ Готово! Запускайте: docker compose up -d"

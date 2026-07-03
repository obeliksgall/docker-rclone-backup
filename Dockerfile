FROM python:3.11-slim

# Instalacja zależności systemowych, ca-certificates (wymagane do SSL/chmur) oraz curl
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Pobranie i instalacja oficjalnego binarium rclone
RUN curl https://rclone.org/install.sh | bash

# Ustawienie katalogu roboczego
WORKDIR /app

# Instalacja zależności Pythona
RUN pip install --no-cache-dir apscheduler requests

# Skopiowanie kodu źródłowego
COPY src/ /app/src/

# Domyślna ścieżka, w której rclone szuka pliku konfiguracyjnego wewnątrz kontenera
ENV RCLONE_CONFIG=/app/config/rclone.conf

# Uruchomienie aplikacji
CMD ["python", "-u", "src/main.py"]
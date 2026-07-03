# Docker Rclone Scheduler & Cloud Backup Service

Uniwersalne, lekkie i w pełni konteneryzowane narzędzie do automatycznego tworzenia kopii zapasowych (synchronizacja mirror, kopia przyrostowa oraz przenoszenie danych) bezpośrednio do chmur publicznych (Google Drive, OneDrive, Dropbox, S3, itp.) przy użyciu narzędzia `rclone`. Posiada wbudowany mechanizm wersji archiwalnych (Retention Policy/Kosz), aby chronić dane przed przypadkowym usunięciem, oraz dedykowany moduł awaryjnego odzyskiwania danych (Restore).

## 🚀 Główne funkcje

* **Obsługa ponad 40 dostawców chmur:** Działa z Google Drive, OneDrive, Nextcloud, AWS S3, Backblaze B2 i wieloma innymi (w tym wsparcie dla natywnego szyfrowania `rclone crypt`).
* **Trzy tryby pracy (`type`):**
  * `sync` – Synchronizacja 1:1 (usuwa z chmury pliki skasowane w źródle lokalnym).
  * `copy` – Kopia przyrostowa (dodaje nowe i zmienione pliki, nic nie kasuje z chmury).
  * `move` – Przenoszenie danych (usuwa pliki ze źródła po udanym transferze do chmury).
* **Inteligentna Retencja (Kosz z "Maszyną Czasu"):** Dla trybu `sync`, zamiast bezpowrotnie usuwać pliki z chmury, aplikacja przenosi je do katalogu archiwalnego z datą (np. `2026-07-03`) i automatycznie usuwa foldery chmurowe starsze niż zadeklarowana liczba dni.
* **Kompleksowe przywracanie danych (Restore Module):** Dedykowany, drugi kontener działający w trybie *One-Shot* (uruchamiany na żądanie z profilem Docker), który automatycznie odwraca kierunek mapowania chmury i przywraca pliki na Twój dysk lokalny (w locie obsługując deszyfrowanie, jeśli używasz `crypt`).
* **Zabezpieczenie przed limitami API (Anti-Throttling):** Zintegrowana ochrona przed blokadami Google API (`Quota Exceeded / Error 403`) w procesach sprawdzania połączenia oraz transferu.
* **Inteligentny Scheduler:** Dynamiczne przeładowywanie zadań po edycji pliku konfiguracji JSON, bez konieczności restartowania całego kontenera.
* **Kolejkowanie zadań:** Możliwość wymuszenia wykonywania zadań sekwencyjnie (jedno po drugim), aby nie przeciążać sieci i procesora.
* **Krytyczny Sanity Check:** Przed uruchomieniem serwisu aplikacja wykonuje twardą weryfikację obecności plików `config.json` oraz `rclone.conf`. Jeśli plików brakuje lub JSON zawiera błędy składniowe, kontener bezpiecznie zatrzymuje pracę i informuje o tym w logach.
* **Dwuetapowy Healthcheck zadań:** Przed uruchomieniem każdego transferu aplikacja weryfikuje obecność lokalnego folderu oraz sprawdza stabilność połączenia z chmurą za pomocą zabezpieczonej komendy `rclone lsd`.
* **Graceful Shutdown:** Bezpieczne przerywanie aktywnych procesów `rclone` i informowanie o zamknięciu kontenera (obsługa sygnałów `SIGTERM`/`SIGINT`).
* **Powiadomienia Discord:** Raporty o starcie procesów, sukcesach (zarówno dla Backupów, jak i Restore) oraz błędach prosto na Twój kanał.

---

## 📂 Struktura katalogów

```text
.
├── config/
│   ├── config.json         # Główny plik konfiguracyjny aplikacji (Backup + Restore)
│   └── rclone.conf         # Wygenerowany plik konfiguracyjny rclone
├── logs/
│   ├── app.log             # Główne logi systemowe schedulera (start, zmiany konfiguracji)
│   ├── restore.log         # Ogólne logi z procesów przywracania danych
│   ├── task_*.log          # Stałe logi zbiorcze dla konkretnych zadań
│   └── [Nazwa_Zadania]/    # Podkatalogi z unikalnymi, szczegółowymi logami rclone (-vv)
│       └── 20260703_212448.log
├── src/
│   ├── main.py             # Silnik schedulera i backupu (Python)
│   └── restore.py          # Skrypt procesu przywracania danych (Python)
├── Dockerfile              # Definicja kontenera kopii zapasowych
├── Dockerfile.restore      # Definicja kontenera odzyskiwania danych
└── docker-compose.yml      # Definicja wielokontenerowego środowiska

```

---

## ⚙️ Konfiguracja `config/config.json`

Aplikacja sterowana jest jednym plikiem JSON. Pamiętaj, aby ścieżki `source` wskazywały na foldery **wewnątrz kontenera** (zdefiniowane po prawej stronie sekcji `volumes` w `docker-compose.yml`).

```json
{
  "general": {
    "discord_webhook_url": "[https://discord.com/api/webhooks/](https://discord.com/api/webhooks/)...",
    "notification_level": "all",
    "config_check_interval_seconds": 10,
    "max_concurrent_tasks": 1,
    "default_scheduler": "0 2 * * *",
    "default_retention_days": 7,
    "retention_suffix": "_retention",
    "max_log_age_days": 14,
    "restore_global_enabled": false
  },
  "tasks": [
    {
      "name": "Zdjecia do Google Drive (Szyfrowane)",
      "enabled": true,
      "restore_enabled": true,
      "type": "sync", 
      "source": "/source/photos",
      "dest_remote": "gdrive:",
      "dest_path": "Backup/Photos",
      "scheduler": "0 1 * * *",
      "retention": true,
      "exclude": [".DS_Store", "Thumbs.db"],
      "extra_rclone_flags": "--tpslimit 10 --transfers 2 --checkers 4 --fast-list"
    }
  ]
}

```

### 📋 Opis parametrów w sekcji `general`:
* `notification_level` – Definiuje poziom szczegółowości powiadomień wysyłanych na Discorda. Dostępne opcje to:
  * `"all"` – Pełne raportowanie: powiadomienia o starcie zadania, sukcesie (zakończeniu) oraz ewentualnych błędach.
  * `"errors_only"` – Tryb cichy: powiadomienia są wysyłane tylko wtedy, gdy zadanie zakończy się niepowodzeniem lub nie przejdzie testu Healthcheck.
  * `"none"` (lub brak parametru) – Całkowite wyłączenie powiadomień na Discordzie (logi będą zapisywane wyłącznie lokalnie).
* `max_log_age_days` – Określa, przez ile dni mają być przechowywane historyczne, szczegółowe logi `rclone` w podfolderach. Starsze pliki są automatycznie usuwane przy każdym cyklu, chroniąc dysk przed zapełnieniem.
* `restore_global_enabled` – Główny bezpiecznik modułu odzyskiwania danych. Musi mieć wartość `true`, aby kontener przywracania podjął jakąkolwiek pracę.

### 📋 Opis nowych parametrów w sekcji `tasks`:

* `restore_enabled` – Pozwala włączyć lub wyłączyć przywracanie dla tego konkretnego zadania w scenariuszu awaryjnym.

---

## 🎛️ Optymalizacja transferów (`extra_rclone_flags`)

W zależności od chmury oraz rodzaju plików, odpowiednie flagi w polu `extra_rclone_flags` drastycznie przyspieszają transfer i zapobiegają banom API:

### 🔒 Ochrona przed limitami Google Drive (Quota Exceeded)

Google Drive nakłada restrykcyjne limity na liczbę zapytań na minutę. Jeśli doświadczasz błędów 403 lub timeoutów, zastosuj poniższy zestaw flag:

* `--tpslimit 10` – Ogranicza liczbę transakcji do maksymalnie 10 na sekundę. Zapobiega to gwałtownym skokom zapytań.
* `--transfers 2` – Zmniejsza liczbę jednocześnie przesyłanych plików, redukując narzut na API.
* `--checkers 4` – Ogranicza liczbę wątków porównujących pliki przed startem.
* `--fast-list` – Pobiera strukturę katalogów z chmury w dużych paczkach. Drastycznie zmniejsza liczbę wymaganych zapytań API.

### 📸 Dużo zdjęć (pliki od 1 MB do 20 MB)

* `--fast-list` – Obowiązkowa flaga. Przyspiesza start backupu i oszczędza API.
* `--transfers=4` – Pozwala na równoległe przesyłanie kilku zdjęć jednocześnie, maksymalizując pasmo.

### 🎬 Bardzo duże pliki (wideo, obrazy ISO powyżej kilku GB)

* `--drive-chunk-size=64M` (lub `128M`) – **Dedykowane dla Google Drive.** Zwiększa rozmiar pojedynczej części wysyłanego pliku w pamięci RAM. Drastycznie przyspiesza upload (wymaga więcej pamięci w kontenerze).
* `--transfers=1` lub `--transfers=2` – Zapobiega dzieleniu pasma i powstawaniu timeoutów przy potężnych plikach.

### 📄 Dokumenty (setki tysięcy małych plików poniżej 1 MB)

* `--fast-list` – Kluczowa flaga dla sprawnego listowania.
* `--checkers=16` – Zwiększa liczbę wątków porównujących pliki lokalne z chmurą, skracając czas przygotowania transferu.
* `--transfers=8` – Pozwala wysyłać wiele małych plików jednocześnie.

### 🌐 Ograniczenie prędkości (Dla każdego typu)

* `--bwlimit 5M` – Ogranicza prędkość wysyłania do 5 MB/s, zapobiegając paraliżowi sieci domowej/firmowej w trakcie dnia.

---

## 🐳 Uruchomienie i obsługa (Docker Compose)

### 1. Przygotowanie pliku `rclone.conf`

Wygeneruj plik konfiguracji na komputerze lokalnym z przeglądarką (`rclone config`). Gotowy plik skopiuj do katalogu `./config/rclone.conf` na serwerze.

### 2. Standardowe uruchomienie (Tryb Backup / Scheduler)

Uruchom główny kontener, który będzie pracował w tle jako usługa systemowa:

```bash
docker compose up -d --build

```

*Uwaga: W logach kontenera po każdym udanym przebiegu wyświetlane jest gotowe przypomnienie o procedurze awaryjnego odzyskiwania.*

### 3. Procedura awaryjna (Tryb Restore / Odzyskiwanie danych)

Kiedy zajdzie potrzeba przywrócenia danych z chmury na dysk lokalny:

1. Otwórz plik `config/config.json`.
2. Ustaw `"restore_global_enabled": true`.
3. Upewnij się, że wybrane zadanie ma ustawione `"restore_enabled": true`.
4. Wywołaj dedykowany profil odzyskiwania danych za pomocą komendy:
```bash
sudo docker compose --profile restore run --rm rclone-restore

```


*Flaga `--rm` automatycznie usunie kontener z pamięci po zakończeniu pobierania plików, nie pozostawiając śmieci w systemie.*
5. Po zakończeniu przywracania danych ustaw parametr `restore_global_enabled` z powrotem na `false` w celu zablokowania możliwości przypadkowego nadpisania plików.

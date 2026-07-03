import os
import sys
import json
import time
import signal
import subprocess
import requests
import re
import shutil
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor

# Globalne flagi sterujące stanem aplikacji
running_processes = {}
exiting = False
scheduler = None

def log(message, level="INFO", task_name="SYSTEM"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] [{level}] [{task_name}] {message}"
    print(log_line, flush=True)
    
    log_dir = "/app/logs"
    os.makedirs(log_dir, exist_ok=True)
    
    # Główny log systemowy
    with open(os.path.join(log_dir, "app.log"), "a", encoding="utf-8") as f:
        f.write(log_line + "\n")
        
    # Stały, ogólny log per zadanie (jeśli to nie jest komunikat SYSTEM)
    if task_name != "SYSTEM":
        filename = f"task_{task_name.replace(' ', '_')}.log"
        with open(os.path.join(log_dir, filename), "a", encoding="utf-8") as f:
            f.write(log_line + "\n")

def send_discord(webhook_url, content, task_name, level="INFO"):
    if not webhook_url or "discord.com" not in webhook_url:
        return
    colors = {"INFO": 5814783, "SUCCESS": 3066993, "ERROR": 15158332, "WARNING": 16743168}
    embed = {
        "title": f"Task: {task_name}",
        "description": content,
        "color": colors.get(level, 5814783),
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    try:
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    except Exception as e:
        log(f"Failed to send Discord notification: {e}", "ERROR")

def clean_old_local_logs(general_config):
    """Usuwa stare pliki logów z podkatalogów zadań na podstawie max_log_age_days"""
    try:
        max_days = general_config.get("max_log_age_days", 14)
        log_dir = "/app/logs"
        if not os.path.exists(log_dir):
            return
            
        cutoff_date = datetime.now() - timedelta(days=int(max_days))
        
        for item in os.listdir(log_dir):
            item_path = os.path.join(log_dir, item)
            
            # Przeszukujemy tylko podkatalogi zadań, pomijając pliki .log w katalogu głównym
            if os.path.isdir(item_path):
                for filename in os.listdir(item_path):
                    if filename.endswith(".log"):
                        file_path = os.path.join(item_path, filename)
                        file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                        
                        if file_mtime < cutoff_date:
                            os.remove(file_path)
                            log(f"Removed old local history log file: {item}/{filename}", "INFO")
                
                # Jeśli po oczyszczeniu podkatalog jest pusty, usuwamy go
                if not os.listdir(item_path):
                    os.rmdir(item_path)
    except Exception as e:
        log(f"Error during local logs cleaning: {e}", "ERROR")

def run_retention_pruning(task, general_config, archive_base_path, retention_days):
    """Funkcja czyszcząca stare katalogi w chmurowym archiwum/koszu"""
    task_name = task["name"]
    remote = task["dest_remote"]
    
    log(f"Starting retention pruning (keeping last {retention_days} days)...", "INFO", task_name)
    
    full_archive_path = f"{remote}{archive_base_path}"
    cmd = ["rclone", "lsf", full_archive_path, "--dirs-only", "--tpslimit", "10", "--retries", "1"]
    
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        if proc.returncode != 0:
            return
            
        directories = proc.stdout.splitlines()
        cutoff_date = datetime.now() - timedelta(days=int(retention_days))
        
        for dir_name in directories:
            dir_name_clean = dir_name.strip("/")
            if re.match(r"^\d{4}-\d{2}-\d{2}$", dir_name_clean):
                try:
                    dir_date = datetime.strptime(dir_name_clean, "%Y-%m-%d")
                    if dir_date < cutoff_date:
                        log(f"Retention match found! Purging old archive directory: {dir_name_clean}", "WARNING", task_name)
                        purge_cmd = ["rclone", "purge", f"{full_archive_path}/{dir_name_clean}", "--tpslimit", "10", "--retries", "1"]
                        subprocess.run(purge_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                except ValueError:
                    continue
    except Exception as e:
        log(f"Error during retention pruning: {e}", "ERROR", task_name)

def run_rclone_task(task, general_config):
    global exiting
    task_name = task["name"]
    
    if exiting:
        log("System is shutting down. Task skipped.", "WARNING", task_name)
        return

    # 1. Healthcheck lokalnego źródła
    if not os.path.exists(task["source"]):
        msg = f"Validation failed! Local source path '{task['source']}' does not exist."
        log(msg, "ERROR", task_name)
        if general_config.get("notification_level") in ["all", "errors_only"]:
            send_discord(general_config.get("discord_webhook_url"), msg, task_name, "ERROR")
        return

    # 2. Healthcheck połączenia z chmurą (Zabezpieczony przed Quota Throttling)
    remote = task["dest_remote"]
    try:
        # Dodajemy --tpslimit oraz --retries=1, żeby test nie prowokował Google i nie wisiał
        check_cmd = ["rclone", "lsd", remote, "--tpslimit", "10", "--retries", "1"]
        check_cloud = subprocess.run(check_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        
        if check_cloud.returncode != 0:
            msg = f"Validation failed! Cannot connect to remote cloud '{remote}'. Check your rclone.conf file."
            log(msg, "ERROR", task_name)
            if general_config.get("notification_level") in ["all", "errors_only"]:
                send_discord(general_config.get("discord_webhook_url"), msg, task_name, "ERROR")
            return
    except Exception as e:
        msg = f"Validation failed! Error during cloud check: {e}"
        log(msg, "ERROR", task_name)
        if general_config.get("notification_level") in ["all", "errors_only"]:
            send_discord(general_config.get("discord_webhook_url"), msg, task_name, "ERROR")
        return

    # 3. Blokowanie jednoczesnego wykonania (Locking)
    lock_file = f"/tmp/task_{task_name.replace(' ', '_')}.lock"
    if os.path.exists(lock_file):
        log("Task is already running. Skipping this execution.", "WARNING", task_name)
        return
        
    with open(lock_file, "w") as f:
        f.write(str(os.getpid()))

    log(f"Starting rclone {task['type']} job...", "INFO", task_name)
    if general_config.get("notification_level") == "all":
        send_discord(general_config.get("discord_webhook_url"), "Job started.", task_name, "INFO")

    # 4. Przygotowanie parametrów Retencji
    retention_config = task.get("retention", False)
    retention_enabled = False
    retention_days = general_config.get("default_retention_days", 7)
    retention_suffix = general_config.get("retention_suffix", "_retention")
    archive_base_path = None

    if isinstance(retention_config, bool) and retention_config:
        retention_enabled = True
        archive_base_path = f"{task['dest_path'].rstrip('/')}{retention_suffix}"
    elif isinstance(retention_config, dict) and retention_config.get("enabled", False):
        retention_enabled = True
        retention_days = retention_config.get("days", retention_days)
        archive_base_path = retention_config.get("archive_path") or f"{task['dest_path'].rstrip('/')}{retention_suffix}"

    # 5. Przygotowanie podkatalogu historycznego dla szczegółowych logów rclone
    safe_task_name = task_name.replace(' ', '_')
    task_log_dir = f"/app/logs/{safe_task_name}"
    os.makedirs(task_log_dir, exist_ok=True)
    
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file_path = f"{task_log_dir}/{now_str}.log"
    
    # Komenda z flagą -vv dla zapisu szczegółów do pliku historycznego
    rclone_cmd = ["rclone", task["type"], task["source"], f"{remote}{task['dest_path']}", "-vv"]
    
    if task["type"] == "sync" and retention_enabled and archive_base_path:
        today_str = datetime.now().strftime("%Y-%m-%d")
        full_backup_dir = f"{remote}{archive_base_path}/{today_str}"
        rclone_cmd.extend(["--backup-dir", full_backup_dir])

    for exc in task.get("exclude", []):
        rclone_cmd.extend(["--exclude", exc])

    if task.get("extra_rclone_flags"):
        rclone_cmd.extend(task["extra_rclone_flags"].split())

    # 6. Wywołanie procesu rclone z zapisem do pliku historycznego
    try:
        with open(log_file_path, "w", encoding="utf-8") as f_log:
            process = subprocess.Popen(rclone_cmd, stdout=f_log, stderr=f_log, text=True)
            running_processes[task_name] = process
            process.communicate()
    except Exception as e:
        with open(log_file_path, "a", encoding="utf-8") as f_log:
            f_log.write(f"\n[SCRIPT ERROR] Failed execution: {e}\n")
    finally:
        running_processes.pop(task_name, None)
        if os.path.exists(lock_file):
            os.remove(lock_file)

    # 7. Interpretacja rezultatu operacji
    if exiting:
        log("Task interrupted by system shutdown.", "WARNING", task_name)
        send_discord(general_config.get("discord_webhook_url"), "Job interrupted by container shutdown.", task_name, "WARNING")
        return

    if process.returncode != 0:
        log(f"Rclone failed with exit code {process.returncode}. Details saved in folder: logs/{safe_task_name}/", "ERROR", task_name)
        if general_config.get("notification_level") in ["all", "errors_only"]:
            send_discord(general_config.get("discord_webhook_url"), f"Job failed! Exit code: {process.returncode}. Check folder logs/{safe_task_name}/ for details.", task_name, "ERROR")
    else:
        success_msg = f"Job finished successfully. Detailed rclone log saved to: logs/{safe_task_name}/{now_str}.log"
        log(success_msg, "SUCCESS", task_name)
        
        if general_config.get("notification_level") == "all":
            send_discord(general_config.get("discord_webhook_url"), "Job finished successfully. All files synced securely.", task_name, "SUCCESS")

        # 8. Czyszczenie chmurowego kosza (retencja)
        if task["type"] == "sync" and retention_enabled and archive_base_path:
            run_retention_pruning(task, general_config, archive_base_path, retention_days)
            
        # 9. Czyszczenie starych lokalnych plików logów w podfolderach
        clean_old_local_logs(general_config)
        
        # === NOWY KROK: Przypomnienie o procedurze Restore ===
        log("--- EMERGENCY RESTORE INFO ---", "INFO", task_name)
        log("To restore data from cloud for this stack, ensure 'restore_global_enabled': true and 'restore_enabled': true in config.json, then run:", "INFO", task_name)
        log("docker compose --profile restore run --rm rclone-restore", "INFO", task_name)
        log("------------------------------", "INFO", task_name)

def load_config():
    try:
        with open("/app/config/config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Error reading configuration file: {e}", "ERROR")
        return None

def handle_shutdown(signum, frame):
    global exiting
    log("Received shutdown signal. Stopping active tasks gracefully...", "WARNING")
    exiting = True
    
    if scheduler:
        scheduler.shutdown(wait=False)
        
    for name, proc in list(running_processes.items()):
        log(f"Terminating rclone process for task: {name}", "WARNING")
        proc.terminate()
        
    sys.exit(0)

def main():
    global scheduler
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    log("Starting Docker Cloud Rclone Backup Service...", "INFO")
    
    config_path = "/app/config/config.json"
    rclone_config_path = "/app/config/rclone.conf"
    
    errors = []
    if not os.path.exists(config_path):
        errors.append(f"Missing critical configuration file: {config_path}")
    if not os.path.exists(rclone_config_path):
        errors.append(f"Missing rclone configuration file: {rclone_config_path}.")

    if errors:
        for err in errors:
            log(err, "ERROR")
        sys.exit(1)

    current_config = None
    last_config_mtime = 0
    
    config_loaded = load_config()
    if not config_loaded:
        log("Config file exists but is NOT valid JSON. Fix syntax errors! Exiting.", "ERROR")
        sys.exit(1)

    current_config = config_loaded
    max_workers = current_config.get("general", {}).get("max_concurrent_tasks", 1)
    log(f"Setting maximum concurrent tasks limit to: {max_workers}", "INFO")

    executors = {'default': ThreadPoolExecutor(max_workers=max_workers)}
    scheduler = BackgroundScheduler(executors=executors)
    scheduler.start()
    
    last_config_mtime = os.path.getmtime(config_path)
    register_tasks(current_config)

    while not exiting:
        try:
            if os.path.exists(config_path):
                mtime = os.path.getmtime(config_path)
                if mtime > last_config_mtime:
                    log("Configuration change detected. Reloading tasks...", "INFO")
                    new_config = load_config()
                    if new_config:
                        current_config = new_config
                        last_config_mtime = mtime
                        
                        new_max = current_config.get("general", {}).get("max_concurrent_tasks", 1)
                        if new_max != max_workers:
                            max_workers = new_max
                            log(f"Updating concurrent tasks limit to: {max_workers}. Restarting scheduler...", "WARNING")
                            scheduler.shutdown()
                            executors = {'default': ThreadPoolExecutor(max_workers=max_workers)}
                            scheduler = BackgroundScheduler(executors=executors)
                            scheduler.start()

                        register_tasks(current_config)
            
            interval = current_config.get("general", {}).get("config_check_interval_seconds", 30) if current_config else 10
            time.sleep(interval)
        except Exception as e:
            log(f"Main loop error: {e}", "ERROR")
            time.sleep(10)

def register_tasks(config):
    scheduler.remove_all_jobs()
    gen = config.get("general", {})
    
    for task in config.get("tasks", []):
        if not task.get("enabled", True):
            continue
        
        cron_expr = task.get("scheduler") or gen.get("default_scheduler", "0 2 * * *")
        cron_parts = cron_expr.split()
        if len(cron_parts) == 5:
            scheduler.add_job(
                run_rclone_task,
                'cron',
                minute=cron_parts[0],
                hour=cron_parts[1],
                day=cron_parts[2],
                month=cron_parts[3],
                day_of_week=cron_parts[4],
                args=[task, gen],
                id=task["name"],
                misfire_grace_time=None,
                coalesce=True
            )
            log(f"Scheduled cloud task '{task['name']}' with cron: {cron_expr}")

if __name__ == "__main__":
    main()
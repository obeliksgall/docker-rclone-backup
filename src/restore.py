import os
import sys
import json
import subprocess
import requests
from datetime import datetime

def log(message, level="INFO", task_name="RESTORE_SYSTEM"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] [{level}] [{task_name}] {message}"
    print(log_line, flush=True)
    
    log_dir = "/app/logs"
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "restore.log"), "a", encoding="utf-8") as f:
        f.write(log_line + "\n")

def send_discord(webhook_url, content, task_name, level="INFO"):
    if not webhook_url or "discord.com" not in webhook_url:
        return
    colors = {"INFO": 5814783, "SUCCESS": 3066993, "ERROR": 15158332, "WARNING": 16743168}
    embed = {
        "title": f"RESTORE Task: {task_name}",
        "description": content,
        "color": colors.get(level, 5814783),
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    try:
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    except Exception as e:
        log(f"Failed to send Discord notification: {e}", "ERROR")

def main():
    log("Starting Docker Cloud Rclone Restore Service...", "INFO")
    
    config_path = "/app/config/config.json"
    rclone_config_path = "/app/config/rclone.conf"
    
    if not os.path.exists(config_path) or not os.path.exists(rclone_config_path):
        log("Missing configuration files. Exiting.", "ERROR")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    gen = config.get("general", {})
    
    # Główny przełącznik bezpieczeństwa dla całości przywracania
    if not gen.get("restore_global_enabled", False):
        log("Global restore switch 'restore_global_enabled' is set to false. Aborting execution for safety.", "WARNING")
        sys.exit(0)

    log("GLOBAL RESTORE ACTIVE! Processing tasks...", "WARNING")
    if gen.get("notification_level") == "all":
        send_discord(gen.get("discord_webhook_url"), "Global Restore process started.", "SYSTEM", "WARNING")

    for task in config.get("tasks", []):
        task_name = task["name"]
        
        # Sprawdzamy czy przywracanie jest włączone dla tego konkretnego zadania
        if not task.get("restore_enabled", False):
            log(f"Restore disabled for task '{task_name}'. Skipping.", "INFO", task_name)
            continue

        log(f"Initiating restore for: {task_name}", "INFO", task_name)
        
        # ODWRÓCENIE KIERUNKU:
        # backup:  rclone sync [source] [remote][dest_path]
        # restore: rclone sync [remote][dest_path] [source]
        remote_source = f"{task['dest_remote']}{task['dest_path']}"
        local_destination = task["source"]

        # Upewniamy się, że lokalny katalog docelowy istnieje
        os.makedirs(local_destination, exist_ok=True)

        # Budowanie komendy rclone (przywracamy za pomocą typu zdefiniowanego w zadaniu, np. sync lub copy)
        rclone_cmd = ["rclone", task["type"], remote_source, local_destination, "-vv"]

        # Dodanie wykluczeń
        for exc in task.get("exclude", []):
            rclone_cmd.extend(["--exclude", exc])

        # Dodanie flag optymalizacyjnych z pliku JSON
        if task.get("extra_rclone_flags"):
            rclone_cmd.extend(task["extra_rclone_flags"].split())

        log(f"Executing: {' '.join(rclone_cmd)}", "INFO", task_name)
        
        try:
            process = subprocess.run(rclone_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            
            # Zapis pełnego logu rclone do pliku restore_task_*.log
            safe_name = task_name.replace(' ', '_')
            with open(f"/app/logs/restore_{safe_name}.log", "w", encoding="utf-8") as f_log:
                f_log.write(process.stderr) # rclone sypie debug do stderr przy -vv

            if process.returncode != 0:
                log(f"Restore failed with exit code {process.returncode}.", "ERROR", task_name)
                if gen.get("notification_level") in ["all", "errors_only"]:
                    send_discord(gen.get("discord_webhook_url"), f"Restore FAILED! Exit code: {process.returncode}", task_name, "ERROR")
            else:
                log("Restore finished successfully.", "SUCCESS", task_name)
                if gen.get("notification_level") == "all":
                    send_discord(gen.get("discord_webhook_url"), "Restore completed successfully. Local folder is synchronized with cloud.", task_name, "SUCCESS")
                    
        except Exception as e:
            log(f"Error during restore execution: {e}", "ERROR", task_name)

if __name__ == "__main__":
    main()
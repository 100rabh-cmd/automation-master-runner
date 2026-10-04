import sys
import time
import subprocess
from datetime import datetime

# Script schedule with rate-limit pauses (in seconds)
SCRIPT_SCHEDULE = [
    {"script": "expansion_watcher.py", "pause_after": 120},  # 2 min pause
    {"script": "concall_watcher.py",   "pause_after": 180},  # 3 min pause
    {"script": "results_watcher.py",   "pause_after": 180},  # 3 min pause
    {"script": "order_watcher.py",     "pause_after": 180},  # 3 min pause
    {"script": "stock_scanner.py",     "pause_after": 0}     # Final script
]

def run_sequence():
    print("==========================================")
    print(f"Master Sequence Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("==========================================")

    total_tasks = len(SCRIPT_SCHEDULE)

    for idx, item in enumerate(SCRIPT_SCHEDULE, start=1):
        script = item["script"]
        pause_sec = item["pause_after"]

        print(f"\n[{idx}/{total_tasks}] 🚀 Running {script}...")
        start_time = time.time()

        try:
            subprocess.run([sys.executable, script], check=True)
            elapsed = round(time.time() - start_time, 2)
            print(f"✅ {script} completed in {elapsed} seconds.")

        except subprocess.CalledProcessError as e:
            print(f"❌ Error in {script} (Exit Code: {e.returncode}). Moving to next script.")
        except FileNotFoundError:
            print(f"⚠️ Script file not found: {script}")
        except Exception as e:
            print(f"❌ Unexpected error running {script}: {e}")

        if idx < total_tasks and pause_sec > 0:
            print(f"⏳ Waiting {pause_sec}s before next task to satisfy rate limits...")
            time.sleep(pause_sec)

    print("\n==========================================")
    print(f"Master Sequence Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("==========================================")

if __name__ == "__main__":
    run_sequence()

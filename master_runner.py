import sys
import time
import subprocess
from datetime import datetime

# Script sequence with custom wait time (in seconds) AFTER each script finishes
SCRIPT_SCHEDULE = [
    {"script": "expansion_watcher.py", "pause_after": 120},  # Wait 2 mins after expansion
    {"script": "concall_watcher.py",   "pause_after": 180},  # Wait 3 mins after concall
    {"script": "results_watcher.py",   "pause_after": 180},  # Wait 3 mins after results
    {"script": "stock_scanner.py",     "pause_after": 0}     # Final script, no pause needed
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
            # 1. Execute script and WAIT until it completely finishes
            result = subprocess.run([sys.executable, script], check=True)
            elapsed = round(time.time() - start_time, 2)
            print(f"✅ {script} completed in {elapsed} seconds.")

        except subprocess.CalledProcessError as e:
            print(f"❌ Error in {script} (Exit Code: {e.returncode}). Proceeding to next task.")
        except FileNotFoundError:
            print(f"⚠️ Script file not found: {script}")

        # 2. Pause before launching the next script (if not the last one)
        if idx < total_tasks and pause_sec > 0:
            print(f"⏳ Resting for {pause_sec} seconds before next script to clear API rate limits...")
            time.sleep(pause_sec)

    print("\n==========================================")
    print(f"Master Sequence Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("==========================================")

if __name__ == "__main__":
    run_sequence()
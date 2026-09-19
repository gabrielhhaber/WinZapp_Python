import os
import sys
import subprocess
import signal

from pulse_audio_lifecycle import cleanup_winzapp_pulse_modules

def kill_port(port):
    print(f"Attempting to kill process on port {port}...")
    if sys.platform == "win32":
        try:
            # Find PID on Windows
            output = subprocess.check_output(f'netstat -ano | findstr :{port}', shell=True).decode()
            pids = set()
            for line in output.strip().split('\n'):
                parts = line.strip().split()
                if len(parts) >= 5:
                    pid = parts[-1]
                    if pid.isdigit() and pid != '0':
                        pids.add(int(pid))
            for pid in pids:
                print(f"Killing PID {pid} (Windows)")
                os.system(f"taskkill /F /PID {pid}")
        except Exception as e:
            print(f"No process found on port {port} or failed to kill: {e}")
    else:
        try:
            # Find PID on Linux
            output = subprocess.check_output(f'lsof -t -i:{port}', shell=True).decode()
            for pid in output.strip().split('\n'):
                if pid.isdigit():
                    print(f"Killing PID {pid} (Linux)")
                    os.kill(int(pid), signal.SIGKILL)
        except Exception as e:
            print(f"No process found on port {port} or failed to kill: {e}")

def kill_process_by_name(name):
    print(f"Killing processes matching '{name}'...")
    if sys.platform == "win32":
        os.system(f"taskkill /F /IM {name}.exe /T")
    else:
        os.system(f"pkill -9 -f '{name}'")

if __name__ == "__main__":
    cleanup_winzapp_pulse_modules()
    kill_port(6300)
    kill_process_by_name("chrome")
    kill_process_by_name("chromium")
    # Garante a eliminação do processo Node específico da API
    kill_process_by_name("start.js")
    
    # Also clean SingletonLock in userDataDir
    base_dir = os.path.dirname(os.path.abspath(__file__))
    user_data_dir = os.path.join(base_dir, "client", "api", "userDataDir")
    if os.path.exists(user_data_dir):
        print("Cleaning SingletonLock files...")
        for root, dirs, files in os.walk(user_data_dir):
            for file in files:
                if file == "SingletonLock":
                    try:
                        os.unlink(os.path.join(root, file))
                        print(f"Deleted lock: {os.path.join(root, file)}")
                    except Exception as e:
                        print(f"Failed to delete lock: {e}")
                        
    # Clean log files in client/api/log/ and root log/ folders
    api_log_dir = os.path.join(base_dir, "client", "api", "log")
    root_log_dir = os.path.join(base_dir, "log")
    for log_dir in (api_log_dir, root_log_dir):
        if os.path.exists(log_dir):
            print(f"Cleaning log files in {log_dir}...")
            for file in os.listdir(log_dir):
                file_path = os.path.join(log_dir, file)
                if os.path.isfile(file_path):
                    try:
                        os.unlink(file_path)
                        print(f"Deleted log: {file_path}")
                    except Exception as e:
                        print(f"Failed to delete log {file_path}: {e}")
                        
    print("Done killing API and Chrome processes, and cleaning logs.")

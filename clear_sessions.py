import os
import sys
import shutil
import subprocess
import signal

from pulse_audio_lifecycle import cleanup_winzapp_pulse_modules

def kill_port(port):
    print(f"Attempting to kill process on port {port}...")
    if sys.platform == "win32":
        try:
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
        os.system(f"pkill -9 -f {name}")

if __name__ == "__main__":
    cleanup_winzapp_pulse_modules()
    # 1. Encerrar os processos ativos da API e do Chrome/Chromium
    kill_port(6300)
    # chrome-headless-shell is what WinZapp actually launches now (see
    # client/api_patches/start.js). `taskkill /IM chrome.exe` does NOT match
    # chrome-headless-shell.exe — it is a different image name, not a prefix
    # match — so without this entry the browser survived every "clear
    # sessions" run and kept its lock on userDataDir/, which is exactly the
    # folder step 2 below then fails to delete.
    kill_process_by_name("chrome-headless-shell")
    kill_process_by_name("chrome")
    kill_process_by_name("chromium")
    kill_process_by_name("start.js")
    
    # 2. Remover as pastas de sessões salvas e tokens
    base_dir = os.path.dirname(os.path.abspath(__file__))
    folders_to_clean = [
        os.path.join(base_dir, "client", "api", "userDataDir"),
        os.path.join(base_dir, "client", "api", "tokens"),
        os.path.join(base_dir, "client", "api", "wppconnect_tokens"),
        os.path.join(base_dir, "client", "data")
    ]
    
    for folder in folders_to_clean:
        if os.path.exists(folder):
            print(f"Cleaning folder: {folder}...")
            try:
                shutil.rmtree(folder)
                os.makedirs(folder, exist_ok=True)
                print(f"Successfully cleared: {os.path.basename(folder)}")
            except Exception as e:
                print(f"Failed to clear {os.path.basename(folder)}: {e}")
        else:
            os.makedirs(folder, exist_ok=True)
            print(f"Created empty folder: {os.path.basename(folder)}")

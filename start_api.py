import os
import sys
import subprocess
import time
import socket

from pulse_audio_lifecycle import cleanup_winzapp_pulse_modules

def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0

def start_api():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    api_dir = os.path.join(base_dir, "client", "api")
    log_dir = os.path.join(base_dir, "log")
    
    if not os.path.exists(os.path.join(api_dir, "start.js")):
        print(f"Error: start.js not found in {api_dir}")
        sys.exit(1)
        
    # Porta padrão da API
    port = 6300
    if is_port_in_use(port):
        print(f"Error: Port {port} is already in use. The API might already be running.")
        sys.exit(1)

    # Remove devices left by a crashed API before new sessions are created.
    # The API recreates the devices it actually needs for each live account.
    if sys.platform != "win32":
        cleanup_winzapp_pulse_modules()
        
    os.makedirs(log_dir, exist_ok=True)
    stdout_log = os.path.join(log_dir, "node_stdout.log")
    
    print("Starting API server (node start.js)...")
    try:
        if sys.platform == "win32":
            # On Windows, start in a new console window so it runs in background
            process = subprocess.Popen(["node", "--max-old-space-size=4096", "start.js"], cwd=api_dir, creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            # On Linux, run as background process redirecting output using shell redirection
            # This prevents the process from dying when Python closes the file descriptor
            cmd = f"node --max-old-space-size=4096 start.js >> {stdout_log} 2>&1"
            process = subprocess.Popen(cmd, shell=True, cwd=api_dir, preexec_fn=os.setsid)
            
        # Aguarda 2 segundos para validar se o processo não morreu imediatamente
        time.sleep(2)
        if process.poll() is not None:
            # O processo terminou com erro
            print(f"Error: API process exited immediately with code {process.poll()}. Check logs in: {stdout_log}")
            sys.exit(1)
            
        print(f"API started successfully in the background. Logs redirected to: {stdout_log}")
    except Exception as e:
        print(f"Failed to start API: {e}")
        sys.exit(1)

if __name__ == "__main__":
    start_api()

import os
import time
import zipfile
import shutil
import base64
import json
import paho.mqtt.client as mqtt
import uuid
import socket
import sys
import subprocess
import tkinter as tk
from tkinter import simpledialog
import requests

ROOT_DIR = os.path.join(os.getenv('APPDATA'), 'ShareBox')
RAW_CHUNK_SIZE = 100 * 1024
MQTT_BROKER = "broker.emqx.io"
MQTT_PORT = 1883
HEARTBEAT_INTERVAL = 10
VERSION = 1.0

client = None
group_id = None
my_id = None

def check_for_updates():
    print("Checking for updates...")
    try:
        version_url = "https://sharebox.pages.dev/version.txt"
        response = requests.get(version_url)
        latest_version = float(response.text.strip())

        if latest_version > VERSION:
            print(f"New version {latest_version} available. Downloading update...")
            download_url = "https://sharebox.pages.dev/output/ShareBox.exe"
            new_exe_path = os.path.join(os.path.dirname(sys.executable), "ShareBox_new.exe")

            with requests.get(download_url, stream=True) as r:
                r.raise_for_status()
                with open(new_exe_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)

            print("Update downloaded. Restarting...")

            current_exe = sys.executable
            batch_script = f"""
@echo off
timeout /t 2 /nobreak > NUL
del "{current_exe}"
rename "{new_exe_path}" "{os.path.basename(current_exe)}"
start "" "{current_exe}"
del "%~f0"
"""
            script_path = os.path.join(os.path.dirname(sys.executable), 'update.bat')
            with open(script_path, 'w') as f:
                f.write(batch_script)

            subprocess.Popen(script_path, shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
            sys.exit(0)

    except Exception as e:
        print(f"Could not check for updates: {e}")
    print("Ready")

def setup_group_gui():
    root = tk.Tk()
    root.withdraw()
    group = simpledialog.askstring("Group Setup", "Please enter the group name:")
    if group:
        return group
    else:
        print("Group name is required. Exiting.")
        sys.exit(1)

def send_heartbeat(client):
    if not client.is_connected(): return
    heartbeat_topic = f"sharebox/{group_id}/host/heartbeat"
    try:
        hostname = socket.gethostname()
    except:
        hostname = "Unknown Host"
    payload = json.dumps({"hostId": my_id, "hostname": hostname})
    client.publish(heartbeat_topic, payload, qos=0, retain=False)

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        command_topic = f"sharebox/{group_id}/client/command"
        client.subscribe(command_topic)
        send_heartbeat(client)
    else:
        print(f"Failed to connect, return code {rc}\n")

def on_message(client, userdata, msg):
    try:
        payload_str = msg.payload.decode('utf-8')
        data = json.loads(payload_str)
        command, args, request_id = data.get("command"), data.get("args", []), data.get("requestID")
        if not command or not request_id: raise ValueError("Missing command or requestID")

        response_payload = { "requestID": request_id, "data": None, "error": None }
        try:
            path_arg = args[0] if args else ""
            target_path = ROOT_DIR if not path_arg or path_arg in ['/', '\\'] else os.path.join(ROOT_DIR, path_arg.lstrip('/\\').replace('/', os.sep))

            if not is_safe_path(target_path):
                raise ValueError("Access to the specified path is denied.")

            if command == "getfiles": response_payload["data"] = get_files(target_path)
            elif command == "getfolders": response_payload["data"] = get_folders(target_path)
            elif command == "downloadlength": response_payload["data"] = str(get_download_length(target_path))
            elif command == "getproperties": response_payload["data"] = get_properties(target_path)
            elif command == "download":
                binary_chunk = download_part(target_path, int(args[1]))
                response_payload["data"] = base64.b64encode(binary_chunk).decode('ascii')
            elif command == "setfile" or command == "appendfile":
                file_path, b64_data = target_path, args[1]
                dir_name = os.path.dirname(file_path)
                if not os.path.exists(dir_name):
                    os.makedirs(dir_name)
                binary_chunk = base64.b64decode(b64_data)
                mode = 'wb' if command == 'setfile' else 'ab'
                with open(file_path, mode) as f: f.write(binary_chunk)
                response_payload["data"] = "ok"
            elif command == "newfolder":
                os.makedirs(target_path, exist_ok=True)
                response_payload["data"] = "ok"
            elif command == "rename":
                if len(args) < 2:
                    raise ValueError("Rename command requires the target path and the new name.")
                new_name = args[1]
                if '..' in new_name or '/' in new_name or '\\' in new_name:
                    raise ValueError("Invalid characters in new name.")

                new_path = os.path.join(os.path.dirname(target_path), new_name)

                if not is_safe_path(new_path):
                    raise ValueError("Access to the destination path is denied.")
                if not os.path.exists(target_path):
                    raise FileNotFoundError("The item to rename does not exist.")

                os.rename(target_path, new_path)
                response_payload["data"] = "ok"
            elif command == "delete":
                if os.path.isfile(target_path): os.remove(target_path)
                elif os.path.isdir(target_path): shutil.rmtree(target_path)
                else: raise FileNotFoundError("Item not found for deletion.")
                response_payload["data"] = "ok"
            else: raise ValueError(f"Unknown command: {command}")
        except Exception as e:
            response_payload["error"] = str(e)

        output_topic = f"sharebox/{group_id}/host/output"
        client.publish(output_topic, json.dumps(response_payload))
    except Exception as e:
        print(f"FATAL ERROR in on_message: {e}")

def is_safe_path(path: str) -> bool: return os.path.abspath(path).startswith(os.path.abspath(ROOT_DIR))
def get_files(directory: str) -> str: return "\n".join([item for item in os.listdir(directory) if os.path.isfile(os.path.join(directory, item))])
def get_folders(directory: str) -> str: return "\n".join([item for item in os.listdir(directory) if os.path.isdir(os.path.join(directory, item))])

def download_part(path: str, part: int) -> bytes:
    target_path, is_zip = path, False
    if os.path.isdir(path): target_path = shutil.make_archive("temp_zip", 'zip', path); is_zip = True
    elif not os.path.isfile(path): raise FileNotFoundError("File/directory not found.")
    try:
        with open(target_path, 'rb') as f: f.seek((part - 1) * RAW_CHUNK_SIZE); return f.read(RAW_CHUNK_SIZE)
    finally:
        if is_zip: os.remove(target_path)

def get_download_length(path: str) -> int:
    size = 0
    if os.path.isdir(path): zip_path = shutil.make_archive("temp_zip", 'zip', path); size = os.path.getsize(zip_path); os.remove(zip_path)
    elif os.path.isfile(path): size = os.path.getsize(path)
    else: raise FileNotFoundError("File/directory not found.")
    return (size + RAW_CHUNK_SIZE - 1) // RAW_CHUNK_SIZE if size > 0 else 0

def get_properties(file_path: str) -> str:
    stat = os.stat(file_path)
    return f"Size: {stat.st_size} bytes\nModified: {time.ctime(stat.st_mtime)}\nCreated: {time.ctime(stat.st_ctime)}"

def main():
    global group_id, my_id, client, ROOT_DIR
    check_for_updates()

    app_data_dir = os.getenv('APPDATA')
    group_file = os.path.join(app_data_dir, "ShareBoxGroup.txt")

    my_id = socket.gethostname()

    if not os.path.exists(group_file):
        group_id = setup_group_gui()
        with open(group_file, 'w') as f:
            f.write(group_id)
    else:
        with open(group_file, 'r') as f:
            group_id = f.read().strip()
    print(group_id)

    host_client_id = f'sharebox-host-{my_id}-{uuid.uuid4()}'
    client = mqtt.Client(host_client_id)
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
        while True:
            time.sleep(HEARTBEAT_INTERVAL)
            send_heartbeat(client)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        if client.is_connected(): client.loop_stop(); client.disconnect()

if __name__ == "__main__":
    if not os.path.exists(ROOT_DIR):
        os.makedirs(ROOT_DIR)
    main()

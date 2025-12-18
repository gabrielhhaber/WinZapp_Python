import os
import sys
import ssl
import requests
import uvicorn
from traceback import format_exc
from fastapi import FastAPI
from dotenv import load_dotenv
from pydantic import BaseModel
load_dotenv()
HOST = os.getenv("HOST")
PORT = os.getenv("PORT")
EVOLUTION_HOST = os.getenv("EVOLUTION_HOST")
EVOLUTION_PORT = os.getenv("EVOLUTION_PORT")
APIKEY = os.getenv("APIKEY")
SSL_CERTFILE = os.getenv("SSL_CERTFILE")
SSL_KEYFILE = os.getenv("SSL_KEYFILE")


app = FastAPI()
class Instance(BaseModel):
    name: str
    number: str
    token: str

@app.post("/create_instance/")
def create_instance(instance: Instance):
    return add_instance(instance.name, instance.number, instance.token)

def add_instance(name, number, token):
    url = f"https://{EVOLUTION_HOST}:{EVOLUTION_PORT}/instance/create"
    payload = {
        "instanceName": name,
        "integration": "WHATSAPP-BAILEYS",
        "number": number,
        "token": token,
        "qrcode": True,
"syncFullHistory": True
    }
    headers = {
        "apikey": APIKEY,
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(url, json=payload, headers=headers, verify=False)
        if response.status_code == 201:
            try:
                set_websocket_for_instance(number)
            except Exception as e:
                return {"websocket_error": format_exc()}
            return response.json()
        return {"error": f"Could not create instance. {response.text}"}
    except requests.exceptions.RequestException as e:
        return {"program_error": format_exc()}

def set_websocket_for_instance(number):
    url = f"https://{EVOLUTION_HOST}:{EVOLUTION_PORT}/websocket/set/{number}/"
    payload = { "websocket": {
        "enabled": True,
        "events": ["CALL", "APPLICATION_STARTUP", "QRCODE_UPDATED", "MESSAGES_SET", "MESSAGES_UPSERT", "MESSAGES_UPDATE", "MESSAGES_DELETE", "SEND_MESSAGE", "CONTACTS_SET", "CONTACTS_UPSERT", "CONTACTS_UPDATE", "PRESENCE_UPDATE", "CHATS_SET", "CHATS_UPSERT", "CHATS_UPDATE", "CHATS_DELETE", "CONNECTION_UPDATE", "GROUPS_UPSERT", "GROUP_UPDATE", "CALL"]
    } }
    headers = {
        "apikey": APIKEY,
        "Content-Type": "application/json"
    }

    response = requests.post(url, json=payload, verify=False, headers=headers)

if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT, ssl_certfile=SSL_CERTFILE, ssl_keyfile=SSL_KEYFILE)

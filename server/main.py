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


app = FastAPI()
class Instance(BaseModel):
    name: str
    number: str

@app.post("/create_instance/")
def create_instance(instance: Instance):
    return add_instance(instance.name, instance.number)

def add_instance(name, number):
    url = f"http://{EVOLUTION_HOST}:{EVOLUTION_PORT}/instance/create"
    payload = {
        "instanceName": name,
        "integration": "WHATSAPP-BAILEYS",
        "number": number,
        "qrcode": True,
"syncFullHistory": True
    }
    headers = {
        "apikey": APIKEY,
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code == 201:
            return response.json()
        return {"error": f"Could not create instance. {response.text}"}
    except requests.exceptions.RequestException as e:
        return {"program_error": format_exc(e)}


if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT)

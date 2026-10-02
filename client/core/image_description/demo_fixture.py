"""Deterministic, non-personal fixtures for the opt-in manual demo application."""
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


DEMO_ACCOUNT = "photo-description-demo"
DEMO_PHOTO_ID = "demophoto"


def demo_directory(entry_file):
    # Outside the checkout: credentials must never accidentally enter a commit.
    return Path(entry_file).resolve().parents[2] / "_MANUEL_TEST" / "fotograf-betimleme"


def make_demo_photo():
    image = Image.new("RGB", (800, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.text((35, 30), "WINZAPP DEMO 123", fill="black",
              font=ImageFont.load_default(size=36))
    draw.rectangle((50, 150, 290, 360), fill="blue")
    draw.ellipse((340, 160, 540, 360), fill="red")
    draw.polygon(((660, 140), (560, 360), (760, 360)), fill="yellow")
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def demo_messages():
    return [
        {"key": {"id": "demotext", "remoteJid": "demo-chat"},
         "messageType": "conversation", "message": {"conversation": "Demo"}},
        {"key": {"id": DEMO_PHOTO_ID, "remoteJid": "demo-chat"},
         "messageType": "imageMessage", "message": {"imageMessage": {}}},
    ]

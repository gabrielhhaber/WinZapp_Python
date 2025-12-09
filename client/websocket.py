import wx
import websockets
import asyncio
import json
import threading

class EvolutionWebSocket:
    def __init__(self, main_window, ws_url):
        self.main_window = main_window
        self.ws_url = ws_url
        self.on_event_callback = self.on_event
        self.stop_flag = False

    def start(self):
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def run(self):
        asyncio.run(self._ws_loop())

    async def _ws_loop(self):
        while not self.stop_flag:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    wx.CallAfter(self.on_event, {"event": "ws_connected"})

                    async for message in ws:
                        try:
                            data = json.loads(message)
                        except:
                            continue

                        wx.CallAfter(self.on_event, data)

            except Exception as e:
                wx.CallAfter(self.on_event, {
                    "event": "ws_error",
                    "error": str(e)
                })

                await asyncio.sleep(2)

    def stop(self):
        self.stop_flag = True

    def on_event(self, data):
        print(data)
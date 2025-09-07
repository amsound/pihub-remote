#!/usr/bin/env python3
import time, json, sys
from paho.mqtt import client as mqtt

HOST = "192.168.70.24"
PORT = 1883
USER = "remote"
PASS = "remote"
TOPIC = "pihub/living_room/cmd/#"

seen = set()

def on_connect(c, u, f, rc, props=None):
    c.subscribe(TOPIC)

def on_message(c, u, msg):
    seen.add(msg.topic)

def main():
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.username_pw_set(USER, PASS)
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(HOST, PORT, 60)
    c.loop_start()
    time.sleep(3.0)  # wait a moment to collect retained messages
    c.loop_stop()
    if not seen:
        print("No retained cmd topics found.")
        return
    print(f"Clearing {len(seen)} topics…")
    for t in sorted(seen):
        c.publish(t, payload=None, retain=True)  # retained null clears
        print("cleared:", t)
    c.disconnect()

if __name__ == "__main__":
    main()

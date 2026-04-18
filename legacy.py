import customtkinter as ctk
import tkinter as tk
from threading import Thread, Lock
from pynput import keyboard
import pygame
import pyaudio
import os, sys, random, psutil, time
import ctypes
import numpy as np

# ================= CONFIG =================
APP_NAME = "SoundKey"
WIDTH, HEIGHT = 275, 640
LOCK_FILE = "soundkey.lock"

BG = "#0a0d12"
PANEL = "#0f141c"
ACCENT = "#5eead4"
TEXT = "#e5e7eb"

# ================= SINGLE INSTANCE =================
if os.path.exists(LOCK_FILE):
    try:
        pid = int(open(LOCK_FILE).read())
        if psutil.pid_exists(pid):
            sys.exit(0)
    except:
        pass
open(LOCK_FILE, "w").write(str(os.getpid()))

# ================= AUDIO =================
pygame.mixer.init()
p = pyaudio.PyAudio()

stop_mic_event = False
audio_lock = Lock()
is_playing = False
current_sound = None
current_path = ""
start_time = 0
sound_length = 0

def find_cable_input():
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if "cable input" in info.get("name", "").lower():
            if info["maxOutputChannels"] > 0:
                return i
    return None

CABLE_DEVICE = find_cable_input()
if CABLE_DEVICE is None:
    sys.exit("VB-Audio Cable not found")

def scale_pcm(raw, volume):
    pcm = np.frombuffer(raw, dtype=np.int16)
    pcm = np.clip(pcm * volume, -32768, 32767)
    return pcm.astype(np.int16).tobytes()

def play_to_mic(raw):
    global stop_mic_event

    stream = p.open(
        format=pyaudio.paInt16,
        channels=2,
        rate=44100,
        output=True,
        output_device_index=CABLE_DEVICE,
        frames_per_buffer=1024
    )

    stop_mic_event = False
    chunk_size = 4096

    try:
        for i in range(0, len(raw), chunk_size):
            if stop_mic_event:
                break
            stream.write(raw[i:i + chunk_size])
    finally:
        stream.stop_stream()
        stream.close()


# ================= UI =================
ctk.set_appearance_mode("dark")
root = ctk.CTk()
root.geometry(f"{WIDTH}x{HEIGHT}")
root.title(APP_NAME)
root.configure(fg_color=BG)
root.iconbitmap("newicon.ico") 

# ---- Force taskbar presence
root.overrideredirect(False)

# ================= CONTENT =================
content = ctk.CTkFrame(root, fg_color=BG)
content.pack(expand=True, fill="both", padx=20, pady=20)

status = ctk.CTkLabel(content, text="No key bound", text_color=ACCENT)
status.pack(pady=6)

# ================= INFO BOX =================
info_box = ctk.CTkFrame(content, fg_color=PANEL)
info_box.pack(fill="x", pady=8)

info_name = ctk.CTkLabel(info_box, text="Name: —", wraplength=420)
info_name.pack(anchor="w", padx=10, pady=4)

info_path = ctk.CTkLabel(info_box, text="Path: —", text_color="#9ca3af", wraplength=420)
info_path.pack(anchor="w", padx=10)

info_time = ctk.CTkLabel(info_box, text="Time: 0.00 / 0.00", text_color=ACCENT)
info_time.pack(anchor="w", padx=10, pady=4)

# ================= VOLUME =================
ctk.CTkLabel(content, text="Headphones Volume").pack(anchor="w")
os_volume = ctk.CTkSlider(content, from_=0.0, to=1.0)
os_volume.set(0.7)
os_volume.pack(fill="x", pady=4)

ctk.CTkLabel(content, text="Mic Volume").pack(anchor="w")
mic_volume = ctk.CTkSlider(content, from_=0.0, to=1.5)
mic_volume.set(1.0)
mic_volume.pack(fill="x", pady=4)

# ================= OPTIONS =================
options = {
    "topmost": tk.BooleanVar(value=False),
    "notifications": tk.BooleanVar(value=True)
}

def apply_options():
    root.attributes("-topmost", options["topmost"].get())

ctk.CTkCheckBox(
    content, text="Always on top",
    variable=options["topmost"],
    command=apply_options
).pack(anchor="w", pady=4)

ctk.CTkCheckBox(
    content, text="Show notifications",
    variable=options["notifications"]
).pack(anchor="w")

# ================= SOUNDS =================
sounds = []
sound_paths = []

def browse():
    from tkinter import filedialog
    paths = filedialog.askopenfilenames(filetypes=[("Audio", "*.wav *.mp3 *.ogg")])
    sounds.clear()
    sound_paths.clear()

    for pth in paths:
        sounds.append(pygame.mixer.Sound(pth))
        sound_paths.append(pth)

    status.configure(text=f"{len(sounds)} sounds loaded")

ctk.CTkButton(content, text="Load Sounds", command=browse).pack(pady=8)

# ================= NOTIFICATION =================
notify = None

def show_notification(text):
    global notify
    if not options["notifications"].get():
        return
    if notify:
        return

    notify = ctk.CTkToplevel(root)
    notify.overrideredirect(True)
    notify.attributes("-topmost", True)
    notify.geometry("300x60+20+20")
    notify.configure(fg_color=PANEL)
    ctk.CTkLabel(notify, text=text).pack(expand=True)

def close_notification():
    global notify
    if notify:
        notify.destroy()
        notify = None

# ================= PLAY =================
def play_sound():
    global current_sound, current_path, start_time, sound_length, is_playing

    if not sounds or pygame.mixer.get_busy():
        return

    idx = random.randrange(len(sounds))
    current_sound = sounds[idx]
    current_path = sound_paths[idx]

    current_sound.set_volume(os_volume.get())
    current_sound.play()

    raw = scale_pcm(current_sound.get_raw(), mic_volume.get())
    Thread(target=play_to_mic, args=(raw,), daemon=True).start()

    start_time = time.time()
    sound_length = current_sound.get_length()
    is_playing = True

    info_name.configure(text=f"Name: {os.path.basename(current_path)}")
    info_path.configure(text=f"Path: {current_path}")

    show_notification(f"▶ {os.path.basename(current_path)}")

def stop_sound():
    global is_playing, stop_mic_event
    stop_mic_event = True
    pygame.mixer.stop()
    is_playing = False
    close_notification()

ctk.CTkButton(content, text="Stop", fg_color="#ef4444", command=stop_sound).pack(pady=8)

# ================= TIME UPDATE =================
def update_time():
    global is_playing
    if pygame.mixer.get_busy():
        pos = time.time() - start_time
        info_time.configure(text=f"Time: {pos:.2f} / {sound_length:.2f}")
    else:
        if is_playing:
         is_playing = False
         stop_mic_event = True
        close_notification()
    root.after(100, update_time)

update_time()

# ================= KEY BIND =================
selected_key = None

def bind_key():
    status.configure(text="Press any key...")

    def once(k):
        global selected_key
        selected_key = k
        status.configure(text=f"Bound to {k}")
        return False

    keyboard.Listener(on_press=once).start()

def on_key(k):
    if k == selected_key:
        play_sound()

ctk.CTkButton(content, text="Bind Key", command=bind_key).pack(pady=6)
keyboard.Listener(on_press=on_key).start()

# ================= CLEANUP =================
def cleanup():
    pygame.mixer.quit()
    p.terminate()
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)
    root.destroy()

root.protocol("WM_DELETE_WINDOW", cleanup)
root.mainloop()

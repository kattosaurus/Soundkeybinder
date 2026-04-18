import customtkinter as ctk
import tkinter as tk
from threading import Thread, Lock, Event
from pynput import keyboard
import pygame
import pyaudio
import os, sys, random, psutil, time
import ctypes
import numpy as np
import audioop

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
VIRTUAL_RATE = 48000
VIRTUAL_CHANNELS = 2
SAMPLE_WIDTH = 2

pygame.mixer.init(frequency=VIRTUAL_RATE, size=-16, channels=VIRTUAL_CHANNELS)
p = pyaudio.PyAudio()

stop_mic_event = False
stop_passthrough_event = Event()
audio_lock = Lock()
is_playing = False
current_sound = None
current_path = ""
start_time = 0
sound_length = 0
mic_passthrough_thread = None
selected_input_device = None

def find_cable_input():
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if "cable input" in info.get("name", "").lower():
            if info["maxOutputChannels"] > 0:
                return i
    return None

def preferred_input_host_api():
    preferred_order = ["wasapi", "wdm-ks", "directsound", "mme"]
    apis = []
    for i in range(p.get_host_api_count()):
        api = p.get_host_api_info_by_index(i)
        apis.append((i, str(api.get("name", "")).lower()))

    for key in preferred_order:
        for idx, name in apis:
            if key in name:
                return idx
    return None

def normalize_device_name(name):
    base = " ".join(name.replace("\t", " ").split())
    # Strip trailing host tags often shown by PortAudio, e.g. " (...)"
    if base.endswith(")") and " (" in base:
        base = base[:base.rfind(" (")]
    return base.strip().lower()

def is_real_input_name(name):
    low = name.lower()
    blocked = [
        "cable input", "cable output", "vb-audio", "virtual", "stereo mix",
        "wave out", "what u hear", "loopback", "monitor", "mix "
    ]
    return not any(token in low for token in blocked)

def list_input_mics():
    def collect_devices(restrict_host_api):
        best_by_name = {}
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            name = info.get("name", "")
            if not is_real_input_name(name):
                continue

            max_in = int(info.get("maxInputChannels", 0))
            if max_in <= 0:
                continue

            host_api = int(info.get("hostApi", -1))
            if restrict_host_api is not None and host_api != restrict_host_api:
                continue

            device = {
                "index": i,
                "name": name,
                "channels": max_in,
                "rate": int(info.get("defaultSampleRate", 44100)),
                "host_api": host_api
            }
            key = normalize_device_name(name)
            existing = best_by_name.get(key)
            if existing is None or device["channels"] > existing["channels"]:
                best_by_name[key] = device
        return list(best_by_name.values())

    preferred_api = preferred_input_host_api()
    devices = collect_devices(preferred_api)
    if not devices:
        devices = collect_devices(None)

    devices.sort(key=lambda d: ("realtek" not in d["name"].lower(), d["name"].lower()))
    return devices

CABLE_DEVICE = find_cable_input()
if CABLE_DEVICE is None:
    sys.exit("VB-Audio Cable not found")

try:
    virtual_out_stream = p.open(
        format=pyaudio.paInt16,
        channels=VIRTUAL_CHANNELS,
        rate=VIRTUAL_RATE,
        output=True,
        output_device_index=CABLE_DEVICE,
        frames_per_buffer=1024
    )
except Exception as e:
    sys.exit(f"Failed to open VB-Cable output stream: {e}")

def scale_pcm(raw, volume):
    pcm = np.frombuffer(raw, dtype=np.int16)
    pcm = np.clip(pcm * volume, -32768, 32767)
    return pcm.astype(np.int16).tobytes()

def to_stereo(raw, channels):
    if channels == 2:
        return raw
    if channels == 1:
        return audioop.tostereo(raw, SAMPLE_WIDTH, 1.0, 1.0)

    pcm = np.frombuffer(raw, dtype=np.int16)
    if pcm.size == 0:
        return b""
    frame_count = pcm.size // channels
    if frame_count == 0:
        return b""
    pcm = pcm[:frame_count * channels].reshape(frame_count, channels)
    stereo = pcm[:, :2]
    return stereo.astype(np.int16).tobytes()

def write_to_virtual(raw, in_channels, in_rate, volume):
    data = to_stereo(raw, in_channels)
    if in_rate != VIRTUAL_RATE and data:
        data, _ = audioop.ratecv(data, SAMPLE_WIDTH, VIRTUAL_CHANNELS, in_rate, VIRTUAL_RATE, None)
    if volume != 1.0 and data:
        data = scale_pcm(data, volume)
    if not data:
        return
    with audio_lock:
        virtual_out_stream.write(data)

def play_to_mic(raw):
    global stop_mic_event

    stop_mic_event = False
    chunk_size = 4096

    try:
        for i in range(0, len(raw), chunk_size):
            if stop_mic_event:
                break
            chunk = raw[i:i + chunk_size]
            write_to_virtual(chunk, VIRTUAL_CHANNELS, VIRTUAL_RATE, mic_volume.get())
    except Exception as e:
        root.after(0, lambda: status.configure(text=f"Sound route error: {e}"))

def mic_passthrough_loop(device_index):
    in_stream = None
    try:
        info = p.get_device_info_by_index(device_index)
        in_channels = min(2, int(info.get("maxInputChannels", 0)))
        rate = int(info.get("defaultSampleRate", 44100))
        if in_channels == 0:
            return

        in_stream = p.open(
            format=pyaudio.paInt16,
            channels=in_channels,
            rate=rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=1024
        )
    except Exception as e:
        root.after(0, lambda: status.configure(text=f"Mic route error: {e}"))
        return

    try:
        while not stop_passthrough_event.is_set():
            data = in_stream.read(1024, exception_on_overflow=False)
            write_to_virtual(data, in_channels, rate, mic_volume.get())
    finally:
        if in_stream is not None:
            in_stream.stop_stream()
            in_stream.close()


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

ctk.CTkLabel(content, text="Input Mic -> Virtual Mic").pack(anchor="w", pady=(6, 0))
input_mics = list_input_mics()
mic_name_to_index = {f'{m["name"]} [{m["index"]}]': m["index"] for m in input_mics}
mic_names = list(mic_name_to_index.keys()) if input_mics else ["No input devices"]
mic_choice = tk.StringVar(value=mic_names[0])

def on_mic_select(choice):
    global selected_input_device
    selected_input_device = mic_name_to_index.get(choice)

ctk.CTkOptionMenu(
    content,
    values=mic_names,
    variable=mic_choice,
    command=on_mic_select
).pack(fill="x", pady=4)

selected_input_device = mic_name_to_index.get(mic_choice.get())

def toggle_mic_passthrough():
    global mic_passthrough_thread
    if selected_input_device is None:
        status.configure(text="No input mic available")
        return

    if mic_passthrough_thread and mic_passthrough_thread.is_alive():
        stop_passthrough_event.set()
        mic_passthrough_thread = None
        mic_route_btn.configure(text="Start Mic Route")
        status.configure(text="Mic route stopped")
        return

    stop_passthrough_event.clear()
    mic_passthrough_thread = Thread(
        target=mic_passthrough_loop,
        args=(selected_input_device,),
        daemon=True
    )
    mic_passthrough_thread.start()
    mic_route_btn.configure(text="Stop Mic Route")
    status.configure(text=f"Routing mic: {mic_choice.get()}")

mic_route_btn = ctk.CTkButton(content, text="Start Mic Route", command=toggle_mic_passthrough)
mic_route_btn.pack(pady=6)

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

    raw = current_sound.get_raw()
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
    global is_playing, stop_mic_event
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
    stop_passthrough_event.set()
    try:
        virtual_out_stream.stop_stream()
        virtual_out_stream.close()
    except:
        pass
    pygame.mixer.quit()
    p.terminate()
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)
    root.destroy()

root.protocol("WM_DELETE_WINDOW", cleanup)
root.mainloop()

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
import json
import zipfile
import shutil

APP_NAME = "Open Sound Pad"
WIDTH, HEIGHT = 700, 620
LOCK_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "OpenSoundPad")
os.makedirs(LOCK_DIR, exist_ok=True)
LOCK_FILE = os.path.join(LOCK_DIR, "OSP.lock")
CONFIG_FILE = os.path.join(LOCK_DIR, "config.json")
PRESET_CACHE_DIR = os.path.join(LOCK_DIR, "preset_cache")
PRESET_EXT = ".ospad"

BG = "#0a0d12"
PANEL = "#0f141c"
PANEL2 = "#141b26"
ACCENT = "#5eead4"
TEXT = "#e5e7eb"
DANGER = "#ef4444"

if os.path.exists(LOCK_FILE):
    try:
        pid = int(open(LOCK_FILE).read())
        if psutil.pid_exists(pid):
            sys.exit(0)
    except:
        pass
open(LOCK_FILE, "w").write(str(os.getpid()))

VIRTUAL_RATE = 48000
VIRTUAL_CHANNELS = 2
SAMPLE_WIDTH = 2

pygame.mixer.init(frequency=VIRTUAL_RATE, size=-16, channels=VIRTUAL_CHANNELS)
pygame.mixer.set_num_channels(32)
p = pyaudio.PyAudio()

stop_passthrough_event = Event()
audio_lock = Lock()
mic_passthrough_thread = None
selected_input_device = None


def load_app_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_app_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
    except Exception:
        pass


def find_virtual_outputs():
    """Find candidate virtual audio devices we can write mixed audio into so
    it shows up as a virtual microphone elsewhere. Supports VB-Audio Virtual
    Cable and VB-Audio Voicemeeter (Standard/Banana/Potato)."""
    known_tokens = [
        "cable input",
        "voicemeeter input",   
        "voicemeeter aux input",  
        "voicemeeter vaio3 input",  
    ]
    candidates = []
    seen = set()
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        name = info.get("name", "")
        low = name.lower()
        if info.get("maxOutputChannels", 0) <= 0:
            continue
        for token in known_tokens:
            if token in low and i not in seen:
                candidates.append({"index": i, "name": name})
                seen.add(i)
                break
    return candidates


def open_virtual_stream(device_index):
    global virtual_out_stream
    with audio_lock:
        try:
            virtual_out_stream.stop_stream()
            virtual_out_stream.close()
        except Exception:
            pass
        virtual_out_stream = p.open(
            format=pyaudio.paInt16,
            channels=VIRTUAL_CHANNELS,
            rate=VIRTUAL_RATE,
            output=True,
            output_device_index=device_index,
            frames_per_buffer=1024
        )


_app_cfg = load_app_config()
virtual_outputs = find_virtual_outputs()
if not virtual_outputs:
    sys.exit(
        "No virtual audio input device found. Install VB-Audio Virtual Cable "
        "or VB-Audio Voicemeeter (Standard/Banana/Potato) and try again."
    )

_saved_output_index = _app_cfg.get("virtual_output_device")
CABLE_DEVICE = None
if _saved_output_index is not None:
    for d in virtual_outputs:
        if d["index"] == _saved_output_index:
            CABLE_DEVICE = _saved_output_index
            break
if CABLE_DEVICE is None:
    CABLE_DEVICE = virtual_outputs[0]["index"]

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
    sys.exit(f"Failed to open virtual output stream: {e}")


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

def play_to_mic_slot(slot, raw):
    """Feed one slot's audio into the virtual mic, chunk by chunk,
    stopping early if the slot's own stop event is set (interrupt)."""
    chunk_size = 4096
    try:
        for i in range(0, len(raw), chunk_size):
            if slot["mic_stop_event"].is_set():
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


def serialize_key(k):
    if k is None:
        return None
    if isinstance(k, keyboard.KeyCode):
        return {"type": "char", "char": k.char, "vk": k.vk}
    if isinstance(k, keyboard.Key):
        return {"type": "special", "name": k.name}
    return None

def deserialize_key(d):
    if not d:
        return None
    try:
        if d.get("type") == "char":
            if d.get("char"):
                return keyboard.KeyCode.from_char(d["char"])
            if d.get("vk") is not None:
                return keyboard.KeyCode.from_vk(d["vk"])
            return None
        if d.get("type") == "special":
            return getattr(keyboard.Key, d.get("name"), None)
    except Exception:
        return None
    return None


ctk.set_appearance_mode("dark")
root = ctk.CTk()
root.geometry(f"{WIDTH}x{HEIGHT}")
root.title(APP_NAME)
root.configure(fg_color=BG)
try:
    root.iconbitmap("newicon.ico")
except Exception:
    pass

root.overrideredirect(False)

content = ctk.CTkFrame(root, fg_color=BG)
content.pack(expand=True, fill="both", padx=20, pady=20)

left_panel = ctk.CTkFrame(content, fg_color=BG, width=300)
left_panel.pack(side="left", fill="y", padx=(0, 16))
left_panel.pack_propagate(False)

right_panel = ctk.CTkFrame(content, fg_color=BG)
right_panel.pack(side="left", fill="both", expand=True)

status = ctk.CTkLabel(left_panel, text="No key bound", text_color=ACCENT)
status.pack(pady=6)

info_box = ctk.CTkFrame(left_panel, fg_color=PANEL)
info_box.pack(fill="x", pady=8)

info_name = ctk.CTkLabel(info_box, text="Name: —", wraplength=260)
info_name.pack(anchor="w", padx=10, pady=4)

info_path = ctk.CTkLabel(info_box, text="Path: —", text_color="#9ca3af", wraplength=260)
info_path.pack(anchor="w", padx=10)

info_time = ctk.CTkLabel(info_box, text="Time: 0.00 / 0.00", text_color=ACCENT)
info_time.pack(anchor="w", padx=10, pady=4)

ctk.CTkLabel(left_panel, text="Headphones Volume").pack(anchor="w")
os_volume = ctk.CTkSlider(left_panel, from_=0.0, to=1.0)
os_volume.set(0.7)
os_volume.pack(fill="x", pady=4)

ctk.CTkLabel(left_panel, text="Mic Volume").pack(anchor="w")
mic_volume = ctk.CTkSlider(left_panel, from_=0.0, to=1.5)
mic_volume.set(1.0)
mic_volume.pack(fill="x", pady=4)

ctk.CTkLabel(left_panel, text="Virtual Output (VB-Cable / Voicemeeter)").pack(anchor="w", pady=(6, 0))
virtual_output_name_to_index = {f'{d["name"]} [{d["index"]}]': d["index"] for d in virtual_outputs}
virtual_output_names = list(virtual_output_name_to_index.keys())
_current_output_label = next(
    (label for label, idx in virtual_output_name_to_index.items() if idx == CABLE_DEVICE),
    virtual_output_names[0]
)
virtual_output_choice = tk.StringVar(value=_current_output_label)

def on_virtual_output_select(choice):
    global CABLE_DEVICE
    idx = virtual_output_name_to_index.get(choice)
    if idx is None or idx == CABLE_DEVICE:
        return
    try:
        open_virtual_stream(idx)
        CABLE_DEVICE = idx
        cfg = load_app_config()
        cfg["virtual_output_device"] = idx
        save_app_config(cfg)
        status.configure(text=f"Output routed to: {choice}")
    except Exception as e:
        status.configure(text=f"Failed to switch output device: {e}")

ctk.CTkOptionMenu(
    left_panel,
    values=virtual_output_names,
    variable=virtual_output_choice,
    command=on_virtual_output_select
).pack(fill="x", pady=4)

ctk.CTkLabel(left_panel, text="Input Mic -> Virtual Mic").pack(anchor="w", pady=(6, 0))
input_mics = list_input_mics()
mic_name_to_index = {f'{m["name"]} [{m["index"]}]': m["index"] for m in input_mics}
mic_names = list(mic_name_to_index.keys()) if input_mics else ["No input devices"]
mic_choice = tk.StringVar(value=mic_names[0])

def on_mic_select(choice):
    global selected_input_device
    selected_input_device = mic_name_to_index.get(choice)

ctk.CTkOptionMenu(
    left_panel,
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

mic_route_btn = ctk.CTkButton(left_panel, text="Start Mic Route", command=toggle_mic_passthrough)
mic_route_btn.pack(pady=6)

options = {
    "topmost": tk.BooleanVar(value=False),
    "notifications": tk.BooleanVar(value=True),
    "interrupt_on_replay": tk.BooleanVar(value=False),
}

def apply_options():
    root.attributes("-topmost", options["topmost"].get())

ctk.CTkCheckBox(
    left_panel, text="Always on top",
    variable=options["topmost"],
    command=apply_options
).pack(anchor="w", pady=4)

ctk.CTkCheckBox(
    left_panel, text="Show notifications",
    variable=options["notifications"]
).pack(anchor="w")

ctk.CTkCheckBox(
    left_panel, text="Interrupt when pressing keybind again",
    variable=options["interrupt_on_replay"]
).pack(anchor="w", pady=(4, 8))

preset_btns = ctk.CTkFrame(left_panel, fg_color=BG)
preset_btns.pack(fill="x", pady=(0, 8))
ctk.CTkButton(
    preset_btns, text="Save Preset",
    command=lambda: save_preset_dialog()
).pack(side="left", expand=True, fill="x", padx=(0, 4))
ctk.CTkButton(
    preset_btns, text="Load Preset",
    command=lambda: load_preset_dialog()
).pack(side="left", expand=True, fill="x")

ctk.CTkButton(left_panel, text="Stop", fg_color=DANGER, command=lambda: stop_all()).pack(pady=8, fill="x")

sound_slots = []
key_bindings = {}
last_played_slot = None
_next_slot_id = 0

def format_key(k):
    try:
        if hasattr(k, "char") and k.char:
            return k.char.upper()
    except Exception:
        pass
    return str(k).replace("Key.", "")

ctk.CTkLabel(right_panel, text="Sound Slots (key -> sound)").pack(anchor="w", pady=(4, 0))

add_slot_btn_holder = ctk.CTkFrame(right_panel, fg_color=BG)
add_slot_btn_holder.pack(fill="x", pady=(4, 6))

slots_frame = ctk.CTkScrollableFrame(right_panel, fg_color=PANEL)
slots_frame.pack(fill="both", expand=True, pady=(0, 6))

def rebuild_key_bindings():
    """Recompute the key->slot map from the current slot list."""
    key_bindings.clear()
    for slot in sound_slots:
        if slot["key"] is not None:
            key_bindings[slot["key"]] = slot

def make_slot_row(slot):
    row = ctk.CTkFrame(slots_frame, fg_color=PANEL2)
    row.pack(fill="x", pady=4, padx=4)

    label = ctk.CTkLabel(
        row,
        text=f'{slot["name"]}   [{format_key(slot["key"]) if slot["key"] else "unbound"}]',
        anchor="w", justify="left", wraplength=190
    )
    label.pack(side="left", padx=6, pady=6, fill="x", expand=True)
    slot["label"] = label

    def do_load():
        from tkinter import filedialog
        pth = filedialog.askopenfilename(filetypes=[("Audio", "*.wav *.mp3 *.ogg")])
        if not pth:
            return
        try:
            slot["sound"] = pygame.mixer.Sound(pth)
        except Exception as e:
            status.configure(text=f"Couldn't load sound: {e}")
            return
        slot["path"] = pth
        slot["name"] = os.path.basename(pth)
        refresh_label(slot)
        status.configure(text=f'Loaded "{slot["name"]}"')

    def do_bind():
        status.configure(text="Press any key...")

        def once(k):
            for s in sound_slots:
                if s is not slot and s["key"] == k:
                    s["key"] = None
                    refresh_label(s)
            slot["key"] = k
            rebuild_key_bindings()
            refresh_label(slot)
            status.configure(text=f"Bound {format_key(k)} -> {slot['name']}")
            return False  

        keyboard.Listener(on_press=once).start()

    def do_remove():
        stop_slot(slot)
        sound_slots.remove(slot)
        rebuild_key_bindings()
        row.destroy()
        status.configure(text=f'Removed "{slot["name"]}"')

    ctk.CTkButton(row, text="Load", width=56, command=do_load).pack(side="left", padx=2, pady=6)
    ctk.CTkButton(row, text="Bind", width=56, command=do_bind).pack(side="left", padx=2, pady=6)
    ctk.CTkButton(row, text="✕", width=28, fg_color=DANGER, command=do_remove).pack(side="left", padx=(2, 6), pady=6)

    slot["row"] = row

def refresh_label(slot):
    slot["label"].configure(
        text=f'{slot["name"]}   [{format_key(slot["key"]) if slot["key"] else "unbound"}]'
    )

def add_slot():
    global _next_slot_id
    _next_slot_id += 1
    slot = {
        "id": _next_slot_id,
        "name": "No sound loaded",
        "path": "",
        "sound": None,
        "key": None,
        "channel": None,
        "mic_stop_event": Event(),
        "start_time": 0,
        "length": 0,
        "label": None,
        "row": None,
    }
    sound_slots.append(slot)
    make_slot_row(slot)

ctk.CTkButton(add_slot_btn_holder, text="+ Add Sound Slot", command=add_slot).pack(fill="x")

add_slot()

notify = None

def show_notification(text):
    global notify
    if not options["notifications"].get():
        return
    if notify:
        close_notification()

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

def stop_slot(slot):
    if slot["channel"] is not None:
        try:
            slot["channel"].stop()
        except Exception:
            pass
    slot["mic_stop_event"].set()

def play_slot(slot):
    global last_played_slot

    if slot["sound"] is None:
        status.configure(text=f'"{slot["name"]}" has no sound loaded')
        return

    already_playing = slot["channel"] is not None and slot["channel"].get_busy()

    if already_playing:
        if options["interrupt_on_replay"].get():
            stop_slot(slot)
        return

    slot["sound"].set_volume(os_volume.get())
    channel = slot["sound"].play()
    slot["channel"] = channel

    slot["mic_stop_event"].clear()
    raw = slot["sound"].get_raw()
    Thread(target=play_to_mic_slot, args=(slot, raw), daemon=True).start()

    slot["start_time"] = time.time()
    slot["length"] = slot["sound"].get_length()
    last_played_slot = slot

    info_name.configure(text=f'Name: {slot["name"]}')
    info_path.configure(text=f'Path: {slot["path"]}')

    show_notification(f'▶ {slot["name"]}')

def stop_all():
    for slot in sound_slots:
        stop_slot(slot)
    close_notification()

def update_time():
    if last_played_slot and last_played_slot["channel"] is not None and last_played_slot["channel"].get_busy():
        pos = time.time() - last_played_slot["start_time"]
        info_time.configure(text=f'Time: {pos:.2f} / {last_played_slot["length"]:.2f}')
    else:
        if not any(s["channel"] is not None and s["channel"].get_busy() for s in sound_slots):
            close_notification()
    root.after(100, update_time)

update_time()


def save_preset(path):
    manifest = {"slots": []}
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for slot in sound_slots:
                entry = {
                    "id": slot["id"],
                    "name": slot["name"],
                    "key": serialize_key(slot["key"]),
                    "sound_file": None,
                }
                if slot["path"] and os.path.exists(slot["path"]):
                    arcname = f'sounds/{slot["id"]}_{os.path.basename(slot["path"])}'
                    zf.write(slot["path"], arcname)
                    entry["sound_file"] = arcname
                manifest["slots"].append(entry)
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        status.configure(text=f"Preset saved: {os.path.basename(path)}")
        cfg = load_app_config()
        cfg["last_preset"] = path
        save_app_config(cfg)
    except Exception as e:
        status.configure(text=f"Failed to save preset: {e}")

def load_preset(path):
    global sound_slots, _next_slot_id

    if not os.path.exists(path):
        status.configure(text="Preset file not found")
        return

    try:
        extract_dir = os.path.join(PRESET_CACHE_DIR, os.path.splitext(os.path.basename(path))[0])
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)
        os.makedirs(extract_dir, exist_ok=True)

        with zipfile.ZipFile(path, "r") as zf:
            zf.extractall(extract_dir)

        manifest_path = os.path.join(extract_dir, "manifest.json")
        with open(manifest_path, "r") as f:
            manifest = json.load(f)

        for slot in list(sound_slots):
            stop_slot(slot)
            if slot["row"] is not None:
                slot["row"].destroy()
        sound_slots.clear()
        key_bindings.clear()
        _next_slot_id = 0

        for entry in manifest.get("slots", []):
            _next_slot_id = max(_next_slot_id, entry.get("id", 0))
            slot = {
                "id": entry.get("id", _next_slot_id),
                "name": entry.get("name", "No sound loaded"),
                "path": "",
                "sound": None,
                "key": deserialize_key(entry.get("key")),
                "channel": None,
                "mic_stop_event": Event(),
                "start_time": 0,
                "length": 0,
                "label": None,
                "row": None,
            }
            sound_file = entry.get("sound_file")
            if sound_file:
                full_path = os.path.join(extract_dir, sound_file)
                if os.path.exists(full_path):
                    try:
                        slot["sound"] = pygame.mixer.Sound(full_path)
                        slot["path"] = full_path
                    except Exception:
                        pass
            sound_slots.append(slot)
            make_slot_row(slot)

        if not sound_slots:
            add_slot()

        rebuild_key_bindings()
        status.configure(text=f"Preset loaded: {os.path.basename(path)}")

        cfg = load_app_config()
        cfg["last_preset"] = path
        save_app_config(cfg)
    except Exception as e:
        status.configure(text=f"Failed to load preset: {e}")

def save_preset_dialog():
    from tkinter import filedialog
    path = filedialog.asksaveasfilename(
        defaultextension=PRESET_EXT,
        filetypes=[("Open Sound Pad Preset", f"*{PRESET_EXT}")]
    )
    if not path:
        return
    save_preset(path)

def load_preset_dialog():
    from tkinter import filedialog
    path = filedialog.askopenfilename(
        filetypes=[("Open Sound Pad Preset", f"*{PRESET_EXT}")]
    )
    if not path:
        return
    load_preset(path)


def on_key(k):
    slot = key_bindings.get(k)
    if slot:
        play_slot(slot)

keyboard.Listener(on_press=on_key).start()

_startup_cfg = load_app_config()
_last_preset_path = _startup_cfg.get("last_preset")
if _last_preset_path and os.path.exists(_last_preset_path):
    load_preset(_last_preset_path)

def cleanup():
    stop_passthrough_event.set()
    for slot in sound_slots:
        slot["mic_stop_event"].set()
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

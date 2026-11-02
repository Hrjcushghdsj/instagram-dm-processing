#!/usr/bin/env python3

# PyLauncher: Aggregate a user's audio messages from an Instagram export

"""
Extract audio message filenames for a given sender from an Instagram
message export JSON, and optionally concatenate those audio files into
a single track in timestamp order.

Usage (CLI):
    python extract_audio.py -i input.json -u "Mai cm" -o output.json
    python extract_audio.py -i input.json -u "Mai cm" -o output.json \
        -d path/to/audio_dir -m combined.wav

Usage (GUI):
    python extract_audio.py
    # Launches a Tkinter GUI:
    #   1. Select the input JSON
    #   2. Choose a user from the list
    #   3. Pick "Save output.json" or "Combine to audio file"
    #   4. If combining, pick the audio directory
    #   5. Click "Save..." to choose the output path
    #   A progress frame (n / total + progress bar) replaces the Save
    #   button while the worker thread runs.

Requirements for combining:
    pip install pydub
    # plus ffmpeg installed and on PATH
"""

import argparse
import json
import math
import os
import sys
import threading
import unicodedata

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    TK_AVAILABLE = True
except ImportError:
    TK_AVAILABLE = False


class AudioCombineError(Exception):
    """Raised when audio combining cannot proceed."""


def norm(s):
    return " ".join(unicodedata.normalize("NFKC", s).strip().split()).casefold()


def list_senders(input_path):
    """Return a sorted list of unique sender names (original casing)."""
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    seen = {}
    for msg in data.get("messages", []):
        sender = msg.get("sender_name", "")
        if not sender:
            continue
        key = norm(sender)
        if key not in seen:
            seen[key] = sender
    return sorted(seen.values(), key=str.casefold)


def extract_audio(input_path, speaker):
    """
    Extract audio entries for `speaker`, oldest-first.

    No progress reporting: the clip total isn't known until the whole
    message list has been scanned, so the GUI drives the progress bar
    from the combine phase instead.
    """
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    target = norm(speaker)
    results = []

    for msg in data.get("messages", []):
        sender = msg.get("sender_name", "")
        if norm(sender) != target:
            continue
        ts = msg.get("timestamp_ms")
        for audio in msg.get("audio_files", []):
            uri = audio.get("uri")
            if not uri:
                continue
            results.append({
                "sender": sender,
                "filename": os.path.basename(uri),
                "uri": uri,
                "timestamp_ms": ts,
            })

    # Instagram exports are newest-first; reorder to oldest-first
    results.sort(key=lambda r: (r["timestamp_ms"] is None, r["timestamp_ms"] or 0))
    return results


def combine_audio(entries, audio_dir, output_path,
                  crossfade_ms=30, silence_ms=150,
                  peak_dbfs=-1.0, sample_rate=44100, progress=None):
    """
    Concatenate audio files into one track.

    Anti-click measures:
      - Each clip is peak-normalized to peak_dbfs to equalize loudness
        and avoid clipping when summed.
      - A short fade-in and fade-out (crossfade_ms) is applied to every
        clip so it starts/ends at silence, eliminating DC-step clicks.
      - A short silence (silence_ms) is inserted between clips.
      - All clips are resampled to a single sample_rate and mono so the
        concatenation doesn't have format mismatches.

    `progress`, if given, is called as progress(done, total) after each
    clip has been processed (whether it succeeded or not). `total` here
    is the number of audio entries, i.e. the number of clips.
    """
    try:
        from pydub import AudioSegment
    except ImportError:
        raise AudioCombineError(
            "pydub is required for combining audio. Install with: pip install pydub"
        )

    if not os.path.isdir(audio_dir):
        raise AudioCombineError(f"audio directory not found: {audio_dir}")

    combined = AudioSegment.silent(duration=0, frame_rate=sample_rate)
    gap = AudioSegment.silent(duration=silence_ms, frame_rate=sample_rate)

    total = len(entries)
    if progress:
        progress(0, total)

    used, missing = 0, []

    for idx, entry in enumerate(entries, 1):
        fname = entry["filename"]
        fpath = os.path.join(audio_dir, fname)
        if not os.path.isfile(fpath):
            missing.append(fname)
            if progress:
                progress(idx, total)
            continue

        try:
            clip = AudioSegment.from_file(fpath)
        except Exception as e:
            print(f"Warning: could not load {fname}: {e}", file=sys.stderr)
            missing.append(fname)
            if progress:
                progress(idx, total)
            continue

        # Unify format
        clip = clip.set_frame_rate(sample_rate).set_channels(1)

        # Peak-normalize so clips don't sum above 0 dBFS
        if clip.max_possible_amplitude and clip.max > 0:
            target_peak = (10 ** (peak_dbfs / 20.0)) * clip.max_possible_amplitude
            gain_db = 20 * math.log10(target_peak / clip.max)
            clip = clip.apply_gain(gain_db)

        # Short fade in/out to guarantee zero-crossing at the edges
        fade = min(crossfade_ms, len(clip) // 2)
        if fade > 0:
            clip = clip.fade_in(fade).fade_out(fade)

        if used > 0:
            combined += gap
        combined += clip
        used += 1

        if progress:
            progress(idx, total)

    if used == 0:
        raise AudioCombineError("no audio files could be loaded; nothing to combine.")

    fmt = os.path.splitext(output_path)[1].lstrip(".") or "wav"
    combined.export(output_path, format=fmt)
    print(f"Combined {used} clip(s) into {output_path}")
    if missing:
        print(f"Warning: {len(missing)} file(s) missing or unreadable: {missing[:5]}"
              + (" ..." if len(missing) > 5 else ""), file=sys.stderr)


# ---------------------------------------------------------------------------
# Tkinter GUI
# ---------------------------------------------------------------------------

def _shorten(s, maxlen=30):
    if len(s) <= maxlen:
        return s
    return "..." + s[-(maxlen - 3):]


def _label_for_path(path):
    """Basename of `path`, truncated so the label can't grow indefinitely."""
    if not path:
        return "(none)"
    base = os.path.basename(os.path.normpath(path))
    return _shorten(base, 30)

def _parent_dir(path):
    """Parent directory of a file path, or "" if unavailable."""
    if not path:
        return ""
    return os.path.dirname(os.path.abspath(path))


def _grandparent_dir(path):
    """Directory two levels up from a file path, or "" if unavailable."""
    parent = _parent_dir(path)
    if not parent:
        return ""
    return os.path.dirname(parent)

class AudioExtractorApp:
    LABEL_MAX = 30

    def __init__(self, root):
        self.root = root
        self.input_path = None
        self.audio_dir = None
        self.senders = []
        self.processing = False

        root.title("Instagram Audio Message Extractor")
        # Explicitly resizeable (default, but stated for clarity).
        root.resizable(True, True)
        root.minsize(420, 460)

        self.mode = tk.StringVar(master=root, value="json")

        frame = ttk.Frame(root, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(2, weight=1)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        # --- Input file selection ---
        self.input_button = ttk.Button(
            frame, text="Select Input JSON", command=self.choose_input)
        self.input_button.grid(row=0, column=0, sticky="w")
        self.input_label = ttk.Label(
            frame, text="(none)", foreground="gray", width=self.LABEL_MAX)
        self.input_label.grid(row=0, column=1, sticky="w", padx=(8, 0))

        # --- Users list ---
        ttk.Label(
            frame, text="Select a user whose audio to aggregate:",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(12, 0))

        list_frame = ttk.Frame(frame)
        list_frame.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(4, 0))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        self.listbox = tk.Listbox(
            list_frame, height=8, width=40, exportselection=False)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.bind("<<ListboxSelect>>", lambda _e: self._update_save_state())

        # --- Output mode ---
        mode_frame = ttk.LabelFrame(frame, text="Output", padding=10)
        mode_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        mode_frame.columnconfigure(1, weight=1)

        self.rb_json = ttk.Radiobutton(
            mode_frame, text="Save output.json",
            variable=self.mode, value="json", command=self.update_mode)
        self.rb_json.grid(row=0, column=0, columnspan=2, sticky="w")

        self.rb_audio = ttk.Radiobutton(
            mode_frame, text="Combine to audio file",
            variable=self.mode, value="audio", command=self.update_mode)
        self.rb_audio.grid(row=1, column=0, columnspan=2, sticky="w")

        self.audio_btn = ttk.Button(
            mode_frame, text="Select audio directory...",
            command=self.choose_audio_dir, state="disabled")
        self.audio_btn.grid(row=2, column=0, sticky="w", pady=(8, 0), padx=(16, 0))
        self.audio_label = ttk.Label(
            mode_frame, text="(none)", foreground="gray", width=self.LABEL_MAX)
        self.audio_label.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        # --- Save button / progress frame (row 4, mutually exclusive) ---
        self.save_container = ttk.Frame(frame)
        self.save_container.grid(row=4, column=0, columnspan=2, pady=(14, 0))
        self.save_button = ttk.Button(
            self.save_container, text="Save...",
            command=self.on_save, state="disabled")
        self.save_button.pack()

        self.progress_frame = ttk.Frame(frame)
        self.progress_frame.columnconfigure(0, weight=1)
        self.progress_label = ttk.Label(self.progress_frame, text="")
        self.progress_label.grid(row=0, column=0, sticky="w")
        self.progress_bar = ttk.Progressbar(
            self.progress_frame, mode="determinate", length=300)
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        # progress_frame is created but not gridded until processing starts.

        self._update_save_state()
        self.center_and_focus()

    # ------------------------------------------------------------------ UI

    def center_and_focus(self):
        self.root.update_idletasks()
        w = self.root.winfo_reqwidth()
        h = self.root.winfo_reqheight()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.focus_force()
        self.root.after(150, lambda: self.root.attributes("-topmost", False))

    def _set_controls_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        self.input_button.configure(state=state)
        self.listbox.configure(state=state)
        self.rb_json.configure(state=state)
        self.rb_audio.configure(state=state)
        if enabled and self.mode.get() == "audio":
            self.audio_btn.configure(state="normal")
        else:
            self.audio_btn.configure(state="disabled")

    def _update_save_state(self):
        if self.processing:
            self.save_button.configure(state="disabled")
            return
        ready = (
            self.input_path is not None
            and bool(self.listbox.curselection())
            and (self.mode.get() == "json" or self.audio_dir is not None)
        )
        self.save_button.configure(state="normal" if ready else "disabled")

    # -------------------------------------------------------------- actions

    def choose_input(self):
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Select Instagram message export JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            senders = list_senders(path)
        except Exception as e:
            messagebox.showerror(
                "Error", f"Could not read input file:\n{e}", parent=self.root)
            return

        self.input_path = path
        self.senders = senders
        self.input_label.configure(
            text=_label_for_path(path), foreground="black")
        self.listbox.delete(0, "end")
        for s in senders:
            self.listbox.insert("end", s)
        if senders:
            self.listbox.selection_set(0)
        else:
            messagebox.showinfo(
                "No senders",
                "No sender names were found in this export.",
                parent=self.root)
        self._update_save_state()

    def _save_initialdir(self):
        """Grandparent of input.json, used as the default save location."""
        gp = _grandparent_dir(self.input_path)
        return gp if gp and os.path.isdir(gp) else None

    def choose_audio_dir(self):
        kwargs = {
            "parent": self.root,
            "title": "Select directory containing audio files",
        }
        parent = _parent_dir(self.input_path)
        if parent and os.path.isdir(parent):
            kwargs["initialdir"] = parent
        path = filedialog.askdirectory(**kwargs)
        if not path:
            return
        self.audio_dir = path
        self.audio_label.configure(
            text=_label_for_path(path), foreground="black")
        self._update_save_state()

    def update_mode(self):
        if self.mode.get() == "audio":
            if not self.processing:
                self.audio_btn.configure(state="normal")
            if not self.audio_dir:
                self.choose_audio_dir()
        else:
            self.audio_btn.configure(state="disabled")
        self._update_save_state()

    def on_save(self):
        if not self.input_path:
            messagebox.showwarning(
                "Missing input", "Please select an input JSON first.",
                parent=self.root)
            return
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showwarning(
                "No user selected", "Please select a user from the list.",
                parent=self.root)
            return
        sender = self.senders[sel[0]]

        init = self._save_initialdir()

        if self.mode.get() == "json":
            kwargs = {
                "parent": self.root,
                "title": "Save output JSON",
                "defaultextension": ".json",
                "initialfile": "output.json",
                "filetypes": [("JSON files", "*.json"), ("All files", "*.*")],
            }
            if init:
                kwargs["initialdir"] = init
            out = filedialog.asksaveasfilename(**kwargs)
            if not out:
                return
            self._start_processing("json", sender, out)
        else:
            if not self.audio_dir:
                messagebox.showwarning(
                    "Missing audio directory",
                    "Please select the directory containing audio files.",
                    parent=self.root)
                return
            kwargs = {
                "parent": self.root,
                "title": "Save combined audio",
                "defaultextension": ".wav",
                "initialfile": "combined.wav",
                "filetypes": [("WAV files", "*.wav"), ("All files", "*.*")],
            }
            if init:
                kwargs["initialdir"] = init
            out = filedialog.asksaveasfilename(**kwargs)
            if not out:
                return
            self._start_processing("audio", sender, out)

    # ---------------------------------------------------------- processing

    def _start_processing(self, mode, sender, out):
        self.processing = True
        self.save_container.grid_remove()
        self.progress_frame.grid(row=4, column=0, columnspan=2,
                                 pady=(14, 0), sticky="ew")
        self.progress_label.configure(text="Loading...")
        self.progress_bar.configure(mode="indeterminate", maximum=1, value=0)
        self.progress_bar.start(12)
        self._set_controls_enabled(False)
        self.save_button.configure(state="disabled")

        t = threading.Thread(
            target=self._worker, args=(mode, sender, out), daemon=True)
        t.start()

    def _report_progress(self, done, total):
        """Called from the worker thread; marshals the UI update to the main loop."""
        def upd():
            try:
                if self.progress_bar.cget("mode") != "determinate":
                    self.progress_bar.stop()
                    self.progress_bar.configure(mode="determinate")
            except Exception:
                pass
            self.progress_bar.configure(maximum=max(1, total), value=done)
            self.progress_label.configure(text=f"{done} / {total}")
        self.root.after(0, upd)

    def _end_processing(self):
        self.processing = False
        self.progress_bar.stop()
        self.progress_frame.grid_remove()
        self.save_container.grid(row=4, column=0, columnspan=2, pady=(14, 0))
        self._set_controls_enabled(True)
        self._update_save_state()

    def _finish_ok(self, msg):
        def f():
            self._end_processing()
            messagebox.showinfo("Done", msg, parent=self.root)
        self.root.after(0, f)

    def _finish_err(self, msg):
        def f():
            self._end_processing()
            messagebox.showerror("Error", msg, parent=self.root)
        self.root.after(0, f)

    def _worker(self, mode, sender, out):
        try:
            # Extraction: we don't know the clip total until it finishes,
            # so the bar stays in indeterminate "Loading..." mode during
            # this phase. Once entries are known, the bar is driven purely
            # by audio clips (not by messages scanned).
            entries = extract_audio(self.input_path, sender)

            if mode == "json":
                with open(out, "w", encoding="utf-8") as f:
                    json.dump(entries, f, ensure_ascii=False, indent=2)
                n = len(entries)
                self._finish_ok(
                    f"Wrote {n} audio entr{'y' if n == 1 else 'ies'} to:\n{out}")
                return

            n_entries = len(entries)
            if n_entries == 0:
                raise AudioCombineError(
                    f"No audio entries found for '{sender}'.")

            total = n_entries
            self._report_progress(0, total)

            def combine_cb(done, _t):
                self._report_progress(done, total)

            combine_audio(entries, self.audio_dir, out, progress=combine_cb)
            self._finish_ok(f"Combined audio saved to:\n{out}")
        except AudioCombineError as e:
            self._finish_err(str(e))
        except Exception as e:
            self._finish_err(str(e))


def run_gui():
    root = tk.Tk()
    AudioExtractorApp(root)
    root.mainloop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Extract audio filenames for a sender; optionally concatenate them."
    )
    p.add_argument("-i", "--input", required=True, help="Input JSON file")
    p.add_argument("-u", "--user", required=True, help="Speaker name to filter by")
    p.add_argument("-o", "--output", required=True, help="Output JSON file")
    p.add_argument("-d", "--audio-dir", help="Directory containing the audio files")
    p.add_argument("-m", "--combined-output", help="Path to save combined audio track")
    args = p.parse_args()

    if (args.audio_dir is None) != (args.combined_output is None):
        sys.exit("Error: -d and -m must be specified together.")

    if not os.path.isfile(args.input):
        sys.exit(f"Error: input file not found: {args.input}")

    try:
        entries = extract_audio(args.input, args.user)
    except json.JSONDecodeError as e:
        sys.exit(f"Error: invalid JSON in {args.input}: {e}")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(entries)} audio entries for '{args.user}' to {args.output}")

    if args.audio_dir and args.combined_output:
        try:
            combine_audio(entries, args.audio_dir, args.combined_output)
        except AudioCombineError as e:
            sys.exit(f"Error: {e}")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        if not TK_AVAILABLE:
            sys.exit("Error: tkinter is not available. "
                     "Please pass CLI arguments instead.")
        run_gui()
    else:
        main()
#!/usr/bin/env python3

# PyLauncher: Make Instagram DM History More Readable

"""
Process an Instagram message export JSON:
- Convert timestamps to local Central Time (US/Central)
- Remove geoblock and unsent flags
- Condense shared links, media, and reactions into plain text
- Repair double-encoded UTF-8 (mojibake) so accents and emojis render
- Detect and categorize link types (Instagram, GIF, PDF, etc.)
- Sort messages chronologically
- Optionally rename participants (e.g. John -> Jack)
- Optionally filter by min/max date (YYYY-MM-DD)
- Output a simplified JSON array
- Optionally output a human-readable text transcript

Usage:
  reformat_instagram_dms.py -i IN.json -o OUT.json [-t OUT.txt] [-m 30]
                            [-r OLD=NEW ...] [--min-date YYYY-MM-DD]
                            [--max-date YYYY-MM-DD]
  reformat_instagram_dms.py            # launches GUI
"""

import argparse
import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

CENTRAL_TZ = ZoneInfo("US/Central")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def fix_mojibake(text: str) -> str:
    """Reverse double-encoded UTF-8 (mojibake)."""
    if not text:
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def detect_link_type(url: str) -> str:
    """Detect and categorize the type of link."""
    if not url:
        return "sent a link"

    url_lower = url.lower()

    if "instagram.com" in url_lower:
        return "sent an Instagram post"
    if "giphy.com" in url_lower or "media.giphy.com" in url_lower:
        return "sent a GIF"

    if url_lower.endswith('.gif') or '.gif?' in url_lower:
        return "sent a GIF"
    if url_lower.endswith('.pdf') or '.pdf?' in url_lower:
        return "sent a PDF"
    if (url_lower.endswith('.jpg') or '.jpg?' in url_lower
            or url_lower.endswith('.jpeg') or '.jpeg?' in url_lower):
        return "sent an image"
    if url_lower.endswith('.png') or '.png?' in url_lower:
        return "sent an image"
    if url_lower.endswith('.mp4') or '.mp4?' in url_lower:
        return "sent a video"
    if (url_lower.endswith('.mp3') or '.mp3?' in url_lower
            or url_lower.endswith('.ogg') or '.ogg?' in url_lower):
        return "sent an audio file"

    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "sent a YouTube link"
    if "tiktok.com" in url_lower:
        return "sent a TikTok link"
    if "spotify.com" in url_lower:
        return "sent a Spotify link"

    return "sent a link"


def to_central(ms: int) -> datetime:
    """Convert milliseconds since epoch to a Central Time datetime."""
    return datetime.fromtimestamp(ms / 1000.0, tz=dt_timezone.utc).astimezone(CENTRAL_TZ)


def format_timestamp(ms: int) -> str:
    """Convert milliseconds since epoch to formatted Central Time string."""
    return to_central(ms).strftime("%Y-%m-%d %H:%M:%S %Z")


def build_content(msg: dict) -> str:
    """Build a concise content string from a message dict."""
    if "share" in msg:
        share = msg["share"]
        link = share.get("link", "")

        if link and "instagram.com" in link.lower():
            return "sent an Instagram post"
        if link and ("giphy.com" in link.lower() or "media.giphy.com" in link.lower()):
            return "sent a GIF"

        return detect_link_type(link)

    if "photos" in msg:
        return f"sent {len(msg['photos'])} photo(s)"
    if "videos" in msg:
        return f"sent {len(msg['videos'])} video(s)"
    if "audio_files" in msg:
        return "sent an audio message"
    if "gifs" in msg:
        return "sent a GIF"

    content = msg.get("content", "").strip()
    if content:
        return fix_mojibake(content)

    return ""


def parse_renames(specs: list, parser) -> dict:
    """Turn a list of 'OLD=NEW' strings into a rename mapping."""
    rename_map = {}
    for spec in specs:
        if "=" not in spec:
            parser.error(f"Invalid --rename value {spec!r}: expected OLD=NEW")
        old, new = spec.split("=", 1)
        rename_map[old] = new
    return rename_map


def parse_date(value) -> "date | None":
    """Parse 'YYYY-MM-DD' into a date object, or None if empty."""
    s = (value or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Invalid date {s!r}: expected YYYY-MM-DD")


def build_transcript(entries: list, gap_minutes: int) -> str:
    """Build a human-readable transcript from chronologically sorted entries."""
    lines = []
    prev_dt = None

    for ts, entry in entries:
        dt = to_central(ts)

        if prev_dt is None or dt.date() != prev_dt.date():
            lines.append(f"{dt.strftime('%B')} {dt.day}, {dt.year}")
            lines.append(dt.strftime("%H:%M"))
        else:
            elapsed_minutes = (dt - prev_dt).total_seconds() / 60.0
            if elapsed_minutes >= gap_minutes:
                lines.append(dt.strftime("%H:%M"))

        lines.append(f'{entry["sender"]}: "{entry["message"]}"')
        prev_dt = dt

    return "\n".join(lines)


def get_senders(input_path: str) -> list:
    """Return the sorted list of unique sender names in the export."""
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    senders = set()
    for msg in data.get("messages", []):
        s = msg.get("sender_name")
        if s:
            senders.add(s)
    return sorted(senders)


def process_messages(data: dict, rename_map: dict, gap_minutes: int,
                     min_date=None, max_date=None):
    """
    Core transformation. Returns (simplified_list, entries_with_ms).
    Optionally filters messages to [min_date, max_date] inclusive.
    """
    messages = data.get("messages", [])
    simplified_with_ms = []

    for msg in messages:
        content_text = msg.get("content", "")
        if content_text == "You sent an attachment." and "share" not in msg:
            continue

        content = build_content(msg)
        if not content:
            continue

        sender = msg.get("sender_name", "Unknown")
        sender = rename_map.get(sender, sender)

        ts = msg.get("timestamp_ms")
        if ts is None:
            continue

        if min_date is not None or max_date is not None:
            msg_day = to_central(ts).date()
            if min_date is not None and msg_day < min_date:
                continue
            if max_date is not None and msg_day > max_date:
                continue

        simplified_with_ms.append((ts, {
            "sender": sender,
            "timestamp": format_timestamp(ts),
            "message": content,
        }))

    simplified_with_ms.sort(key=lambda x: x[0])
    simplified = [item[1] for item in simplified_with_ms]
    return simplified, simplified_with_ms


def write_outputs(input_path, output_path, text_path, rename_map, gap_minutes,
                  min_date_str="", max_date_str=""):
    """Read input, process, and write whichever outputs are requested."""
    min_date = parse_date(min_date_str)
    max_date = parse_date(max_date_str)

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    simplified, entries = process_messages(
        data, rename_map, gap_minutes, min_date, max_date)

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(simplified, f, indent=2, ensure_ascii=False)

    if text_path:
        transcript = build_transcript(entries, gap_minutes)
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(transcript + "\n" if transcript else "")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_cli(argv):
    parser = argparse.ArgumentParser(description="Transform Instagram message export")
    parser.add_argument("-i", "--input", required=True, help="Input JSON file")
    parser.add_argument("-o", "--output", required=True, help="Output JSON file")
    parser.add_argument(
        "-r", "--rename", action="append", default=[], metavar="OLD=NEW",
        help="Rename a participant (repeatable). Example: -r John=Jack -r Jane=Jill",
    )
    parser.add_argument(
        "-t", "--text-output", metavar="OUTPUT_TXT", default=None,
        help="Also write a human-readable transcript to this file",
    )
    parser.add_argument(
        "-m", "--minutes", type=int, default=30, metavar="MINUTES",
        help="Gap (in minutes) required before a new timestamp line is "
             "printed in the transcript. Default: 30",
    )
    parser.add_argument("--min-date", default="", metavar="YYYY-MM-DD",
                        help="Only include messages on or after this date")
    parser.add_argument("--max-date", default="", metavar="YYYY-MM-DD",
                        help="Only include messages on or before this date")
    args = parser.parse_args(argv)

    rename_map = parse_renames(args.rename, parser)
    write_outputs(args.input, args.output, args.text_output, rename_map,
                  args.minutes, args.min_date, args.max_date)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def _browse_input(input_var):
    from tkinter import filedialog
    path = filedialog.askopenfilename(
        title="Select Instagram export JSON",
        filetypes=[("JSON", "*.json"), ("All files", "*.*")],
    )
    if path:
        input_var.set(path)


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox

    # --- Bootstrap detection ---
    use_bootstrap = False
    style = None
    theme_names = []
    _TBDateEntry = None
    _ScrolledFrame = None

    try:
        from ttkbootstrap import Style as TBStyle
        try:
            from ttkbootstrap.widgets import DateEntry as _TBDateEntry  # noqa: F811
        except Exception:
            _TBDateEntry = None
        try:
            from ttkbootstrap.scrolled import ScrolledFrame as _ScrolledFrame  # noqa: F811
        except Exception:
            _ScrolledFrame = None

        style = TBStyle()
        theme_names = list(style.theme_names())
        default_theme = ("cyborg" if "cyborg" in theme_names
                         else (theme_names[0] if theme_names else None))
        if default_theme:
            try:
                style.theme_use(default_theme)
            except Exception:
                pass
        root = style.master
        use_bootstrap = True
        from tkinter import ttk
    except Exception:
        import tkinter as tk  # noqa: F811
        from tkinter import ttk
        root = tk.Tk()

    root.title("Instagram DM Reformat")
    root_width = 600
    root_height = 600
    root.minsize(root_width, root_height)

    # --- State ---
    input_var = tk.StringVar()
    save_json_var = tk.BooleanVar(value=True)
    save_txt_var = tk.BooleanVar(value=False)
    minutes_var = tk.IntVar(value=30)
    use_date_range_var = tk.BooleanVar(value=False)

    rename_entries = {}
    last_loaded_path = [None]

    # Root grid
    root.columnconfigure(0, weight=1)
    root.rowconfigure(3, weight=1)   # rename pane absorbs growth

    PAD = {"padx": 16, "pady": 12}    # outer padding
    IPAD_X = 10                        # inner horizontal padding

    # --- Theme menu (ttkbootstrap only) ---
    current_theme_var = tk.StringVar(
        value=(style.theme_use() if use_bootstrap else ""))

    if use_bootstrap and theme_names:
        menubar = tk.Menu(root)
        theme_menu = tk.Menu(menubar, tearoff=0)

        def _apply_theme(name):
            try:
                style.theme_use(name)
                current_theme_var.set(name)
            except Exception as e:
                messagebox.showerror(
                    "Theme", f"Could not apply theme {name!r}:\n{e}")

        for name in theme_names:
            theme_menu.add_radiobutton(
                label=name,
                variable=current_theme_var,
                value=name,
                command=lambda n=name: _apply_theme(n),
            )
        menubar.add_cascade(label="Theme", menu=theme_menu)
        root.config(menu=menubar)

    # --- Input row ---
    frame_in = ttk.Frame(root)
    frame_in.grid(row=0, column=0, sticky="ew", **PAD)
    frame_in.columnconfigure(1, weight=1)
    ttk.Label(frame_in, text="Input JSON:").grid(
        row=0, column=0, sticky="w", padx=(0, 10))
    ttk.Entry(frame_in, textvariable=input_var).grid(
        row=0, column=1, sticky="ew", padx=(0, 10))
    ttk.Button(frame_in, text="Browse…",
               command=lambda: _browse_input(input_var)).grid(row=0, column=2)

    # --- Output options ---
    frame_out = ttk.LabelFrame(root, text="Output")
    frame_out.grid(row=1, column=0, sticky="ew", **PAD)

    ttk.Checkbutton(frame_out, text="JSON",
                    variable=save_json_var).grid(
        row=0, column=0, sticky="w", padx=IPAD_X + 4, pady=(12, 6))
    ttk.Checkbutton(frame_out, text="TXT transcript",
                    variable=save_txt_var).grid(
        row=1, column=0, sticky="w", padx=IPAD_X + 4, pady=(0, 12))

    # Minutes control — to the right of the checkboxes; only shown when TXT is checked
    frame_min = ttk.Frame(frame_out)
    frame_min.grid(row=0, column=1, rowspan=2, sticky="w",
                padx=(24, IPAD_X + 4), pady=(12, 12))
    ttk.Label(frame_min,
            text="Minutes between timestamps in transcript:").pack(
        side="left", padx=(0, 10))
    ttk.Spinbox(frame_min, from_=1, to=100000,
                textvariable=minutes_var, width=8).pack(side="left")

    def update_minutes_visibility(*_):
        if save_txt_var.get():
            frame_min.grid()
        else:
            frame_min.grid_remove()

    save_txt_var.trace_add("write", update_minutes_visibility)
    update_minutes_visibility()

    # --- Date range ---
    frame_date = ttk.LabelFrame(root, text="Date range (optional)")
    frame_date.grid(row=2, column=0, sticky="ew", **PAD)

    ttk.Checkbutton(frame_date, text="Filter by date range",
                    variable=use_date_range_var).grid(
        row=0, column=0, columnspan=4, sticky="w",
        padx=IPAD_X + 4, pady=(12, 6))

    def _make_date_entry(parent):
        if _TBDateEntry is not None:
            try:
                de = _TBDateEntry(parent, width=12, dateformat="%Y-%m-%d")
                try:
                    de.delete(0, "end")
                except Exception:
                    try:
                        de.entry.delete(0, "end")
                    except Exception:
                        pass
                return de
            except Exception:
                pass
        return ttk.Entry(parent, width=12)

    def _date_value(w):
        try:
            v = w.get()
        except Exception:
            try:
                v = w.entry.get()
            except Exception:
                return ""
        return "" if v is None else str(v).strip()

    def _clear_date(w):
        try:
            w.delete(0, "end")
        except Exception:
            try:
                w.entry.delete(0, "end")
            except Exception:
                pass

    frame_date_inputs = ttk.Frame(frame_date)
    frame_date_inputs.grid(row=1, column=0, columnspan=4, sticky="w",
                        padx=IPAD_X + 4, pady=(6, 12))

    ttk.Label(frame_date_inputs, text="From (YYYY-MM-DD):").grid(
        row=0, column=0, sticky="w", padx=(0, 10), pady=(0, 6))
    min_date_widget = _make_date_entry(frame_date_inputs)
    min_date_widget.grid(row=0, column=1, sticky="w", padx=(0, 10), pady=(0, 6))
    ttk.Button(frame_date_inputs, text="Clear",
            command=lambda: _clear_date(min_date_widget)).grid(
        row=0, column=2, sticky="w", pady=(0, 6))

    ttk.Label(frame_date_inputs, text="To (YYYY-MM-DD):").grid(
        row=1, column=0, sticky="w", padx=(0, 10))
    max_date_widget = _make_date_entry(frame_date_inputs)
    max_date_widget.grid(row=1, column=1, sticky="w", padx=(0, 10))
    ttk.Button(frame_date_inputs, text="Clear",
            command=lambda: _clear_date(max_date_widget)).grid(
        row=1, column=2, sticky="w")
    
    def update_date_visibility(*_):
        if use_date_range_var.get():
            frame_date_inputs.grid()
        else:
            frame_date_inputs.grid_remove()

    use_date_range_var.trace_add("write", update_date_visibility)
    update_date_visibility()

    # --- Rename frame (scrollable) ---
    frame_ren = ttk.LabelFrame(root, text="Rename senders (optional)")
    frame_ren.grid(row=3, column=0, sticky="nsew", **PAD)
    frame_ren.columnconfigure(0, weight=1)
    frame_ren.rowconfigure(0, weight=1)

    rename_inner = None
    canvas = None

    if _ScrolledFrame is not None and use_bootstrap:
        try:
            sf = _ScrolledFrame(frame_ren, autohide_scrollbar=False)
            sf.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
            rename_inner = sf.container
        except Exception:
            _ScrolledFrame = None   # fall through to canvas

    if rename_inner is None:
        canvas = tk.Canvas(frame_ren, highlightthickness=0, borderwidth=0)
        vscroll = ttk.Scrollbar(frame_ren, orient="vertical",
                                command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        canvas.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        vscroll.grid(row=0, column=1, sticky="ns", padx=(0, 8), pady=8)
        rename_inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=rename_inner,
                                        anchor="nw")
        rename_inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfigure(inner_id, width=e.width))

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

    def rebuild_rename_list(senders):
        for child in rename_inner.winfo_children():
            child.destroy()
        rename_entries.clear()

        if not senders:
            ttk.Label(
                rename_inner,
                text="(Select an input file above to list senders here.)",
                foreground="gray",
            ).pack(padx=14, pady=14, anchor="w")
            return

        header = ttk.Frame(rename_inner)
        header.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Label(header, text="Original name", width=34,
                  anchor="w").pack(side="left")
        ttk.Label(header, text="→  New name (leave blank to keep)",
                  anchor="w").pack(side="left", padx=10)

        for s in senders:
            row = ttk.Frame(rename_inner)
            row.pack(fill="x", padx=10, pady=4)
            ttk.Label(row, text=s, width=34, anchor="w").pack(side="left")
            ttk.Label(row, text="→").pack(side="left", padx=6)
            e = ttk.Entry(row)
            e.pack(side="left", fill="x", expand=True, padx=8)
            rename_entries[s] = e

    rebuild_rename_list([])

    def load_senders_from_input(*_):
        path = input_var.get().strip()
        if path == last_loaded_path[0]:
            return
        if not path or not os.path.isfile(path):
            if last_loaded_path[0] is not None:
                rebuild_rename_list([])
                last_loaded_path[0] = None
            return
        try:
            senders = get_senders(path)
            rebuild_rename_list(senders)
            last_loaded_path[0] = path
        except Exception:
            pass

    input_var.trace_add("write", load_senders_from_input)

    # --- Save ---
    def on_save():
        input_path = input_var.get().strip()
        if not input_path:
            messagebox.showerror("Error", "Please select an input JSON file.")
            return
        if not os.path.isfile(input_path):
            messagebox.showerror("Error", f"Input file not found:\n{input_path}")
            return

        want_json = save_json_var.get()
        want_txt = save_txt_var.get()
        if not want_json and not want_txt:
            messagebox.showerror(
                "Error", "Check at least one output format (JSON or TXT).")
            return

        try:
            minutes = int(minutes_var.get())
            if minutes < 1:
                raise ValueError
        except (tk.TclError, ValueError):
            messagebox.showerror("Error", "Minutes must be a positive integer.")
            return

        rename_map = {}
        for s, e in rename_entries.items():
            new = e.get().strip()
            if new:
                rename_map[s] = new

        min_date_str = ""
        max_date_str = ""
        if use_date_range_var.get():
            min_date_str = _date_value(min_date_widget)
            max_date_str = _date_value(max_date_widget)
            try:
                parse_date(min_date_str)
                parse_date(max_date_str)
            except ValueError as ve:
                messagebox.showerror("Error", str(ve))
                return

        try:
            if want_json and want_txt:
                path = filedialog.asksaveasfilename(
                    title="Save ZIP containing JSON + TXT",
                    defaultextension=".zip",
                    filetypes=[("ZIP archive", "*.zip")],
                )
                if not path:
                    return
                with tempfile.TemporaryDirectory() as td:
                    jp = os.path.join(td, "messages.json")
                    tp = os.path.join(td, "transcript.txt")
                    write_outputs(input_path, jp, tp, rename_map, minutes,
                                  min_date_str, max_date_str)
                    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                        zf.write(jp, arcname="messages.json")
                        zf.write(tp, arcname="transcript.txt")
                messagebox.showinfo("Done", f"Saved ZIP:\n{path}")

            elif want_json:
                path = filedialog.asksaveasfilename(
                    title="Save JSON",
                    defaultextension=".json",
                    filetypes=[("JSON", "*.json")],
                )
                if not path:
                    return
                write_outputs(input_path, path, None, rename_map, minutes,
                              min_date_str, max_date_str)
                messagebox.showinfo("Done", f"Saved JSON:\n{path}")

            else:  # txt only
                path = filedialog.asksaveasfilename(
                    title="Save TXT transcript",
                    defaultextension=".txt",
                    filetypes=[("Text", "*.txt")],
                )
                if not path:
                    return
                write_outputs(input_path, None, path, rename_map, minutes,
                              min_date_str, max_date_str)
                messagebox.showinfo("Done", f"Saved TXT:\n{path}")

        except Exception as e:
            messagebox.showerror("Error", f"Failed to save:\n{e}")

    save_btn = ttk.Button(root, text="Save", command=on_save)
    save_btn.grid(row=4, column=0, pady=(14, 20))

    # --- Center window and force focus ---
    root.update_idletasks()
    w = root.winfo_reqwidth()
    h = root.winfo_reqheight()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 2)
    root.geometry(f"{w}x{h}+{x}+{y}")

    root.lift()
    root.attributes("-topmost", True)
    root.after_idle(lambda: root.attributes("-topmost", False))
    root.focus_force()
    root.after(50, lambda: root.focus_force())

    root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) <= 1:
        run_gui()
    else:
        run_cli(sys.argv[1:])


if __name__ == "__main__":
    main()
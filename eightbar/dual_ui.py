"""Arrange two source performances into a full song from validated AI JSON."""
from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .ai_arranger import AISettings, DEFAULT_MODEL
from .credentials import key_status, resolve_api_key
from .dual_ai import build_context, parse_plan, prompt_for_context, request_plan, validate_plan
from .dual_master import inspect_midi, export_dual
from .paths import output_base

BG = '#111820'
PANEL = '#19232e'
LINE = '#2b3b4b'
INK = '#e5edf5'
MUTED = '#98aabd'
GREEN = '#8be0a6'
ROLES = ('Instrument', 'Chords', 'Drums', 'Keep')
AI_MODE = 'AI full song'
MANUAL_MODE = 'Manual pattern order'


def _duration_label(bars, bpm):
    seconds = round(bars * 4 * 60 / bpm)
    return f'{bars} bars · about {seconds // 60}:{seconds % 60:02d} at {bpm:g} BPM'


class DualStudio:
    def __init__(self, root, classic_callback=None):
        self.root = root
        self.root.title('16 Bar Studio 4.5.1 · My Drum Style')
        self.root.geometry('1120x880')
        self.root.minsize(940, 780)
        self.root.configure(bg=BG)
        self.classic_callback = classic_callback
        self.inputs = {}
        self.roles = {'A': {}, 'B': {}}
        self.drum_labels = {'A': {}, 'B': {}}
        self.drum_map_buttons = {}
        self.file_labels = {}
        self.tables = {}
        self.role_vars = {}
        self.role_boxes = {}
        self.events = queue.Queue()
        self.busy = False
        self.latest = None
        self.last_result = None
        self.pasted_json = ''
        self._frozen = []
        self.mode = tk.StringVar(value=AI_MODE)
        self.sequence = tk.StringVar(value='A B AC BC')
        self.target_bars = tk.StringVar(value='64')
        self.direction = tk.StringVar(value='Intro, verses, rising choruses, bridge, final chorus, outro.')
        self.api_key = tk.StringVar(value='')
        self.api_key_status = tk.StringVar(value=key_status())
        self.api_key.trace_add('write', lambda *_: self._refresh_api_key_status())
        self.ai_model = tk.StringVar(value=DEFAULT_MODEL)
        self.expression_automation = tk.BooleanVar(value=True)
        self.drum_trimming = tk.BooleanVar(value=True)
        self.personal_style = tk.BooleanVar(value=True)
        self.drum_style = tk.BooleanVar(value=True)
        self.destination = tk.StringVar(value=str(output_base(Path(__file__).resolve().parents[1]) / 'Generated songs'))
        self.length_preview = tk.StringVar(value='Target: 64 bars · load A for duration')
        self.status = tk.StringVar(value='Load A, B and variation chords. AI JSON will arrange a complete song.')
        self._style()
        self._build()
        self.drum_trimming.trace_add('write', lambda *_: self._drum_style_ready())
        self.target_bars.trace_add('write', lambda *_: self._update_length())
        self.sequence.trace_add('write', lambda *_: self._update_length())
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.poll_id = self.root.after(100, self._poll)

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', background=BG, foreground=INK, font=('Segoe UI', 10))
        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG, foreground=INK)
        style.configure('Muted.TLabel', foreground=MUTED)
        style.configure('Title.TLabel', font=('Segoe UI Semibold', 24))
        style.configure('TButton', background=LINE, foreground=INK, padding=(11, 6))
        style.map('TButton', background=[('active', '#3b5064')], foreground=[('disabled', '#708090')])
        style.configure('Go.TButton', background=GREEN, foreground='#10271a', font=('Segoe UI Semibold', 12), padding=(16, 10))
        style.map('Go.TButton', background=[('active', '#b0f4c4'), ('disabled', '#31453a')])
        style.configure('TEntry', fieldbackground=LINE, foreground=INK, insertcolor=INK, padding=5)
        style.configure('TCombobox', fieldbackground=LINE, background=LINE, foreground=INK, padding=5)
        style.map('TCombobox', fieldbackground=[('readonly', LINE)], foreground=[('readonly', INK)])
        style.configure('TCheckbutton', background=BG, foreground=INK)
        style.configure('Treeview', background=PANEL, fieldbackground=PANEL, foreground=INK, rowheight=27)
        style.configure('Treeview.Heading', background=LINE, foreground=MUTED, padding=5)
        style.map('Treeview', background=[('selected', '#304b4c')])
        self.root.option_add('*TCombobox*Listbox.background', LINE)
        self.root.option_add('*TCombobox*Listbox.foreground', INK)

    def _build(self):
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill='both', expand=True)
        header = ttk.Frame(outer)
        header.pack(fill='x')
        ttk.Label(header, text='Your ideas. A complete song.', style='Title.TLabel').pack(side='left')
        if self.classic_callback:
            ttk.Button(header, text='Single-master tools…', command=self.classic_callback).pack(side='right')
        ttk.Label(outer, text='AI arranges your song with phrase handoffs, selective rests and musical dynamics.', style='Muted.TLabel').pack(anchor='w', pady=(4, 10))

        for key, title in (('A', 'Master A'), ('B', 'Master B'), ('C', 'Variation chords')):
            row = ttk.Frame(outer)
            row.pack(fill='x', pady=2)
            ttk.Label(row, text=title, width=17, font=('Segoe UI Semibold', 10)).pack(side='left')
            self.file_labels[key] = ttk.Label(row, text='Choose a MIDI file', style='Muted.TLabel')
            self.file_labels[key].pack(side='left', fill='x', expand=True, padx=8)
            ttk.Button(row, text='Browse…', command=lambda k=key: self.load_file(k)).pack(side='right')

        ttk.Label(outer, text='A / B = originals   ·   AC / BC = same performances following the variation chords.', style='Muted.TLabel', font=('Segoe UI', 9)).pack(anchor='w', pady=(6, 10))

        # Pack the action area before the flexible tables so the export button stays visible.
        footer = ttk.Frame(outer)
        footer.pack(side='bottom', fill='x', pady=(10, 0))
        self.export_button = ttk.Button(footer, text='Arrange full song with AI', style='Go.TButton', command=self.export, state='disabled')
        self.export_button.pack(side='right')
        self.open_button = ttk.Button(footer, text='Open results', command=self.open_results, state='disabled')
        self.open_button.pack(side='right', padx=8)
        ttk.Label(footer, textvariable=self.status, style='Muted.TLabel', wraplength=450).pack(side='left', fill='x', expand=True, padx=(0, 10))
        self.progress = ttk.Progressbar(outer, mode='indeterminate')
        self.progress.pack(side='bottom', fill='x', pady=(9, 0))

        options = ttk.Frame(outer)
        options.pack(side='bottom', fill='x', pady=(12, 0))
        mode_row = ttk.Frame(options)
        mode_row.pack(fill='x')
        ttk.Label(mode_row, text='Arrangement', width=13).pack(side='left')
        mode_box = ttk.Combobox(mode_row, textvariable=self.mode, values=(AI_MODE, MANUAL_MODE), state='readonly', width=21)
        mode_box.pack(side='left', padx=(0, 14))
        mode_box.bind('<<ComboboxSelected>>', self._mode_changed)
        ttk.Label(mode_row, text='Target bars').pack(side='left', padx=(0, 7))
        self.target_box = ttk.Combobox(mode_row, textvariable=self.target_bars, values=('64', '80', '96', '112', '128', '160', '192', '256'), width=6)
        self.target_box.pack(side='left')
        ttk.Label(mode_row, textvariable=self.length_preview, style='Muted.TLabel').pack(side='right')

        self.ai_options = ttk.Frame(options)
        self.ai_options.pack(fill='x', pady=(8, 0))
        ttk.Label(self.ai_options, text='Song direction', width=13).grid(row=0, column=0, sticky='w')
        ttk.Entry(self.ai_options, textvariable=self.direction).grid(row=0, column=1, columnspan=3, sticky='ew')
        ttk.Label(self.ai_options, text='OpenAI API key', width=13).grid(row=1, column=0, sticky='w', pady=(7, 0))
        ttk.Entry(self.ai_options, textvariable=self.api_key, show='•').grid(row=1, column=1, sticky='ew', pady=(7, 0))
        ttk.Label(self.ai_options, text='Model').grid(row=1, column=2, padx=(14, 7), pady=(7, 0))
        ttk.Entry(self.ai_options, textvariable=self.ai_model, width=16).grid(row=1, column=3, sticky='ew', pady=(7, 0))
        self.ai_options.columnconfigure(1, weight=1)
        ttk.Label(self.ai_options, textvariable=self.api_key_status, style='Muted.TLabel', font=('Segoe UI', 9)).grid(row=2, column=0, columnspan=4, sticky='w', pady=(5, 0))
        json_row = ttk.Frame(self.ai_options)
        json_row.grid(row=3, column=0, columnspan=4, sticky='ew', pady=(7, 0))
        self.copy_prompt_button = ttk.Button(json_row, text='Copy AI prompt', command=self.copy_ai_prompt, state='disabled')
        self.copy_prompt_button.pack(side='left')
        self.paste_json_button = ttk.Button(json_row, text='Paste AI JSON…', command=self.show_json_editor, state='disabled')
        self.paste_json_button.pack(side='left', padx=8)
        ttk.Checkbutton(json_row, text='Expression (CC11)', variable=self.expression_automation).pack(side='right')
        self.drum_trim_box = ttk.Checkbutton(json_row, text='AI drum trims', variable=self.drum_trimming)
        self.drum_trim_box.pack(side='right', padx=(0, 14))
        ttk.Label(self.ai_options, text='Full song includes melodic velocity changes and per-instrument velocity automation MIDI.', style='Muted.TLabel', font=('Segoe UI', 9)).grid(row=4, column=0, columnspan=4, sticky='w', pady=(5, 0))
        style_row = ttk.Frame(self.ai_options)
        style_row.grid(row=5, column=0, columnspan=4, sticky='ew', pady=(6, 0))
        self.personal_style_box = ttk.Checkbutton(style_row, text='My arrangement style', variable=self.personal_style)
        self.personal_style_box.pack(side='left')
        ttk.Label(style_row, text='Phrase handoffs and breathing room, based on your edited song.', style='Muted.TLabel', font=('Segoe UI', 9)).pack(side='left', padx=(12, 0))
        drum_style_row = ttk.Frame(self.ai_options)
        drum_style_row.grid(row=6, column=0, columnspan=4, sticky='ew', pady=(5, 0))
        self.drum_style_box = ttk.Checkbutton(drum_style_row, text='My drum style', variable=self.drum_style)
        self.drum_style_box.pack(side='left')
        ttk.Label(drum_style_row, text='Remove hits using your reference. Requires AI drum trims; never adds fills.',
                  style='Muted.TLabel', font=('Segoe UI', 9)).pack(side='left', padx=(12, 0))

        self.manual_options = ttk.Frame(options)
        ttk.Label(self.manual_options, text='Pattern order', width=13).pack(side='left')
        ttk.Entry(self.manual_options, textvariable=self.sequence).pack(side='left', fill='x', expand=True)
        ttk.Label(self.manual_options, text='  One pass of 8-bar A B AC BC = 32 bars', style='Muted.TLabel').pack(side='left')
        save_row = ttk.Frame(options)
        save_row.pack(side='bottom', fill='x', pady=(8, 0))
        ttk.Label(save_row, text='Save inside', width=13).pack(side='left')
        ttk.Entry(save_row, textvariable=self.destination).pack(side='left', fill='x', expand=True)
        ttk.Button(save_row, text='Choose folder…', command=self.choose_destination).pack(side='right', padx=(8, 0))

        tracks = ttk.Frame(outer)
        tracks.pack(fill='both', expand=True)
        tracks.columnconfigure(0, weight=1)
        tracks.columnconfigure(1, weight=1)
        tracks.rowconfigure(0, weight=1)
        for col, key in enumerate(('A', 'B')):
            pane = ttk.Frame(tracks)
            pane.grid(row=0, column=col, sticky='nsew', padx=(0, 15) if col == 0 else (0, 0))
            pane_header = ttk.Frame(pane)
            pane_header.pack(fill='x', pady=(0, 6))
            ttk.Label(pane_header, text=f'MASTER {key} · TRACK ROLES', style='Muted.TLabel', font=('Segoe UI Semibold', 9)).pack(side='left')
            self.drum_map_buttons[key] = ttk.Button(pane_header, text='Drum note map…',
                command=lambda k=key: self.show_drum_map(k), state='disabled')
            self.drum_map_buttons[key].pack(side='right')
            wrap = ttk.Frame(pane)
            table = ttk.Treeview(wrap, columns=('role', 'notes'), show='tree headings', height=5, selectmode='browse')
            table.heading('#0', text='Track')
            table.heading('role', text='Use as')
            table.heading('notes', text='Notes')
            table.column('#0', width=195, minwidth=90)
            table.column('role', width=94, minwidth=80, stretch=False)
            table.column('notes', width=60, stretch=False)
            table.pack(side='left', fill='both', expand=True)
            scroll = ttk.Scrollbar(wrap, orient='vertical', command=table.yview)
            scroll.pack(side='right', fill='y')
            table.configure(yscrollcommand=scroll.set)
            table.bind('<<TreeviewSelect>>', lambda event, k=key: self.select_role(k))
            self.tables[key] = table
            role_row = ttk.Frame(pane)
            role_row.pack(side='bottom', fill='x', pady=(8, 0))
            ttk.Label(role_row, text='Selected track').pack(side='left')
            var = tk.StringVar(value='Instrument')
            self.role_vars[key] = var
            box = ttk.Combobox(role_row, textvariable=var, values=ROLES, state='disabled', width=13)
            box.pack(side='right')
            box.bind('<<ComboboxSelected>>', lambda event, k=key: self.set_role(k))
            self.role_boxes[key] = box
            wrap.pack(fill='both', expand=True)
        self.role_help = ttk.Label(tracks, text='Instrument = follow chords  ·  Chords = chord MIDI  ·  Drums = AI can trim hits  ·  Keep = always intact.', style='Muted.TLabel', font=('Segoe UI', 9), justify='left')
        self.role_help.grid(row=1, column=0, columnspan=2, sticky='w', pady=(8, 0))

    def _drum_style_ready(self):
        if not self.busy:
            self.drum_style_box.configure(state='normal' if self.drum_trimming.get() else 'disabled')

    def load_file(self, key, path=None):
        if self.busy:
            return False
        if not path:
            path = filedialog.askopenfilename(parent=self.root, title=f'Choose {key} MIDI', filetypes=[('MIDI files', '*.mid *.midi'), ('All files', '*.*')])
        if not path:
            return False
        try:
            info = inspect_midi(path)
        except Exception as exc:
            messagebox.showerror('Could not open MIDI', str(exc), parent=self.root)
            return False
        self.inputs[key] = info
        self.file_labels[key].configure(text=f"{Path(path).name}   ·   {info['bars']} bars   ·   {info['bpm']:g} BPM")
        if key in self.tables:
            table = self.tables[key]
            table.delete(*table.get_children())
            self.roles[key] = {}
            for track in info['tracks']:
                index = int(track['index'])
                role = track.get('role', 'Instrument')
                self.roles[key][index] = role
                channels = ','.join(str(c + 1) for c in track.get('channels', []))
                label = track['name'] or f'Track {index + 1}'
                if channels:
                    label += f'  [ch {channels}]'
                table.insert('', 'end', iid=str(index), text=label, values=(role, track['note_count']))
            children = table.get_children()
            if children:
                table.selection_set(children[0])
                self.select_role(key)
            self.drum_labels[key] = {}
            self._refresh_drum_labels(key)
        warnings = info.get('warnings', [])
        self.status.set(f"Loaded {key}. " + (' '.join(warnings) if warnings else 'Review the chord and drum roles.'))
        self._update_length()
        self._update_ready()
        return True

    def select_role(self, key):
        selected = self.tables[key].selection()
        self.role_boxes[key].configure(state='readonly' if selected and not self.busy else 'disabled')
        if selected:
            self.role_vars[key].set(self.roles[key][int(selected[0])])

    def set_role(self, key):
        if self.busy:
            return
        selected = self.tables[key].selection()
        if not selected:
            return
        index = int(selected[0])
        role = self.role_vars[key].get()
        if role not in ROLES:
            return
        self.roles[key][index] = role
        self.tables[key].set(selected[0], 'role', role)
        self._refresh_drum_labels(key)
        self.status.set(f'Master {key}: track role updated. Keep stays intact; AI drum trims are optional.')

    def _refresh_drum_labels(self, key):
        from .drum_labels import starting_drum_labels
        defaults = starting_drum_labels(self.inputs[key]['path'], self.roles[key])
        prior = self.drum_labels[key]
        self.drum_labels[key] = {track: {pitch: prior.get(track, {}).get(pitch, label)
                                       for pitch, label in pitches.items()}
                                 for track, pitches in defaults.items()}
        self.drum_map_buttons[key].configure(state='normal' if defaults and not self.busy else 'disabled')

    def show_drum_map(self, key):
        if self.busy or key not in self.inputs:
            return
        from .drum_labels import DRUM_LABEL_ORDER
        self._refresh_drum_labels(key)
        if not self.drum_labels[key]:
            self.status.set(f'Master {key}: mark the drum track as Drums to label its note lines.')
            return
        draft = deepcopy(self.drum_labels[key])
        names = {t['index']: t['name'] for t in self.inputs[key]['tracks']}
        window = tk.Toplevel(self.root)
        window.title(f'Master {key} · Drum note map')
        window.geometry('670x580')
        window.minsize(540, 430)
        window.configure(bg=BG)
        window.transient(self.root)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Tell the AI what each drum note plays', font=('Segoe UI Semibold', 16)).pack(anchor='w')
        ttk.Label(frame, text='Starting order runs from lowest to highest MIDI pitch. Select a row, choose or type its sound, then Assign.\nThe AI can remove just that note line during a break. Keep tracks stay protected.',
                  style='Muted.TLabel', wraplength=610).pack(anchor='w', pady=(6, 12))
        actions = ttk.Frame(frame)
        actions.pack(side='bottom', fill='x', pady=(12, 0))
        edit = ttk.Frame(frame)
        edit.pack(side='bottom', fill='x', pady=(12, 0))
        label_var = tk.StringVar()
        ttk.Label(edit, text='Sound').pack(side='left', padx=(0, 8))
        sound_box = ttk.Combobox(edit, textvariable=label_var,
                                values=(*DRUM_LABEL_ORDER, 'Unassigned'), width=25)
        sound_box.pack(side='left', fill='x', expand=True)
        wrap = ttk.Frame(frame)
        wrap.pack(fill='both', expand=True)
        table = ttk.Treeview(wrap, columns=('pitch', 'sound'), show='tree headings', selectmode='browse')
        table.heading('#0', text='Drum track')
        table.heading('pitch', text='MIDI pitch')
        table.heading('sound', text='Sound')
        table.column('#0', width=190)
        table.column('pitch', width=90, stretch=False)
        table.column('sound', width=190)
        table.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(wrap, command=table.yview)
        scroll.pack(side='right', fill='y')
        table.configure(yscrollcommand=scroll.set)
        for track, pitches in draft.items():
            for pitch, label in pitches.items():
                table.insert('', 'end', iid=f'{track}:{pitch}', text=names[track], values=(pitch, label))

        def select(event=None):
            if table.selection():
                track, pitch = map(int, table.selection()[0].split(':'))
                label_var.set(draft[track][pitch])

        def assign():
            if not table.selection():
                return False
            label = label_var.get().strip()
            if not label or len(label) > 40 or any(ord(ch) < 32 or ord(ch) == 127 for ch in label):
                messagebox.showerror('Check drum name', 'Use a drum name from 1 to 40 characters on one line.', parent=window)
                return False
            track, pitch = map(int, table.selection()[0].split(':'))
            draft[track][pitch] = label
            table.set(table.selection()[0], 'sound', label)
            return True

        def apply():
            if not assign():
                return
            self.drum_labels[key] = draft
            self.status.set(f'Master {key}: drum note labels applied. AI drum trims can cut individual sounds.')
            window.destroy()

        ttk.Button(edit, text='Assign', command=assign).pack(side='right', padx=(8, 0))
        ttk.Button(actions, text='Cancel', command=window.destroy).pack(side='left')
        ttk.Button(actions, text='Use this drum map', style='Go.TButton', command=apply).pack(side='right')
        table.bind('<<TreeviewSelect>>', select)
        table.selection_set(table.get_children()[0])
        select()
        window.grab_set()
        # Expose widgets for the packaged acceptance test, without storing labels globally.
        window.drum_table = table
        window.drum_label_var = label_var
        window.assign_label = assign
        window.apply_map = apply
        return window

    def choose_destination(self):
        if self.busy:
            return
        folder = filedialog.askdirectory(parent=self.root, title='Save song exports inside')
        if folder:
            self.destination.set(folder)

    def _mode_changed(self, event=None):
        manual = self.mode.get() == MANUAL_MODE
        self.ai_options.pack_forget()
        self.manual_options.pack_forget()
        (self.manual_options if manual else self.ai_options).pack(fill='x', pady=(8, 0))
        self.target_box.configure(state='disabled' if manual else 'normal')
        self.export_button.configure(text='Export manual pattern order' if manual else 'Arrange full song with AI')
        self.status.set('Manual mode exports exactly the pattern order you enter.' if manual else 'AI JSON arranges a full song to the target length.')
        self._update_length()
        self._update_ready()

    def _update_length(self):
        manual = self.mode.get() == MANUAL_MODE
        try:
            if manual:
                tokens = self.sequence.get().strip().upper().replace(',', ' ').split()
                if not tokens or any(t not in ('A', 'B', 'AC', 'BC') for t in tokens):
                    raise ValueError
                bars = sum(self.inputs[t[0]]['bars'] for t in tokens)
            else:
                bars = int(self.target_bars.get())
                if not 40 <= bars <= 512:
                    raise ValueError
            if 'A' in self.inputs:
                self.length_preview.set(('Manual: ' if manual else 'Target: ') + _duration_label(bars, self.inputs['A']['bpm']))
            else:
                self.length_preview.set(f'Target: {bars} bars · load A for duration')
        except (ValueError, KeyError):
            self.length_preview.set('Load both masters for length' if manual else 'Choose 40–512 whole bars')

    def _update_ready(self):
        ready = all(k in self.inputs for k in ('A', 'B', 'C')) and not self.busy
        for button in (self.export_button, self.copy_prompt_button, self.paste_json_button):
            button.configure(state='normal' if ready else 'disabled')

    def _snapshot(self, ai=True):
        if self.busy or not all(k in self.inputs for k in ('A', 'B', 'C')):
            raise ValueError('Load Master A, Master B and the variation chord MIDI first.')
        destination = self.destination.get().strip()
        if not destination:
            raise ValueError('Choose where to save your song.')
        target = None
        if ai:
            try:
                target = int(self.target_bars.get())
            except ValueError as exc:
                raise ValueError('Target length must be a whole number from 40 to 512 bars.') from exc
            if not 40 <= target <= 512:
                raise ValueError('Target length must be from 40 to 512 bars.')
        return {'paths': [self.inputs[key]['path'] for key in ('A', 'B', 'C')],
                'roles_a': dict(self.roles['A']), 'roles_b': dict(self.roles['B']),
                'destination': destination, 'target_bars': target,
                'direction': self.direction.get().strip(),
                'expression_automation': bool(self.expression_automation.get()),
                'drum_trimming': bool(self.drum_trimming.get()),
                'personal_style': bool(self.personal_style.get()),
                'drum_style': bool(self.drum_style.get()),
                'drum_labels_a': deepcopy(self.drum_labels['A']),
                'drum_labels_b': deepcopy(self.drum_labels['B'])}

    @staticmethod
    def _context(snapshot):
        return build_context(*snapshot['paths'], snapshot['roles_a'], snapshot['roles_b'],
                             target_bars=snapshot['target_bars'], direction=snapshot['direction'],
                             drum_trimming=snapshot['drum_trimming'],
                             personal_style=snapshot.get('personal_style', False),
                             drum_style=snapshot.get('drum_style', False),
                             drum_labels_a=snapshot.get('drum_labels_a'),
                             drum_labels_b=snapshot.get('drum_labels_b'))

    def copy_ai_prompt(self):
        if self.busy:
            return False
        try:
            prompt = prompt_for_context(self._context(self._snapshot()))
            self.root.clipboard_clear()
            self.root.clipboard_append(prompt)
        except Exception as exc:
            messagebox.showerror('Could not prepare AI prompt', str(exc), parent=self.root)
            return False
        self.status.set('AI prompt copied. Send it to your AI, then use Paste AI JSON to export the complete song.')
        return True

    def show_json_editor(self):
        if self.busy or not all(k in self.inputs for k in ('A', 'B', 'C')):
            return
        window = tk.Toplevel(self.root)
        window.title('Paste AI JSON · Full song')
        window.geometry('850x630')
        window.minsize(650, 450)
        window.configure(bg=BG)
        window.transient(self.root)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Paste the full-song AI JSON', font=('Segoe UI Semibold', 18)).pack(anchor='w')
        ttk.Label(frame, text='Copy the prompt for your current MIDI files and target length. Paste the AI reply below.\nThe plan is checked before export; an API key is not needed for this route.', style='Muted.TLabel', wraplength=580).pack(anchor='w', pady=(5, 10))
        row = ttk.Frame(frame)
        row.pack(side='bottom', fill='x', pady=(12, 0))
        editor_wrap = ttk.Frame(frame)
        editor_wrap.pack(fill='both', expand=True)
        editor_wrap.columnconfigure(0, weight=1)
        editor_wrap.rowconfigure(0, weight=1)
        editor = tk.Text(editor_wrap, bg=PANEL, fg=INK, insertbackground=INK, font=('Consolas', 10), wrap='none', undo=True, relief='flat', padx=10, pady=10)
        editor.grid(row=0, column=0, sticky='nsew')
        vertical = ttk.Scrollbar(editor_wrap, orient='vertical', command=editor.yview)
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(editor_wrap, orient='horizontal', command=editor.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        editor.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        editor.insert('1.0', self.pasted_json)
        ttk.Button(row, text='Copy AI prompt', command=self.copy_ai_prompt).pack(side='left')

        def apply():
            self.pasted_json = editor.get('1.0', 'end-1c')
            if self.export_json(self.pasted_json):
                window.destroy()

        def dismiss():
            self.pasted_json = editor.get('1.0', 'end-1c')
            window.destroy()

        ttk.Button(row, text='Validate and export full song', style='Go.TButton', command=apply).pack(side='right')
        window.protocol('WM_DELETE_WINDOW', dismiss)
        editor.focus_set()
        return window

    def export_json(self, text):
        """Validate external JSON against current sources before starting export."""
        if self.busy:
            return False
        try:
            snapshot = self._snapshot()
            plan = parse_plan(text, self._context(snapshot))
        except Exception as exc:
            messagebox.showerror('Check AI JSON', str(exc), parent=self.root)
            return False
        self.pasted_json = text
        self._start_export(snapshot, plan=plan)
        return True

    def _refresh_api_key_status(self):
        self.api_key_status.set(key_status(self.api_key.get()))

    def export(self):
        if self.busy:
            return
        manual = self.mode.get() == MANUAL_MODE
        try:
            snapshot = self._snapshot(ai=not manual)
            if manual:
                tokens = self.sequence.get().strip().upper().replace(',', ' ').split()
                if not tokens or any(token not in ('A', 'B', 'AC', 'BC') for token in tokens):
                    raise ValueError('Use A, B, AC and BC separated by spaces. Example: A B A AC B BC.')
                sequence = ' '.join(tokens)
                self.sequence.set(sequence)
                self._start_export(snapshot, sequence=sequence)
            else:
                self._refresh_api_key_status()
                key = resolve_api_key(self.api_key.get()).value
                if not key:
                    raise ValueError('Enter your OpenAI API key, or use Copy AI prompt and Paste AI JSON to arrange the song with your AI chat.')
                settings = AISettings(api_key=key, model=self.ai_model.get().strip() or DEFAULT_MODEL,
                                      direction=snapshot['direction'])
                self._start_export(snapshot, settings=settings)
        except Exception as exc:
            messagebox.showerror('Could not start arrangement', str(exc), parent=self.root)

    def _start_export(self, snapshot, settings=None, plan=None, sequence=None):
        self.busy = True
        self._freeze(True)
        self.progress.start(12)
        self.status.set('Exporting your manual pattern order…' if sequence else
                        (f"Building your {plan['total_bars']}-bar song from validated AI JSON…" if plan else
                         f"AI is planning your {snapshot['target_bars']}-bar song. This can take a few minutes…"))

        def worker():
            try:
                validated = plan
                if sequence is None:
                    context = self._context(snapshot)
                    if validated is None:
                        validated = request_plan(context, settings)
                    validated = validate_plan(validated, context)
                    self.events.put(('planned', validated))
                    result = export_dual(*snapshot['paths'], snapshot['roles_a'], snapshot['roles_b'],
                                         snapshot['destination'], arrangement_plan=validated,
                                         target_bars=snapshot['target_bars'],
                                         expression_automation=snapshot['expression_automation'],
                                         drum_trimming=snapshot['drum_trimming'],
                                         personal_style=snapshot.get('personal_style', False),
                                         drum_style=snapshot.get('drum_style', False),
                                         drum_labels_a=snapshot.get('drum_labels_a'),
                                         drum_labels_b=snapshot.get('drum_labels_b'),
                                         expected_source_hashes={
                                             'A': context['masters']['A']['sha256'],
                                             'B': context['masters']['B']['sha256'],
                                             'C': context['chord_reference']['sha256']})
                else:
                    result = export_dual(*snapshot['paths'], snapshot['roles_a'], snapshot['roles_b'],
                                         snapshot['destination'], sequence=sequence)
                if not (Path(result['folder']) / 'Song.mid').is_file():
                    raise RuntimeError('Song.mid was not created. ' + ' '.join(result.get('warnings', [])))
                if validated:
                    result.setdefault('arrangement_plan', validated)
                    result.setdefault('song_bars', validated['total_bars'])
                self.events.put(('done', result))
            except Exception as exc:
                detail = str(exc)
                if settings and settings.api_key:
                    detail = detail.replace(settings.api_key, '[redacted]')
                self.events.put(('error', detail))
        threading.Thread(target=worker, daemon=True).start()

    def _freeze(self, active):
        if active:
            self._frozen = []
            def visit(widget):
                for child in widget.winfo_children():
                    if isinstance(child, (ttk.Button, ttk.Entry, ttk.Combobox, ttk.Checkbutton, ttk.Spinbox)):
                        self._frozen.append((child, child.state()))
                        child.state(['disabled'])
                    elif isinstance(child, tk.Text):
                        self._frozen.append((child, child.cget('state')))
                        child.configure(state='disabled')
                    visit(child)
            visit(self.root)
        else:
            for widget, state in self._frozen:
                if widget.winfo_exists():
                    if isinstance(widget, tk.Text):
                        widget.configure(state=state)
                    else:
                        widget.state(['!disabled'])
                        widget.state(state)
            self._frozen = []

    def _poll(self):
        try:
            while True:
                kind, result = self.events.get_nowait()
                if kind == 'planned':
                    bars = result['total_bars']
                    self.length_preview.set('Planned: ' + _duration_label(bars, self.inputs['A']['bpm']))
                    self.status.set(f"AI JSON validated: {bars} bars, {len(result['sections'])} sections. Creating song and automation MIDI…")
                    continue
                self.busy = False
                self.progress.stop()
                self._freeze(False)
                self._update_ready()
                if kind == 'done':
                    self.latest = Path(result['folder'])
                    self.last_result = result
                    self.open_button.configure(state='normal')
                    bars = result.get('song_bars')
                    if bars:
                        seconds = result.get('song_seconds')
                        detail = _duration_label(bars, self.inputs['A']['bpm'])
                        if seconds is not None:
                            seconds = round(seconds)
                            detail = f'{bars} bars · {seconds // 60}:{seconds % 60:02d}'
                        self.length_preview.set('Exported: ' + detail)
                        trimmed = result.get('drum_notes_trimmed', 0)
                        trim_note = f' · {trimmed} drum hits trimmed' if trimmed else ''
                        self.status.set(f'Song.mid is ready: {detail}{trim_note}.')
                    else:
                        self.status.set(result.get('summary', 'Your manual pattern order is ready.'))
                    self.show_result(result)
                else:
                    self.status.set('Arrangement stopped. ' + result)
                    messagebox.showerror('Could not arrange the song', result, parent=self.root)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.poll_id = self.root.after(100, self._poll)

    def show_result(self, result):
        window = tk.Toplevel(self.root)
        window.title('Your full song is ready')
        window.geometry('720x540')
        window.configure(bg=BG)
        window.transient(self.root)
        frame = ttk.Frame(window, padding=22)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Your song is ready', style='Title.TLabel').pack(anchor='w')
        ttk.Label(frame, text='Open Song.mid for the arrangement. Source patterns and automation MIDI are saved alongside it.', style='Muted.TLabel', wraplength=660).pack(anchor='w', pady=(4, 12))
        details = tk.Text(frame, bg=PANEL, fg=INK, font=('Segoe UI', 10), wrap='word', relief='flat', padx=12, pady=12)
        details.pack(fill='both', expand=True)
        lines = [result.get('summary', '')]
        plan = result.get('arrangement_plan')
        if result.get('song_bars'):
            lines.extend(['', _duration_label(result['song_bars'], self.inputs['A']['bpm'])])
        if result.get('drum_notes_trimmed'):
            lines.append(f"AI drum trims: {result['drum_notes_trimmed']} hits removed.")
            lines.append('Drum Removal Report.json shows the cuts for each drum line; retained hits are unchanged.')
        if result.get('personal_style'):
            lines.append(f"My arrangement style: {result.get('instrument_notes_removed', 0)} notes removed, "
                         f"{result.get('instrument_notes_shortened', 0)} shortened, "
                         f"{result.get('instrument_notes_retriggered', 0)} tails resumed.")
        if plan:
            lines.extend(['', plan.get('title', 'AI arrangement'), plan.get('summary', ''), '', 'Song sections:'])
            start = 1
            for section in plan['sections']:
                bars = self.inputs[section['pattern'][0]]['bars'] * section['repeats']
                lines.append(f"Bars {start}–{start + bars - 1}: {section['name']} · {section['pattern']} × {section['repeats']}")
                start += bars
        lines.extend(['', 'Files:'])
        lines.extend(Path(path).name for path in result.get('files', []))
        warnings = result.get('warnings', [])
        if warnings:
            lines.extend(['', 'Export notes:', *warnings])
        lines.extend(['', str(result['folder'])])
        details.insert('1.0', '\n'.join(lines))
        details.configure(state='disabled')
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=(15, 0))
        ttk.Button(buttons, text='Open results folder', command=self.open_results).pack(side='left')
        ttk.Button(buttons, text='Keep working', command=window.destroy).pack(side='right')

    def open_results(self):
        if self.latest and self.latest.is_dir():
            os.startfile(self.latest)

    def close(self):
        if self.busy:
            self.status.set('Song arrangement is running. You can close this window when it finishes.')
            return
        self.root.after_cancel(self.poll_id)
        self.root.destroy()

"""16 Bar Studio desktop entry point. python app.py [eight-or-sixteen-bar.mid]"""
from __future__ import annotations
from eightbar.frozen_worker import maybe_run_velocity_worker
maybe_run_velocity_worker()

import copy
import json
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from eightbar.model import ROLES, DRUM_ROLES, BAR, StudioError
from eightbar.midi_io import load_source
from eightbar.arranger import profiles, make_arrangement
from eightbar.space import apply_space
from eightbar.automation import (DENSITIES, SHAPES, AUTOMATION_STYLES, default_envelopes, preset_points,
    note_name, parse_note, velocity_at, midi_velocity, Envelope)
from eightbar.service import export_project
from eightbar.paths import output_base
from eightbar.ai_arranger import AISettings, DEFAULT_MODEL, make_ai_arrangement
from eightbar.credentials import key_status, resolve_api_key

BASE=output_base(Path(__file__).resolve().parent)
BG='#111820';PANEL='#19232e';INK='#e5edf5';MUTED='#98aabd';LINE='#2b3b4b';GREEN='#8be0a6';TEAL='#78cde0'

class Studio:
    def __init__(self,root):
        self.root=root;root.title('16 Bar Studio 4.5.1 · AI');root.geometry('1220x990');root.minsize(1030,920)
        root.configure(bg=BG)
        self.source=None;self.arrangement=None;self.original_arrangement=None;self.envelopes={};self.enabled={};self.selected_track=None
        self.protected_ids=set();self.protect=tk.BooleanVar(value=False)
        self.ai_ready=False;self.api_key=''  # Only a manually entered override lives here.
        self.ai_model=tk.StringVar(value=DEFAULT_MODEL)
        self.ai_direction=tk.StringVar(value='Make a spacious, expressive arrangement with clear sections, a strong hook, and room for vocals.')
        self.adjust_chords=tk.BooleanVar(value=True)
        self._load_ai_preferences()
        self.space=tk.StringVar(value='Medium');self.automation_style=tk.StringVar(value='Clean lines')
        self.selected_section=0;self.busy=False;self.drag_index=None;self.latest=None
        self.events=queue.Queue();self.profile_list=profiles();self.profile_by_name={p['name']:p for p in self.profile_list}
        self.family=tk.StringVar(value=next(p['name'] for p in self.profile_list if p['id']=='rap'))
        self.style=tk.StringVar();self.length=tk.StringVar(value='Full song');self.variation=tk.StringVar(value='Balanced')
        self.bpm=tk.StringVar(value='130');self.seed=2026;self.density=tk.StringVar(value='2')
        self.role=tk.StringVar();self.auto_enabled=tk.BooleanVar(value=True)
        self.pitch=tk.StringVar(value='C5');self.shape=tk.StringVar(value='Intro rise')
        self.low=tk.StringVar(value='0');self.high=tk.StringVar(value='100')
        self.status=tk.StringVar(value='Open an 8- or 16-bar MIDI. Place each Layer’s empty drum lanes directly after it, in note order from C5.')
        self._style();self._build();self._styles_changed();self.poll_id=self.root.after(100,self._poll)
        self.root.protocol('WM_DELETE_WINDOW',self._close)

    def _style(self):
        s=ttk.Style();s.theme_use('clam')
        s.configure('.',background=BG,foreground=INK,font=('Segoe UI',10))
        s.configure('TFrame',background=BG);s.configure('Panel.TFrame',background=PANEL)
        s.configure('TLabel',background=BG,foreground=INK);s.configure('Muted.TLabel',foreground=MUTED)
        s.configure('Panel.TLabel',background=PANEL);s.configure('Title.TLabel',font=('Segoe UI Semibold',23))
        s.configure('TButton',background=LINE,foreground=INK,padding=(12,7),borderwidth=0)
        s.map('TButton',background=[('active','#3b5064'),('disabled','#202932')],foreground=[('disabled','#637281')])
        s.configure('Go.TButton',background=GREEN,foreground='#10271a',font=('Segoe UI Semibold',12),padding=(24,12))
        s.map('Go.TButton',background=[('active','#b0f4c4'),('disabled','#31453a')])
        s.configure('TCombobox',fieldbackground=LINE,background=LINE,foreground=INK,arrowcolor=INK,padding=5)
        s.map('TCombobox',fieldbackground=[('readonly',LINE)],foreground=[('readonly',INK)])
        s.configure('TEntry',fieldbackground=LINE,foreground=INK,insertcolor=INK,padding=5)
        s.configure('Treeview',background=PANEL,fieldbackground=PANEL,foreground=INK,rowheight=29,borderwidth=0)
        s.configure('Treeview.Heading',background=LINE,foreground=MUTED,font=('Segoe UI Semibold',9),padding=5)
        s.map('Treeview',background=[('selected','#304b4c')],foreground=[('selected',INK)])
        s.configure('TCheckbutton',background=BG,foreground=INK)
        self.root.option_add('*TCombobox*Listbox.background',LINE);self.root.option_add('*TCombobox*Listbox.foreground',INK)

    def _build(self):
        outer=ttk.Frame(self.root,padding=18);outer.pack(fill='both',expand=True)
        header=ttk.Frame(outer);header.pack(fill='x')
        ttk.Label(header,text='16 Bar Studio',style='Title.TLabel').pack(side='left')
        ttk.Label(header,text='ARRANGE  /  SHAPE VELOCITY  /  AUTOMATE',style='Muted.TLabel',font=('Segoe UI',9)).pack(side='right',pady=9)
        ttk.Label(outer,text='AI arranges your performance. Existing chords can change; melodies and drum pitches stay yours.',style='Muted.TLabel').pack(anchor='w',pady=(4,10))
        filebar=ttk.Frame(outer);filebar.pack(fill='x')
        self.file_label=ttk.Label(filebar,text='No MIDI loaded',font=('Segoe UI Semibold',11));self.file_label.pack(side='left',fill='x',expand=True)
        ttk.Button(filebar,text='Open MIDI…',command=self.open_file).pack(side='left',padx=6)
        ttk.Button(filebar,text='AI settings…',command=self.ai_settings).pack(side='left')
        ttk.Button(filebar,text='Two masters…',command=self.open_dual).pack(side='left',padx=6)
        airow=ttk.Frame(outer);airow.pack(fill='x',pady=(10,0))
        ttk.Label(airow,text='Direction',style='Muted.TLabel').pack(side='left')
        self.direction_entry=ttk.Entry(airow,textvariable=self.ai_direction)
        self.direction_entry.pack(side='left',fill='x',expand=True,padx=8)
        self.ai_direction.trace_add('write',self._direction_changed)
        ttk.Checkbutton(airow,text='Adjust existing chords',variable=self.adjust_chords,command=self._refresh_plan).pack(side='left')
        settings=ttk.Frame(outer);settings.pack(fill='x',pady=(10,10))
        self.family_box=self._field(settings,'Genre',self.family,list(self.profile_by_name),0,25,self._styles_changed)
        self.style_box=self._field(settings,'Style',self.style,[],1,20,self._refresh_plan)
        self._field(settings,'Length',self.length,['Full song','Short song'],2,16,self._refresh_plan)
        self._field(settings,'Variation',self.variation,['Faithful','Balanced','Bold'],3,12,self._refresh_plan)
        f=ttk.Frame(settings);f.grid(row=0,column=4,padx=(0,10),sticky='w')
        ttk.Label(f,text='BPM',style='Muted.TLabel').pack(anchor='w');e=ttk.Entry(f,textvariable=self.bpm,width=7);e.pack()
        e.bind('<FocusOut>',self._refresh_plan);e.bind('<Return>',self._refresh_plan)
        ttk.Button(settings,text='New variation',command=self.new_variation).grid(row=0,column=5,sticky='s')
        finishing=ttk.Frame(outer);finishing.pack(fill='x',pady=(0,14))
        ttk.Label(finishing,text='Space',style='Muted.TLabel').pack(side='left')
        space_box=ttk.Combobox(finishing,textvariable=self.space,values=('Off','Light','Medium','Strong'),state='readonly',width=10)
        space_box.pack(side='left',padx=(8,20));space_box.bind('<<ComboboxSelected>>',self._refresh_plan)
        ttk.Label(finishing,text='Automation',style='Muted.TLabel').pack(side='left')
        auto_box=ttk.Combobox(finishing,textvariable=self.automation_style,values=AUTOMATION_STYLES,state='readonly',width=14)
        auto_box.pack(side='left',padx=8);auto_box.bind('<<ComboboxSelected>>',self.reset_automation)
        ttk.Label(finishing,text='Space guides the AI’s phrase rests. No new musical notes are composed.',style='Muted.TLabel',font=('Segoe UI',9)).pack(side='left',padx=8)
        body=ttk.Frame(outer);body.pack(fill='both',expand=True);body.columnconfigure(1,weight=1);body.rowconfigure(0,weight=1)
        left=ttk.Frame(body);left.grid(row=0,column=0,sticky='nsew',padx=(0,18))
        ttk.Label(left,text='YOUR INSTRUMENTS',style='Muted.TLabel',font=('Segoe UI Semibold',9)).pack(anchor='w',pady=(0,8))
        self.tracks=ttk.Treeview(left,columns=('role','auto'),show='tree headings',height=10,selectmode='browse')
        self.tracks.heading('#0',text='Name');self.tracks.heading('role',text='Role');self.tracks.heading('auto',text='Auto')
        self.tracks.column('#0',width=155,minwidth=100);self.tracks.column('role',width=88,minwidth=60);self.tracks.column('auto',width=42,stretch=False)
        self.tracks.pack(fill='both',expand=True);self.tracks.bind('<<TreeviewSelect>>',self.select_track)
        ttk.Label(left,text='Selected instrument',style='Muted.TLabel').pack(anchor='w',pady=(12,4))
        self.role_box=ttk.Combobox(left,textvariable=self.role,values=ROLES,state='readonly');self.role_box.pack(fill='x');self.role_box.bind('<<ComboboxSelected>>',self.set_role)
        self.map_info=ttk.Frame(left)
        self.map_label=ttk.Label(self.map_info,text='',style='Muted.TLabel');self.map_label.pack(anchor='w',pady=(7,4))
        ttk.Button(self.map_info,text='View drum map…',command=self.show_drum_map).pack(fill='x')
        self.map_anchor=ttk.Frame(left);self.map_anchor.pack(fill='x')
        ttk.Checkbutton(left,text='Generate automation for this part',variable=self.auto_enabled,command=self.toggle_automation).pack(anchor='w',pady=10)
        ttk.Checkbutton(left,text='Keep this part throughout the song',variable=self.protect,command=self.toggle_protection).pack(anchor='w',pady=(0,10))
        ttk.Button(left,text='Use these curves for all parts',command=self.copy_curves).pack(fill='x')
        ttk.Button(left,text='Reset this part’s curves',command=lambda:self.reset_automation(selected_only=True)).pack(fill='x',pady=(6,0))
        ttk.Label(left,text='Velocity shaping: automatic\nApplied to music on every generation.',style='Muted.TLabel',wraplength=260).pack(anchor='w',pady=(16,0))
        right=ttk.Frame(body);right.grid(row=0,column=1,sticky='nsew');right.columnconfigure(0,weight=1);right.rowconfigure(5,weight=1)
        self.lane_label=ttk.Label(right,text='SECTION AUTOMATION',style='Muted.TLabel',font=('Segoe UI Semibold',9));self.lane_label.grid(row=0,column=0,sticky='w',pady=(0,8))
        section_wrap=ttk.Frame(right);section_wrap.grid(row=1,column=0,sticky='ew');section_wrap.columnconfigure(0,weight=1)
        self.sections=ttk.Treeview(section_wrap,columns=('bars','pitch','shape'),show='tree headings',height=3,selectmode='browse')
        self.sections.heading('#0',text='Section');self.sections.heading('bars',text='Bars');self.sections.heading('pitch',text='Note');self.sections.heading('shape',text='Velocity shape')
        self.sections.column('#0',width=155);self.sections.column('bars',width=70,stretch=False);self.sections.column('pitch',width=58,stretch=False);self.sections.column('shape',width=155)
        self.sections.grid(row=0,column=0,sticky='nsew');scroll=ttk.Scrollbar(section_wrap,orient='vertical',command=self.sections.yview);scroll.grid(row=0,column=1,sticky='ns');self.sections.configure(yscrollcommand=scroll.set)
        self.sections.bind('<<TreeviewSelect>>',self.select_section)
        self.overview=tk.Canvas(right,bg='#101a24',height=78,highlightthickness=1,highlightbackground=LINE)
        self.overview.grid(row=2,column=0,sticky='ew',pady=(8,0));self.overview.bind('<Configure>',lambda e:self.draw_overview())
        self.overview.bind('<Button-1>',self.select_overview)
        controls=ttk.Frame(right);controls.grid(row=3,column=0,sticky='ew',pady=(10,8))
        for col,(label,var,width) in enumerate((('Section note',self.pitch,8),('Low %',self.low,6),('High %',self.high,6))):
            f=ttk.Frame(controls);f.grid(row=0,column=col,padx=(0,10),sticky='w');ttk.Label(f,text=label,style='Muted.TLabel').pack(anchor='w')
            entry=ttk.Entry(f,textvariable=var,width=width);entry.pack();entry.bind('<Return>',self.apply_shape)
        self._field(controls,'Shape',self.shape,SHAPES,3,17,None)
        ttk.Button(controls,text='Apply',command=self.apply_shape).grid(row=0,column=4,sticky='s')
        detail=ttk.Frame(right);detail.grid(row=4,column=0,sticky='ew',pady=(0,6))
        ttk.Label(detail,text='Click to add a corner · drag to move · right-click to remove',style='Muted.TLabel',font=('Segoe UI',9)).pack(side='left')
        ttk.Button(detail,text='Exact points…',command=self.edit_points).pack(side='right')
        self.canvas=tk.Canvas(right,bg='#101a24',highlightthickness=1,highlightbackground=LINE,height=250)
        self.canvas.grid(row=5,column=0,sticky='nsew');self.canvas.bind('<Configure>',lambda e:self.draw())
        self.canvas.bind('<Button-1>',self.canvas_down);self.canvas.bind('<B1-Motion>',self.canvas_drag);self.canvas.bind('<ButtonRelease-1>',self.canvas_up);self.canvas.bind('<Button-3>',self.canvas_delete)
        bottom=ttk.Frame(right);bottom.grid(row=6,column=0,sticky='ew',pady=(8,0))
        ttk.Label(bottom,text='Notes per beat',style='Muted.TLabel').pack(side='left')
        density_box=ttk.Combobox(bottom,textvariable=self.density,values=DENSITIES,state='readonly',width=6);density_box.pack(side='left',padx=8);density_box.bind('<<ComboboxSelected>>',lambda e:self.draw())
        self.preview_label=ttk.Label(bottom,text='Pitch holds. Velocity moves.',style='Muted.TLabel',font=('Segoe UI',9));self.preview_label.pack(side='left')
        footer=ttk.Frame(outer);footer.pack(fill='x',pady=(18,0))
        self.status_label=ttk.Label(footer,textvariable=self.status,style='Muted.TLabel',wraplength=750);self.status_label.pack(side='left',fill='x',expand=True,padx=(0,15))
        self.generate_button=ttk.Button(footer,text='Export MIDI',command=self.generate,state='disabled');self.generate_button.pack(side='right')
        self.ai_button=ttk.Button(footer,text='Arrange with AI',style='Go.TButton',command=self.arrange_ai,state='disabled');self.ai_button.pack(side='right',padx=(0,8))
        self.progress=ttk.Progressbar(outer,mode='indeterminate');self.progress.pack(fill='x',pady=(10,0))
        # Reserve the action area before allowing the resizable editor to take
        # the remaining height; the Generate button must never be clipped.
        body.pack_forget();footer.pack_forget();self.progress.pack_forget()
        self.progress.pack(side='bottom',fill='x',pady=(10,0))
        footer.pack(side='bottom',fill='x',pady=(18,0))
        body.pack(fill='both',expand=True)
        try:
            self.root.drop_target_register('DND_Files');self.root.dnd_bind('<<Drop>>',lambda e:self.drop(e.data))
        except (AttributeError,tk.TclError):pass

    def open_dual(self):
        if self.busy:return
        from eightbar.dual_ui import DualStudio
        window=tk.Toplevel(self.root)
        window._studio=DualStudio(window)

    def _field(self,parent,label,var,values,col,width,callback):
        frame=ttk.Frame(parent);frame.grid(row=0,column=col,padx=(0,10),sticky='w')
        ttk.Label(frame,text=label,style='Muted.TLabel').pack(anchor='w')
        box=ttk.Combobox(frame,textvariable=var,values=values,state='readonly',width=width);box.pack()
        if callback:box.bind('<<ComboboxSelected>>',callback)
        return box

    def _load_ai_preferences(self):
        try:
            saved=json.loads((BASE/'AI preferences.json').read_text(encoding='utf-8'))
            if isinstance(saved.get('model'),str):self.ai_model.set(saved['model'][:100])
            if isinstance(saved.get('direction'),str):self.ai_direction.set(saved['direction'][:2000])
            if type(saved.get('adjust_chords')) is bool:self.adjust_chords.set(saved['adjust_chords'])
        except (OSError,ValueError,TypeError,AttributeError):pass

    def _save_ai_preferences(self):
        # Only these non-secret fields are persisted. The key stays in memory.
        BASE.mkdir(parents=True,exist_ok=True)
        (BASE/'AI preferences.json').write_text(json.dumps({
            'model':self.ai_model.get(),'direction':self.ai_direction.get(),
            'adjust_chords':self.adjust_chords.get()},indent=2),encoding='utf-8')

    def _direction_changed(self,*args):
        if self.busy:return
        self.ai_ready=False
        if hasattr(self,'generate_button'):self.generate_button.configure(state='disabled')
        if self.source:self.status.set('Direction changed. Arrange with AI to preview the updated song.')

    def ai_settings(self):
        if self.busy:return
        win=tk.Toplevel(self.root);win.title('AI settings');win.geometry('560x420');win.minsize(560,420);win.configure(bg=BG);win.transient(self.root)
        frame=ttk.Frame(win,padding=20);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='OpenAI connection',font=('Segoe UI Semibold',16)).pack(anchor='w')
        connection_status=tk.StringVar(value=key_status(self.api_key))
        ttk.Label(frame,textvariable=connection_status,style='Muted.TLabel',wraplength=510).pack(anchor='w',pady=(8,12))
        ttk.Label(frame,text='API key override · leave blank to use this PC’s key').pack(anchor='w')
        key=tk.StringVar(value=self.api_key);ttk.Entry(frame,textvariable=key,show='•').pack(fill='x',pady=(4,12))
        key.trace_add('write',lambda *_:connection_status.set(key_status(key.get())))
        ttk.Label(frame,text='Model').pack(anchor='w')
        model=tk.StringVar(value=self.ai_model.get())
        ttk.Combobox(frame,textvariable=model,values=(DEFAULT_MODEL,'gpt-5.4-mini'),width=32).pack(anchor='w',pady=(4,12))
        ttk.Label(frame,text='Arrange with AI sends MIDI note information and your direction to OpenAI. API usage is billed to your API account. Keys entered here are kept only until the app closes.',style='Muted.TLabel',wraplength=510).pack(anchor='w')
        def save():
            import re
            value=model.get().strip()
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,99}',value):
                messagebox.showerror('Check model','Enter a valid model name.',parent=win);return
            candidate=key.get().strip()
            if candidate and any(c.isspace() for c in candidate):
                messagebox.showerror('Check API key','The API key must not contain spaces or line breaks.',parent=win);return
            self.api_key=candidate
            self.ai_model.set(value)
            try:self._save_ai_preferences()
            except OSError:pass
            self._direction_changed();win.destroy()
            self.status.set('AI settings updated. Ready to arrange.' if resolve_api_key(self.api_key).value else 'Add an API key in AI settings to arrange.')
        ttk.Button(frame,text='Save settings',command=save).pack(anchor='e',pady=(12,0))

    def _styles_changed(self,event=None):
        p=self.profile_by_name[self.family.get()];names=[v['name'] for v in p['styles'].values()]
        self.style_box.configure(values=names)
        if self.style.get() not in names:self.style.set(names[0])
        self._refresh_plan()

    def drop(self,data):
        paths=self.root.tk.splitlist(data)
        if len(paths)!=1:messagebox.showinfo('One MIDI','Drop one MIDI file containing all your instrument tracks.');return
        self.open_file(paths[0])

    def open_file(self,path=None):
        if self.busy:return
        if path is None:path=filedialog.askopenfilename(title='Open your 8- or 16-bar MIDI',filetypes=[('MIDI files','*.mid *.midi')])
        if not path:return
        try:source=load_source(path)
        except Exception as exc:messagebox.showerror('Could not open MIDI',str(exc));return
        self.source=source;self.arrangement=None;self.original_arrangement=None;self.envelopes={};self.selected_track=None;self.protected_ids=set()
        self.enabled={t.id:t.role!='skip' for t in source.tracks};self.bpm.set(f'{source.bpm:g}')
        self.file_label.configure(text=Path(path).name+f'  ·  {source.bars} bars  ·  {len(source.tracks)} instruments')
        for item in self.tracks.get_children():self.tracks.delete(item)
        for t in source.tracks:self.tracks.insert('', 'end',iid=t.id,text=t.name,values=('Layer drums' if getattr(t,'drum_map',{}) else t.role,'Yes' if self.enabled[t.id] else '—'))
        self.tracks.selection_set(source.tracks[0].id);self.selected_track=source.tracks[0].id
        self.select_track();self._refresh_plan()
        if source.warnings:self.status.set(' '.join(source.warnings))

    def _refresh_plan(self,event=None):
        if not self.source or self.busy:return
        self.ai_ready=False
        self.generate_button.configure(state='disabled')
        try:
            profile=self.profile_by_name[self.family.get()]
            sid=next(k for k,v in profile['styles'].items() if v['name']==self.style.get())
            plan=make_arrangement(self.source,profile['id'],sid,{'Full song':'full','Short song':'compact','Original loop':'source','Eight-bar check':'source'}[self.length.get()],self.variation.get(),self.seed,float(self.bpm.get()))
            self.original_arrangement=plan
            plan=apply_space(plan,self.space.get(),self.protected_ids)
            previous=self.arrangement
            same=previous and [(s.kind,s.bars) for s in previous.sections]==[(s.kind,s.bars) for s in plan.sections]
            self.arrangement=plan
            for track in self.source.tracks:
                if not same or track.id not in self.envelopes:self.envelopes[track.id]=default_envelopes(plan,track,self.automation_style.get())
            self.ai_button.configure(state='normal');self._populate_sections()
            minutes=plan.bars*4/plan.bpm
            before=sum(len(t.notes) for t in self.original_arrangement.tracks);after=sum(len(t.notes) for t in plan.tracks)
            self.status.set(f'{plan.bars} bars · {int(minutes)}:{round((minutes%1)*60):02d} · Section template ready. Click Arrange with AI to create the musical arrangement.')
        except Exception as exc:
            self.arrangement=None;self.ai_button.configure(state='disabled');self.generate_button.configure(state='disabled');self.status.set(str(exc));self._populate_sections()

    def select_track(self,event=None):
        if not self.source:return
        selection=self.tracks.selection()
        if not selection:return
        self.selected_track=selection[0];t=next(t for t in self.source.tracks if t.id==self.selected_track)
        mapped=getattr(t,'drum_map',{})
        self.role.set('Layer drums' if mapped else t.role);self.role_box.configure(state='disabled' if mapped or self.busy else 'readonly')
        if mapped:
            self.map_label.configure(text=f'{len(mapped)} drum parts detected from empty lanes')
            self.map_info.pack(fill='x',before=self.map_anchor)
        else:self.map_info.pack_forget()
        self.auto_enabled.set(self.enabled.get(t.id,False));self.lane_label.configure(text='AUTOMATION  /  '+t.name)
        self.protect.set(t.id in self.protected_ids)
        self._populate_sections()

    def set_role(self,event=None):
        if self.busy or not self.source or not self.selected_track:return
        track=next(t for t in self.source.tracks if t.id==self.selected_track)
        if getattr(track,'drum_map',{}):self.role.set('Layer drums');return
        track.role=self.role.get()
        if track.role=='skip':
            self.enabled[track.id]=False;self.auto_enabled.set(False)
            self.protected_ids.discard(track.id);self.protect.set(False)
        self.tracks.set(track.id,'role',track.role);self.tracks.set(track.id,'auto','Yes' if self.enabled[track.id] else '—')
        self._refresh_plan()

    def show_drum_map(self):
        if not self.source or not self.selected_track or self.busy:return
        track=next(t for t in self.source.tracks if t.id==self.selected_track)
        mapping=getattr(track,'drum_map',{})
        if not mapping:return
        names=getattr(track,'drum_names',{})
        win=tk.Toplevel(self.root);win.title(track.name+' — drum map');win.geometry('610x450');win.minsize(610,450);win.configure(bg=BG);win.transient(self.root)
        ttk.Label(win,text=track.name+' · detected drum parts',font=('Segoe UI Semibold',14),padding=(18,15)).pack(anchor='w')
        ttk.Label(win,text='Empty lanes map upward from C5 in file order. Each part is arranged independently; its pitch stays fixed.',style='Muted.TLabel',wraplength=550,padding=(18,0)).pack(anchor='w')
        frame=ttk.Frame(win,padding=(18,12));frame.pack(fill='both',expand=True)
        table=ttk.Treeview(frame,columns=('note','name','role'),show='headings',height=7)
        for key,label,width in (('note','Layer note',82),('name','Empty lane name',230),('role','Detected role',130)):
            table.heading(key,text=label);table.column(key,width=width,minwidth=65)
        scroll=ttk.Scrollbar(frame,orient='vertical',command=table.yview);table.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y');table.pack(side='left',fill='both',expand=True)
        for pitch,role in sorted(mapping.items()):
            table.insert('','end',values=(note_name(pitch),names.get(pitch,'Unnamed lane'),role.replace('_',' ')))
        ttk.Label(win,text='Only the Layer carries drum notes into your song. Empty reference lanes are removed. No new drum hits are created.',style='Muted.TLabel',wraplength=550,padding=(18,0)).pack(anchor='w')
        ttk.Button(win,text='Close',command=win.destroy).pack(anchor='e',padx=18,pady=12)

    def toggle_automation(self):
        if not self.selected_track or self.busy:return
        self.enabled[self.selected_track]=self.auto_enabled.get();self.tracks.set(self.selected_track,'auto','Yes' if self.auto_enabled.get() else '—')

    def toggle_protection(self):
        if not self.selected_track or self.busy:return
        if self.protect.get():self.protected_ids.add(self.selected_track)
        else:self.protected_ids.discard(self.selected_track)
        self._refresh_plan()

    def reset_automation(self,event=None,selected_only=False):
        if not self.arrangement or self.busy:return
        for track in self.source.tracks:
            if not selected_only or track.id==self.selected_track:
                self.envelopes[track.id]=default_envelopes(self.arrangement,track,self.automation_style.get())
        self._populate_sections()
        self.status.set('Applied '+self.automation_style.get()+' defaults to '+('this part.' if selected_only else 'all parts.'))

    def _populate_sections(self):
        for row in self.sections.get_children():self.sections.delete(row)
        if not self.arrangement or not self.selected_track:self.draw();return
        for i,(s,e) in enumerate(zip(self.arrangement.sections,self.envelopes[self.selected_track])):
            self.sections.insert('','end',iid=str(i),text=s.name,values=(f'{s.start_bar+1}–{s.end_bar}',note_name(e.pitch),e.shape))
        self.selected_section=min(self.selected_section,len(self.arrangement.sections)-1)
        self.sections.selection_set(str(self.selected_section));self.sections.see(str(self.selected_section));self.select_section()

    def current(self):
        if not self.arrangement or not self.selected_track:return None
        return self.envelopes[self.selected_track][self.selected_section]

    def select_section(self,event=None):
        selected=self.sections.selection()
        if not selected:return
        self.selected_section=int(selected[0]);env=self.current()
        if not env:return
        self.pitch.set(note_name(env.pitch));self.shape.set(env.shape)
        self.low.set(f'{min(y for x,y in env.points)*100:g}');self.high.set(f'{max(y for x,y in env.points)*100:g}');self.draw()

    def apply_shape(self,event=None):
        env=self.current()
        if not env or self.busy:return False
        try:
            pitch=parse_note(self.pitch.get());shape=self.shape.get()
            points=env.points if shape=='Custom' else preset_points(shape,float(self.low.get())/100,float(self.high.get())/100)
            env.pitch=pitch;env.points=points;env.shape=shape;self._update_section_row();self.draw();return True
        except Exception as exc:messagebox.showerror('Check the envelope',str(exc));return False

    def _update_section_row(self):
        env=self.current()
        if env and self.sections.exists(str(self.selected_section)):
            self.sections.set(str(self.selected_section),'pitch',note_name(env.pitch));self.sections.set(str(self.selected_section),'shape',env.shape)

    def copy_curves(self):
        if not self.current() or self.busy:return
        for key in self.envelopes:self.envelopes[key]=copy.deepcopy(self.envelopes[self.selected_track])
        self.status.set('Copied these section notes and velocity curves to every instrument.')

    def new_variation(self):
        if self.busy:return
        self.seed+=1;self._refresh_plan()

    def _bounds(self):
        height=self.canvas.winfo_height();top=74 if height<210 else 98
        return 46,top,max(100,self.canvas.winfo_width()-22),max(top+25,height-28)

    def draw(self):
        self.draw_overview()
        c=self.canvas;c.delete('all');w=c.winfo_width();h=c.winfo_height();env=self.current()
        if not env:c.create_text(w/2,h/2,text='Load your MIDI to preview section notes and velocity.',fill=MUTED,font=('Segoe UI',11));return
        s=self.arrangement.sections[self.selected_section];left,top,right,bottom=self._bounds()
        compact=h<210
        c.create_text(left,18,text=f'{s.name}  ·  {s.bars} bars  ·  note held at {note_name(env.pitch)}',anchor='w',fill=INK,font=('Segoe UI Semibold',10))
        count=s.bars*4*int(self.density.get());display=min(count,240)
        for k in range(display):
            x0=left+(right-left)*k/display;x1=left+(right-left)*(k+1)/display
            c.create_rectangle(x0,32 if compact else 42,max(x0+1,x1-1),44 if compact else 56,fill=GREEN,outline='')
        c.create_text(left,59 if compact else 77,text='VELOCITY → VST3 CONTROL',anchor='w',fill=MUTED,font=('Segoe UI',9))
        for value in ((0,.5,1) if bottom-top<60 else (0,.25,.5,.75,1)):
            y=bottom-(bottom-top)*value;c.create_line(left,y,right,y,fill=LINE)
            c.create_text(left-8,y,text=str(round(value*100)),anchor='e',fill=MUTED,font=('Segoe UI',8))
        for bar in range(s.bars+1):
            x=left+(right-left)*bar/s.bars;c.create_line(x,top,x,bottom,fill=LINE)
            if s.bars<=16 or bar%2==0:c.create_text(x,bottom+15,text=str(s.start_bar+bar+1),fill=MUTED,font=('Segoe UI',8))
        # The preview may omit stems for high density, never exported notes.
        for k in range(min(count,320)):
            index=round(k*(count-1)/max(1,min(count,320)-1));pos=index/max(1,count-1)
            value=(midi_velocity(velocity_at(env.points,pos))-1)/126
            x=left+(right-left)*pos;y=bottom-(bottom-top)*value;c.create_line(x,bottom,x,y,fill='#659883')
        coords=[]
        for x,y in env.points:coords.extend((left+(right-left)*x,bottom-(bottom-top)*y))
        c.create_line(*coords,fill=GREEN,width=2)
        for x,y in env.points:
            px=left+(right-left)*x;py=bottom-(bottom-top)*y;c.create_oval(px-4,py-4,px+4,py+4,fill=INK,outline=GREEN)
        text=f'{count:,} grid notes / section'
        if count>320:text+=' · stems simplified in preview'
        self.preview_label.configure(text=text)

    def draw_overview(self):
        c=self.overview;c.delete('all')
        if not self.arrangement or self.selected_track not in self.envelopes:return
        left=12;right=max(30,c.winfo_width()-12);top=23;bottom=max(35,c.winfo_height()-22);strip=c.winfo_height()-10
        total=max(1,self.arrangement.bars)
        c.create_text(left,10,text='WHOLE SONG · click a section · lower strip shows where this part plays',anchor='w',fill=MUTED,font=('Segoe UI',8))
        previous=None
        for i,(section,env) in enumerate(zip(self.arrangement.sections,self.envelopes[self.selected_track])):
            start=left+(right-left)*section.start_bar/total;end=left+(right-left)*section.end_bar/total
            if i==self.selected_section:c.create_rectangle(start,top,end,bottom,fill='#223c36',outline='')
            c.create_line(start,top,start,bottom,fill=LINE)
            coords=[]
            for x,y in env.points:coords.extend((start+(end-start)*x,bottom-(bottom-top)*y))
            if previous is not None:c.create_line(start,previous,start,coords[1],fill=GREEN,width=2)
            c.create_line(*coords,fill=GREEN,width=2);previous=coords[-1]
        track=next((t for t in self.arrangement.tracks if t.id==self.selected_track),None)
        if track:
            intervals=[]
            for note in sorted(track.notes,key=lambda n:n.start):
                a=max(0,note.start/BAR);b=min(total,note.end/BAR)
                if intervals and a<=intervals[-1][1]:intervals[-1][1]=max(intervals[-1][1],b)
                else:intervals.append([a,b])
            c.create_line(left,strip,right,strip,fill=LINE,width=4)
            for a,b in intervals:c.create_line(left+(right-left)*a/total,strip,left+(right-left)*b/total,strip,fill=TEAL,width=4)

    def select_overview(self,event):
        if not self.arrangement or self.busy:return
        bar=max(0,min(self.arrangement.bars-1e-6,(event.x-12)/max(1,self.overview.winfo_width()-24)*self.arrangement.bars))
        index=next(i for i,s in enumerate(self.arrangement.sections) if s.start_bar<=bar<s.end_bar)
        self.sections.selection_set(str(index));self.sections.see(str(index));self.select_section()

    def _point_at(self,event):
        l,t,r,b=self._bounds();return min(1,max(0,(event.x-l)/(r-l))),min(1,max(0,(b-event.y)/(b-t)))

    def _nearest(self,event):
        env=self.current()
        if not env:return None
        l,t,r,b=self._bounds();hits=[((l+x*(r-l)-event.x)**2+(b-y*(b-t)-event.y)**2,i) for i,(x,y) in enumerate(env.points)]
        distance,index=min(hits);return index if distance<=100 else None

    def canvas_down(self,event):
        env=self.current()
        if not env or self.busy or event.y<self._bounds()[1]-8:return
        self.drag_index=self._nearest(event)
        if self.drag_index is None:
            if len(env.points)>=256:return
            x,y=self._point_at(event);env.points.append((round(x,4),round(y,4)));env.points.sort(key=lambda p:p[0]);self.drag_index=env.points.index((round(x,4),round(y,4)))
        env.shape='Custom';self.shape.set('Custom');self._update_section_row();self.draw()

    def canvas_drag(self,event):
        env=self.current()
        if not env or self.drag_index is None or self.busy:return
        x,y=self._point_at(event);i=self.drag_index
        if i==0:x=0
        elif i==len(env.points)-1:x=1
        else:x=max(env.points[i-1][0],min(env.points[i+1][0],x))
        env.points[i]=(round(x,4),round(y,4));self.draw()

    def canvas_up(self,event):
        self.drag_index=None
        if self.current():self.select_section()

    def canvas_delete(self,event):
        env=self.current();i=self._nearest(event)
        if env and not self.busy and i is not None and 0<i<len(env.points)-1:
            env.points.pop(i);env.shape='Custom';self.select_section();self._update_section_row()

    def edit_points(self):
        env=self.current()
        if not env or self.busy:return
        win=tk.Toplevel(self.root);win.title('Exact velocity corners');win.geometry('450x420');win.configure(bg=BG);win.transient(self.root)
        ttk.Label(win,text='Time %, value % — one point per line.\nRepeat a time to make an immediate jump.',padding=15).pack(anchor='w')
        editor=tk.Text(win,bg=PANEL,fg=INK,insertbackground=INK,font=('Consolas',11),height=12);editor.pack(fill='both',expand=True,padx=15)
        editor.insert('1.0','\n'.join(f'{x*100:g}, {y*100:g}' for x,y in env.points))
        def save():
            try:
                points=[tuple(float(v.strip())/100 for v in line.split(',')) for line in editor.get('1.0','end').splitlines() if line.strip()]
                if any(len(p)!=2 for p in points):raise StudioError('Each line needs time %, value %.')
                from eightbar.automation import validate_envelopes
                candidate=copy.deepcopy(self.envelopes[self.selected_track]);candidate[self.selected_section].points=points;validate_envelopes(self.arrangement,candidate)
                env.points=points;env.shape='Custom';win.destroy();self.select_section();self._update_section_row()
            except Exception as exc:messagebox.showerror('Check the points',str(exc),parent=win)
        ttk.Button(win,text='Save corners',command=save).pack(pady=15)

    def arrange_ai(self):
        if self.busy or not self.source:return
        key=resolve_api_key(self.api_key).value
        if not key:
            self.status.set('Add an OpenAI API key in AI settings.');self.ai_settings();return
        if self.current() and not self.apply_shape():return
        self._refresh_plan()
        if not self.arrangement:return
        direction=self.ai_direction.get().strip()
        if len(direction)>2000:
            messagebox.showerror('Shorten direction','Keep your arrangement direction under 2,000 characters.');return
        source=copy.deepcopy(self.source);template=copy.deepcopy(self.original_arrangement)
        direction+='\nSpace requested: '+self.space.get()+'. Variation: '+self.variation.get()+'.'
        settings=AISettings(api_key=key,model=self.ai_model.get(),direction=direction,adjust_chords=self.adjust_chords.get())
        protected=set(self.protected_ids)
        try:self._save_ai_preferences()
        except OSError:pass
        self.busy=True;self.ai_ready=False;self._freeze(True);self.progress.start(12)
        self.status.set('AI is planning your arrangement…')
        def worker():
            try:
                plan=make_ai_arrangement(source,template,settings,protected_ids=protected,progress=lambda value:self.events.put(('status',value)))
                self.events.put(('ai_done',plan))
            except StudioError as exc:self.events.put(('error',str(exc)))
            except Exception:self.events.put(('error','The AI arrangement could not be completed. Your MIDI is unchanged; try again.'))
        threading.Thread(target=worker,daemon=True).start()

    def generate(self):
        if self.busy or not self.arrangement or not self.ai_ready:return
        # Apply a typed note/preset before taking the immutable generation snapshot.
        if not self.apply_shape():return
        plan=copy.deepcopy(self.arrangement);source=copy.deepcopy(self.source);original=copy.deepcopy(self.original_arrangement)
        envelopes={key:copy.deepcopy(value) for key,value in self.envelopes.items() if self.enabled.get(key,False) and next(t for t in source.tracks if t.id==key).role!='skip'}
        density=int(self.density.get());self.busy=True;self._freeze(True);self.progress.start(12)
        def worker():
            try:
                project=export_project(source,plan,envelopes,density,BASE/'Generated songs',lambda text:self.events.put(('status',text)),original_arrangement=original)
                self.events.put(('done',project))
            except Exception as exc:self.events.put(('error',str(exc)))
        threading.Thread(target=worker,daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait()
                if kind=='status':self.status.set(value)
                else:
                    self.busy=False;self.progress.stop();self._freeze(False)
                    if kind=='ai_done':
                        self.arrangement=value;self.ai_ready=True;self._populate_sections()
                        summary=next((d.get('summary','') for d in value.decisions if d.get('kind')=='ai_arrangement'),'')
                        if len(summary)>280:summary=summary[:277]+'…'
                        self.status.set('AI arrangement ready. Review the part activity and curves, then Export MIDI. '+summary)
                    elif kind=='done':
                        self.latest=Path(value);self.status.set('Ready — '+self.latest.name);self._show_ready()
                    else:self.status.set('Stopped. '+value);messagebox.showerror('Could not finish',value)
                    self.generate_button.configure(state='normal' if self.arrangement and self.ai_ready else 'disabled')
        except queue.Empty:pass
        self.poll_id=self.root.after(100,self._poll)

    def _freeze(self,freeze):
        if freeze:
            self.frozen=[]
            def visit(widget):
                for child in widget.winfo_children():
                    if isinstance(child,(ttk.Button,ttk.Entry,ttk.Combobox,ttk.Checkbutton)):
                        self.frozen.append((child,child.state()));child.state(['disabled'])
                    visit(child)
            visit(self.root)
        else:
            for widget,state in self.frozen:
                if widget.winfo_exists():widget.state(['!disabled']);widget.state(state)
            if self.source and self.selected_track:self.select_track()

    def _show_ready(self):
        win=tk.Toplevel(self.root);win.title('Your song is ready');win.geometry('485x270');win.configure(bg=BG);win.transient(self.root)
        ttk.Label(win,text='Your song is ready',style='Title.TLabel',padding=20).pack(anchor='w')
        ttk.Label(win,text='AI arrangement + reference MIDI + edit notes\nSeparate instrument parts + checked automation MIDI',padding=(20,0)).pack(anchor='w')
        ttk.Label(win,text='All files start at the same song position.\nRoute automation MIDI to Velocity Pass to control audio level.',style='Muted.TLabel',padding=20).pack(anchor='w')
        ttk.Button(win,text='Open song folder',command=lambda:os.startfile(self.latest)).pack(side='left',padx=20)
        ttk.Button(win,text='Keep working',command=win.destroy).pack(side='right',padx=20)

    def _close(self):
        if self.busy:
            self.status.set('Export is still running. The window can close when it finishes.');return
        self.root.after_cancel(self.poll_id)
        self.root.destroy()

def main():
    try:
        from tkinterdnd2 import TkinterDnD
        root=TkinterDnD.Tk()
    except ImportError:root=tk.Tk()
    if '--classic' in sys.argv:
        studio=Studio(root)
        paths=[arg for arg in sys.argv[1:] if not arg.startswith('--')]
        if paths:root.after(150,lambda:studio.open_file(paths[0]))
    else:
        from eightbar.dual_ui import DualStudio
        def open_classic():
            window=tk.Toplevel(root)
            window._studio=Studio(window)
        studio=DualStudio(root,classic_callback=open_classic)
        paths=[arg for arg in sys.argv[1:] if not arg.startswith('--')]
        if paths:root.after(150,lambda:studio.load_file('A',paths[0]))
    root.mainloop()

if __name__=='__main__':main()

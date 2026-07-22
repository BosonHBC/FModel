#!/usr/bin/env python3
"""UE Asset Reference Viewer - Build and visualize asset reference databases from FModel exports."""

import os, sys, json, sqlite3, math, re, threading, queue
from collections import defaultdict
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
DB_PATH = os.path.join(SCRIPT_DIR, "asset_references.db")

TYPE_COLORS = {
    'StaticMesh': '#4A90D9', 'Texture2D': '#7EC850', 'Material': '#E85A35',
    'MaterialInstanceConstant': '#E8A735', 'MaterialInstance': '#E8A735',
    'SkeletalMesh': '#9D6AD9', 'MaterialFunction': '#D94A8A',
}

def type_color(t):
    if not t:
        return '#999999'
    for k, v in TYPE_COLORS.items():
        if k in t:
            return v
    return '#999999'

TYPE_ABBREV = {
    'StaticMesh': 'SM', 'SkeletalMesh': 'SKM', 'Texture2D': 'T',
    'Texture': 'T', 'MaterialInstanceConstant': 'MI', 'MaterialInstance': 'MI',
    'MaterialFunction': 'MF', 'Material': 'M',
}

def type_abbrev(t):
    if not t:
        return '?'
    for k, v in TYPE_ABBREV.items():
        if k in t:
            return v
    return '?'

def norm_path(p):
    """Strip subobject suffix from UE path (/Game/.../Asset.0 -> /Game/.../Asset)."""
    if not p:
        return p
    ls, ld = p.rfind('/'), p.rfind('.')
    return p[:ld] if ld > ls else p

def fmt_size(n):
    if n is None:
        return 'N/A'
    if n > 1048576:
        return f"{n / 1048576:.2f} MB"
    if n > 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


# ======================== Database ========================

class Database:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_path TEXT UNIQUE NOT NULL,
                name TEXT, type TEXT, file_path TEXT, file_size INTEGER
            );
            CREATE TABLE IF NOT EXISTS refs (
                source_id INTEGER, target_path TEXT, target_type TEXT
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        ''')
        self.conn.commit()

    def clear(self):
        self.conn.executescript('DELETE FROM assets; DELETE FROM refs; DELETE FROM meta;')
        self.conn.commit()

    def add_asset(self, pp, name, atype, fpath, fsize):
        self.conn.execute(
            'INSERT OR REPLACE INTO assets (package_path,name,type,file_path,file_size) VALUES (?,?,?,?,?)',
            (pp, name, atype, fpath, fsize))
        r = self.conn.execute('SELECT id FROM assets WHERE package_path=?', (pp,)).fetchone()
        return r[0]

    def add_ref(self, sid, tpath, ttype):
        self.conn.execute('INSERT INTO refs (source_id,target_path,target_type) VALUES (?,?,?)',
                          (sid, tpath, ttype))

    def commit(self):
        self.conn.commit()

    def set_meta(self, k, v):
        self.conn.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (k, v))
        self.conn.commit()

    def get_meta(self, k):
        r = self.conn.execute('SELECT value FROM meta WHERE key=?', (k,)).fetchone()
        return r[0] if r else None

    def get_by_path(self, pp):
        r = self.conn.execute('SELECT * FROM assets WHERE package_path=?', (pp,)).fetchone()
        return dict(r) if r else None

    def get_outgoing(self, aid):
        return [dict(r) for r in self.conn.execute(
            'SELECT r.target_path, r.target_type, a.id AS tid, a.name, a.type, a.file_path, a.file_size '
            'FROM refs r LEFT JOIN assets a ON r.target_path=a.package_path WHERE r.source_id=?', (aid,))]

    def get_incoming(self, pp):
        return [dict(r) for r in self.conn.execute(
            'SELECT a.id AS sid, a.name, a.type, a.package_path, a.file_path, a.file_size '
            'FROM refs r JOIN assets a ON r.source_id=a.id WHERE r.target_path=?', (pp,))]

    def all_assets(self):
        return [dict(r) for r in self.conn.execute('SELECT * FROM assets ORDER BY type, name')]

    def stats(self):
        a = self.conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0]
        r = self.conn.execute('SELECT COUNT(*) FROM refs').fetchone()[0]
        return a, r

    def ref_counts(self, aid, pp):
        out_n = self.conn.execute('SELECT COUNT(*) FROM refs WHERE source_id=?', (aid,)).fetchone()[0]
        in_n = self.conn.execute('SELECT COUNT(*) FROM refs WHERE target_path=?', (pp,)).fetchone()[0]
        return out_n, in_n


# ======================== Scanner ========================

class Scanner:
    @staticmethod
    def extract_refs(data, own_pp):
        """Recursively extract all ObjectPath/AssetPathName references from JSON."""
        refs = set()

        def scan(obj, d=0):
            if d > 15 or not isinstance(obj, (dict, list)):
                return
            if isinstance(obj, dict):
                p = obj.get('ObjectPath')
                n = obj.get('ObjectName', '')
                if isinstance(p, str) and p and not p.startswith('/Script/') and not p.startswith('/Engine/'):
                    b = norm_path(p)
                    if b != own_pp:
                        rt = n.split("'")[0] if "'" in n else None
                        refs.add((b, rt))
                ap = obj.get('AssetPathName')
                if isinstance(ap, str) and ap and not ap.startswith('/Script/') and not ap.startswith('/Engine/'):
                    b = norm_path(ap)
                    if b != own_pp:
                        refs.add((b, None))
                for v in obj.values():
                    scan(v, d + 1)
            else:
                for i in obj:
                    scan(i, d + 1)

        for e in (data if isinstance(data, list) else [data]):
            scan(e)
        return refs

    @staticmethod
    def scan_file(fpath, root):
        try:
            with open(fpath, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return None
        entries = data if isinstance(data, list) else [data]
        pp = None
        atype = 'Unknown'
        name = os.path.basename(fpath).replace('.json', '')
        for e in entries:
            if isinstance(e, dict) and e.get('Package'):
                pp = e['Package']
                atype = e.get('Type', 'Unknown')
                name = e.get('Name', name)
                break
        if not pp:
            rel = os.path.relpath(fpath, root).replace('\\', '/')
            pp = '/Game/' + rel.replace('.json', '')
        refs = Scanner.extract_refs(data, pp)
        try:
            fsize = os.path.getsize(fpath)
        except OSError:
            fsize = 0
        return {
            'package_path': pp, 'name': name, 'type': atype,
            'file_path': os.path.relpath(fpath, root).replace('\\', '/'),
            'file_size': fsize, 'references': list(refs)
        }


# ======================== Graph Canvas ========================

class GraphCanvas(tk.Canvas):
    def __init__(self, parent, app):
        super().__init__(parent, bg='#1E1E1E', highlightthickness=0)
        self.app = app
        self.nodes = {}       # path -> {x, y, r, info, center, dir}
        self.edges = []       # (src_path, tgt_path, direction)
        self.center = None
        self.depth = 1
        self.ox, self.oy = 0, 0
        self.zoom = 1.0
        self.drag = None
        self.hovered = None
        self.bind('<Button-1>', self._click)
        self.bind('<B1-Motion>', self._drag)
        self.bind('<ButtonRelease-1>', self._release)
        self.bind('<Double-Button-1>', self._dblclick)
        self.bind('<Motion>', self._motion)
        self.bind('<Leave>', lambda e: self._set_hovered(None))
        self.bind('<Button-3>', self._right_click)
        self.bind('<MouseWheel>', self._wheel)

    def show(self, pp, depth=1):
        self.center = pp
        self.depth = depth
        self.ox = self.oy = 0
        self.zoom = 1.0
        self._build()

    def _build(self):
        self.nodes.clear()
        self.edges.clear()
        if not self.center:
            self.draw()
            return
        db = self.app.db
        if not db:
            self.draw()
            return
        ca = db.get_by_path(self.center)
        w = max(self.winfo_width(), 700)
        h = max(self.winfo_height(), 450)
        cx, cy = w / 2, h / 2

        if ca:
            self.nodes[self.center] = {'x': cx, 'y': cy, 'r': 35, 'info': ca, 'center': True}
        else:
            self.nodes[self.center] = {
                'x': cx, 'y': cy, 'r': 30,
                'info': {'name': os.path.basename(self.center), 'type': 'Unknown', 'package_path': self.center},
                'center': True
            }

        out = db.get_outgoing(ca['id']) if ca else []
        inn = db.get_incoming(self.center)

        # Layout outgoing on right, incoming on left
        col_x_out = cx + 260
        col_x_in = cx - 260
        sp_out = max(80, min(110, h / max(len(out), 1) / 2)) if out else 60
        sp_in = max(80, min(110, h / max(len(inn), 1) / 2)) if inn else 60

        # When depth=2, we need extra vertical space for sub-nodes, so use larger spacing
        if self.depth >= 2 and out:
            sp_out = max(100, min(130, h / max(len(out), 1) / 2))

        for i, r in enumerate(out):
            y = cy + (i - len(out) / 2 + 0.5) * sp_out
            p = r['target_path']
            if p not in self.nodes:
                self.nodes[p] = {'x': col_x_out, 'y': y, 'r': 22, 'info': r, 'center': False, 'dir': 'out'}
            self.edges.append((self.center, p, 'out'))

        for i, r in enumerate(inn):
            y = cy + (i - len(inn) / 2 + 0.5) * sp_in
            p = r['package_path']
            if p not in self.nodes:
                self.nodes[p] = {'x': col_x_in, 'y': y, 'r': 22, 'info': r, 'center': False, 'dir': 'in'}
            self.edges.append((p, self.center, 'in'))

        # Depth 2: show second-level outgoing (greedy non-overlapping layout)
        if self.depth >= 2 and ca:
            sub_col_x = col_x_out + 200
            sub_sp = 48
            sub_r = 15
            # Collect sub-node groups per parent
            groups = []
            for r in out:
                p = r['target_path']
                if p in self.nodes and not self.nodes[p].get('center'):
                    ra = db.get_by_path(p)
                    if ra:
                        o2 = db.get_outgoing(ra['id'])
                        subs = []
                        for r2 in o2[:6]:
                            p2 = r2['target_path']
                            if p2 not in self.nodes and p2 != self.center:
                                subs.append(r2)
                        if subs:
                            groups.append((p, subs))
            # Sort by parent y position and place greedily without overlap
            groups.sort(key=lambda g: self.nodes[g[0]]['y'])
            cur_bottom = -1e9
            for parent_path, subs in groups:
                parent_y = self.nodes[parent_path]['y']
                span = len(subs) * sub_sp
                desired_top = parent_y - span / 2
                top = max(desired_top, cur_bottom + 5)
                for j, r2 in enumerate(subs):
                    p2 = r2['target_path']
                    y = top + (j + 0.5) * sub_sp
                    self.nodes[p2] = {
                        'x': sub_col_x, 'y': y, 'r': sub_r,
                        'info': r2, 'center': False, 'dir': 'out2'
                    }
                    self.edges.append((parent_path, p2, 'out2'))
                cur_bottom = top + span

        self.draw()

    def draw(self):
        self.delete('all')
        z = self.zoom
        # Draw edges
        for s, t, d in self.edges:
            if s in self.nodes and t in self.nodes:
                sn, tn = self.nodes[s], self.nodes[t]
                x1, y1 = sn['x'] * z + self.ox, sn['y'] * z + self.oy
                x2, y2 = tn['x'] * z + self.ox, tn['y'] * z + self.oy
                if d == 'in':
                    self.create_line(x2, y2, x1, y1, fill='#555555', dash=(3, 2), arrow='last', width=1)
                elif d == 'out2':
                    self.create_line(x1, y1, x2, y2, fill='#444444', arrow='last', width=1)
                else:
                    self.create_line(x1, y1, x2, y2, fill='#888888', arrow='last', width=1.5)
        # Draw nodes as rounded squares with type abbreviation inside
        for p, n in self.nodes.items():
            x, y = n['x'] * z + self.ox, n['y'] * z + self.oy
            r = n['r'] * z
            info = n['info']
            at = info.get('type') or info.get('target_type') or 'Unknown'
            c = type_color(at)
            ow = 3 if n.get('center') else (2 if self.hovered == p else 1)
            oc = '#FFFFFF' if n.get('center') else ('#FFAAAA' if self.hovered == p else '#333333')
            self._create_rounded_rect(x - r, y - r, x + r, y + r, radius=r * 0.25, fill=c, outline=oc, width=ow)
            abbrev = type_abbrev(at)
            fs = int(r * 0.75)
            self.create_text(x, y, text=abbrev, fill='#FFFFFF', font=('Segoe UI', max(fs, 6), 'bold'))
            nm = info.get('name') or os.path.basename(p)
            if len(nm) > 28:
                nm = nm[:25] + '...'
            ty1 = y + r + 12
            tw = max(len(nm) * 4.5, 40)
            self.create_rectangle(x - tw/2, ty1 - 8, x + tw/2, ty1 + 8, fill='#1E1E1E', outline='')
            self.create_text(x, ty1, text=nm, fill='#DDDDDD', font=('Segoe UI', 8))
        # Direction labels
        if self.nodes:
            w = max(self.winfo_width(), 700)
            self.create_text(w - 80, 12, text="-->", fill='#888', font=('Segoe UI', 8))
            self.create_text(w - 80, 24, text="references", fill='#666', font=('Segoe UI', 7))
            self.create_text(80, 12, text="<--", fill='#666', font=('Segoe UI', 8))
            self.create_text(80, 24, text="referenced by", fill='#555', font=('Segoe UI', 7))
        self._draw_legend()

    def _draw_legend(self):
        x, y = 12, 50
        items = [('SM', 'StaticMesh', '#4A90D9'), ('T', 'Texture2D', '#7EC850'),
                 ('M', 'Material', '#E85A35'), ('MI', 'MatInstance', '#E8A735'),
                 ('?', 'Missing', '#999999')]
        for abbrev, label, color in items:
            self._create_rounded_rect(x, y, x + 14, y + 14, radius=3, fill=color, outline='#333333')
            self.create_text(x + 7, y + 7, text=abbrev, fill='#FFFFFF', font=('Segoe UI', 6, 'bold'))
            self.create_text(x + 18, y + 7, text=label, fill='#999999', font=('Segoe UI', 7), anchor='w')
            y += 18

    def _create_rounded_rect(self, x1, y1, x2, y2, radius=8, **kwargs):
        """Draw a rounded rectangle on the canvas."""
        radius = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
        points = []
        # Top-left corner
        points.extend([x1 + radius, y1, x1, y1, x1, y1 + radius])
        # Left edge to bottom-left corner
        points.extend([x1, y2 - radius, x1, y2, x1 + radius, y2])
        # Bottom edge to bottom-right corner
        points.extend([x2 - radius, y2, x2, y2, x2, y2 - radius])
        # Right edge to top-right corner
        points.extend([x2, y1 + radius, x2, y1, x2 - radius, y1])
        return self.create_polygon(points, smooth=True, **kwargs)

    def _hit(self, ex, ey):
        for p, n in self.nodes.items():
            x = n['x'] * self.zoom + self.ox
            y = n['y'] * self.zoom + self.oy
            r = n['r'] * self.zoom
            if abs(ex - x) <= r and abs(ey - y) <= r:
                return p
        return None

    def _click(self, e):
        p = self._hit(e.x, e.y)
        if p:
            self.app.show_details(self.nodes[p]['info'], p)
        else:
            self.drag = (e.x, e.y)

    def _drag(self, e):
        if self.drag:
            self.ox += e.x - self.drag[0]
            self.oy += e.y - self.drag[1]
            self.drag = (e.x, e.y)
            self.draw()

    def _release(self, e):
        self.drag = None

    def _wheel(self, e):
        factor = 1.15 if e.delta > 0 else 1 / 1.15
        new_zoom = self.zoom * factor
        if new_zoom < 0.3 or new_zoom > 5.0:
            return
        mx, my = e.x, e.y
        wx = (mx - self.ox) / self.zoom
        wy = (my - self.oy) / self.zoom
        self.zoom = new_zoom
        self.ox = mx - wx * self.zoom
        self.oy = my - wy * self.zoom
        self.draw()

    def _dblclick(self, e):
        p = self._hit(e.x, e.y)
        if p and p != self.center:
            self.show(p, self.depth)
            self.app.on_graph_centered(p)

    def _motion(self, e):
        p = self._hit(e.x, e.y)
        self._set_hovered(p)
        self.config(cursor='hand2' if p else '')

    def _set_hovered(self, p):
        if self.hovered != p:
            self.hovered = p
            self.draw()

    def _right_click(self, e):
        p = self._hit(e.x, e.y)
        if not p:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Center Here", command=lambda: self._menu_center(p))
        menu.add_command(label="Copy Path", command=lambda: self._menu_copy(p))
        info = self.nodes[p]['info']
        fp = info.get('file_path')
        full_path = os.path.join(self.app.last_dir, fp) if fp and self.app.last_dir else None
        if full_path and os.path.isfile(full_path):
            menu.add_command(label="Open in Explorer", command=lambda: self._menu_explorer(full_path))
        menu.tk_popup(e.x_root, e.y_root)

    def _menu_center(self, p):
        self.show(p, self.depth)
        self.app.on_graph_centered(p)

    def _menu_copy(self, p):
        self.clipboard_clear()
        self.clipboard_append(p)

    def _menu_explorer(self, full_path):
        import subprocess
        normalized = os.path.normpath(full_path)
        subprocess.Popen(['explorer', '/select,', normalized])


# ======================== Main Application ========================

class App:
    def __init__(self, root):
        self.root = root
        self.db = Database(DB_PATH)
        self.queue = queue.Queue()
        self.scanning = False
        self.last_dir = self.db.get_meta('last_dir') or r"I:\FModelOutput\Exports\SLASHER\Content"
        self.tree_map = {}  # tree_item_id -> package_path
        root.title("UE Asset Reference Viewer")
        root.geometry("1280x780")
        root.minsize(960, 620)
        self._build_ui()
        self._refresh_tree()
        self._poll()

    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill='both', expand=True)
        self._build_db_tab(nb)
        self._build_viewer_tab(nb)

    def _build_db_tab(self, nb):
        f = ttk.Frame(nb, padding=15)
        nb.add(f, text="  Database  ")
        ttk.Label(f, text="Root Directory:").grid(row=0, column=0, sticky='w', pady=5)
        self.dir_var = tk.StringVar(value=self.last_dir)
        ttk.Entry(f, textvariable=self.dir_var, width=80).grid(row=0, column=1, sticky='we', padx=5)
        ttk.Button(f, text="Browse...", command=self._browse).grid(row=0, column=2, padx=5)

        bf = ttk.Frame(f)
        bf.grid(row=1, column=0, columnspan=3, pady=10)
        self.build_btn = ttk.Button(bf, text="  Build Database  ", command=self._start_scan)
        self.build_btn.pack(side='left', padx=5)
        self.clear_btn = ttk.Button(bf, text="  Clear Database  ", command=self._clear_db)
        self.clear_btn.pack(side='left', padx=5)

        self.pb = ttk.Progressbar(f, mode='determinate')
        self.pb.grid(row=2, column=0, columnspan=3, sticky='we', pady=5)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(f, textvariable=self.status_var).grid(row=3, column=0, columnspan=3, sticky='w')

        info_text = (
            "Instructions:\n"
            "  1. Set the root directory to your FModel export Content folder\n"
            "  2. Click 'Build Database' to scan all .json files\n"
            "  3. Switch to 'Reference Viewer' tab to explore references\n"
            "\n"
            "Reference types detected:\n"
            "  - Mesh -> Material (via StaticMaterials)\n"
            "  - Material Instance -> Texture (via TextureParameterValues)\n"
            "  - Material Instance -> Parent Material (via Parent)\n"
            "  - Material -> Texture (via ReferencedTextures / TextureValues)\n"
            "\n"
            "Graph controls:\n"
            "  - Double-click node: center on it\n"
            "  - Single-click: show details\n"
            "  - Drag empty space: pan\n"
            "  - Mouse wheel: zoom in/out (toward cursor)\n"
            "  - Right-click: context menu (copy path, open in explorer)"
        )
        ttk.Label(f, text=info_text, justify='left', font=('Segoe UI', 9)).grid(
            row=4, column=0, columnspan=3, sticky='w', pady=10)
        f.columnconfigure(1, weight=1)

    def _build_viewer_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="  Reference Viewer  ")

        # Left panel: asset tree
        lp = ttk.Frame(f, width=300)
        lp.pack(side='left', fill='y', padx=(5, 0), pady=5)
        lp.pack_propagate(False)
        ttk.Label(lp, text="Search:", font=('Segoe UI', 9)).pack(anchor='w')
        self.search_var = tk.StringVar()
        self.search_var.trace('w', lambda *_: self._filter_tree())
        ttk.Entry(lp, textvariable=self.search_var).pack(fill='x', pady=(0, 5))
        tree_frame = ttk.Frame(lp)
        tree_frame.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=('type',), show='tree headings')
        self.tree.heading('#0', text='Asset')
        self.tree.heading('type', text='Type')
        self.tree.column('#0', width=200)
        self.tree.column('type', width=80)
        tree_scroll = ttk.Scrollbar(tree_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side='left', fill='both', expand=True)
        tree_scroll.pack(side='right', fill='y')
        self.tree.bind('<Double-1>', self._tree_dblclick)

        # Right panel: details (pack before canvas so it gets proper space)
        rp = ttk.Frame(f, width=320)
        rp.pack(side='right', fill='y', padx=(0, 5), pady=5)
        rp.pack_propagate(False)
        ttk.Label(rp, text="Details", font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(0, 5))
        self.detail_text = tk.Text(rp, wrap='word', state='disabled', bg='#F5F5F5',
                                   font=('Consolas', 9), relief='flat')
        self.detail_text.pack(fill='both', expand=True, pady=(0, 5))

        # Center: controls bar + graph canvas
        center = ttk.Frame(f)
        center.pack(side='left', fill='both', expand=True, padx=5, pady=5)

        # Top controls bar (compact, replaces old bottom bar)
        cf = ttk.Frame(center)
        cf.pack(side='top', fill='x', pady=(0, 5))
        ttk.Label(cf, text="Depth:").pack(side='left', padx=(5, 2))
        self.depth_var = tk.IntVar(value=1)
        ttk.Radiobutton(cf, text="1 (direct)", variable=self.depth_var, value=1,
                        command=self._refresh_graph).pack(side='left', padx=2)
        ttk.Radiobutton(cf, text="2 (extended)", variable=self.depth_var, value=2,
                        command=self._refresh_graph).pack(side='left', padx=2)
        ttk.Button(cf, text="Refresh Graph", command=self._refresh_graph).pack(side='right', padx=5)
        ttk.Button(cf, text="Clear Graph", command=lambda: self.canvas.show(None)).pack(side='right', padx=5)

        # Graph canvas
        self.canvas = GraphCanvas(center, self)
        self.canvas.pack(side='top', fill='both', expand=True)

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get())
        if d:
            self.dir_var.set(d)

    def _start_scan(self):
        if self.scanning:
            return
        d = self.dir_var.get()
        if not os.path.isdir(d):
            messagebox.showerror("Error", f"Directory not found:\n{d}")
            return
        self.scanning = True
        self.build_btn.config(state='disabled')
        self.status_var.set("Scanning...")
        self.pb['value'] = 0
        t = threading.Thread(target=self._scan_worker, args=(d,), daemon=True)
        t.start()

    def _scan_worker(self, directory):
        files = []
        for root, _, fs in os.walk(directory):
            for fn in fs:
                if fn.endswith('.json'):
                    files.append(os.path.join(root, fn))
        self.queue.put(('total', len(files)))
        self.db.clear()
        assets = []
        for i, fp in enumerate(files):
            r = Scanner.scan_file(fp, directory)
            if r:
                assets.append(r)
            if (i + 1) % 25 == 0 or i + 1 == len(files):
                self.queue.put(('progress', i + 1))
        # First pass: insert assets
        for a in assets:
            aid = self.db.add_asset(a['package_path'], a['name'], a['type'],
                                    a['file_path'], a['file_size'])
            a['id'] = aid
        # Second pass: insert references
        for a in assets:
            for rp, rt in a['references']:
                self.db.add_ref(a['id'], rp, rt)
        self.db.commit()
        self.db.set_meta('last_dir', directory)
        na, nr = self.db.stats()
        self.queue.put(('done', na, nr, directory))

    def _poll(self):
        try:
            while True:
                m = self.queue.get_nowait()
                if m[0] == 'total':
                    self.pb['maximum'] = m[1]
                elif m[0] == 'progress':
                    self.pb['value'] = m[1]
                    mx = int(self.pb['maximum']) if self.pb['maximum'] else 0
                    self.status_var.set(f"Scanning... {m[1]}/{mx}")
                elif m[0] == 'done':
                    self.status_var.set(f"Done!  {m[1]} assets,  {m[2]} references")
                    self.scanning = False
                    self.build_btn.config(state='normal')
                    self.last_dir = m[3]
                    self._refresh_tree()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _clear_db(self):
        self.db.clear()
        self._refresh_tree()
        self.status_var.set("Database cleared")
        self.canvas.show(None)

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.tree_map.clear()
        assets = self.db.all_assets() if self.db else []
        groups = defaultdict(list)
        for a in assets:
            groups[a['type']].append(a)
        for atype in sorted(groups):
            n = len(groups[atype])
            parent = self.tree.insert('', 'end', text=f"{atype} ({n})", open=False)
            for a in groups[atype]:
                item = self.tree.insert(parent, 'end', text=a['name'], values=(atype,))
                self.tree_map[item] = a['package_path']

    def _filter_tree(self):
        q = self.search_var.get().lower()
        for parent in self.tree.get_children():
            children = self.tree.get_children(parent)
            any_visible = False
            for child in children:
                name = self.tree.item(child, 'text').lower()
                if q in name:
                    self.tree.reattach(child, parent, 'end')
                    any_visible = True
                else:
                    self.tree.detach(child)
            if any_visible:
                self.tree.item(parent, open=True)

    def _tree_dblclick(self, e):
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        pp = self.tree_map.get(item)
        if pp:
            self.canvas.show(pp, self.depth_var.get())
            self.show_details(self.db.get_by_path(pp), pp)

    def show_details(self, info, path=None):
        self.detail_text.config(state='normal')
        self.detail_text.delete('1.0', 'end')
        if not info:
            if path:
                self.detail_text.insert('end', f"Path: {path}\n\n")
                self.detail_text.insert('end', "(Status: Not scanned / Placeholder)\n")
                inn = self.db.get_incoming(path)
                if inn:
                    self.detail_text.insert('end', f"\nReferenced by {len(inn)} asset(s):\n")
                    for r in inn[:15]:
                        self.detail_text.insert('end', f"  - {r['name']} [{r['type']}]\n")
                    if len(inn) > 15:
                        self.detail_text.insert('end', f"  ... and {len(inn) - 15} more\n")
        else:
            name = info.get('name') or (os.path.basename(path) if path else 'N/A')
            atype = info.get('type') or info.get('target_type') or 'Unknown'
            pp = info.get('package_path') or path or 'N/A'
            fp = info.get('file_path')
            fs = info.get('file_size')
            self.detail_text.insert('end', f"Name:   {name}\n")
            self.detail_text.insert('end', f"Type:   {atype}\n")
            self.detail_text.insert('end', f"Path:   {pp}\n")
            if fp:
                self.detail_text.insert('end', f"File:   {fp}\n")
            if fs is not None:
                self.detail_text.insert('end', f"Size:   {fmt_size(fs)}\n")
            if path and pp != path:
                self.detail_text.insert('end', f"\nRefPath: {path}\n")
            # Reference counts
            ca = self.db.get_by_path(pp) if info.get('id') or info.get('tid') else None
            aid = info.get('id') or info.get('tid') or (ca['id'] if ca else None)
            if aid:
                out_n, in_n = self.db.ref_counts(aid, pp)
                self.detail_text.insert('end', f"\nReferences:\n")
                self.detail_text.insert('end', f"  Outgoing: {out_n}\n")
                self.detail_text.insert('end', f"  Incoming: {in_n}\n")
            # Status for unresolved references
            if info.get('tid') is None and 'target_path' in info:
                self.detail_text.insert('end', "\n(Status: Referenced but not scanned)\n")
                rt = info.get('target_type')
                if rt:
                    self.detail_text.insert('end', f"Expected type: {rt}\n")
        self.detail_text.config(state='disabled')

    def _refresh_graph(self):
        if self.canvas.center:
            self.canvas.show(self.canvas.center, self.depth_var.get())

    def on_graph_centered(self, path):
        self.show_details(self.db.get_by_path(path), path)


# ======================== Entry Point ========================

def main():
    try:
        root = tk.Tk()
    except tk.TclError:
        print("Error: tkinter not available. Please install a full Python installation.")
        sys.exit(1)
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
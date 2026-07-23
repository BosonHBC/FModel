#!/usr/bin/env python3
"""UE Asset Reference Viewer - Build and visualize asset reference databases from FModel exports."""

import os, sys, json, sqlite3, math, re, threading, queue
from collections import defaultdict
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk

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


def resolve_asset_path(json_path, ext, export_root=None):
    """Resolve the real asset file path (.glb / .png / etc.) from a JSON path.

    With the unified export root (I:\\FModelOutput\\Exports), both JSON
    metadata and binary assets (.glb, .png) live in the same directory.
    This function simply swaps the .json extension for the target extension.

    Args:
        json_path: Absolute path to the .json file (or a path joined from
                   last_dir + file_path).
        ext: Target extension, e.g. '.glb' or '.png'.
        export_root: Optional root directory. If given and json_path is
                     relative, it is joined to make an absolute path first.
    Returns:
        Absolute path if the file exists, otherwise None.
    """
    if not ext.startswith('.'):
        ext = '.' + ext
    p = json_path
    if export_root and not os.path.isabs(p):
        p = os.path.join(export_root, p)
    norm = p.replace('\\', '/')
    candidate = os.path.normpath(norm.rsplit('.json', 1)[0] + ext)
    if os.path.isfile(candidate):
        return candidate
    return None


# ======================== Database ========================

class Database:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_path TEXT UNIQUE NOT NULL,
                name TEXT, type TEXT, file_path TEXT, file_size INTEGER,
                exported INTEGER DEFAULT 0,
                ue_imported INTEGER DEFAULT NULL
            );
            CREATE TABLE IF NOT EXISTS refs (
                source_id INTEGER, target_path TEXT, target_type TEXT
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        ''')
        # Migration: add 'exported' column for databases created before this feature.
        try:
            self.conn.execute('ALTER TABLE assets ADD COLUMN exported INTEGER DEFAULT 0')
        except sqlite3.OperationalError:
            pass  # Column already exists
        # Migration: add 'ue_imported' column (NULL=not scanned, 1=imported, 0=not imported)
        try:
            self.conn.execute('ALTER TABLE assets ADD COLUMN ue_imported INTEGER DEFAULT NULL')
        except sqlite3.OperationalError:
            pass  # Column already exists
        self.conn.commit()

    def clear(self):
        self.conn.executescript('DELETE FROM assets; DELETE FROM refs; DELETE FROM meta;')
        self.conn.commit()

    def add_asset(self, pp, name, atype, fpath, fsize, exported=0):
        self.conn.execute(
            'INSERT OR REPLACE INTO assets (package_path,name,type,file_path,file_size,exported) VALUES (?,?,?,?,?,?)',
            (pp, name, atype, fpath, fsize, exported))
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
        # Check whether the actual asset binary has been exported.
        # Binary assets (.glb, .png) are in the same directory as the JSON.
        exported = 0
        if 'Mesh' in atype:
            abs_json = os.path.join(root, os.path.relpath(fpath, root))
            if resolve_asset_path(abs_json, '.glb'):
                exported = 1
        elif 'Texture' in atype:
            abs_json = os.path.join(root, os.path.relpath(fpath, root))
            if resolve_asset_path(abs_json, '.png'):
                exported = 1
        return {
            'package_path': pp, 'name': name, 'type': atype,
            'file_path': os.path.relpath(fpath, root).replace('\\', '/'),
            'file_size': fsize, 'exported': exported, 'references': list(refs)
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
        self.last_dir = self.db.get_meta('last_dir') or r"I:\FModelOutput\Exports"
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
        self.update_export_btn = ttk.Button(bf, text="  Update Export Status  ", command=self._start_update_export)
        self.update_export_btn.pack(side='left', padx=5)
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
        self.glb_only_var = tk.BooleanVar(value=False)
        self.glb_only_var.trace('w', lambda *_: self._filter_tree())
        ttk.Checkbutton(lp, text="Only show meshes with GLB",
                        variable=self.glb_only_var).pack(anchor='w', pady=(0, 5))
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
                                   font=('Consolas', 9), relief='flat', height=12)
        self.detail_text.pack(fill='x', pady=(0, 5))

        # Preview area: texture image or mesh wireframe
        ttk.Label(rp, text="Preview", font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(0, 2))
        self.preview_frame = ttk.Frame(rp, relief='sunken', borderwidth=1)
        self.preview_frame.pack(fill='both', expand=True, pady=(0, 5))
        self.preview_canvas = tk.Canvas(self.preview_frame, bg='#2B2B2B', highlightthickness=0)
        self.preview_canvas.pack(fill='both', expand=True)
        self.preview_label = None
        self.preview_photo = None  # keep reference to prevent GC

        # Preview interaction state
        self._preview_mode = None          # 'mesh' | 'texture' | None
        self._mesh_verts = None            # centered 3D verts
        self._mesh_edges = None            # list of (i, j) edges
        self._mesh_meta = None             # (base_scale, total_tris, shown_tris)
        self._mesh_rot_x = -25.0           # pitch (degrees)
        self._mesh_rot_y = 30.0            # yaw (degrees)
        self._mesh_zoom = 1.0
        self._tex_img = None               # original PIL image for texture zoom
        self._tex_zoom = 1.0
        self._drag_last = None
        # Bind interactions
        self.preview_canvas.bind('<ButtonPress-1>', self._preview_drag_start)
        self.preview_canvas.bind('<B1-Motion>', self._preview_drag_move)
        self.preview_canvas.bind('<ButtonRelease-1>', self._preview_drag_end)
        self.preview_canvas.bind('<MouseWheel>', self._preview_wheel)      # Windows
        self.preview_canvas.bind('<Button-4>', self._preview_wheel)        # Linux up
        self.preview_canvas.bind('<Button-5>', self._preview_wheel)        # Linux down
        self.preview_canvas.bind('<Double-Button-1>', self._preview_reset_view)

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
                                    a['file_path'], a['file_size'], a.get('exported', 0))
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
                    self.update_export_btn.config(state='normal')
                    self.clear_btn.config(state='normal')
                    self.last_dir = m[3]
                    self._refresh_tree()
                elif m[0] == 'export_done':
                    self.status_var.set(f"Export status updated: {m[1]} of {m[2]} assets changed")
                    self.scanning = False
                    self.build_btn.config(state='normal')
                    self.update_export_btn.config(state='normal')
                    self.clear_btn.config(state='normal')
                    self._refresh_tree()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _start_update_export(self):
        if self.scanning:
            return
        if not self.last_dir:
            self.status_var.set("Set a root directory first")
            return
        self.scanning = True
        self.build_btn.config(state='disabled')
        self.update_export_btn.config(state='disabled')
        self.clear_btn.config(state='disabled')
        self.status_var.set("Updating export status...")
        self.pb['value'] = 0
        t = threading.Thread(target=self._update_export_worker, daemon=True)
        t.start()

    def _update_export_worker(self):
        """Re-check GLB/PNG existence for all assets and update the exported column."""
        assets = self.db.all_assets()
        self.queue.put(('total', len(assets)))
        updated = 0
        for i, a in enumerate(assets):
            atype = a.get('type', '') or ''
            fp = a.get('file_path', '')
            if fp and self.last_dir:
                abs_path = os.path.join(self.last_dir, fp)
                if 'Mesh' in atype:
                    new_val = 1 if resolve_asset_path(abs_path, '.glb') else 0
                elif 'Texture' in atype:
                    new_val = 1 if resolve_asset_path(abs_path, '.png') else 0
                else:
                    new_val = 0
                if new_val != a.get('exported', 0):
                    self.conn_or_db_execute_export(a['id'], new_val)
                    updated += 1
            if (i + 1) % 50 == 0 or i + 1 == len(assets):
                self.queue.put(('progress', i + 1))
        self.db.commit()
        self.queue.put(('export_done', updated, len(assets)))

    def conn_or_db_execute_export(self, aid, val):
        self.db.conn.execute('UPDATE assets SET exported=? WHERE id=?', (val, aid))

    def _clear_db(self):
        self.db.clear()
        self._refresh_tree()
        self.status_var.set("Database cleared")
        self.canvas.show(None)

    def _refresh_tree(self):
        # Cache the full asset list so filtering can rebuild without touching the DB.
        self._all_assets = self.db.all_assets() if self.db else []
        self._build_tree(self.search_var.get() if hasattr(self, 'search_var') else '')

    def _build_tree(self, query=''):
        """(Re)build the tree, optionally filtered by a case-insensitive query."""
        q = (query or '').strip().lower()
        glb_only = getattr(self, 'glb_only_var', None) and self.glb_only_var.get()
        self.tree.delete(*self.tree.get_children())
        self.tree_map.clear()
        assets = getattr(self, '_all_assets', None)
        if assets is None:
            assets = self.db.all_assets() if self.db else []
            self._all_assets = assets

        groups = defaultdict(list)
        for a in assets:
            if q and q not in a['name'].lower():
                continue
            if glb_only:
                # Only show meshes that have a GLB exported
                if 'Mesh' not in (a.get('type') or ''):
                    continue
                if not a.get('exported', 0):
                    continue
            groups[a['type']].append(a)

        for atype in sorted(groups):
            items = groups[atype]
            n = len(items)
            # Expand groups automatically while searching so matches are visible.
            parent = self.tree.insert('', 'end', text=f"{atype} ({n})",
                                      open=bool(q))
            for a in items:
                # Show export status for mesh assets
                if 'Mesh' in (a.get('type') or ''):
                    exported = a.get('exported', 0)
                    mark = '✓' if exported else '✗'
                    label = f"[{mark}] {a['name']}"
                    type_label = f"{atype} {mark}"
                else:
                    label = a['name']
                    type_label = atype
                item = self.tree.insert(parent, 'end', text=label, values=(type_label,))
                self.tree_map[item] = a['package_path']

    def _filter_tree(self):
        self._build_tree(self.search_var.get())

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
            # Export status
            if 'Mesh' in atype:
                exported = info.get('exported')
                if exported is not None:
                    status = "GLB exported ✓" if exported else "GLB NOT exported ✗"
                    self.detail_text.insert('end', f"Export: {status}\n")
            elif 'Texture' in atype:
                exported = info.get('exported')
                if exported is not None:
                    status = "PNG exported ✓" if exported else "PNG NOT exported ✗"
                    self.detail_text.insert('end', f"Export: {status}\n")
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
        self._update_preview(info, path)

    def _refresh_graph(self):
        if self.canvas.center:
            self.canvas.show(self.canvas.center, self.depth_var.get())

    def _update_preview(self, info, path=None):
        """Update the preview area: show PNG for textures, wireframe for meshes."""
        self.preview_canvas.delete('all')
        self.preview_photo = None
        self._preview_mode = None
        self._drag_last = None
        self.preview_canvas.update_idletasks()
        cw = max(self.preview_canvas.winfo_width(), 280)
        ch = max(self.preview_canvas.winfo_height(), 200)
        if not info:
            self.preview_canvas.create_text(cw//2, ch//2, anchor='center', text='No preview',
                                            fill='#666', font=('Segoe UI', 9))
            return
        atype = info.get('type') or info.get('target_type') or ''
        fp = info.get('file_path')
        if not fp or not self.last_dir:
            self.preview_canvas.create_text(cw//2, ch//2, anchor='center', text='No file',
                                            fill='#666', font=('Segoe UI', 9))
            return
        full_path = os.path.join(self.last_dir, fp)
        if 'Texture' in atype:
            png_path = self._find_asset_file(full_path, '.png')
            if png_path:
                self._show_texture_preview(png_path)
            else:
                self.preview_canvas.create_text(cw//2, ch//2, anchor='center', text='Texture PNG not found',
                                                fill='#888', font=('Segoe UI', 8))
        elif 'Mesh' in atype:
            # GLB is in the same directory as the JSON
            glb_path = self._find_glb(full_path)
            if glb_path:
                self._render_wireframe_glb(glb_path)
            elif os.path.isfile(full_path):
                self._render_wireframe(full_path)
            else:
                self.preview_canvas.create_text(cw//2, ch//2, anchor='center', text='Mesh file not found',
                                                fill='#888', font=('Segoe UI', 8))
        else:
            self.preview_canvas.create_text(cw//2, ch//2, anchor='center', text=f'({atype})',
                                            fill='#666', font=('Segoe UI', 8))

    def _show_texture_preview(self, png_path):
        """Load a PNG texture, cache it, and display it (supports wheel zoom)."""
        try:
            img = Image.open(png_path)
            img.load()
            self._tex_img = img
            self._tex_zoom = 1.0
            self._preview_mode = 'texture'
            self._redraw_texture()
        except Exception as e:
            self._preview_mode = None
            cw = max(self.preview_canvas.winfo_width(), 280)
            ch = max(self.preview_canvas.winfo_height(), 200)
            self.preview_canvas.delete('all')
            self.preview_canvas.create_text(cw//2, ch//2, anchor='center', text=f'Error: {e}',
                                            fill='#A55', font=('Segoe UI', 8))

    def _redraw_texture(self):
        """Render the cached texture at the current zoom, centered."""
        if self._tex_img is None:
            return
        self.preview_canvas.update_idletasks()
        cw = max(self.preview_canvas.winfo_width(), 100)
        ch = max(self.preview_canvas.winfo_height(), 100)
        iw, ih = self._tex_img.size
        # Fit-to-canvas base scale, then apply user zoom
        fit = min(cw / iw, ch / ih, 1.0)
        scale = fit * self._tex_zoom
        nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
        resample = Image.LANCZOS if scale < 1 else Image.NEAREST
        img = self._tex_img.resize((nw, nh), resample)
        self.preview_photo = ImageTk.PhotoImage(img)
        self.preview_canvas.delete('all')
        self.preview_canvas.create_image(cw // 2, ch // 2, image=self.preview_photo)
        self.preview_canvas.create_text(6, ch - 6, anchor='sw',
                                        text=f'{iw}x{ih}  |  {int(scale*100)}%',
                                        fill='#999', font=('Segoe UI', 7))
        self.preview_canvas.create_text(6, 6, anchor='nw',
                                        text='wheel: zoom  |  dbl-click: reset',
                                        fill='#555', font=('Segoe UI', 7))

    def _render_wireframe(self, json_path):
        """Parse vertex/index data from a StaticMesh JSON and draw wireframe."""
        try:
            with open(json_path, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            entries = data if isinstance(data, list) else [data]
            verts = []
            indices = []
            for e in entries:
                if not isinstance(e, dict):
                    continue
                props = e.get('Properties') or {}
                agg = props.get('AggGeom') or {}
                for ce in agg.get('ConvexElems', []):
                    vd = ce.get('VertexData', [])
                    base = len(verts)
                    for v in vd:
                        verts.append((v['X'], v['Y'], v['Z']))
                    idata = ce.get('IndexData', [])
                    for i in range(0, len(idata), 3):
                        if i + 2 < len(idata):
                            a, b, c = idata[i], idata[i+1], idata[i+2]
                            indices.append((a + base, b + base, c + base))
            if not verts:
                cw = max(self.preview_canvas.winfo_width(), 280)
                ch = max(self.preview_canvas.winfo_height(), 200)
                self.preview_canvas.create_text(cw//2, ch//2, anchor='center', text='No vertex data',
                                                fill='#888', font=('Segoe UI', 8))
                return
            self._draw_wireframe(verts, indices)
        except Exception as e:
            cw = max(self.preview_canvas.winfo_width(), 280)
            ch = max(self.preview_canvas.winfo_height(), 200)
            self.preview_canvas.create_text(cw//2, ch//2, anchor='center', text=f'Error: {e}',
                                            fill='#A55', font=('Segoe UI', 8))

    def _find_asset_file(self, json_path, ext):
        """Find a sibling asset file (.glb / .png) by swapping the .json
        extension for `ext` in the same directory.
        Delegates to the module-level resolve_asset_path for reuse."""
        return resolve_asset_path(json_path, ext)

    def _find_glb(self, json_path):
        """Find the GLB file corresponding to a mesh JSON."""
        return self._find_asset_file(json_path, '.glb')

    def _render_wireframe_glb(self, glb_path):
        """Parse a GLB in a background thread, then draw wireframe on the main thread."""
        cw = max(self.preview_canvas.winfo_width(), 280)
        ch = max(self.preview_canvas.winfo_height(), 200)
        self.preview_canvas.create_text(cw//2, ch//2, anchor='center',
                                        text='Loading mesh...', fill='#888', font=('Segoe UI', 9))
        token = object()
        self._preview_token = token

        def worker():
            try:
                result = self._parse_glb(glb_path)
                err = None
            except Exception as e:
                result, err = None, str(e)

            def apply():
                # Ignore if user selected another asset meanwhile
                if getattr(self, '_preview_token', None) is not token:
                    return
                self.preview_canvas.delete('all')
                if err:
                    self.preview_canvas.create_text(cw//2, ch//2, anchor='center',
                                                    text=f'Error: {err}', fill='#A55', font=('Segoe UI', 8))
                elif not result or not result[0]:
                    self.preview_canvas.create_text(cw//2, ch//2, anchor='center',
                                                    text='No vertex data in GLB', fill='#888', font=('Segoe UI', 8))
                else:
                    verts, indices, total_tris = result
                    self._draw_wireframe(verts, indices, total_tris)
            self.root.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def _parse_glb(self, glb_path):
        """Parse GLB geometry for wireframe preview.

        Vertices are interleaved (byteStride can be e.g. 108 bytes with
        NORMAL/TANGENT/UVs). Meshes here are often Nanite (>1M triangles) whose
        triangles are grouped by spatial cluster, so we uniformly stride across
        ALL triangles: this scatters samples over the whole surface and, given
        the extreme density, still reveals the overall shape.

        Returns (verts, indices, total_triangle_count).
        """
        import struct
        from pygltflib import GLTF2
        MAX_TRIS = 20000
        gltf = GLTF2().load(glb_path)
        blob = gltf.binary_blob()
        if not blob:
            return ([], [], 0)

        # First pass: gather primitive layouts and total triangle count.
        prims = []
        total_tris = 0
        for mesh in gltf.meshes:
            for prim in mesh.primitives:
                if prim.mode not in (None, 4):  # only TRIANGLES
                    continue
                if prim.attributes.POSITION is None:
                    continue
                pos_acc = gltf.accessors[prim.attributes.POSITION]
                bv = gltf.bufferViews[pos_acc.bufferView]
                pstart = (bv.byteOffset or 0) + (pos_acc.byteOffset or 0)
                pstride = bv.byteStride or 12  # interleaved => real stride (e.g. 108)
                pcount = pos_acc.count
                entry = {'pstart': pstart, 'pstride': pstride, 'pcount': pcount}
                if prim.indices is not None:
                    idx_acc = gltf.accessors[prim.indices]
                    ibv = gltf.bufferViews[idx_acc.bufferView]
                    istart = (ibv.byteOffset or 0) + (idx_acc.byteOffset or 0)
                    fmt, sz = {5121: ('<B', 1), 5123: ('<H', 2), 5125: ('<I', 4)}.get(
                        idx_acc.componentType, ('<I', 4))
                    entry.update({'indexed': True, 'istart': istart, 'fmt': fmt, 'sz': sz,
                                  'tri_count': idx_acc.count // 3})
                else:
                    entry.update({'indexed': False, 'tri_count': pcount // 3})
                prims.append(entry)
                total_tris += entry['tri_count']

        if total_tris == 0:
            return ([], [], 0)

        step = max(1, total_tris // MAX_TRIS)

        verts = []
        indices = []
        vert_cache = {}

        def get_vert(pstart, pstride, local_vi):
            key = (pstart, local_vi)
            cached = vert_cache.get(key)
            if cached is not None:
                return cached
            off = pstart + local_vi * pstride
            x, y, z = struct.unpack_from('<fff', blob, off)
            idx = len(verts)
            verts.append((x, y, z))
            vert_cache[key] = idx
            return idx

        # Uniform stride across every triangle of every primitive.
        for prim in prims:
            pstart, pstride, pcount = prim['pstart'], prim['pstride'], prim['pcount']
            if prim['indexed']:
                istart, fmt, sz = prim['istart'], prim['fmt'], prim['sz']
                for t in range(0, prim['tri_count'], step):
                    k = t * 3
                    a = struct.unpack_from(fmt, blob, istart + k * sz)[0]
                    b = struct.unpack_from(fmt, blob, istart + (k + 1) * sz)[0]
                    c = struct.unpack_from(fmt, blob, istart + (k + 2) * sz)[0]
                    if a < pcount and b < pcount and c < pcount:
                        indices.append((get_vert(pstart, pstride, a),
                                        get_vert(pstart, pstride, b),
                                        get_vert(pstart, pstride, c)))
            else:
                for t in range(0, prim['tri_count'], step):
                    base = t * 3
                    if base + 2 < pcount:
                        indices.append((get_vert(pstart, pstride, base),
                                        get_vert(pstart, pstride, base + 1),
                                        get_vert(pstart, pstride, base + 2)))

        return (verts, indices, total_tris)

    def _draw_wireframe(self, verts, indices, total_tris=None):
        """Prepare wireframe geometry, cache it, and draw with the current view.

        total_tris: original triangle count before sampling (for the label).
        """
        # Compute bounds and center the model at origin.
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        zs = [v[2] for v in verts]
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
        cz = (min(zs) + max(zs)) / 2
        centered = [(v[0] - cx, v[1] - cy, v[2] - cz) for v in verts]

        # Deduplicate edges. Sampling already happened in _parse_glb (contiguous
        # runs), so here we only build the unique edge set.
        drawn_tris = len(indices)
        orig_tris = total_tris if total_tris is not None else drawn_tris
        n = len(centered)
        seen = set()
        edges = []
        for a, b, c in indices:
            if a >= n or b >= n or c >= n:
                continue
            for u, v in ((a, b), (b, c), (c, a)):
                e = (u, v) if u < v else (v, u)
                if e not in seen:
                    seen.add(e)
                    edges.append(e)

        # Base scale so the model fits the canvas at zoom 1.0 (max extent across any axis).
        ext = max(max(xs)-min(xs), max(ys)-min(ys), max(zs)-min(zs)) or 1.0

        # Cache and (re)set the view.
        self._preview_mode = 'mesh'
        self._mesh_verts = centered
        self._mesh_edges = edges
        self._mesh_meta = (ext, orig_tris, drawn_tris)
        self._mesh_rot_x = -25.0
        self._mesh_rot_y = 30.0
        self._mesh_zoom = 1.0
        self._redraw_mesh()

    def _redraw_mesh(self):
        """Project cached mesh verts with current rotation/zoom and draw edges."""
        import math
        if not self._mesh_verts or not self._mesh_edges:
            return
        ext, orig_tris, shown = self._mesh_meta
        self.preview_canvas.delete('all')
        self.preview_canvas.update_idletasks()
        cw = max(self.preview_canvas.winfo_width(), 100)
        ch = max(self.preview_canvas.winfo_height(), 100)
        ay = math.radians(self._mesh_rot_y)
        ax = math.radians(self._mesh_rot_x)
        cosa, sina = math.cos(ay), math.sin(ay)
        cosx, sinx = math.cos(ax), math.sin(ax)
        # Project all verts
        proj = []
        for vx, vy, vz in self._mesh_verts:
            x1 = vx * cosa - vy * sina
            y1 = vx * sina + vy * cosa
            z1 = vz
            x2 = x1
            y2 = y1 * cosx - z1 * sinx
            proj.append((x2, y2))
        # Fit: base scale makes the model span ~76% of the smaller canvas dim at zoom 1
        base_scale = min(cw, ch) * 0.76 / ext
        scale = base_scale * self._mesh_zoom
        ox, oy = cw / 2, ch / 2
        pts = [(ox + x * scale, oy - y * scale) for x, y in proj]
        for u, v in self._mesh_edges:
            x1, y1 = pts[u]
            x2, y2 = pts[v]
            self.preview_canvas.create_line(x1, y1, x2, y2, fill='#4A90D9', width=1)
        # Info label
        note = f"{orig_tris:,} tris"
        if orig_tris > shown:
            note += f" (~{shown:,} shown)"
        self.preview_canvas.create_text(6, ch - 6, anchor='sw', text=note,
                                        fill='#777', font=('Segoe UI', 7))
        self.preview_canvas.create_text(6, 6, anchor='nw',
                                        text='drag: rotate  |  wheel: zoom  |  dbl-click: reset',
                                        fill='#555', font=('Segoe UI', 7))

    # ---- Preview interaction handlers ----
    def _preview_drag_start(self, e):
        self._drag_last = (e.x, e.y)

    def _preview_drag_move(self, e):
        if self._drag_last is None or self._preview_mode != 'mesh':
            return
        dx = e.x - self._drag_last[0]
        dy = e.y - self._drag_last[1]
        self._drag_last = (e.x, e.y)
        # Horizontal drag -> yaw, vertical drag -> pitch
        self._mesh_rot_y += dx * 0.5
        self._mesh_rot_x += dy * 0.5
        # Clamp pitch to avoid flipping
        self._mesh_rot_x = max(-89.0, min(89.0, self._mesh_rot_x))
        self._redraw_mesh()

    def _preview_drag_end(self, e):
        self._drag_last = None

    def _preview_wheel(self, e):
        # Normalize wheel direction across platforms
        if getattr(e, 'num', None) == 5 or getattr(e, 'delta', 0) < 0:
            factor = 1 / 1.15
        else:
            factor = 1.15
        if self._preview_mode == 'mesh':
            self._mesh_zoom = max(0.1, min(30.0, self._mesh_zoom * factor))
            self._redraw_mesh()
        elif self._preview_mode == 'texture':
            self._tex_zoom = max(0.1, min(20.0, self._tex_zoom * factor))
            self._redraw_texture()

    def _preview_reset_view(self, e):
        if self._preview_mode == 'mesh':
            self._mesh_rot_x = -25.0
            self._mesh_rot_y = 30.0
            self._mesh_zoom = 1.0
            self._redraw_mesh()
        elif self._preview_mode == 'texture':
            self._tex_zoom = 1.0
            self._redraw_texture()

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
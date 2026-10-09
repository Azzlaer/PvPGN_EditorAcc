"""LatinBattle PvPGN Account Manager - schema-adaptive MySQL editor."""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import threading
import tkinter as tk
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:
    pymysql = None

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"
LOG_DIR = APP_DIR / "logs"
TABLES = ("pvpgn_bnet", "pvpgn_profile")
IDENT = re.compile(r"^[A-Za-z0-9_]+$")
SENSITIVE = re.compile(r"password|passwd|secret|token|salt|hash|^pass$|^acct_pass", re.I)
PAGE_SIZE = 100


def quoted(name):
    if not IDENT.fullmatch(name):
        raise ValueError("Nombre de tabla o columna no permitido")
    return '`' + name + '`'


def safe_str(value):
    if value is None:
        return "∅ NULL"
    if isinstance(value, bytes):
        return "0x" + value.hex()
    return str(value)


def to_db(raw, info):
    if raw == "∅ NULL":
        if info["Null"] == "YES":
            return None
        raise ValueError("Esta columna no admite NULL")
    kind = info["Type"].lower()
    if kind.startswith(("binary", "varbinary", "blob", "tinyblob", "mediumblob", "longblob")):
        if raw.startswith("0x"):
            return bytes.fromhex(raw[2:])
        raise ValueError("Los campos binarios deben comenzar con 0x (hexadecimal)")
    return raw


class Database:
    def __init__(self, settings, password):
        self.settings = settings
        self.password = password

    def connect(self):
        if pymysql is None:
            raise RuntimeError("Falta PyMySQL. Ejecuta: pip install -r requirements.txt")
        opts = dict(host=self.settings["host"], port=int(self.settings["port"]),
                    user=self.settings["user"], password=self.password,
                    database=self.settings["database"], charset="utf8mb4",
                    cursorclass=DictCursor, connect_timeout=8, read_timeout=20,
                    write_timeout=20, autocommit=False)
        ssl_ca = self.settings.get("ssl_ca", "").strip()
        if ssl_ca:
            opts["ssl_ca"] = ssl_ca
            opts["ssl_verify_cert"] = True
            opts["ssl_verify_identity"] = True
        return pymysql.connect(**opts)

    def inspect(self, table):
        with self.connect() as con:
            with con.cursor() as cur:
                cur.execute("SHOW TABLES LIKE %s", (table,))
                if not cur.fetchone():
                    raise RuntimeError(f"No existe la tabla {table} en esta base de datos")
                cur.execute(f"SHOW FULL COLUMNS FROM {quoted(table)}")
                cols = cur.fetchall()
                cur.execute(f"SHOW INDEX FROM {quoted(table)}")
                indexes = cur.fetchall()
            con.commit()
        pk = [c["Field"] for c in cols if c["Key"] == "PRI"]
        # Only use a unique index where no constituent field is nullable.
        if not pk:
            candidates = {}
            for idx in indexes:
                if idx["Non_unique"] == 0:
                    candidates.setdefault(idx["Key_name"], []).append(idx)
            field_map = {c["Field"]: c for c in cols}
            for items in candidates.values():
                items.sort(key=lambda x: x["Seq_in_index"])
                fields = [i["Column_name"] for i in items]
                if all(f and f in field_map and field_map[f]["Null"] == "NO" for f in fields):
                    pk = fields
                    break
        return cols, pk

    def list_rows(self, table, columns, pk, search, offset):
        names = [c["Field"] for c in columns]
        # Search first up to 12 textual fields, never search sensitive fields.
        textual = [c for c in columns if any(x in c["Type"].lower() for x in
                  ("char", "text", "enum")) and not SENSITIVE.search(c["Field"])][:12]
        where, params = "", []
        if search.strip():
            if not textual:
                return [], 0
            where = " WHERE " + " OR ".join(f"{quoted(c['Field'])} LIKE %s" for c in textual)
            params = ["%" + search.strip() + "%"] * len(textual)
        order = " ORDER BY " + ",".join(quoted(k) for k in pk) if pk else ""
        with self.connect() as con:
            with con.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS n FROM {quoted(table)}{where}", params)
                total = cur.fetchone()["n"]
                cur.execute(f"SELECT * FROM {quoted(table)}{where}{order} LIMIT %s OFFSET %s",
                            params + [PAGE_SIZE, offset])
                rows = cur.fetchall()
            con.commit()
        return rows, total

    def update(self, table, changed, key):
        if not key or not changed:
            return 0
        condition = " AND ".join(f"{quoted(k)} <=> %s" for k in key)
        stmt = (f"UPDATE {quoted(table)} SET " +
                ",".join(f"{quoted(k)}=%s" for k in changed) +
                f" WHERE {condition} LIMIT 1")
        with self.connect() as con:
            try:
                with con.cursor() as cur:
                    count = cur.execute(stmt, list(changed.values()) + list(key.values()))
                con.commit()
                return count
            except Exception:
                con.rollback()
                raise

    def delete(self, table, key):
        if not key:
            raise ValueError("No existe una clave única para identificar el registro")
        condition = " AND ".join(f"{quoted(k)} <=> %s" for k in key)
        with self.connect() as con:
            try:
                with con.cursor() as cur:
                    count = cur.execute(f"DELETE FROM {quoted(table)} WHERE {condition} LIMIT 1",
                                        list(key.values()))
                con.commit()
                return count
            except Exception:
                con.rollback()
                raise

    def export(self, table, dest):
        # CSV export intentionally masks fields that may contain auth data.
        cols, _ = self.inspect(table)
        names = [c["Field"] for c in cols]
        count = 0
        with self.connect() as con:
            with con.cursor() as cur, open(dest, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerow(names)
                cur.execute(f"SELECT * FROM {quoted(table)}")
                while True:
                    chunk = cur.fetchmany(500)
                    if not chunk:
                        break
                    for record in chunk:
                        writer.writerow(["[REDACTED]" if SENSITIVE.search(k) else
                                         ("" if record[k] is None else safe_str(record[k]))
                                         for k in names])
                    count += len(chunk)
            con.commit()
        return count


class Editor(tk.Toplevel):
    def __init__(self, parent, columns, row, key_fields, save_fn):
        super().__init__(parent)
        self.title("Editar registro · LatinBattle")
        self.geometry("780x680")
        self.configure(bg="#111827")
        self.transient(parent)
        self.grab_set()
        self.entries = {}
        self.original = row
        self.columns = columns
        self.key_fields = key_fields
        self.save_fn = save_fn
        box = ttk.Frame(self, padding=14)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="Editar campos", style="Title.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(box, text="∅ NULL = valor nulo. Las claves identificadoras no se modifican. Los campos de contraseña/hash deben respetar el formato de PvPGN.", wraplength=720).pack(anchor="w", pady=(0, 12))
        outer = ttk.Frame(box)
        outer.pack(fill="both", expand=True)
        cv = tk.Canvas(outer, background="#172033", highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(cv)
        window = cv.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfigure(window, width=e.width))
        for c in columns:
            field = c["Field"]
            panel = ttk.Frame(inner)
            panel.pack(fill="x", padx=6, pady=4)
            ttk.Label(panel, text=f"{field}   ({c['Type']})", width=38).pack(side="left")
            v = tk.StringVar(value=safe_str(row.get(field)))
            e = ttk.Entry(panel, textvariable=v, show="•" if SENSITIVE.search(field) else "")
            e.pack(side="left", fill="x", expand=True)
            if field in key_fields or "auto_increment" in c["Extra"].lower() or "GENERATED" in c["Extra"].upper():
                e.state(["disabled"])
            self.entries[field] = v
        buttons = ttk.Frame(box)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Guardar cambios", command=self.save).pack(side="right", padx=4)
        ttk.Button(buttons, text="Cancelar", command=self.destroy).pack(side="right", padx=4)

    def save(self):
        changed = {}
        try:
            for c in self.columns:
                f = c["Field"]
                if f in self.key_fields or "auto_increment" in c["Extra"].lower() or "GENERATED" in c["Extra"].upper():
                    continue
                value = self.entries[f].get()
                if value != safe_str(self.original.get(f)):
                    changed[f] = to_db(value, c)
            if not changed:
                messagebox.showinfo("Sin cambios", "No modificaste ningún campo.", parent=self)
                return
            if any(SENSITIVE.search(f) for f in changed):
                if not messagebox.askyesno("Campo sensible", "Estás editando un campo de autenticación. Este editor NO calcula automáticamente hashes de PvPGN. ¿Confirmas que el valor ya está en el formato correcto?", parent=self):
                    return
            if self.save_fn(changed, {f: self.original[f] for f in self.key_fields}):
                self.destroy()
        except Exception as ex:
            messagebox.showerror("Error de validación", str(ex), parent=self)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LatinBattle · PvPGN Account Manager")
        self.geometry("1220x760")
        self.minsize(900, 560)
        self.settings = {"host":"127.0.0.1", "port":3306, "user":"latinbattle",
                         "database":"latinbat_pvpgn", "ssl_ca":""}
        if CONFIG_FILE.exists():
            try:
                self.settings.update({k:v for k,v in json.loads(CONFIG_FILE.read_text(encoding="utf-8")).items() if k in self.settings})
            except Exception:
                pass
        self.db = None
        self.table = tk.StringVar(value=TABLES[0])
        self.search = tk.StringVar()
        self.status = tk.StringVar(value="Desconectado")
        self.host = tk.StringVar(value=self.settings["host"])
        self.port = tk.StringVar(value=str(self.settings["port"]))
        self.user = tk.StringVar(value=self.settings["user"])
        self.database = tk.StringVar(value=self.settings["database"])
        self.ssl_ca = tk.StringVar(value=self.settings["ssl_ca"])
        self.password = tk.StringVar(value=os.environ.get("LATINBATTLE_DB_PASSWORD", ""))
        self.columns = []
        self.key_fields = []
        self.rows = []
        self.offset = 0
        self.total = 0
        self.busy = False
        self.make_style()
        self.make_ui()
        self.table.trace_add("write", lambda *_: self.change_table())
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def make_style(self):
        self.configure(bg="#0e1625")
        sty = ttk.Style(self)
        sty.theme_use("clam")
        sty.configure(".", background="#172033", foreground="#e8eefc", fieldbackground="#202d45", font=("Segoe UI", 10))
        sty.configure("TFrame", background="#172033")
        sty.configure("TLabel", background="#172033", foreground="#e8eefc")
        sty.configure("Title.TLabel", font=("Segoe UI Semibold", 19), foreground="#66d9ef")
        sty.configure("TButton", background="#293c58", foreground="white", padding=7, borderwidth=0)
        sty.map("TButton", background=[("active", "#386389")])
        sty.configure("TEntry", foreground="white", fieldbackground="#202d45", padding=5)
        sty.configure("TCombobox", foreground="white", fieldbackground="#202d45")
        sty.configure("Treeview", background="#182339", fieldbackground="#182339", foreground="#e3edf7", rowheight=27, borderwidth=0)
        sty.map("Treeview", background=[("selected", "#285880")])
        sty.configure("Treeview.Heading", background="#253852", foreground="#f5fafc", font=("Segoe UI Semibold", 10))
        sty.configure("TLabelframe", background="#172033")
        sty.configure("TLabelframe.Label", background="#172033", foreground="#66d9ef")

    def make_ui(self):
        main = ttk.Frame(self, padding=16)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="⚔  LatinBattle · PvPGN Account Manager", style="Title.TLabel").pack(anchor="w", pady=(0, 10))
        connection = ttk.LabelFrame(main, text="Conexión MySQL / MariaDB", padding=10)
        connection.pack(fill="x")
        items = [("Host",self.host,14), ("Puerto",self.port,7), ("Usuario",self.user,14),
                 ("Base de datos",self.database,19), ("Contraseña",self.password,17)]
        top = ttk.Frame(connection)
        top.pack(fill="x")
        for label, variable, width in items:
            slot = ttk.Frame(top)
            slot.pack(side="left", padx=(0, 12))
            ttk.Label(slot, text=label).pack(anchor="w")
            ttk.Entry(slot, textvariable=variable, width=width, show="•" if label=="Contraseña" else "").pack()
        ttk.Button(top, text="Conectar", command=self.connect).pack(side="left", padx=6, pady=(19, 0))
        ttk.Button(top, text="Desconectar", command=self.disconnect).pack(side="left", padx=4, pady=(19, 0))
        row2 = ttk.Frame(connection)
        row2.pack(fill="x", pady=(7, 0))
        ttk.Label(row2, text="CA TLS (opcional):").pack(side="left")
        ttk.Entry(row2, textvariable=self.ssl_ca, width=55).pack(side="left", padx=5)
        ttk.Button(row2, text="Buscar CA", command=self.choose_ca).pack(side="left")
        ttk.Label(row2, text="La contraseña no se guarda en config.json").pack(side="right")

        actions = ttk.Frame(main)
        actions.pack(fill="x", pady=12)
        ttk.Label(actions, text="Tabla:").pack(side="left")
        ttk.Combobox(actions, state="readonly", values=TABLES, textvariable=self.table, width=19).pack(side="left", padx=7)
        ttk.Label(actions, text="Buscar:").pack(side="left", padx=(14, 0))
        entry = ttk.Entry(actions, textvariable=self.search, width=32)
        entry.pack(side="left", padx=6)
        entry.bind("<Return>", lambda e: self.refresh(reset=True))
        ttk.Button(actions, text="Buscar", command=lambda: self.refresh(reset=True)).pack(side="left", padx=3)
        ttk.Button(actions, text="Actualizar", command=lambda: self.refresh(reset=False)).pack(side="left", padx=3)
        ttk.Button(actions, text="Editar seleccionado", command=self.edit).pack(side="left", padx=3)
        ttk.Button(actions, text="Eliminar", command=self.delete).pack(side="left", padx=3)
        ttk.Button(actions, text="Exportar CSV", command=self.export).pack(side="right")

        area = ttk.Frame(main)
        area.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(area, show="headings", selectmode="browse")
        vs = ttk.Scrollbar(area, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(area, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.edit())
        foot = ttk.Frame(main)
        foot.pack(fill="x", pady=(10,0))
        ttk.Label(foot, textvariable=self.status).pack(side="left")
        ttk.Button(foot, text="◀ Anterior", command=self.prev).pack(side="right", padx=4)
        ttk.Button(foot, text="Siguiente ▶", command=self.next).pack(side="right", padx=4)
        self.page_label = ttk.Label(foot, text="Página 0 de 0")
        self.page_label.pack(side="right", padx=12)
        ttk.Label(main, text="Seguridad: las columnas sensibles se ocultan en la tabla y se censuran en CSV. Solo se editan registros con clave primaria o índice único no nulo.").pack(anchor="w", pady=(9,0))

    def choose_ca(self):
        p = filedialog.askopenfilename(title="Seleccionar certificado CA", filetypes=[("Certificados", "*.pem *.crt *.cer"), ("Todos", "*.*")])
        if p:
            self.ssl_ca.set(p)

    def run_task(self, task, done):
        if self.busy:
            return
        self.busy = True
        self.config(cursor="watch")
        def work():
            try:
                result = task()
                self.after(0, lambda: finish(result, None))
            except Exception as ex:
                message = str(ex)
                self.after(0, lambda: finish(None, message))
        def finish(result, error):
            self.busy = False
            self.config(cursor="")
            if error:
                self.status.set("Error de operación")
                messagebox.showerror("Error MySQL", error, parent=self)
            else:
                done(result)
        threading.Thread(target=work, daemon=True).start()

    def connect(self):
        try:
            settings = {"host":self.host.get().strip(), "port":int(self.port.get()),
                        "user":self.user.get().strip(), "database":self.database.get().strip(),
                        "ssl_ca":self.ssl_ca.get().strip()}
            if not 1 <= settings["port"] <= 65535:
                raise ValueError("Puerto fuera de rango")
            if not all(settings[k] for k in ("host", "user", "database")):
                raise ValueError("Faltan datos de conexión")
            db = Database(settings, self.password.get())
        except Exception as ex:
            messagebox.showerror("Configuración", str(ex), parent=self)
            return
        def task():
            cols, keys = db.inspect(self.table.get())
            rows, total = db.list_rows(self.table.get(), cols, keys, "", 0)
            return db, settings, cols, keys, rows, total
        def done(result):
            self.db, self.settings, self.columns, self.key_fields, self.rows, self.total = result
            self.offset = 0
            try:
                CONFIG_FILE.write_text(json.dumps(self.settings, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            self.search.set("")
            self.draw()
            self.status.set(f"Conectado a {self.settings['database']} · {self.table.get()}")
        self.run_task(task, done)

    def disconnect(self):
        if self.busy:
            return
        self.db = None
        self.rows = []
        self.columns = []
        self.key_fields = []
        self.password.set("")
        self.draw()
        self.status.set("Desconectado")

    def change_table(self):
        if self.db:
            table = self.table.get()
            def task():
                cols, keys = self.db.inspect(table)
                rows, total = self.db.list_rows(table, cols, keys, "", 0)
                return cols, keys, rows, total
            def done(result):
                if self.table.get() != table:
                    return
                self.columns, self.key_fields, self.rows, self.total = result
                self.offset = 0
                self.search.set("")
                self.draw()
            self.run_task(task, done)

    def refresh(self, reset=False):
        if not self.db:
            messagebox.showwarning("Desconectado", "Primero conecta a MySQL", parent=self)
            return
        table = self.table.get()
        offset = 0 if reset else self.offset
        search = self.search.get()
        def task():
            return self.db.list_rows(table, self.columns, self.key_fields, search, offset)
        def done(result):
            if self.table.get() != table:
                return
            self.rows, self.total = result
            self.offset = offset
            self.draw()
        self.run_task(task, done)

    def draw(self):
        names = [c["Field"] for c in self.columns]
        self.tree.delete(*self.tree.get_children())
        self.tree.configure(columns=names)
        for name in names:
            self.tree.heading(name, text=name)
            self.tree.column(name, width=160, minwidth=90, anchor="w", stretch=False)
        for i, row in enumerate(self.rows):
            self.tree.insert("", "end", iid=str(i), values=["••••••" if SENSITIVE.search(n) and row.get(n) is not None else safe_str(row.get(n)) for n in names])
        pages = max(1, (self.total + PAGE_SIZE - 1)//PAGE_SIZE)
        self.page_label.config(text=f"Página {self.offset//PAGE_SIZE + 1 if self.db else 0} de {pages if self.db else 0} · {self.total} registros")
        if self.db:
            self.status.set(f"{self.table.get()} · {len(self.rows)} mostrados · {'Clave: ' + ', '.join(self.key_fields) if self.key_fields else 'SIN CLAVE ÚNICA: solo lectura'}")

    def selected(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("Seleccionar", "Selecciona una cuenta primero", parent=self)
            return None
        return self.rows[int(selection[0])]

    def edit(self):
        if not self.db:
            return
        if not self.key_fields:
            messagebox.showwarning("Solo lectura", "La tabla no tiene una clave primaria ni índice único no nulo. No es seguro editar filas individuales.", parent=self)
            return
        row = self.selected()
        if row is None:
            return
        Editor(self, self.columns, row, self.key_fields, self.save)

    def save(self, changed, key):
        # Writes are synchronous so the editor closes only when COMMIT succeeded.
        try:
            count = self.db.update(self.table.get(), changed, key)
            if count != 1:
                messagebox.showwarning("Sin cambios", "No se modificó exactamente una fila. Comprueba si otro administrador cambió la cuenta.", parent=self)
                return False
            self.refresh()
            return True
        except Exception as ex:
            messagebox.showerror("No se pudo guardar", str(ex), parent=self)
            return False

    def delete(self):
        if not self.db or not self.key_fields:
            messagebox.showwarning("No disponible", "Para eliminar debe existir una clave única.", parent=self)
            return
        row = self.selected()
        if row is None:
            return
        key = {k:row[k] for k in self.key_fields}
        if not messagebox.askyesno("Eliminar cuenta", f"¿Eliminar permanentemente este registro de {self.table.get()}?\n\nClave: {key}\n\nLa operación no se puede deshacer. Haz un respaldo SQL previo.", icon="warning", parent=self):
            return
        table = self.table.get()
        def done(count):
            if count != 1:
                messagebox.showwarning("Atención", "No se eliminó exactamente una fila", parent=self)
            self.refresh()
        self.run_task(lambda: self.db.delete(table, key), done)

    def export(self):
        if not self.db:
            return
        table = self.table.get()
        dest = filedialog.asksaveasfilename(title="Exportar CSV censurado", defaultextension=".csv",
                                           initialfile=f"{table}_{date.today():%Y%m%d}.csv", filetypes=[("CSV", "*.csv")])
        if dest:
            self.run_task(lambda: self.db.export(table, dest),
                          lambda n: messagebox.showinfo("Exportación", f"{n} registros exportados. Columnas sensibles censuradas.", parent=self))

    def prev(self):
        if self.offset >= PAGE_SIZE:
            self.offset -= PAGE_SIZE
            self.refresh()

    def next(self):
        if self.offset + PAGE_SIZE < self.total:
            self.offset += PAGE_SIZE
            self.refresh()

    def on_close(self):
        self.destroy()


if __name__ == "__main__":
    App().mainloop()

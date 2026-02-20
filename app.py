"""
Менеджер заказов СП  v2
=======================
Прайс загружается из файлов в папке  ./prices/
Формат CSV (разделитель ; или ,):
    Наименование;Вес;Цена;НормКол
    Вяленые томаты с прованскими травами;130 гр;95.40;10

Хоткеи:
    Enter           — перейти к следующему полю / добавить позицию в корзину
    ↓               — открыть список совпадений
    Cmd/Ctrl+Enter  — зафиксировать заказ текущего заказчика
    Cmd/Ctrl+S      — показать сводку
    Cmd/Ctrl+R      — перезагрузить прайс из файлов
    F5              — перезагрузить прайс из файлов
    Escape          — закрыть подсказку / очистить поля позиции
    Cmd/Ctrl+Delete — очистить все заказы
"""

import csv
import difflib
import json
import os
import re
import subprocess
import sys
import tkinter as tk
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

def _base_dir() -> Path:
    """Папка с ресурсами — корректна и при обычном запуске, и в PyInstaller-бандле."""
    if getattr(sys, "frozen", False):
        # PyInstaller: временная папка _MEIPASS содержит все включённые ресурсы
        return Path(sys._MEIPASS)       # type: ignore[attr-defined]
    return Path(__file__).parent

def _data_dir() -> Path:
    """Папка для записи данных (orders_save.json).
    При обычном запуске — рядом с app.py.
    При запуске из .app-бандла (PyInstaller) — рядом с самим .app (на 3 уровня выше бинарника).
    """
    if getattr(sys, "frozen", False):
        # sys.executable = .../СП Заказы.app/Contents/MacOS/СП Заказы
        # parents[3] = папка, в которой лежит сам .app
        return Path(sys.executable).parents[3]
    return Path(__file__).parent

PRICES_DIR = _base_dir() / "prices"
SAVE_FILE  = _data_dir() / "orders_save.json"

# Опорные паттерны для slice-парсера строк заказа
# Вес: «130 г», «150 гр», «1,6 кг» ИЛИ «пл. ведро 1,6 / 3,1» (формат ГРЕКО)
_WEIGHT_RE = re.compile(
    r'((?:пл\.?\s*ведро\s+\d+[.,]\d+(?:\s*/\s*\d+[.,]\d+)?(?:\s*кг)?)'  # «пл. ведро 1,6 / 3,1»
    r'|(?:\d+[\.,]?\d*\s*(?:гр?|мл|кг|ml|g)\.?))',                        # «130 г», «1,6 кг»
    re.IGNORECASE,
)
_QTY_RE    = re.compile(r'(\d+)\s*шт', re.IGNORECASE)
_PRICE_RE  = re.compile(r'(\d+[.,]\d+|\d+)')


def parse_pasted_order(text: str) -> tuple[list[dict], list[str]]:
    """
    Slice-парсер строк заказа. Логика:
      1. Ищем вес (напр. «130 г», «150 гр»)  — всё до него = название.
      2. Ищем «N шт» в хвосте               — это количество.
      3. Последнее число в хвосте до «шт»    — это цена.
    Нечувствителен к пробелам, запятым, тире между полями.
    Возвращает (parsed_list, skipped_lines).
    """
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    result: list[dict] = []
    skipped: list[str] = []

    for raw_line in text.split('\n'):
        line = raw_line.strip()
        if not line:
            continue

        # 1. Находим вес — опорная точка деления строки
        wm = _WEIGHT_RE.search(line)
        if not wm:
            skipped.append(line)
            continue

        name   = line[:wm.start()].strip().rstrip('.,- ').strip()
        weight = wm.group(1).strip()
        tail   = line[wm.end():]   # всё после веса

        if not name:
            skipped.append(line)
            continue

        # 2. Количество: «N шт» в хвосте
        qm = _QTY_RE.search(tail)
        quantity = int(qm.group(1)) if qm else 1

        # 3. Цена: последнее число в части хвоста ДО «шт» (или во всём хвосте)
        price_zone = tail[:qm.start()] if qm else tail
        prices = _PRICE_RE.findall(price_zone)
        if not prices:
            skipped.append(line)
            continue
        price = float(prices[-1].replace(',', '.'))

        result.append(dict(name=name, weight=weight,
                           price=price, quantity=quantity))

    return result, skipped


# ─────────────────────────────────────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────────────────────────────────────

def fmt(value: float) -> str:
    """1 234,56"""
    return f"{value:,.2f}".replace(",", "\u202f").replace(".", ",")


def _detect_columns(header: list[str]) -> dict | None:
    """
    По строке заголовка определяет индексы нужных колонок.
    Возвращает {name, weight, price, std_qty} или None.
    """
    h = [c.lower().strip() for c in header]

    def first(keywords):
        for kw in keywords:
            for i, cell in enumerate(h):
                if kw in cell:
                    return i
        return None

    name_col    = first(["наименов"])
    weight_col  = first(["масса нетто", "масса", "вес, кг", "вес"])
    std_qty_col = first(["штук в коробке", "ведер в коробке", "штук", "ведер", "уп.", "в коробке"])
    # Цена за единицу (штуку / ведро / уп), НЕ за коробку
    price_col = None
    for kw in ["цена за штуку", "цена за ведро", "цена за уп", "цена за ед"]:
        c = first([kw])
        if c is not None:
            price_col = c
            break
    if price_col is None:
        price_col = first(["цена"])

    if name_col is None or price_col is None:
        return None
    return dict(name=name_col, weight=weight_col,
                price=price_col, std_qty=std_qty_col)


def _parse_weight(raw: str) -> str:
    """Нормализует весовое поле: '1,8 / 3,1' → '1,8 кг'"""
    raw = raw.strip()
    if "/" in raw:
        raw = raw.split("/")[0].strip()
    # добавим 'кг' если нет единицы
    has_unit = any(u in raw.lower() for u in ("г", "кг", "мл", "л", "g", "kg"))
    if raw and not has_unit:
        raw = raw + " кг"
    return raw


def load_price_files(folder: Path) -> dict:
    """
    Читает все *.csv / *.txt из папки.
    Поддерживает произвольный формат с заголовком:
    определяет колонки по ключевым словам.
    Возвращает  key → {name, weight, price, std_qty, source}
    """
    result: dict = {}
    if not folder.exists():
        folder.mkdir(parents=True, exist_ok=True)
        return result

    for fpath in sorted(folder.iterdir()):
        if fpath.suffix.lower() not in (".csv", ".txt"):
            continue
        try:
            with open(fpath, encoding="utf-8-sig", newline="") as f:
                content = f.read()
            if not content.strip():
                continue

            # Определяем диалект
            try:
                dialect = csv.Sniffer().sniff(content[:4096], delimiters=";,\t")
            except csv.Error:
                dialect = csv.excel
                dialect.delimiter = ","

            rows = list(csv.reader(content.splitlines(), dialect))

            # Ищем строку-заголовок (содержит «наименов» и «цена»)
            col_map = None
            header_idx = None
            for i, row in enumerate(rows[:20]):
                low = " ".join(c.lower() for c in row)
                if "наименов" in low and "цена" in low:
                    col_map = _detect_columns(row)
                    header_idx = i
                    break

            added = 0
            if col_map is not None and header_idx is not None:
                ni = col_map["name"]
                wi = col_map["weight"]
                pi = col_map["price"]
                qi = col_map["std_qty"]

                for row in rows[header_idx + 1:]:
                    if not row:
                        continue
                    # Строка данных: первая колонка — число (порядковый №)
                    if not row[0].strip().isdigit():
                        continue
                    try:
                        name = row[ni].strip().replace("\n", " ").strip() if ni < len(row) else ""
                        if not name:
                            continue
                        weight  = _parse_weight(row[wi]) if wi is not None and wi < len(row) else ""
                        price   = float(row[pi].replace(",", ".").strip()) if pi < len(row) else 0.0
                        std_qty = int(row[qi].strip()) if qi is not None and qi < len(row) else 1
                        if price <= 0:
                            continue
                    except (ValueError, IndexError):
                        continue
                    key = f"{name.lower()}|{weight.lower()}"
                    result[key] = dict(name=name, weight=weight,
                                       price=price, std_qty=std_qty,
                                       source=fpath.name)
                    added += 1
            else:
                # Фолбэк: простой формат Наименование;Вес;Цена;НормКол
                for row in rows:
                    if not row or not row[0].strip():
                        continue
                    name = row[0].strip()
                    skip_words = ("наименование", "название", "name", "товар", "№", "#")
                    if name.lower() in skip_words:
                        continue
                    try:
                        weight  = row[1].strip() if len(row) > 1 else ""
                        price   = float(row[2].replace(",", ".").strip()) if len(row) > 2 else 0.0
                        std_qty = int(row[3].strip()) if len(row) > 3 else 1
                        if price <= 0:
                            continue
                    except (ValueError, IndexError):
                        continue
                    key = f"{name.lower()}|{weight.lower()}"
                    result[key] = dict(name=name, weight=weight,
                                       price=price, std_qty=std_qty,
                                       source=fpath.name)
        except Exception:
            pass
    return result


def fuzzy_find(query: str, price_list: dict, limit: int = 10) -> list:
    if not query or not price_list:
        return []
    q = query.lower().strip()
    out = []
    for key, item in price_list.items():
        name_low = item["name"].lower()
        if q in name_low:
            out.append((name_low.index(q), key, item))
        else:
            ratio = difflib.SequenceMatcher(None, q, name_low).ratio()
            if ratio > 0.42:
                out.append((1 - ratio + 10, key, item))
    out.sort(key=lambda x: x[0])
    return [item for _, _, item in out[:limit]]


# ─────────────────────────────────────────────────────────────────────────────
# Главное приложение
# ─────────────────────────────────────────────────────────────────────────────

class OrderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Менеджер заказов СП")
        self.root.geometry("1120x820")
        self.root.minsize(900, 620)

        # ── Данные ──────────────────────────────────────────────────────────
        self.price_list: dict = {}
        self.orders:     list = []
        self.rows_items: dict = {}
        self.cart:       list = []           # позиции текущего заказчика
        self._found_item: dict | None = None  # результат поиска по прайсу
        self.markup_var = tk.BooleanVar(value=False)  # наценка +13%

        # ── UI ──────────────────────────────────────────────────────────────
        self.root.configure(bg="#f5f4ef")
        self._apply_styles()
        self._build_header()
        self._build_top_panel()
        self._build_cart_panel()
        self._build_notebook()
        self._build_status_bar()
        self._bind_hotkeys()

        # ── Загрузка прайса и сохранённых заказов ────────────────────────────
        self._reload_prices()
        self._load_orders()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ─── Стили ───────────────────────────────────────────────────────────────

    def _apply_styles(self) -> None:
        BG = "#f5f4ef"
        s = ttk.Style()
        s.theme_use("clam")

        # ── Фреймы и LabelFrame ──────────────────────────────────────────────
        s.configure("TFrame",      background=BG)
        s.configure("TLabelframe", background=BG, bordercolor="#b8c4cc",
                    relief="groove")
        s.configure("TLabelframe.Label", background=BG,
                    font=("Helvetica", 10, "bold"), foreground="#2c3e50")

        # ── Вкладки ──────────────────────────────────────────────────────────
        s.configure("TNotebook",     background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", padding=[14, 6],
                    font=("Helvetica", 10, "bold"),
                    background="#d4dce6", foreground="#34495e")
        s.map("TNotebook.Tab",
              background=[("selected", "#2c3e50")],
              foreground=[("selected", "#ecf0f1")])

        # ── Кнопки ───────────────────────────────────────────────────────────
        s.configure("TButton",
                    font=("Helvetica", 10), padding=[8, 4],
                    background="#4a6fa5", foreground="#ffffff",
                    borderwidth=0, focusthickness=0, relief="flat")
        s.map("TButton",
              background=[("active", "#325d8a"), ("pressed", "#1a3f6f")],
              foreground=[("disabled", "#aaaaaa")])

        s.configure("Accent.TButton",
                    font=("Helvetica", 10, "bold"), padding=[8, 5],
                    background="#1a6b3c", foreground="#ffffff",
                    borderwidth=0, focusthickness=0, relief="flat")
        s.map("Accent.TButton",
              background=[("active", "#155932"), ("pressed", "#0e3d22")])

        s.configure("Danger.TButton",
                    font=("Helvetica", 10), padding=[8, 4],
                    background="#c0392b", foreground="#ffffff",
                    borderwidth=0, focusthickness=0, relief="flat")
        s.map("Danger.TButton",
              background=[("active", "#a93226"), ("pressed", "#7b241c")])

        # ── Метки ────────────────────────────────────────────────────────────
        s.configure("TLabel",        background=BG, font=("Helvetica", 10))
        s.configure("Status.TLabel", font=("Helvetica", 11, "bold"),
                    foreground="#ecf0f1", background="#2c3e50", padding=(10, 6))
        s.configure("Found.TLabel",    background=BG,
                    font=("Helvetica", 10), foreground="#1a6b3c")
        s.configure("NotFound.TLabel", background=BG,
                    font=("Helvetica", 10), foreground="#c0392b")
        s.configure("Hint.TLabel",     background=BG,
                    font=("Helvetica", 10), foreground="#7f8c8d")
        s.configure("Heading.TLabel",  background=BG,
                    font=("Helvetica", 10, "bold"), foreground="#2c3e50")

        # ── Таблицы Treeview ──────────────────────────────────────────────────
        s.configure("Treeview",
                    font=("Helvetica", 10), rowheight=24,
                    background="#ffffff", fieldbackground="#ffffff")
        s.configure("Treeview.Heading",
                    font=("Helvetica", 10, "bold"),
                    background="#34495e", foreground="#ecf0f1",
                    relief="flat", borderwidth=0)
        s.map("Treeview.Heading",
              background=[("active", "#2c3e50")])
        s.map("Treeview",
              background=[("selected", "#2980b9")],
              foreground=[("selected", "#ffffff")])

        # ── Entry, Separator, Checkbutton ─────────────────────────────────────
        s.configure("TEntry",       font=("Helvetica", 11), padding=[4, 3])
        s.configure("TSeparator",   background="#b0b8c0")
        s.configure("TCheckbutton", background=BG, font=("Helvetica", 10),
                    foreground="#2c3e50")

    # ─── Заголовок-шапка ─────────────────────────────────────────────────────

    def _build_header(self) -> None:
        hdr = tk.Frame(self.root, bg="#2c3e50", height=50)
        hdr.pack(fill=tk.X, side=tk.TOP)
        hdr.pack_propagate(False)
        tk.Label(
            hdr, text="🛒  Менеджер заказов СП",
            font=("Helvetica", 15, "bold"),
            bg="#2c3e50", fg="#ecf0f1",
        ).pack(side=tk.LEFT, padx=16, pady=8)
        tk.Label(
            hdr, text="Совместные покупки — учёт и расчёт",
            font=("Helvetica", 10), bg="#2c3e50", fg="#95a5a6",
        ).pack(side=tk.LEFT, pady=8)
        tk.Label(
            hdr, text="v3.0", font=("Helvetica", 9),
            bg="#2c3e50", fg="#7f8c8d",
        ).pack(side=tk.RIGHT, padx=12, pady=8)

    # ─── Верхняя панель: заказчик + позиция + кнопки ─────────────────────────

    def _build_top_panel(self) -> None:
        outer = ttk.Frame(self.root, padding=0)
        outer.pack(fill=tk.X, padx=8, pady=(6, 0))

        # ── Блок «Заказчик» ──────────────────────────────────────────────────
        nick_box = ttk.LabelFrame(outer, text="  Заказчик  ", padding=8)
        nick_box.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 6))

        ttk.Label(nick_box, text="Ник:", style="Heading.TLabel").grid(
            row=0, column=0, sticky=tk.W)
        self.nick_var = tk.StringVar()
        self.nick_entry = ttk.Entry(nick_box, textvariable=self.nick_var,
                                    width=18, font=("Helvetica", 11))
        self.nick_entry.grid(row=0, column=1, padx=(4, 0))
        # Cmd+V / Ctrl+V в поле ника
        for seq in ("<Command-v>", "<Command-V>", "<Control-v>", "<Control-V>"):
            self.nick_entry.bind(seq, lambda e: (
                self.nick_entry.event_generate("<<Paste>>"), "break")[1])
        ttk.Button(nick_box, text="Новый заказчик",
                   command=self._new_customer).grid(row=1, column=0, columnspan=2,
                                                    pady=(6, 0), sticky=tk.EW)

        # ── Блок «Позиция» ───────────────────────────────────────────────────
        item_box = ttk.LabelFrame(outer, text="  Позиция  ", padding=8)
        item_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))

        ttk.Label(item_box, text="Товар:", style="Heading.TLabel").grid(
            row=0, column=0, sticky=tk.W, padx=(0, 4))
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(item_box, textvariable=self.name_var,
                                    width=44, font=("Helvetica", 11))
        self.name_entry.grid(row=0, column=1, sticky=tk.EW)
        self.name_var.trace_add("write", self._on_name_changed)

        ttk.Label(item_box, text="Кол-во:", style="Heading.TLabel").grid(
            row=0, column=2, sticky=tk.W, padx=(10, 4))
        self.qty_var = tk.StringVar()
        self.qty_entry = ttk.Entry(item_box, textvariable=self.qty_var,
                                   width=7, font=("Helvetica", 11))
        self.qty_entry.grid(row=0, column=3)
        self.qty_entry.bind("<Return>", lambda _e: self.add_to_cart())

        self.found_var = tk.StringVar(value="Начните вводить название товара")
        self.found_lbl = ttk.Label(item_box, textvariable=self.found_var,
                                   style="Hint.TLabel")
        self.found_lbl.grid(row=1, column=0, columnspan=4, sticky=tk.W, pady=(3, 0))
        item_box.columnconfigure(1, weight=1)

        # autocomplete popup
        self._ac_win: tk.Toplevel | None = None
        self._ac_lb:  tk.Listbox  | None = None
        self._ac_candidates: list = []
        self.name_entry.bind("<Down>",     self._ac_focus)
        self.name_entry.bind("<FocusOut>", self._ac_focusout)

        # ── Блок кнопок ──────────────────────────────────────────────────────
        btn_box = ttk.LabelFrame(outer, text="  Действия  ", padding=8)
        btn_box.pack(side=tk.LEFT, fill=tk.Y)

        ttk.Button(btn_box, text="⊕  В корзину  [↵]",
                   command=self.add_to_cart,
                   style="Accent.TButton", width=19).pack(pady=2, fill=tk.X)
        ttk.Button(btn_box, text="✔  Сохранить заказ  [⌘↩]",
                   command=self.commit_cart,
                   style="Accent.TButton", width=19).pack(pady=2, fill=tk.X)
        ttk.Button(btn_box, text="📋  Сводка заказов  [⌘S]",
                   command=self.show_summary, width=20).pack(pady=2, fill=tk.X)
        ttk.Button(btn_box, text="📊  Сводка рядов  [⌘D]",
                   command=self.show_rows_summary, width=20).pack(pady=2, fill=tk.X)
        ttk.Separator(btn_box, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=3)
        ttk.Checkbutton(btn_box, text="  Наценка +13%",
                        variable=self.markup_var,
                        command=self._on_markup_changed).pack(
            pady=2, anchor=tk.W, fill=tk.X)
        ttk.Separator(btn_box, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=3)
        ttk.Button(btn_box, text="🗑  Очистить всё  [⌘⌫]",
                   command=self.clear_all,
                   style="Danger.TButton", width=19).pack(pady=2, fill=tk.X)

    # ─── Корзина ─────────────────────────────────────────────────────────────

    def _build_cart_panel(self) -> None:
        self.cart_frame = ttk.LabelFrame(
            self.root, text="  Корзина — несохранённые позиции  ", padding=5)
        self.cart_frame.pack(fill=tk.X, padx=8, pady=(4, 0))

        h = ttk.Frame(self.cart_frame)
        h.pack(fill=tk.BOTH, expand=True)

        cols    = ("Товар", "Вес", "Цена, руб.", "Кол-во", "Сумма, руб.")
        widths  = [320, 80, 90, 70, 110]
        anchors = (tk.W, tk.CENTER, tk.CENTER, tk.CENTER, tk.CENTER)

        self.cart_tree = ttk.Treeview(
            h, columns=cols, show="headings", height=4, selectmode="browse")
        for col, w, a in zip(cols, widths, anchors):
            self.cart_tree.heading(col, text=col)
            self.cart_tree.column(col, width=w, anchor=a, minwidth=40)
        self.cart_tree.tag_configure("even", background="#ffffff")
        self.cart_tree.tag_configure("odd",  background="#eaf4ea")

        vsb = ttk.Scrollbar(h, orient=tk.VERTICAL, command=self.cart_tree.yview)
        self.cart_tree.configure(yscrollcommand=vsb.set)
        self.cart_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)

        self.cart_tree.bind("<Delete>",    self._cart_del_sel)
        self.cart_tree.bind("<BackSpace>", self._cart_del_sel)

        foot = ttk.Frame(self.cart_frame)
        foot.pack(fill=tk.X, pady=(3, 0))
        self.cart_total_var = tk.StringVar(value="Сумма корзины: 0,00 руб.")
        ttk.Label(foot, textvariable=self.cart_total_var,
                  font=("Helvetica", 10, "bold"),
                  foreground="#1a5276").pack(side=tk.LEFT, padx=4)
        ttk.Button(foot, text="Удалить выбранное",
                   command=self._cart_del_sel).pack(side=tk.RIGHT, padx=4)
        ttk.Button(foot, text="Очистить корзину",
                   command=self._clear_cart).pack(side=tk.RIGHT, padx=4)
        ttk.Button(foot, text="📥  Вставить заказ списком",
                   command=self.show_paste_dialog).pack(side=tk.RIGHT, padx=4)

    # ─── Notebook ─────────────────────────────────────────────────────────────

    def _build_notebook(self) -> None:
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self._build_orders_tab()
        self._build_rows_tab()
        self._build_price_tab()

    # ──  Вкладка «Заказы»  ────────────────────────────────────────────────────

    def _build_orders_tab(self) -> None:
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="  Заказы  ")
        # Футер с кнопкой удаления (пакуется первым, чтобы занять пространство снизу)
        foot = ttk.Frame(frame)
        foot.pack(side=tk.BOTTOM, fill=tk.X, pady=(2, 4), padx=5)
        ttk.Button(foot, text="🗑  Удалить выбранный заказ  [Del]",
                   command=self._delete_order_sel,
                   style="Danger.TButton").pack(side=tk.RIGHT, padx=4)
        cols    = ("#", "Ник", "Наименование", "Вес", "Цена", "Кол-во", "Сумма", "Время")
        widths  = [36, 110, 290, 72, 80, 64, 100, 120]
        anchors = (tk.CENTER, tk.W, tk.W,
                   tk.CENTER, tk.CENTER, tk.CENTER, tk.CENTER, tk.CENTER)
        self.orders_tree = self._make_tree(frame, cols, widths, anchors)
        self.orders_tree.tag_configure("even", background="#ffffff")
        self.orders_tree.tag_configure("odd",  background="#eaf4ea")
        self.orders_tree.bind("<Delete>",    self._delete_order_sel)
        self.orders_tree.bind("<BackSpace>", self._delete_order_sel)

    # ──  Вкладка «Ряды»  ──────────────────────────────────────────────────────

    def _build_rows_tab(self) -> None:
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="  Ряды  ")

        # Футер с кнопкой удаления (пакуется первым)
        foot = ttk.Frame(frame)
        foot.pack(side=tk.BOTTOM, fill=tk.X, pady=(2, 4), padx=5)
        ttk.Button(foot,
                   text="🗑  Удалить ряд и связанные заказы  [Del]",
                   command=self._delete_row_sel,
                   style="Danger.TButton").pack(side=tk.RIGHT, padx=4)

        # Одна строка на товар — агрегированный вид
        cols    = ("Наименование", "Вес", "Накоплено", "Норма",
                   "Осталось", "Заказчики")
        widths  = [300, 90, 90, 72, 90, 250]
        anchors = (tk.W, tk.CENTER, tk.CENTER,
                   tk.CENTER, tk.CENTER, tk.W)
        self.rows_tree = self._make_tree(frame, cols, widths, anchors)
        self.rows_tree.tag_configure("warn",  background="#fef9e7")
        self.rows_tree.tag_configure("warn2", background="#fde8e8")
        self.rows_tree.bind("<Delete>",    self._delete_row_sel)
        self.rows_tree.bind("<BackSpace>", self._delete_row_sel)

    # ──  Вкладка «Прайс»  ─────────────────────────────────────────────────────

    def _build_price_tab(self) -> None:
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="  Прайс  ")

        top = ttk.Frame(frame, padding=4)
        top.pack(fill=tk.X)
        ttk.Label(top, text=f"Папка: {PRICES_DIR}",
                  foreground="#555").pack(side=tk.LEFT)
        ttk.Button(top, text="Открыть папку",
                   command=self._open_prices_dir).pack(side=tk.LEFT, padx=8)
        ttk.Button(top, text="↺ Перезагрузить [F5]",
                   command=self._reload_prices).pack(side=tk.LEFT)
        self.price_count_var = tk.StringVar(value="Загружено: 0 позиций")
        ttk.Label(top, textvariable=self.price_count_var,
                  foreground="#1a5276").pack(side=tk.RIGHT, padx=8)

        add_frame = ttk.LabelFrame(
            frame, text="  Добавить / изменить позицию вручную  ", padding=8)
        add_frame.pack(fill=tk.X, padx=5, pady=4)

        fields = [("Наименование:", 36), ("Вес:", 12),
                  ("Цена, руб.:", 10), ("Норм. кол-во:", 8)]
        self.price_vars = [tk.StringVar() for _ in fields]
        for col, ((label, width), var) in enumerate(zip(fields, self.price_vars)):
            ttk.Label(add_frame, text=label).grid(
                row=0, column=col * 2, padx=(6, 2), pady=4, sticky=tk.W)
            ttk.Entry(add_frame, textvariable=var, width=width).grid(
                row=0, column=col * 2 + 1, padx=(0, 6), pady=4)
        bf = ttk.Frame(add_frame)
        bf.grid(row=0, column=8, padx=8)
        ttk.Button(bf, text="Сохранить",
                   command=self.save_price_item).pack(side=tk.LEFT, padx=2)
        ttk.Button(bf, text="Удалить выбранное",
                   command=self.delete_price_item).pack(side=tk.LEFT, padx=2)

        cols    = ("Наименование", "Вес", "Цена, руб.", "Норм. кол-во", "Источник")
        widths  = [300, 90, 100, 110, 150]
        anchors = (tk.W, tk.CENTER, tk.CENTER, tk.CENTER, tk.W)
        self.price_tree = self._make_tree(frame, cols, widths, anchors)
        self.price_tree.bind("<<TreeviewSelect>>", self._on_price_select)

    # ─── Статус-бар ───────────────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        self.total_var = tk.StringVar(value="Общая сумма: 0,00 руб.  |  Заказов: 0")
        ttk.Label(self.root, textvariable=self.total_var,
                  style="Status.TLabel", anchor=tk.W).pack(
            fill=tk.X, padx=0, pady=0)

    # ─── Хоткеи ──────────────────────────────────────────────────────────────

    def _bind_hotkeys(self) -> None:
        winsys = self.root.tk.call("tk", "windowingsystem")
        mod = "Command" if winsys == "aqua" else "Control"
        r = self.root
        r.bind(f"<{mod}-Return>",   lambda _e: self.commit_cart())
        r.bind(f"<{mod}-KP_Enter>", lambda _e: self.commit_cart())
        r.bind(f"<{mod}-s>",        lambda _e: self.show_summary())
        r.bind(f"<{mod}-S>",        lambda _e: self.show_summary())
        r.bind(f"<{mod}-d>",        lambda _e: self.show_rows_summary())
        r.bind(f"<{mod}-D>",        lambda _e: self.show_rows_summary())
        r.bind(f"<{mod}-r>",        lambda _e: self._reload_prices())
        r.bind(f"<{mod}-R>",        lambda _e: self._reload_prices())
        r.bind(f"<{mod}-BackSpace>", lambda _e: self.clear_all())
        r.bind("<F5>",               lambda _e: self._reload_prices())
        r.bind("<Escape>",           self._on_escape)
        # Enter: ник → товар → кол-во
        self.nick_entry.bind("<Return>", lambda _e: self.name_entry.focus_set())
        self.name_entry.bind("<Return>", lambda _e: self.qty_entry.focus_set())

    def _on_escape(self, _event=None) -> None:
        if self._ac_win and self._ac_win.winfo_exists():
            self._ac_hide()
            return
        self.name_var.set("")
        self.qty_var.set("")
        self._found_item = None
        self.found_var.set("Начните вводить название товара")
        self.found_lbl.configure(style="Hint.TLabel")
        self.name_entry.focus_set()

    # ─── Автодополнение ───────────────────────────────────────────────────────

    def _on_name_changed(self, *_) -> None:
        q = self.name_var.get().strip()
        if not q:
            self._found_item = None
            self.found_var.set("Начните вводить название товара")
            self.found_lbl.configure(style="Hint.TLabel")
            self._ac_hide()
            return
        hits = fuzzy_find(q, self.price_list)
        if hits:
            exact = next((h for h in hits if h["name"].lower() == q.lower()), None)
            best  = exact or hits[0]
            self._found_item = best
            flag = "✓" if exact else "⟳"
            self.found_var.set(
                f"{flag}  {best['name']}  {best['weight']}"
                f"  ·  {fmt(best['price'])} руб."
                f"  ·  норма {best['std_qty']} шт.")
            self.found_lbl.configure(style="Found.TLabel")
            if not exact:
                self._ac_show(hits)
            else:
                self._ac_hide()
        else:
            self._found_item = None
            self.found_var.set("✗  Товар не найден в прайсе")
            self.found_lbl.configure(style="NotFound.TLabel")
            self._ac_hide()

    def _ac_show(self, hits: list) -> None:
        self._ac_candidates = hits
        self._ac_hide()
        x = self.name_entry.winfo_rootx()
        y = self.name_entry.winfo_rooty() + self.name_entry.winfo_height()
        win = tk.Toplevel(self.root)
        win.wm_overrideredirect(True)
        win.wm_geometry(f"+{x}+{y}")
        win.attributes("-topmost", True)
        self._ac_win = win
        lb = tk.Listbox(win, font=("Helvetica", 10), bd=1, relief=tk.SOLID,
                        height=min(9, len(hits)), width=64,
                        activestyle="dotbox",
                        selectbackground="#2980b9", selectforeground="white")
        lb.pack()
        self._ac_lb = lb
        for h in hits:
            lb.insert(tk.END,
                      f"  {h['name']}  {h['weight']}  —  {fmt(h['price'])} руб.")
        lb.bind("<<ListboxSelect>>", self._ac_select)
        lb.bind("<Return>",          self._ac_select)
        lb.bind("<Escape>",          lambda _e: self._ac_hide())
        lb.bind("<FocusOut>",        self._ac_focusout)

    def _ac_focusout(self, _event=None) -> None:
        self.root.after(150, self._ac_hide_unless_focused)

    def _ac_hide_unless_focused(self) -> None:
        if self._ac_win and self._ac_win.winfo_exists():
            fw = self.root.focus_get()
            if fw not in (self._ac_lb, self.name_entry):
                self._ac_hide()

    def _ac_focus(self, _event=None) -> None:
        if self._ac_lb and self._ac_win and self._ac_win.winfo_exists():
            self._ac_lb.focus_set()
            if self._ac_lb.size():
                self._ac_lb.selection_set(0)

    def _ac_hide(self, *_) -> None:
        if self._ac_win:
            try:
                self._ac_win.destroy()
            except Exception:
                pass
            self._ac_win = None
            self._ac_lb  = None

    def _ac_select(self, _event=None) -> None:
        if not self._ac_lb:
            return
        sel = self._ac_lb.curselection()
        if not sel:
            return
        item = self._ac_candidates[sel[0]]
        self._found_item = item
        self.name_var.set(item["name"])
        self.found_var.set(
            f"✓  {item['name']}  {item['weight']}"
            f"  ·  {fmt(item['price'])} руб."
            f"  ·  норма {item['std_qty']} шт.")
        self.found_lbl.configure(style="Found.TLabel")
        self._ac_hide()
        self.qty_entry.focus_set()

    # ─────────────────────────────────────────────────────────────────────────
    # Логика корзины
    # ─────────────────────────────────────────────────────────────────────────

    def add_to_cart(self) -> None:
        nick = self.nick_var.get().strip()
        if not nick:
            messagebox.showwarning("Ник", "Введите ник заказчика.")
            self.nick_entry.focus_set()
            return
        qty_raw = self.qty_var.get().strip()
        try:
            quantity = int(re.sub(r"[^\d]", "", qty_raw))
            if quantity <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Кол-во",
                                   "Введите корректное количество (целое число > 0).")
            self.qty_entry.focus_set()
            return
        item = self._found_item
        if item is None:
            hits = fuzzy_find(self.name_var.get().strip(), self.price_list, limit=1)
            if hits:
                item = hits[0]
                self._found_item = item
            else:
                messagebox.showwarning(
                    "Товар не найден",
                    "Товар не найден в прайсе.\n"
                    "Проверьте название или добавьте позицию во вкладке «Прайс».")
                self.name_entry.focus_set()
                return
        self.cart.append(dict(
            name=item["name"], weight=item["weight"],
            price=item["price"], quantity=quantity,
            total=round(item["price"] * quantity, 2),
            std_qty=item["std_qty"],
        ))
        self._refresh_cart_tree()
        self.name_var.set("")
        self.qty_var.set("")
        self._found_item = None
        self.found_var.set("Начните вводить название товара")
        self.found_lbl.configure(style="Hint.TLabel")
        self.name_entry.focus_set()

    def commit_cart(self) -> None:
        """Фиксирует корзину — добавляет все позиции в список заказов."""
        nick = self.nick_var.get().strip()
        if not nick:
            messagebox.showwarning("Ник", "Введите ник заказчика.")
            self.nick_entry.focus_set()
            return
        if not self.cart:
            messagebox.showinfo("Корзина", "Корзина пуста. Добавьте позиции.")
            return
        ts = datetime.now().strftime("%d.%m  %H:%M")
        for ci in self.cart:
            idx = len(self.orders)
            key = self._make_key(ci["name"], ci["weight"])
            if key not in self.price_list:
                self.price_list[key] = dict(
                    name=ci["name"], weight=ci["weight"],
                    price=ci["price"], std_qty=ci["std_qty"],
                    source="ручной ввод")
                self._refresh_price_tree()
                self.price_count_var.set(
                    f"Загружено: {len(self.price_list)} позиций")
            order = dict(idx=idx, nick=nick, name=ci["name"], weight=ci["weight"],
                         price=ci["price"], quantity=ci["quantity"],
                         total=ci["total"], time=ts)
            self.orders.append(order)
            tag = "odd" if idx % 2 else "even"
            self.orders_tree.insert(
                "", tk.END, iid=str(idx),
                values=(idx + 1, nick, ci["name"], ci["weight"],
                        f"{ci['price']:.2f}", ci["quantity"],
                        fmt(self._m(ci["total"])), ts),
                tags=(tag,),
            )
            self.orders_tree.see(str(idx))
            if ci["quantity"] != ci["std_qty"]:
                self._process_rows(key, order)
        n = len(self.cart)
        self._update_total()
        self.cart.clear()
        self._refresh_cart_tree()
        self.nb.select(0)
        nick_total = sum(o["total"] for o in self.orders if o["nick"] == nick)
        self.total_var.set(
            self.total_var.get()
            + f"   ✓ {nick}: {n} поз. ({fmt(self._m(nick_total))} руб.)")
        self.root.after(4000, self._update_total)
        self._save_orders()

    def _new_customer(self) -> None:
        if self.cart:
            if messagebox.askyesno(
                    "Новый заказчик",
                    "В корзине есть несохранённые позиции.\n"
                    "Сохранить их для текущего заказчика?"):
                self.commit_cart()
            else:
                self.cart.clear()
                self._refresh_cart_tree()
        self.nick_var.set("")
        self.nick_entry.focus_set()

    def _refresh_cart_tree(self) -> None:
        self.cart_tree.delete(*self.cart_tree.get_children())
        total = 0.0
        for i, ci in enumerate(self.cart):
            tag = "odd" if i % 2 else "even"
            self.cart_tree.insert(
                "", tk.END,
                values=(ci["name"], ci["weight"],
                        f"{ci['price']:.2f}", ci["quantity"], fmt(ci["total"])),
                tags=(tag,),
            )
            total += ci["total"]
        sfx = "  [наценка +13%]" if self.markup_var.get() else ""
        self.cart_total_var.set(
            f"  Сумма корзины: {fmt(self._m(total))} руб.{sfx}"
            f"  |  Позиций: {len(self.cart)}")

    def _cart_del_sel(self, _event=None) -> None:
        sel = self.cart_tree.selection()
        if not sel:
            return
        children = list(self.cart_tree.get_children())
        idx = children.index(sel[0])
        if 0 <= idx < len(self.cart):
            del self.cart[idx]
        self._refresh_cart_tree()

    def _clear_cart(self) -> None:
        if self.cart and messagebox.askyesno("Корзина", "Очистить корзину?"):
            self.cart.clear()
            self._refresh_cart_tree()

    # ─────────────────────────────────────────────────────────────────────────
    # Прайс из файлов
    # ─────────────────────────────────────────────────────────────────────────

    def _reload_prices(self) -> None:
        self.price_list = load_price_files(PRICES_DIR)
        self._refresh_price_tree()
        n = len(self.price_list)
        self.price_count_var.set(
            f"Загружено: {n} позиций из «{PRICES_DIR.name}/»")
        self.found_var.set(f"Прайс перезагружен: {n} позиций")
        self.found_lbl.configure(style="Found.TLabel")
        self.root.after(3000, lambda: (
            self.found_var.set("Начните вводить название товара"),
            self.found_lbl.configure(style="Hint.TLabel"),
        ))

    def _open_prices_dir(self) -> None:
        PRICES_DIR.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(PRICES_DIR)])
        elif sys.platform == "win32":
            os.startfile(str(PRICES_DIR))
        else:
            subprocess.Popen(["xdg-open", str(PRICES_DIR)])

    # ─────────────────────────────────────────────────────────────────────────
    # Логика рядов
    # ─────────────────────────────────────────────────────────────────────────

    def _process_rows(self, key: str, order: dict) -> None:
        bucket = self.rows_items.setdefault(key, [])
        bucket.append(dict(nick=order["nick"], name=order["name"],
                           weight=order["weight"], quantity=order["quantity"]))
        std_qty = self.price_list[key]["std_qty"]
        while True:
            total_acc = sum(r["quantity"] for r in bucket)
            if total_acc < std_qty:
                break
            consumed, remove = 0, []
            for i, r in enumerate(bucket):
                if consumed >= std_qty:
                    break
                need = std_qty - consumed
                if r["quantity"] <= need:
                    consumed += r["quantity"]
                    remove.append(i)
                else:
                    r["quantity"] -= need
                    consumed = std_qty
                    break
            for i in reversed(remove):
                bucket.pop(i)
        if not bucket:
            del self.rows_items[key]
        self._refresh_rows_tree()

    def _refresh_rows_tree(self) -> None:
        self.rows_tree.delete(*self.rows_tree.get_children())
        for key, items in self.rows_items.items():
            pl        = self.price_list.get(key, {})
            name      = pl.get("name", items[0]["name"] if items else key)
            weight    = pl.get("weight", items[0].get("weight", "") if items else "")
            std_qty   = pl.get("std_qty", "?")
            total_acc = sum(r["quantity"] for r in items)
            remaining = (std_qty - total_acc) if isinstance(std_qty, int) else "?"
            # Заказчики: «Маша(2), Петя(1)»
            nick_qty: dict = {}
            for r in items:
                nick_qty[r["nick"]] = nick_qty.get(r["nick"], 0) + r["quantity"]
            nicks_str = ", ".join(
                f"{n}({q})" for n, q in nick_qty.items())
            tag = "warn" if isinstance(remaining, int) and remaining > 0 else "warn2"
            self.rows_tree.insert(
                "", tk.END, iid=key,
                values=(name, weight, total_acc, std_qty, remaining, nicks_str),
                tags=(tag,),
            )
        cnt = len(self.rows_tree.get_children())
        self.nb.tab(1, text=f"  Ряды ({cnt})  " if cnt else "  Ряды  ")

    def _delete_row_sel(self, _event=None) -> None:
        """Удаляет выбранный ряд и все заказы по этому товару из списка заказов."""
        sel = self.rows_tree.selection()
        if not sel:
            messagebox.showinfo("Удаление", "Выберите ряд в таблице.")
            return
        key = sel[0]
        pl   = self.price_list.get(key, {})
        name = pl.get("name", key)
        if not messagebox.askyesno(
                "Удалить ряд",
                f"Удалить ряд «{name}» и все связанные заказы этого товара?"):
            return
        # Удаляем заказы по этому товару
        self.orders = [
            o for o in self.orders
            if self._make_key(o["name"], o["weight"]) != key
        ]
        # Перенумеровываем
        for i, o in enumerate(self.orders):
            o["idx"] = i
        # Удаляем ряд
        del self.rows_items[key]
        # Перерисовываем
        self.orders_tree.delete(*self.orders_tree.get_children())
        for o in self.orders:
            idx = o["idx"]
            tag = "odd" if idx % 2 else "even"
            self.orders_tree.insert(
                "", tk.END, iid=str(idx),
                values=(idx + 1, o["nick"], o["name"], o["weight"],
                        f"{o['price']:.2f}", o["quantity"],
                        fmt(self._m(o["total"])), o.get("time", "")),
                tags=(tag,),
            )
        self._refresh_rows_tree()
        self._update_total()
        self._save_orders()

    # ─────────────────────────────────────────────────────────────────────────
    # Прайс (таблица)
    # ─────────────────────────────────────────────────────────────────────────

    def _refresh_price_tree(self) -> None:
        self.price_tree.delete(*self.price_tree.get_children())
        for key, item in self.price_list.items():
            self.price_tree.insert(
                "", tk.END, iid=key,
                values=(item["name"], item["weight"],
                        f"{item['price']:.2f}", item["std_qty"],
                        item.get("source", "")),
            )

    def _on_price_select(self, _event=None) -> None:
        sel = self.price_tree.selection()
        if not sel:
            return
        vals = self.price_tree.item(sel[0], "values")
        for var, v in zip(self.price_vars, vals[:4]):
            var.set(v)

    def save_price_item(self) -> None:
        try:
            name    = self.price_vars[0].get().strip()
            weight  = self.price_vars[1].get().strip()
            price   = float(self.price_vars[2].get().replace(",", "."))
            std_qty = int(self.price_vars[3].get())
        except ValueError:
            messagebox.showerror("Ошибка", "Проверьте заполнение всех полей.")
            return
        if not name:
            messagebox.showerror("Ошибка", "Введите наименование.")
            return
        key = self._make_key(name, weight)
        self.price_list[key] = dict(name=name, weight=weight, price=price,
                                    std_qty=std_qty, source="ручной ввод")
        self._refresh_price_tree()
        self.price_count_var.set(f"Загружено: {len(self.price_list)} позиций")
        for v in self.price_vars:
            v.set("")

    def delete_price_item(self) -> None:
        sel = self.price_tree.selection()
        if not sel:
            messagebox.showinfo("Удаление", "Выберите позицию в таблице.")
            return
        key = sel[0]
        if messagebox.askyesno("Подтверждение",
                                f"Удалить «{self.price_list[key]['name']}»?"):
            del self.price_list[key]
            self._refresh_price_tree()
            self.price_count_var.set(f"Загружено: {len(self.price_list)} позиций")

    # ─────────────────────────────────────────────────────────────────────────
    # Сводка
    # ─────────────────────────────────────────────────────────────────────────

    # ─────────────────────────────────────────────────────────────────────────
    # Вставка заказа списком
    # ─────────────────────────────────────────────────────────────────────────

    def show_paste_dialog(self) -> None:
        """Диалог для вставки многострочного заказа.
        Если ник не заполнен — первая строка считается ником.
        """
        win = tk.Toplevel(self.root)
        win.title("Вставить заказ списком")
        win.geometry("720x500")
        win.grab_set()
        win.configure(bg="#f5f4ef")

        ttk.Label(win,
                  text="Вставьте заказ списком",
                  font=("Helvetica", 12, "bold")).pack(
            padx=14, pady=(12, 2), anchor=tk.W)
        ttk.Label(
            win,
            text="Если ник не введён — первая строка будет считаться ником.\n"
                 "Формат строк с товарами:  Название  130 г  132,50  - 2шт  "
                 "(кол-во можно опустить — будет 1 шт.)",
            foreground="#7f8c8d",
            font=("Helvetica", 10),
            justify=tk.LEFT,
            wraplength=690,
        ).pack(padx=14, pady=(0, 4), anchor=tk.W)

        nick_preview_var = tk.StringVar()
        nick_cur = self.nick_var.get().strip()
        nick_preview_var.set(
            f"Текущий ник: «{nick_cur}»" if nick_cur
            else "Ник не введён — будет взят из первой строки")
        nick_preview_lbl = ttk.Label(
            win, textvariable=nick_preview_var,
            font=("Helvetica", 10, "bold"),
            foreground="#1a6b3c" if nick_cur else "#e67e22")
        nick_preview_lbl.pack(padx=14, pady=(0, 4), anchor=tk.W)

        area = scrolledtext.ScrolledText(
            win, wrap=tk.WORD, font=("Helvetica", 11),
            height=12, relief=tk.GROOVE,
            bg="#ffffff", fg="#2c3e50",
            padx=8, pady=6,
        )
        area.pack(fill=tk.BOTH, expand=True, padx=14)

        def _update_preview(*_) -> None:
            if self.nick_var.get().strip():
                return  # ник уже задан
            lines = area.get("1.0", tk.END).splitlines()
            first = next((l.strip() for l in lines if l.strip()), "")
            if first:
                nick_preview_var.set(f"Ник из первой строки: «{first}»")
                nick_preview_lbl.configure(foreground="#1a6b3c")
            else:
                nick_preview_var.set("Ник не введён — будет взят из первой строки")
                nick_preview_lbl.configure(foreground="#e67e22")

        area.bind("<<Modified>>", lambda e: (
            area.edit_modified(False), _update_preview()))

        def _paste_from_clipboard(_event=None) -> str:
            try:
                text = win.clipboard_get()
                area.delete("1.0", tk.END)
                area.insert("1.0", text)
                _update_preview()
            except tk.TclError:
                pass
            return "break"

        area.bind("<Command-v>", _paste_from_clipboard)
        area.bind("<Command-V>", _paste_from_clipboard)
        area.bind("<Control-v>", _paste_from_clipboard)
        area.bind("<Control-V>", _paste_from_clipboard)
        area.focus_set()

        result_var = tk.StringVar()
        result_lbl = ttk.Label(win, textvariable=result_var,
                               font=("Helvetica", 10), wraplength=690)
        result_lbl.pack(padx=14, pady=(4, 0), anchor=tk.W)

        def do_import() -> None:
            raw_all = area.get("1.0", tk.END)
            nick = self.nick_var.get().strip()
            if nick:
                # ник уже известен — все строки считаются товарами
                # нормализуем переносы строк
                order_text = raw_all.replace('\r\n', '\n').replace('\r', '\n')
            else:
                # первая непустая строка — ник
                # нормализуем переносы строк перед разбиением
                raw_norm = raw_all.replace('\r\n', '\n').replace('\r', '\n')
                order_lines = []
                for line in raw_norm.split('\n'):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if not nick:
                        nick = stripped
                    else:
                        order_lines.append(stripped)
                if not nick:
                    result_var.set(
                        "⚠  Введите ник в поле «Ник» или напишите его первой строкой.")
                    result_lbl.configure(foreground="#c0392b")
                    return
                order_text = "\n".join(order_lines)

            parsed, skipped = parse_pasted_order(order_text)
            if not parsed:
                skip_info = ""
                if skipped:
                    skip_info = "  Нераспознанные строки: " + " | ".join(skipped[:3])
                result_var.set(
                    "⚠  Не удалось распознать ни одной позиции. "
                    "Проверьте формат текста." + skip_info)
                result_lbl.configure(foreground="#c0392b")
                return

            if self.cart and self.nick_var.get().strip() and self.nick_var.get().strip() != nick:
                if not messagebox.askyesno(
                        "Смена заказчика",
                        f"В корзине позиции для «{self.nick_var.get().strip()}».\n"
                        f"Заменить заказчика на «{nick}»?",
                        parent=win):
                    return

            self.nick_var.set(nick)

            added, not_found = [], []
            for p in parsed:
                # Ищем по названию — берём несколько кандидатов
                hits = fuzzy_find(p["name"], self.price_list, limit=10)

                item = None
                if hits:
                    # Предпочитаем кандидата с совпадающим весом
                    def _norm_w(w: str) -> str:
                        return re.sub(r'\s+', '', w).lower()
                    pw = _norm_w(p["weight"])
                    item = next(
                        (h for h in hits if _norm_w(h["weight"]) == pw),
                        hits[0]   # если вес не совпал — берём лучшее по имени
                    )

                if item is None:
                    # Товар не найден в прайсе — создаём запись из текста
                    key = self._make_key(p["name"], p["weight"])
                    if key not in self.price_list:
                        self.price_list[key] = dict(
                            name=p["name"], weight=p["weight"],
                            price=p["price"], std_qty=1,
                            source="вставка")
                    item = self.price_list[key]
                    not_found.append(p["name"])

                # Цену берём из прайса; если товар не найден — из текста
                use_price = item["price"]
                self.cart.append(dict(
                    name=item["name"],
                    weight=item["weight"],
                    price=use_price,
                    quantity=p["quantity"],
                    total=round(use_price * p["quantity"], 2),
                    std_qty=item.get("std_qty", 1),
                ))
                added.append(item["name"])
            self._refresh_cart_tree()
            # Сохраняем заказ и очищаем корзину + ник для следующего заказчика
            self.commit_cart()
            self.nick_var.set("")
            area.delete("1.0", tk.END)
            nick_preview_var.set("Ник не введён — будет взят из первой строки")
            nick_preview_lbl.configure(foreground="#e67e22")
            parts = [f"✓  Заказ «{nick}» сохранён ({len(added)} поз.). Готово к следующему заказчику."]
            if not_found:
                parts.append(
                    f"  Не найдено в прайсе ({len(not_found)} шт.): "
                    + ", ".join(not_found[:3])
                    + ("..." if len(not_found) > 3 else "")
                    + " — добавлены с ценой из текста.")
            if skipped:
                parts.append(
                    f"  ⚠ Нераспознано {len(skipped)} строк: "
                    + " | ".join(skipped[:2])
                    + ("..." if len(skipped) > 2 else ""))
            result_var.set(" ".join(parts))
            result_lbl.configure(
                foreground="#1a6b3c" if not skipped else "#e67e22")

        frm = ttk.Frame(win)
        frm.pack(pady=6)
        ttk.Button(frm, text="📋  Вставить из буфера",
                   command=_paste_from_clipboard).pack(side=tk.LEFT, padx=6)
        ttk.Button(frm, text="📥  Добавить в корзину",
                   command=do_import,
                   style="Accent.TButton").pack(side=tk.LEFT, padx=6)
        ttk.Button(frm, text="Закрыть",
                   command=win.destroy).pack(side=tk.LEFT, padx=6)

    def show_summary(self) -> None:
        if not self.orders:
            messagebox.showinfo("Сводка", "Нет заказов для формирования сводки.")
            return
        win = tk.Toplevel(self.root)
        win.title("Итоговая сводка заказов")
        win.geometry("740x660")
        win.grab_set()

        text = scrolledtext.ScrolledText(
            win, wrap=tk.WORD, font=("Courier New", 10),
            bg="#ffffff", fg="#000000", relief=tk.FLAT, padx=10, pady=10)
        text.pack(fill=tk.BOTH, expand=True)

        now = datetime.now().strftime("%d.%m.%Y  %H:%M")
        W   = 64
        lines = [
            "=" * W,
            f"{'СВОДКА ЗАКАЗОВ':^{W}}",
            f"{'Сформирована: ' + now:^{W}}",
            "=" * W,
        ]

        lines += ["", "── ПО ЗАКАЗЧИКАМ " + "─" * (W - 17)]
        by_nick: dict = defaultdict(list)
        for o in self.orders:
            by_nick[o["nick"]].append(o)
        grand_total = 0.0
        for nick in sorted(by_nick):
            nick_orders = by_nick[nick]
            nick_total  = sum(self._m(o["total"]) for o in nick_orders)
            grand_total += nick_total
            lines.append(f"\n  {nick}  —  итого: {fmt(nick_total)} руб.")
            for o in nick_orders:
                lines.append(
                    f"    • {o['name']} {o['weight']}"
                    f"  {o['price']:.2f} × {o['quantity']} шт"
                    f" = {fmt(self._m(o['total']))} руб.")

        lines += ["", "── ПО ТОВАРАМ " + "─" * (W - 13)]
        by_prod: dict = defaultdict(lambda: {"qty": 0, "total": 0.0, "nicks": []})
        for o in self.orders:
            k = (o["name"], o["weight"])
            by_prod[k]["qty"]   += o["quantity"]
            by_prod[k]["total"] += o["total"]
            by_prod[k]["nicks"].append(o["nick"])
        for (name, weight), d in sorted(by_prod.items()):
            nicks = list(dict.fromkeys(d["nicks"]))
            lines += [
                f"\n  {name}  {weight}",
                f"    Итого: {d['qty']} шт. = {fmt(self._m(d['total']))} руб.",
                f"    Заказчики: {', '.join(nicks)}",
            ]

        lines += [
            "",
            "=" * W,
            f"  ИТОГО:  {fmt(grand_total)} руб."
            f"   |   Строк заказов: {len(self.orders)}"
            + ("   [наценка +13% включена]" if self.markup_var.get() else ""),
            "=" * W,
        ]

        if self.rows_items:
            lines += ["", "⚠  НЕЗАКРЫТЫЕ РЯДЫ:"]
            for key, items in self.rows_items.items():
                pl        = self.price_list[key]
                total_acc = sum(r["quantity"] for r in items)
                std_qty   = pl["std_qty"]
                lines.append(
                    f"  • {pl['name']}  {pl['weight']}"
                    f"  :  накоплено {total_acc}/{std_qty} шт."
                    f"  (не хватает {std_qty - total_acc} шт.)")
                for r in items:
                    lines.append(f"      – {r['nick']}: {r['quantity']} шт.")

        if self.cart:
            nick = self.nick_var.get().strip() or "?"
            lines += ["", f"⏳  НЕСОХРАНЁННАЯ КОРЗИНА ({nick}):"]
            for ci in self.cart:
                lines.append(
                    f"  • {ci['name']} {ci['weight']}"
                    f"  {ci['quantity']} шт. = {fmt(ci['total'])} руб.")

        content = "\n".join(lines)
        text.insert(tk.END, content)
        text.configure(state="disabled")

        frm = ttk.Frame(win, padding=(0, 4))
        frm.pack()
        ttk.Button(frm, text="Скопировать",
                   command=lambda: self._copy_to_clipboard(win, content)).pack(
            side=tk.LEFT, padx=6)
        ttk.Button(frm, text="Закрыть",
                   command=win.destroy).pack(side=tk.LEFT, padx=6)

    @staticmethod
    def _copy_to_clipboard(win: tk.Toplevel, text: str) -> None:
        win.clipboard_clear()
        win.clipboard_append(text)
        messagebox.showinfo("Скопировано", "Сводка скопирована в буфер обмена.",
                            parent=win)

    # ─────────────────────────────────────────────────────────────────────────
    # Сводка рядов
    # ─────────────────────────────────────────────────────────────────────────

    def show_rows_summary(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Сводка рядов")
        win.geometry("680x540")
        win.grab_set()

        text = scrolledtext.ScrolledText(
            win, wrap=tk.WORD, font=("Courier New", 10),
            bg="#fffff0", fg="#000000", relief=tk.FLAT, padx=10, pady=10)
        text.pack(fill=tk.BOTH, expand=True)

        now = datetime.now().strftime("%d.%m.%Y  %H:%M")
        W   = 64
        lines = [
            "=" * W,
            f"{'СВОДКА РЯДОВ':^{W}}",
            f"{'Сформирована: ' + now:^{W}}",
            "=" * W,
        ]

        if not self.rows_items:
            lines += ["", "  ✓  Все ряды закрыты — незакрытых позиций нет."]
        else:
            lines += ["", f"  Незакрытых рядов: {len(self.rows_items)}"]
            for key, items in self.rows_items.items():
                pl        = self.price_list.get(key, {})
                name      = pl.get("name", key.split("|")[0])
                weight    = pl.get("weight", "")
                std_qty   = pl.get("std_qty", "?")
                total_acc = sum(r["quantity"] for r in items)
                remaining = (std_qty - total_acc) if isinstance(std_qty, int) else "?"
                lines += [
                    "",
                    f"  {'─'*58}",
                    f"  {name}  {weight}",
                    f"  Норма: {std_qty} шт.  |  Накоплено: {total_acc} шт."
                    f"  |  Осталось: {remaining} шт.",
                    "  Заказчики:",
                ]
                for r in items:
                    lines.append(f"    • {r['nick']}: {r['quantity']} шт.")

        lines += ["", "=" * W]
        content = "\n".join(lines)
        text.insert(tk.END, content)
        text.configure(state="disabled")

        frm = ttk.Frame(win, padding=(0, 4))
        frm.pack()
        ttk.Button(frm, text="Скопировать",
                   command=lambda: self._copy_to_clipboard(win, content)).pack(
            side=tk.LEFT, padx=6)
        ttk.Button(frm, text="Закрыть",
                   command=win.destroy).pack(side=tk.LEFT, padx=6)

    # ─────────────────────────────────────────────────────────────────────────
    # Сохранение / восстановление заказов
    # ─────────────────────────────────────────────────────────────────────────

    def _save_orders(self) -> None:
        """Сохраняет заказы и ряды в JSON-файл рядом с приложением."""
        try:
            data = {
                "orders": self.orders,
                "rows_items": self.rows_items,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
            }
            with open(SAVE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showwarning("Сохранение", f"Не удалось сохранить заказы:\n{e}")

    def _load_orders(self) -> None:
        """Восстанавливает заказы из файла сохранения при запуске."""
        if not SAVE_FILE.exists():
            return
        try:
            with open(SAVE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            self.orders     = data.get("orders", [])
            self.rows_items = data.get("rows_items", {})

            # Восстанавливаем таблицу заказов
            for order in self.orders:
                idx = order["idx"]
                tag = "odd" if idx % 2 else "even"
                self.orders_tree.insert(
                    "", tk.END, iid=str(idx),
                    values=(idx + 1, order["nick"], order["name"], order["weight"],
                            f"{order['price']:.2f}", order["quantity"],
                            fmt(self._m(order["total"])), order.get("time", "")),
                    tags=(tag,),
                )
            self._update_total()
            self._refresh_rows_tree()

            saved_at = data.get("saved_at", "")
            if self.orders:
                self.total_var.set(
                    self.total_var.get()
                    + f"   (восст. {saved_at[:16]})")
                self.root.after(5000, self._update_total)
        except Exception:
            pass  # повреждённый файл — просто игнорируем

    def _on_close(self) -> None:
        """Сохраняет заказы и закрывает приложение."""
        if self.orders or self.rows_items:
            self._save_orders()
        self.root.destroy()

    # ─────────────────────────────────────────────────────────────────────────
    # Утилиты
    # ─────────────────────────────────────────────────────────────────────────

    def clear_all(self) -> None:
        if not self.orders and not self.cart:
            return
        if not messagebox.askyesno(
                "Очистить",
                "Удалить все заказы, корзину и ряды?\n(Прайс сохранится.)"):
            return
        self.orders.clear()
        self.rows_items.clear()
        self.cart.clear()
        self.orders_tree.delete(*self.orders_tree.get_children())
        self.rows_tree.delete(*self.rows_tree.get_children())
        self._refresh_cart_tree()
        self.nb.tab(1, text="  Ряды  ")
        self._update_total()
        # удаляем файл сохранения после полной очистки
        try:
            if SAVE_FILE.exists():
                SAVE_FILE.unlink()
        except Exception:
            pass

    def _update_total(self) -> None:
        total = self._m(sum(o["total"] for o in self.orders))
        sfx   = "  [наценка +13%]" if self.markup_var.get() else ""
        self.total_var.set(
            f"  Общая сумма: {fmt(total)} руб.{sfx}"
            f"   |   Заказов: {len(self.orders)}")

    @staticmethod
    def _make_key(name: str, weight: str) -> str:
        return f"{name.strip().lower()}|{weight.strip().lower()}"

    def _m(self, value: float) -> float:
        """Apply +13% markup if the toggle is enabled."""
        return round(value * 1.13, 2) if self.markup_var.get() else value

    def _on_markup_changed(self, _event=None) -> None:
        """Перерисовывает суммы во всех отображениях при включении/выключении наценки."""
        self.orders_tree.delete(*self.orders_tree.get_children())
        for order in self.orders:
            idx = order["idx"]
            tag = "odd" if idx % 2 else "even"
            self.orders_tree.insert(
                "", tk.END, iid=str(idx),
                values=(idx + 1, order["nick"], order["name"], order["weight"],
                        f"{order['price']:.2f}", order["quantity"],
                        fmt(self._m(order["total"])), order.get("time", "")),
                tags=(tag,),
            )
        self._update_total()
        self._refresh_cart_tree()

    def _delete_order_sel(self, _event=None) -> None:
        """Удаляет выбранный заказ из списка и пересчитывает ряды."""
        sel = self.orders_tree.selection()
        if not sel:
            messagebox.showinfo("Удаление", "Выберите заказ в таблице.")
            return
        iid   = sel[0]
        order = next((o for o in self.orders if str(o["idx"]) == iid), None)
        if order is None:
            return
        if not messagebox.askyesno(
                "Удалить заказ",
                f"Удалить: {order['nick']} — {order['name']}"
                f" × {order['quantity']} шт. = {fmt(order['total'])} руб.?"):
            return
        self.orders.remove(order)
        # Перенумеровываем оставшиеся заказы
        for i, o in enumerate(self.orders):
            o["idx"] = i
        # Перестраиваем ряды из оставшихся заказов
        self.rows_items.clear()
        for o in self.orders:
            key = self._make_key(o["name"], o["weight"])
            if key in self.price_list:
                std_qty = self.price_list[key]["std_qty"]
                if o["quantity"] != std_qty:
                    self._process_rows(key, o)
        # Перерисовываем таблицу заказов
        self.orders_tree.delete(*self.orders_tree.get_children())
        for o in self.orders:
            i   = o["idx"]
            tag = "odd" if i % 2 else "even"
            self.orders_tree.insert(
                "", tk.END, iid=str(i),
                values=(i + 1, o["nick"], o["name"], o["weight"],
                        f"{o['price']:.2f}", o["quantity"],
                        fmt(self._m(o["total"])), o.get("time", "")),
                tags=(tag,),
            )
        self._update_total()
        self._save_orders()

    @staticmethod
    def _make_tree(parent, columns, widths, anchors) -> ttk.Treeview:
        h = ttk.Frame(parent)
        h.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        tree = ttk.Treeview(h, columns=columns, show="headings",
                            selectmode="browse")
        for col, w, a in zip(columns, widths, anchors):
            tree.heading(col, text=col)
            tree.column(col, width=w, anchor=a, minwidth=40)
        vsb = ttk.Scrollbar(h, orient=tk.VERTICAL,   command=tree.yview)
        hsb = ttk.Scrollbar(h, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        h.rowconfigure(0, weight=1)
        h.columnconfigure(0, weight=1)
        return tree


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    root = tk.Tk()
    OrderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

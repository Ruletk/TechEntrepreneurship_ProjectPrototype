import datetime
import os
import telebot
from telebot import types
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from decimal import Decimal, InvalidOperation

from .export_xslx import export_outlet_xlsx
from .config import Config
from .models import User, Outlet, Group, Item, StockBalance
from .services.onboarding import get_or_create_user
from .services import groups as groups_svc
from .access import can_access_outlet, has_wide_access
from .audit import log
from .models import AuditAction


# ---------------------------
# Callback prefixes
# ---------------------------
CB_MENU = "m"  # main menu
CB_GRP = "g"  # groups
CB_OUT = "o"  # outlets
CB_INV = "i"  # inventory

# Sort keys
SORT_ALPHA = "alpha"
SORT_CREATED = "created"
SORT_UPDATED = "updated"


class BotApp:
    def __init__(self, cfg: Config, session_factory):
        self.bot = telebot.TeleBot(cfg.bot_token)
        self.Session = session_factory

        # in-memory state for dialog steps
        # tg_user_id -> dict: {mode, group_id, outlet_id, item_id, sort}
        self.user_states = {}

        self._register_handlers()

    # ---------------------------
    # State helpers
    # ---------------------------
    def _st(self, tg_user_id: int) -> dict:
        return self.user_states.setdefault(tg_user_id, {})

    def _set_mode(self, tg_user_id: int, mode: str, **kwargs):
        st = self._st(tg_user_id)
        st["mode"] = mode
        for k, v in kwargs.items():
            st[k] = v

    def _clear_mode(self, tg_user_id: int):
        st = self._st(tg_user_id)
        st.pop("mode", None)
        st.pop("group_id", None)
        st.pop("outlet_id", None)
        st.pop("item_id", None)
        # sort оставляем, это предпочтение

    def _get_sort(self, tg_user_id: int) -> str:
        return self._st(tg_user_id).get("sort", SORT_ALPHA)

    def _set_sort(self, tg_user_id: int, sort: str):
        self._st(tg_user_id)["sort"] = sort

    # ---------------------------
    # DB helpers (inventory)
    # ---------------------------
    def _get_balance(self, db, outlet_id: int, item_id: int) -> StockBalance:
        bal = db.scalar(
            select(StockBalance).where(
                StockBalance.outlet_id == outlet_id, StockBalance.item_id == item_id
            )
        )
        if not bal:
            bal = StockBalance(outlet_id=outlet_id, item_id=item_id, quantity=0)
            db.add(bal)
            db.flush()
        return bal

    def _list_items_with_qty(self, db, outlet_id: int, sort: str):
        q = select(Item).where(Item.outlet_id == outlet_id, Item.is_active == True)
        # если у Item нет created_at/updated_at — оставь только alpha или сортируй по id
        if sort == SORT_CREATED and hasattr(Item, "created_at"):
            q = q.order_by(Item.created_at.desc(), Item.id.desc())
        elif sort == SORT_UPDATED and hasattr(Item, "updated_at"):
            q = q.order_by(Item.updated_at.desc(), Item.id.desc())
        else:
            q = q.order_by(Item.name.asc())

        items = db.scalars(q).all()

        # balances одним проходом
        item_ids = [it.id for it in items]
        if not item_ids:
            return []

        bals = db.scalars(
            select(StockBalance).where(
                StockBalance.outlet_id == outlet_id, StockBalance.item_id.in_(item_ids)
            )
        ).all()
        bmap = {b.item_id: b for b in bals}

        result = []
        for it in items:
            qty = bmap.get(it.id).quantity if bmap.get(it.id) else 0
            try:
                qty = float(qty)
            except Exception:
                qty = 0.0
            result.append((it, qty))
        return result

    # ---------------------------
    # Keyboards
    # ---------------------------
    def _kb_main(self):
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("🏢 Группы", callback_data=f"{CB_GRP}:list"))
        kb.row(
            types.InlineKeyboardButton("🏬 Точки", callback_data=f"{CB_OUT}:pick_group")
        )
        kb.row(
            types.InlineKeyboardButton(
                "📦 Инвентарь", callback_data=f"{CB_INV}:pick_group"
            )
        )
        return kb

    def _kb_groups_list(self):
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton(
                "➕ Создать группу", callback_data=f"{CB_GRP}:create"
            )
        )
        kb.row(types.InlineKeyboardButton("⬅️ В меню", callback_data=f"{CB_MENU}:home"))
        return kb

    def _kb_group_pick(self, groups: list[Group], back_cb: str):
        kb = types.InlineKeyboardMarkup()
        for g in groups:
            kb.row(
                types.InlineKeyboardButton(
                    f"🏢 {g.name} (#{g.id})",
                    callback_data=f"{CB_GRP}:select:{g.id}:{back_cb}",
                )
            )
        kb.row(types.InlineKeyboardButton("⬅️ В меню", callback_data=f"{CB_MENU}:home"))
        return kb

    def _kb_outlets_list(self, group_id: int, can_create: bool):
        kb = types.InlineKeyboardMarkup()
        if can_create:
            kb.row(
                types.InlineKeyboardButton(
                    "➕ Создать точку", callback_data=f"{CB_OUT}:create:{group_id}"
                )
            )
        kb.row(
            types.InlineKeyboardButton(
                "⬅️ Назад (группы)", callback_data=f"{CB_OUT}:pick_group"
            )
        )
        kb.row(types.InlineKeyboardButton("⬅️ В меню", callback_data=f"{CB_MENU}:home"))
        return kb

    def _kb_outlet_pick(self, group_id: int, outlets: list[Outlet], next_cb: str):
        kb = types.InlineKeyboardMarkup()
        for o in outlets:
            kb.row(
                types.InlineKeyboardButton(
                    f"🏬 {o.name} (#{o.id})",
                    callback_data=f"{CB_OUT}:select:{o.id}:{next_cb}",
                )
            )
        kb.row(
            types.InlineKeyboardButton(
                "⬅️ Назад (группы)", callback_data=f"{CB_OUT}:pick_group"
            )
        )
        kb.row(types.InlineKeyboardButton("⬅️ В меню", callback_data=f"{CB_MENU}:home"))
        return kb

    def _kb_inventory(self, outlet_id: int, sort: str):
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton(
                "➕ Добавить товар", callback_data=f"{CB_INV}:add:{outlet_id}"
            ),
            types.InlineKeyboardButton(
                "↕️ Сортировка", callback_data=f"{CB_INV}:sort:{outlet_id}"
            ),
        )
        kb.row(
            types.InlineKeyboardButton(
                "🔄 Обновить", callback_data=f"{CB_INV}:open:{outlet_id}:{sort}"
            )
        )
        kb.row(
            types.InlineKeyboardButton(
                "📤 Экспорт в Excel", callback_data=f"i:export:{outlet_id}"
            )
        )

        kb.row(
            types.InlineKeyboardButton(
                "⬅️ Назад (точки)", callback_data=f"{CB_INV}:pick_group"
            )
        )
        kb.row(types.InlineKeyboardButton("⬅️ В меню", callback_data=f"{CB_MENU}:home"))
        return kb

    def _kb_inventory_sort(self, outlet_id: int, current: str):
        kb = types.InlineKeyboardMarkup()

        def b(lbl, key):
            mark = " ✅" if current == key else ""
            return types.InlineKeyboardButton(
                lbl + mark, callback_data=f"{CB_INV}:setsort:{outlet_id}:{key}"
            )

        kb.row(b("🔤 По алфавиту", SORT_ALPHA))
        kb.row(b("🕒 По времени добавления", SORT_CREATED))
        kb.row(b("✏️ По времени изменения", SORT_UPDATED))
        kb.row(
            types.InlineKeyboardButton(
                "⬅️ Назад", callback_data=f"{CB_INV}:open:{outlet_id}:{current}"
            )
        )
        return kb

    def _kb_item_card(self, outlet_id: int, item_id: int, sort: str):
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton(
                "➖10", callback_data=f"{CB_INV}:qty:{outlet_id}:{item_id}:-10:{sort}"
            ),
            types.InlineKeyboardButton(
                "➖1", callback_data=f"{CB_INV}:qty:{outlet_id}:{item_id}:-1:{sort}"
            ),
            types.InlineKeyboardButton(
                "➕1", callback_data=f"{CB_INV}:qty:{outlet_id}:{item_id}:1:{sort}"
            ),
            types.InlineKeyboardButton(
                "➕10", callback_data=f"{CB_INV}:qty:{outlet_id}:{item_id}:10:{sort}"
            ),
        )
        kb.row(
            types.InlineKeyboardButton(
                "✍️ Задать количество",
                callback_data=f"{CB_INV}:setqty:{outlet_id}:{item_id}:{sort}",
            )
        )
        kb.row(
            types.InlineKeyboardButton(
                "✏️ Переименовать",
                callback_data=f"{CB_INV}:rename:{outlet_id}:{item_id}:{sort}",
            ),
            types.InlineKeyboardButton(
                "📏 Изменить unit",
                callback_data=f"{CB_INV}:unit:{outlet_id}:{item_id}:{sort}",
            ),
        )
        kb.row(
            types.InlineKeyboardButton(
                "🗑 Удалить", callback_data=f"{CB_INV}:del:{outlet_id}:{item_id}:{sort}"
            )
        )
        kb.row(
            types.InlineKeyboardButton(
                "⬅️ К списку", callback_data=f"{CB_INV}:open:{outlet_id}:{sort}"
            )
        )
        return kb

    def _kb_delete_confirm(self, outlet_id: int, item_id: int, sort: str):
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton(
                "✅ Да, удалить",
                callback_data=f"{CB_INV}:delok:{outlet_id}:{item_id}:{sort}",
            ),
            types.InlineKeyboardButton(
                "❌ Отмена", callback_data=f"{CB_INV}:item:{outlet_id}:{item_id}:{sort}"
            ),
        )
        return kb

    # ---------------------------
    # Renderers
    # ---------------------------
    def _send_or_edit(self, chat_id: int, message_id: int | None, text: str, kb=None):
        # пытаемся редактировать, если не выйдет — отправим новое
        if message_id:
            try:
                self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
                return
            except Exception:
                pass
        self.bot.send_message(chat_id, text, reply_markup=kb)

    def _render_main(self, chat_id: int, message_id: int | None, u: User):
        active = f"#{u.active_outlet_id}" if u.active_outlet_id else "не выбрана"
        text = (
            "StockBot (prototype)\n\n"
            f"Активная точка: {active}\n\n"
            "Выбери действие:"
        )
        self._send_or_edit(chat_id, message_id, text, self._kb_main())

    # ---------------------------
    # Handlers
    # ---------------------------
    def _register_handlers(self):
        bot = self.bot

        @bot.message_handler(commands=["start"])
        def start(m):
            with self.Session() as db:
                u = get_or_create_user(db, m.from_user.id, m.from_user.full_name)
                self._clear_mode(m.from_user.id)
                self._render_main(m.chat.id, None, u)

        # можно оставить /menu
        @bot.message_handler(commands=["menu"])
        def menu(m):
            with self.Session() as db:
                u = get_or_create_user(db, m.from_user.id, m.from_user.full_name)
                self._clear_mode(m.from_user.id)
                self._render_main(m.chat.id, None, u)

        # ---------------------------
        # MAIN callbacks
        # ---------------------------
        @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_MENU}:"))
        def cb_menu(c):
            with self.Session() as db:
                u = get_or_create_user(db, c.from_user.id, c.from_user.full_name)
                self._clear_mode(c.from_user.id)
                self._render_main(c.message.chat.id, c.message.message_id, u)
                bot.answer_callback_query(c.id)

        # ---------------------------
        # GROUPS callbacks
        # ---------------------------
        @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_GRP}:"))
        def cb_groups(c):
            parts = c.data.split(":")
            action = parts[1]

            with self.Session() as db:
                u = get_or_create_user(db, c.from_user.id, c.from_user.full_name)

                if action == "list":
                    groups = groups_svc.user_groups(db, u.id)
                    if not groups:
                        text = "У тебя пока нет групп.\nНажми «Создать группу»."
                    else:
                        text = "Твои группы:\n" + "\n".join(
                            [f"- 🏢 {g.name} (#{g.id})" for g in groups]
                        )

                    self._send_or_edit(
                        c.message.chat.id,
                        c.message.message_id,
                        text,
                        self._kb_groups_list(),
                    )
                    self._clear_mode(c.from_user.id)
                    bot.answer_callback_query(c.id)
                    return

                if action == "create":
                    self._set_mode(c.from_user.id, "create_group")
                    bot.answer_callback_query(c.id)
                    bot.send_message(
                        c.message.chat.id, "✍️ Введи название группы одним сообщением:"
                    )
                    return

                if action == "select":
                    # g:select:<group_id>:<back_cb>
                    group_id = int(parts[2])
                    back_cb = parts[3] if len(parts) >= 4 else "outlets"

                    # перенаправление: после выбора группы показать точки или инвентарь
                    if back_cb == "outlets":
                        # открываем точки по группе
                        return self._open_outlets_for_group(db, c, u, group_id)
                    if back_cb == "inventory":
                        # открыть точки чтобы выбрать точку для инвентаря
                        return self._pick_outlet_for_inventory(db, c, u, group_id)

                    bot.answer_callback_query(c.id, "Неизвестное действие")
                    return

                bot.answer_callback_query(c.id, "Неизвестное действие")

        # ---------------------------
        # OUTLETS callbacks
        # ---------------------------
        @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_OUT}:"))
        def cb_outlets(c):
            parts = c.data.split(":")
            action = parts[1]

            with self.Session() as db:
                u = get_or_create_user(db, c.from_user.id, c.from_user.full_name)

                if action == "pick_group":
                    groups = groups_svc.user_groups(db, u.id)
                    if not groups:
                        self._send_or_edit(
                            c.message.chat.id,
                            c.message.message_id,
                            "У тебя нет групп. Сначала создай группу.",
                            self._kb_groups_list(),
                        )
                        bot.answer_callback_query(c.id)
                        return

                    self._send_or_edit(
                        c.message.chat.id,
                        c.message.message_id,
                        "Выбери группу для просмотра точек:",
                        self._kb_group_pick(groups, "outlets"),
                    )
                    bot.answer_callback_query(c.id)
                    return

                if action == "select":
                    # o:select:<outlet_id>:<next_cb>
                    outlet_id = int(parts[2])
                    next_cb = parts[3] if len(parts) >= 4 else "inventory"

                    if not can_access_outlet(db, u.id, outlet_id):
                        bot.answer_callback_query(c.id, "Нет доступа к точке")
                        return

                    u.active_outlet_id = outlet_id
                    db.commit()

                    bot.answer_callback_query(c.id, "Активная точка выбрана")
                    if next_cb == "inventory":
                        # открыть инвентарь по этой точке
                        return self._open_inventory(
                            db,
                            c.message.chat.id,
                            c.message.message_id,
                            u,
                            outlet_id,
                            self._get_sort(c.from_user.id),
                        )
                    else:
                        # просто вернемся в меню
                        self._render_main(c.message.chat.id, c.message.message_id, u)
                        return

                if action == "create":
                    # o:create:<group_id>
                    group_id = int(parts[2])

                    # проверка прав: wide-owner/manager
                    if not has_wide_access(db, u.id, group_id):
                        bot.answer_callback_query(
                            c.id, "Нет прав создавать точки в этой группе"
                        )
                        return

                    self._set_mode(c.from_user.id, "create_outlet", group_id=group_id)
                    bot.answer_callback_query(c.id)
                    bot.send_message(
                        c.message.chat.id,
                        "✍️ Введи название точки одним сообщением (адрес можно потом):",
                    )
                    return

                bot.answer_callback_query(c.id, "Неизвестное действие")

        # ---------------------------
        # INVENTORY callbacks
        # ---------------------------
        @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_INV}:"))
        def cb_inventory(c):
            parts = c.data.split(":")
            action = parts[1]

            with self.Session() as db:
                u = get_or_create_user(db, c.from_user.id, c.from_user.full_name)
                
                if action == "export":
                    outlet_id = int(parts[2])

                    if not can_access_outlet(db, u.id, outlet_id):
                        bot.answer_callback_query(c.id, "Нет доступа")
                        return

                    # генерируем файл
                    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                    filename = f"inventory_outlet_{outlet_id}_{ts}.xlsx"
                    path = os.path.join("tmp_exports", filename)

                    export_outlet_xlsx(db, outlet_id, path)

                    bot.answer_callback_query(c.id, "Готовлю файл…")
                    with open(path, "rb") as f:
                        bot.send_document(c.message.chat.id, f, visible_file_name=filename)
                    return

                if action == "pick_group":
                    groups = groups_svc.user_groups(db, u.id)
                    if not groups:
                        self._send_or_edit(
                            c.message.chat.id,
                            c.message.message_id,
                            "У тебя нет групп. Сначала создай группу.",
                            self._kb_groups_list(),
                        )
                        bot.answer_callback_query(c.id)
                        return

                    self._send_or_edit(
                        c.message.chat.id,
                        c.message.message_id,
                        "Выбери группу, затем точку, чтобы открыть инвентарь:",
                        self._kb_group_pick(groups, "inventory"),
                    )
                    bot.answer_callback_query(c.id)
                    return

                if action == "open":
                    # i:open:<outlet_id>:<sort>
                    outlet_id = int(parts[2])
                    sort = (
                        parts[3] if len(parts) >= 4 else self._get_sort(c.from_user.id)
                    )
                    self._set_sort(c.from_user.id, sort)

                    if not can_access_outlet(db, u.id, outlet_id):
                        bot.answer_callback_query(c.id, "Нет доступа к точке")
                        return

                    u.active_outlet_id = outlet_id
                    db.commit()

                    bot.answer_callback_query(c.id)
                    return self._open_inventory(
                        db, c.message.chat.id, c.message.message_id, u, outlet_id, sort
                    )

                if action == "sort":
                    outlet_id = int(parts[2])
                    sort = self._get_sort(c.from_user.id)
                    bot.answer_callback_query(c.id)
                    self._send_or_edit(
                        c.message.chat.id,
                        c.message.message_id,
                        "Выбери сортировку:",
                        self._kb_inventory_sort(outlet_id, sort),
                    )
                    return

                if action == "setsort":
                    outlet_id = int(parts[2])
                    sort = parts[3]
                    self._set_sort(c.from_user.id, sort)
                    bot.answer_callback_query(c.id, "Сортировка сохранена")
                    return self._open_inventory(
                        db, c.message.chat.id, c.message.message_id, u, outlet_id, sort
                    )

                if action == "add":
                    outlet_id = int(parts[2])
                    if not can_access_outlet(db, u.id, outlet_id):
                        bot.answer_callback_query(c.id, "Нет доступа")
                        return
                    self._set_mode(c.from_user.id, "add_item", outlet_id=outlet_id)
                    bot.answer_callback_query(c.id)
                    bot.send_message(
                        c.message.chat.id,
                        "➕ Добавление товара\n"
                        "Введи одной строкой:\n"
                        "`название | unit | qty`\n\n"
                        "Пример:\n"
                        "Молоко | l | 10\n"
                        "Сахар | kg | 3.5\n"
                        "Крышка | pcs | 100\n\n"
                        "qty можно пропустить (тогда 0).",
                        parse_mode="Markdown",
                    )
                    return

                if action == "item":
                    # i:item:<outlet_id>:<item_id>:<sort>
                    outlet_id = int(parts[2])
                    item_id = int(parts[3])
                    sort = (
                        parts[4] if len(parts) >= 5 else self._get_sort(c.from_user.id)
                    )

                    if not can_access_outlet(db, u.id, outlet_id):
                        bot.answer_callback_query(c.id, "Нет доступа")
                        return

                    return self._open_item_card(
                        db,
                        c.message.chat.id,
                        c.message.message_id,
                        outlet_id,
                        item_id,
                        sort,
                        answer_cb=c.id,
                    )

                if action == "qty":
                    # i:qty:<outlet_id>:<item_id>:<delta>:<sort>
                    outlet_id = int(parts[2])
                    item_id = int(parts[3])
                    delta = int(parts[4])
                    sort = (
                        parts[5] if len(parts) >= 6 else self._get_sort(c.from_user.id)
                    )

                    if not can_access_outlet(db, u.id, outlet_id):
                        bot.answer_callback_query(c.id, "Нет доступа")
                        return

                    group_id = db.scalar(
                        select(Outlet.group_id).where(Outlet.id == outlet_id)
                    )

                    item = db.scalar(
                        select(Item).where(
                            Item.id == item_id,
                            Item.outlet_id == outlet_id,
                            Item.is_active == True,
                        )
                    )
                    if not item:
                        bot.answer_callback_query(c.id, "Товар не найден")
                        return

                    bal = self._get_balance(db, outlet_id, item_id)
                    old = Decimal(str(bal.quantity))
                    new_qty = Decimal(str(bal.quantity)) + Decimal(delta)
                    if new_qty < 0:
                        new_qty = Decimal("0")
                    bal.quantity = new_qty
                    # updated_at если есть
                    if hasattr(item, "updated_at"):
                        item.updated_at = datetime.datetime.utcnow()
                    log(
                        db,
                        u.id,
                        AuditAction.QTY_DELTA,
                        "balance",
                        entity_id=item_id,
                        group_id=group_id,
                        outlet_id=outlet_id,
                        details=f"item_id={item_id};delta={delta};from={old};to={bal.quantity}",
                    )
                    db.commit()

                    bot.answer_callback_query(c.id, "Ок")
                    return self._open_item_card(
                        db,
                        c.message.chat.id,
                        c.message.message_id,
                        outlet_id,
                        item_id,
                        sort,
                    )

                if action == "setqty":
                    # i:setqty:<outlet_id>:<item_id>:<sort>
                    outlet_id = int(parts[2])
                    item_id = int(parts[3])
                    sort = (
                        parts[4] if len(parts) >= 5 else self._get_sort(c.from_user.id)
                    )
                    if not can_access_outlet(db, u.id, outlet_id):
                        bot.answer_callback_query(c.id, "Нет доступа")
                        return

                    self._set_mode(
                        c.from_user.id,
                        "set_qty",
                        outlet_id=outlet_id,
                        item_id=item_id,
                        sort=sort,
                    )
                    bot.answer_callback_query(c.id)
                    bot.send_message(
                        c.message.chat.id,
                        "✍️ Введи новое количество числом (например 12 или 3.5):",
                    )
                    return

                if action == "rename":
                    outlet_id = int(parts[2])
                    item_id = int(parts[3])
                    sort = (
                        parts[4] if len(parts) >= 5 else self._get_sort(c.from_user.id)
                    )
                    if not can_access_outlet(db, u.id, outlet_id):
                        bot.answer_callback_query(c.id, "Нет доступа")
                        return

                    self._set_mode(
                        c.from_user.id,
                        "rename_item",
                        outlet_id=outlet_id,
                        item_id=item_id,
                        sort=sort,
                    )
                    bot.answer_callback_query(c.id)
                    bot.send_message(
                        c.message.chat.id, "✍️ Введи новое название товара:"
                    )
                    return

                if action == "unit":
                    outlet_id = int(parts[2])
                    item_id = int(parts[3])
                    sort = (
                        parts[4] if len(parts) >= 5 else self._get_sort(c.from_user.id)
                    )
                    if not can_access_outlet(db, u.id, outlet_id):
                        bot.answer_callback_query(c.id, "Нет доступа")
                        return

                    self._set_mode(
                        c.from_user.id,
                        "set_unit",
                        outlet_id=outlet_id,
                        item_id=item_id,
                        sort=sort,
                    )
                    bot.answer_callback_query(c.id)
                    bot.send_message(
                        c.message.chat.id, "✍️ Введи новый unit (например pcs / kg / l):"
                    )
                    return

                if action == "del":
                    outlet_id = int(parts[2])
                    item_id = int(parts[3])
                    sort = (
                        parts[4] if len(parts) >= 5 else self._get_sort(c.from_user.id)
                    )

                    if not can_access_outlet(db, u.id, outlet_id):
                        bot.answer_callback_query(c.id, "Нет доступа")
                        return

                    bot.answer_callback_query(c.id)
                    self._send_or_edit(
                        c.message.chat.id,
                        c.message.message_id,
                        f"🗑 Удалить товар #{item_id}? (будет скрыт из списка)",
                        self._kb_delete_confirm(outlet_id, item_id, sort),
                    )
                    return

                if action == "delok":
                    outlet_id = int(parts[2])
                    item_id = int(parts[3])
                    sort = (
                        parts[4] if len(parts) >= 5 else self._get_sort(c.from_user.id)
                    )

                    if not can_access_outlet(db, u.id, outlet_id):
                        bot.answer_callback_query(c.id, "Нет доступа")
                        return

                    item = db.scalar(
                        select(Item).where(
                            Item.id == item_id,
                            Item.outlet_id == outlet_id,
                            Item.is_active == True,
                        )
                    )
                    if not item:
                        bot.answer_callback_query(c.id, "Товар не найден")
                        return

                    item.is_active = False
                    if hasattr(item, "updated_at"):
                        item.updated_at = datetime.datetime.utcnow()
                    db.commit()

                    bot.answer_callback_query(c.id, "Удалено")
                    return self._open_inventory(
                        db, c.message.chat.id, c.message.message_id, u, outlet_id, sort
                    )

                bot.answer_callback_query(c.id, "Неизвестное действие")

        # ---------------------------
        # Text router (input steps)
        # ---------------------------
        @bot.message_handler(func=lambda m: True, content_types=["text"])
        def text_router(m):
            st = self._st(m.from_user.id)
            mode = st.get("mode")
            if not mode:
                return

            with self.Session() as db:
                u = get_or_create_user(db, m.from_user.id, m.from_user.full_name)

                # create_group: plain text name
                if mode == "create_group":
                    name = (m.text or "").strip()
                    if not name:
                        bot.reply_to(m, "Название не может быть пустым. Введи ещё раз:")
                        return
                    g = groups_svc.create_group(db, u.id, name)
                    log(
                        db,
                        user_id=u.id,
                        action=AuditAction.GROUP_CREATED,
                        entity_type="group",
                        entity_id=g.id,
                        group_id=g.id,
                        details=f"name={g.name}",
                    )
                    db.commit()
                    self._clear_mode(m.from_user.id)
                    bot.reply_to(m, f"✅ Группа создана: «{g.name}» (#{g.id})")
                    self._render_main(m.chat.id, None, u)
                    return

                # create_outlet: plain text name (optional address later)
                if mode == "create_outlet":
                    group_id = st.get("group_id")
                    name = (m.text or "").strip()
                    if not group_id:
                        self._clear_mode(m.from_user.id)
                        bot.reply_to(
                            m, "Ошибка: не выбрана группа. Открой меню → Точки."
                        )
                        return
                    if not name:
                        bot.reply_to(
                            m, "Название точки не может быть пустым. Введи ещё раз:"
                        )
                        return
                    if not has_wide_access(db, u.id, int(group_id)):
                        self._clear_mode(m.from_user.id)
                        bot.reply_to(m, "⛔ Нет прав создавать точки в этой группе.")
                        return
                    o = groups_svc.create_outlet(db, int(group_id), name, None)
                    log(
                        db,
                        u.id,
                        AuditAction.OUTLET_CREATED,
                        "outlet",
                        o.id,
                        group_id=group_id,
                        outlet_id=o.id,
                        details=f"name={o.name}",
                    )
                    db.commit()
                    self._clear_mode(m.from_user.id)
                    bot.reply_to(m, f"✅ Точка создана: «{o.name}» (#{o.id})")
                    self._render_main(m.chat.id, None, u)
                    return

                # add_item: "name | unit | qty"
                if mode == "add_item":
                    outlet_id = st.get("outlet_id")
                    if not outlet_id:
                        self._clear_mode(m.from_user.id)
                        bot.reply_to(
                            m, "Ошибка: не выбрана точка. Открой Инвентарь заново."
                        )
                        return
                    if not can_access_outlet(db, u.id, int(outlet_id)):
                        self._clear_mode(m.from_user.id)
                        bot.reply_to(m, "⛔ Нет доступа к точке.")
                        return

                    raw = (m.text or "").strip()
                    parts = [p.strip() for p in raw.split("|")]
                    if len(parts) < 2:
                        bot.reply_to(
                            m,
                            "Формат: `название | unit | qty`\nqty можно пропустить.",
                            parse_mode="Markdown",
                        )
                        return

                    name = parts[0]
                    unit = parts[1]
                    qty = Decimal("0")
                    if len(parts) >= 3 and parts[2]:
                        try:
                            qty = Decimal(parts[2].replace(",", "."))
                            if qty < 0:
                                qty = Decimal("0")
                        except InvalidOperation:
                            bot.reply_to(
                                m,
                                "qty должно быть числом (например 10 или 3.5). Попробуй ещё раз:",
                            )
                            return

                    try:
                        group_id = db.scalar(
                            select(Outlet.group_id).where(Outlet.id == int(outlet_id))
                        )
                        item = Item(
                            outlet_id=int(outlet_id),
                            name=name,
                            unit=unit,
                            is_active=True,
                        )
                        # timestamps если есть
                        if hasattr(item, "created_at"):
                            item.created_at = datetime.datetime.utcnow()
                        if hasattr(item, "updated_at"):
                            item.updated_at = datetime.datetime.utcnow()

                        db.add(item)
                        db.flush()

                        bal = self._get_balance(db, int(outlet_id), item.id)
                        bal.quantity = qty
                        log(
                            db,
                            u.id,
                            AuditAction.ITEM_CREATED,
                            "item",
                            item.id,
                            group_id=group_id,
                            outlet_id=outlet_id,
                            details=f"name={item.name};unit={item.unit};qty={qty}",
                        )
                        db.commit()

                    except IntegrityError:
                        db.rollback()
                        bot.reply_to(
                            m, "⛔ Товар с таким названием уже есть в этой точке."
                        )
                        return

                    self._clear_mode(m.from_user.id)
                    bot.reply_to(m, f"✅ Добавлено: {name} ({unit}), qty={qty}")
                    # открываем инвентарь
                    sort = self._get_sort(m.from_user.id)
                    self._open_inventory(db, m.chat.id, None, u, int(outlet_id), sort)
                    return

                # set_qty
                if mode == "set_qty":
                    outlet_id = int(st.get("outlet_id", 0))
                    item_id = int(st.get("item_id", 0))
                    sort = st.get("sort", self._get_sort(m.from_user.id))

                    if not outlet_id or not item_id:
                        self._clear_mode(m.from_user.id)
                        bot.reply_to(
                            m, "Ошибка состояния. Открой карточку товара заново."
                        )
                        return
                    if not can_access_outlet(db, u.id, outlet_id):
                        self._clear_mode(m.from_user.id)
                        bot.reply_to(m, "⛔ Нет доступа.")
                        return

                    try:
                        qty = Decimal((m.text or "").strip().replace(",", "."))
                        if qty < 0:
                            qty = Decimal("0")
                    except InvalidOperation:
                        bot.reply_to(m, "Введите число (например 12 или 3.5):")
                        return

                    item = db.scalar(
                        select(Item).where(
                            Item.id == item_id,
                            Item.outlet_id == outlet_id,
                            Item.is_active == True,
                        )
                    )
                    if not item:
                        self._clear_mode(m.from_user.id)
                        bot.reply_to(m, "Товар не найден.")
                        return

                    group_id = db.scalar(
                        select(Outlet.group_id).where(Outlet.id == outlet_id)
                    )
                    bal = self._get_balance(db, outlet_id, item_id)
                    old = Decimal(str(bal.quantity))
                    bal.quantity = qty
                    if hasattr(item, "updated_at"):
                        item.updated_at = datetime.datetime.utcnow()
                    log(
                        db,
                        u.id,
                        AuditAction.QTY_SET,
                        "balance",
                        entity_id=item_id,
                        group_id=group_id,
                        outlet_id=outlet_id,
                        details=f"item_id={item_id};from={old};to={qty}",
                    )
                    db.commit()

                    self._clear_mode(m.from_user.id)
                    bot.reply_to(m, "✅ Количество обновлено.")
                    self._open_item_card(db, m.chat.id, None, outlet_id, item_id, sort)
                    return

                # rename_item
                if mode == "rename_item":
                    outlet_id = int(st.get("outlet_id", 0))
                    item_id = int(st.get("item_id", 0))
                    sort = st.get("sort", self._get_sort(m.from_user.id))
                    new_name = (m.text or "").strip()

                    if not new_name:
                        bot.reply_to(m, "Название не может быть пустым. Введи ещё раз:")
                        return
                    if not can_access_outlet(db, u.id, outlet_id):
                        self._clear_mode(m.from_user.id)
                        bot.reply_to(m, "⛔ Нет доступа.")
                        return

                    item = db.scalar(
                        select(Item).where(
                            Item.id == item_id,
                            Item.outlet_id == outlet_id,
                            Item.is_active == True,
                        )
                    )
                    if not item:
                        self._clear_mode(m.from_user.id)
                        bot.reply_to(m, "Товар не найден.")
                        return

                    try:
                        item.name = new_name
                        if hasattr(item, "updated_at"):
                            item.updated_at = datetime.datetime.utcnow()
                        db.commit()
                    except IntegrityError:
                        db.rollback()
                        bot.reply_to(
                            m, "⛔ Товар с таким названием уже есть в этой точке."
                        )
                        return

                    self._clear_mode(m.from_user.id)
                    bot.reply_to(m, "✅ Переименовано.")
                    self._open_item_card(db, m.chat.id, None, outlet_id, item_id, sort)
                    return

                # set_unit
                if mode == "set_unit":
                    outlet_id = int(st.get("outlet_id", 0))
                    item_id = int(st.get("item_id", 0))
                    sort = st.get("sort", self._get_sort(m.from_user.id))
                    new_unit = (m.text or "").strip()

                    if not new_unit:
                        bot.reply_to(m, "unit не может быть пустым. Введи ещё раз:")
                        return
                    if not can_access_outlet(db, u.id, outlet_id):
                        self._clear_mode(m.from_user.id)
                        bot.reply_to(m, "⛔ Нет доступа.")
                        return

                    item = db.scalar(
                        select(Item).where(
                            Item.id == item_id,
                            Item.outlet_id == outlet_id,
                            Item.is_active == True,
                        )
                    )
                    if not item:
                        self._clear_mode(m.from_user.id)
                        bot.reply_to(m, "Товар не найден.")
                        return

                    item.unit = new_unit
                    if hasattr(item, "updated_at"):
                        item.updated_at = datetime.datetime.utcnow()
                    db.commit()

                    self._clear_mode(m.from_user.id)
                    bot.reply_to(m, "✅ Unit обновлён.")
                    self._open_item_card(db, m.chat.id, None, outlet_id, item_id, sort)
                    return

                # неизвестный mode
                self._clear_mode(m.from_user.id)
                bot.reply_to(m, "Сбросил состояние. Открой меню: /start")

    # ---------------------------
    # Navigation helpers for group->outlet flows
    # ---------------------------
    def _open_outlets_for_group(self, db, c, u: User, group_id: int):
        # list outlets
        outs = db.scalars(
            select(Outlet).where(Outlet.group_id == group_id, Outlet.is_active == True)
        ).all()
        can_create = has_wide_access(db, u.id, group_id)

        if not outs:
            text = f"🏬 Точки в группе #{group_id}\n\n(пока нет точек)"
        else:
            text = f"🏬 Точки в группе #{group_id}:\n" + "\n".join(
                [f"- {o.name} (#{o.id})" for o in outs]
            )

        self._send_or_edit(
            c.message.chat.id,
            c.message.message_id,
            text,
            self._kb_outlets_list(group_id, can_create),
        )
        self.bot.answer_callback_query(c.id)

    def _pick_outlet_for_inventory(self, db, c, u: User, group_id: int):
        outs = db.scalars(
            select(Outlet).where(Outlet.group_id == group_id, Outlet.is_active == True)
        ).all()
        if not outs:
            self._send_or_edit(
                c.message.chat.id,
                c.message.message_id,
                f"В группе #{group_id} пока нет точек.",
                types.InlineKeyboardMarkup().row(
                    types.InlineKeyboardButton(
                        "⬅️ Назад (группы)", callback_data=f"{CB_INV}:pick_group"
                    )
                ),
            )
            self.bot.answer_callback_query(c.id)
            return

        self._send_or_edit(
            c.message.chat.id,
            c.message.message_id,
            "Выбери точку:",
            self._kb_outlet_pick(group_id, outs, "inventory"),
        )
        self.bot.answer_callback_query(c.id)

    def _open_inventory(
        self,
        db,
        chat_id: int,
        message_id: int | None,
        u: User,
        outlet_id: int,
        sort: str,
    ):
        # build list with inline “open item card” buttons
        items = self._list_items_with_qty(db, outlet_id, sort)

        text_lines = [f"📦 Инвентарь точки #{outlet_id}", f"Сортировка: {sort}", ""]
        if not items:
            text_lines.append("Пока нет товаров. Нажми «Добавить товар».")
        else:
            text_lines.append("Товары:")
            for it, qty in items[:15]:
                text_lines.append(f"- #{it.id}: {it.name} — {qty:g} {it.unit}")
            if len(items) > 15:
                text_lines.append(f"\n…и ещё {len(items)-15}")

        # Клавиатура: список товаров как кнопки (первые 10), плюс управление
        kb = types.InlineKeyboardMarkup()
        for it, qty in items[:10]:
            kb.row(
                types.InlineKeyboardButton(
                    f"{it.name} ({qty:g} {it.unit})",
                    callback_data=f"{CB_INV}:item:{outlet_id}:{it.id}:{sort}",
                )
            )
        # control row(s)
        kb2 = self._kb_inventory(outlet_id, sort)
        # merge kb2 into kb (telebot позволяет просто добавлять rows)
        for row in kb2.keyboard:
            kb.keyboard.append(row)

        self._send_or_edit(chat_id, message_id, "\n".join(text_lines), kb)

    def _open_item_card(
        self,
        db,
        chat_id: int,
        message_id: int | None,
        outlet_id: int,
        item_id: int,
        sort: str,
        answer_cb: str | None = None,
    ):
        item = db.scalar(
            select(Item).where(
                Item.id == item_id, Item.outlet_id == outlet_id, Item.is_active == True
            )
        )
        if not item:
            if answer_cb:
                self.bot.answer_callback_query(answer_cb, "Товар не найден")
            self._send_or_edit(
                chat_id, message_id, "Товар не найден (возможно удалён).", None
            )
            return

        bal = self._get_balance(db, outlet_id, item_id)
        try:
            qty = float(bal.quantity)
        except Exception:
            qty = 0.0

        text = (
            f"📦 Товар #{item.id}\n"
            f"Название: {item.name}\n"
            f"Unit: {item.unit}\n"
            f"Количество: {qty:g}\n"
        )

        if answer_cb:
            self.bot.answer_callback_query(answer_cb)

        self._send_or_edit(
            chat_id, message_id, text, self._kb_item_card(outlet_id, item_id, sort)
        )

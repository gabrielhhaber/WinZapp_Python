"""The provider routes of the Online AI page, in a window of their own.

Which providers a kind of task (voice message, picture, sticker, video, PDF,
chat summary, writing help) tries, and in what order, when the person wants
that to differ from the general order of the page. A task with no route of its
own keeps using the general order (core/ai_media/routes.py has the rules).

Plain controls only: a choice of task, a check box ("Use the general order"),
and a list whose rows say in words whether the provider is in the route and at
what position, with buttons to move it and to include or leave it out (a
CheckListBox would hide its state from some screen readers, as the page's own
provider list notes). Reached from a button on the page, like the usage
summary (ai_usage_options.py); the page saves ``routes()`` with its other
preferences.
"""

import wx

from core.ai_media import config as ai_config
from core.ai_media import routes as ai_routes
from ui.accessible import AccessibleReorderButton
from ui.reorder_keys import MOVE_DOWN, MOVE_UP, bind_reorder_keys


def row_text(t, name, position, ready) -> str:
    """One row: the provider, its place in the route (``position`` from 1, or
    None when it is left out) and, when it could not be used now, why not."""
    parts = [name, t("ai_route_row_in").format(position=position) if position
             else t("ai_route_row_out")]
    if not ready:
        parts.append(t("ai_route_not_ready"))
    return ", ".join(parts)


def route_of(items, included) -> list:
    """The route a window state describes: the included providers, in list order."""
    return [p for p in items if p in included]


class AIRoutesPanel(wx.Panel):
    """The routes of every task; *page* is the AISettingsPage and *config* the
    validated preferences it opened with."""

    def __init__(self, parent, page, config):
        super().__init__(parent)
        self._page = page
        self._saved = dict(config.get("routes") or {})
        self._state = {}   # task -> {"items": [...], "in": set, "general": bool}, built when shown
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.task_label = wx.StaticText(self, label=page._t("ai_route_task_label"))
        sizer.Add(self.task_label, 0, wx.LEFT | wx.TOP, 8)
        self.task = wx.Choice(self, choices=self._task_names(), name=page._plain("ai_route_task_label"))
        self.task.SetSelection(0)
        self.task.Bind(wx.EVT_CHOICE, self._task_changed)
        sizer.Add(self.task, 0, wx.EXPAND | wx.ALL, 8)
        self.general = wx.CheckBox(self, label=page._t("ai_route_general_check"))
        self.general.Bind(wx.EVT_CHECKBOX, self._general_changed)
        sizer.Add(self.general, 0, wx.ALL, 8)
        self.list_label = wx.StaticText(self, label=page._t("ai_route_list_label"))
        sizer.Add(self.list_label, 0, wx.LEFT | wx.TOP, 8)
        self.providers = wx.ListBox(self, name=page._plain("ai_route_list_label"))
        sizer.Add(self.providers, 1, wx.EXPAND | wx.ALL, 8)
        self.toggle = wx.Button(self, label=page._t("ai_route_toggle_button"))
        self.toggle.Bind(wx.EVT_BUTTON, lambda event: self._toggle())
        sizer.Add(self.toggle, 0, wx.ALL, 8)
        self.up = wx.Button(self, label=page._t("ai_provider_move_up_button"))
        self.up.Bind(wx.EVT_BUTTON, lambda event: self._move(-1))
        sizer.Add(self.up, 0, wx.ALL, 8)
        self.down = wx.Button(self, label=page._t("ai_provider_move_down_button"))
        self.down.Bind(wx.EVT_BUTTON, lambda event: self._move(1))
        sizer.Add(self.down, 0, wx.ALL, 8)
        # Alt+Up / Alt+Down move the row too, as on the page's own provider list.
        self.up.SetAccessible(AccessibleReorderButton(MOVE_UP, page._t("accessible_alt_up")))
        self.down.SetAccessible(AccessibleReorderButton(MOVE_DOWN, page._t("accessible_alt_down")))
        bind_reorder_keys(self.providers, self._move)
        self.SetSizer(sizer)
        self._show_task()

    # -- State ---------------------------------------------------------------

    def _task_names(self):
        return [self._page._t(ai_routes.TASK_KEY[task]) for task in ai_routes.TASKS]

    def _current(self):
        return ai_routes.TASKS[max(self.task.GetSelection(), 0)]

    def _config(self):
        """What ``routes.candidates`` reads: the saved routes and the page's
        staged general order."""
        return {"routes": self._saved, "order": self._page._order}

    def _entry(self, task):
        if task not in self._state:
            items, chosen = ai_routes.candidates(self._config(), task)
            self._state[task] = {"items": items, "in": set(chosen),
                                 "general": ai_routes.route_for(self._config(), task) is None}
        return self._state[task]

    def _ready(self, provider):
        """Whether the provider could answer now: on, with a key (a local
        server: an address and a chosen model)."""
        page = self._page
        if provider in page._disabled:
            return False
        if ai_config.is_local(provider):
            return ai_config.local_ready(page._local_config(), provider)
        return page._has_key(provider)

    def routes(self) -> dict:
        """``{task: [provider, ...]}`` to save: the tasks that are not on the
        general order and have at least one provider in their route."""
        found = {task: route_of(entry["items"], entry["in"])
                 for task, entry in self._state.items() if not entry["general"]}
        # A task never opened keeps the route it came with.
        for task, route in self._saved.items():
            if task not in self._state:
                found[task] = route
        return ai_routes.validated(found)

    # -- The window ----------------------------------------------------------

    def _show_task(self, selection=0):
        task = self._current()
        entry = self._entry(task)
        self.general.SetValue(entry["general"])
        self._fill(selection)
        self._enable()

    def _enable(self):
        on = not self.general.GetValue()
        for control in (self.providers, self.toggle, self.up, self.down):
            control.Enable(on)

    def _fill(self, selection=0):
        entry = self._entry(self._current())
        t = self._page._t
        rows = []
        for provider in entry["items"]:
            position = (route_of(entry["items"], entry["in"]).index(provider) + 1
                        if provider in entry["in"] else None)
            rows.append(row_text(t, ai_config.PROVIDERS[provider].name, position,
                                 self._ready(provider)))
        self.providers.Freeze()
        try:
            self.providers.Set(rows)
            if rows:
                self.providers.SetSelection(min(max(selection, 0), len(rows) - 1))
        finally:
            self.providers.Thaw()

    def _say(self, text):
        self._page.main_window.speak_output(text)

    def _task_name(self):
        return self._page._t(ai_routes.TASK_KEY[self._current()])

    def _summary(self):
        """What the screen reader hears on choosing a task: which one, and
        whether it follows the general order or how many providers it has."""
        entry = self._entry(self._current())
        t = self._page._t
        if entry["general"]:
            return t("ai_route_summary_general").format(task=self._task_name())
        return t("ai_route_summary_route").format(
            task=self._task_name(), count=len(route_of(entry["items"], entry["in"])))

    def _task_changed(self, event):
        self._show_task()
        self._say(self._summary())
        event.Skip()

    def _general_changed(self, event):
        entry = self._entry(self._current())
        entry["general"] = self.general.GetValue()
        if not entry["general"] and not entry["in"]:
            # Starting a route: begin with what the general order uses today.
            entry["in"] = {p for p in entry["items"] if self._ready(p)}
        self._fill(self.providers.GetSelection())
        self._enable()
        self._page._on_change()
        self._say(self._summary())

    def _selected(self):
        index = self.providers.GetSelection()
        items = self._entry(self._current())["items"]
        return (index, items[index]) if 0 <= index < len(items) else (wx.NOT_FOUND, None)

    def _toggle(self):
        index, provider = self._selected()
        if provider is None:
            return
        entry = self._entry(self._current())
        t = self._page._t
        if provider in entry["in"]:
            entry["in"].discard(provider)
        else:
            entry["in"].add(provider)
        self._fill(index)
        self._page._on_change()
        name = ai_config.PROVIDERS[provider].name
        if provider in entry["in"]:
            position = route_of(entry["items"], entry["in"]).index(provider) + 1
            self._say(t("ai_route_included").format(provider=name, position=position))
        elif not entry["in"]:
            # A route with no provider is no route: the general order is used.
            self._say(t("ai_route_left_out_empty").format(provider=name))
        else:
            self._say(t("ai_route_left_out").format(provider=name))

    def _move(self, direction):
        index, provider = self._selected()
        entry = self._entry(self._current())
        target = index + direction
        if provider is None or not 0 <= target < len(entry["items"]):
            return
        items = entry["items"]
        items[index], items[target] = items[target], items[index]
        self._fill(target)
        self._page._on_change()
        position = route_of(items, entry["in"]).index(provider) + 1 if provider in entry["in"] else None
        t = self._page._t
        name = ai_config.PROVIDERS[provider].name
        self._say(t("ai_route_moved").format(provider=name, position=position) if position
                  else t("ai_route_left_out").format(provider=name))

    def refresh_labels(self) -> None:
        page = self._page
        t = page._t
        self.task_label.SetLabel(t("ai_route_task_label"))
        self.general.SetLabel(t("ai_route_general_check"))
        self.list_label.SetLabel(t("ai_route_list_label"))
        self.toggle.SetLabel(t("ai_route_toggle_button"))
        self.up.SetLabel(t("ai_provider_move_up_button"))
        self.down.SetLabel(t("ai_provider_move_down_button"))
        self.task.SetName(page._plain("ai_route_task_label"))
        self.providers.SetName(page._plain("ai_route_list_label"))
        selection = self.task.GetSelection()
        self.task.SetItems(self._task_names())
        self.task.SetSelection(max(selection, 0))
        self._fill(self.providers.GetSelection())

    def refresh(self) -> None:
        """Read the page again (its keys, models and general order may have
        changed since the window was last open): a task still on the general
        order follows the new one, a route the person edited stays as it is."""
        for task in [task for task, entry in self._state.items() if entry["general"]]:
            del self._state[task]
        self._show_task(self.providers.GetSelection())

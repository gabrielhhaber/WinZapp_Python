"""Labels a panel of the main window says again after a language change.

A dialog is built anew each time it opens, in the language of the moment. A
panel of the main window (communities, channels) is built once, so it keeps
the key of every label it shows and `_relabel()` reads them again when
MainWindow switches language (refresh_labels()).
"""

from ui.accessible import split_mnemonic


class TranslatedLabels:
    def _init_labels(self):
        self._labels = []

    def _keep_label(self, control, key):
        """A control whose label is the catalog's *key*."""
        self._labels.append((control, "label", key))
        return control

    def _keep_items(self, control, keys):
        """A RadioBox's or Choice's items, in order."""
        self._labels.append((control, "items", tuple(keys)))
        return control

    def _keep_labelled(self, control, key):
        """A control made by add_labelled(): its label text and its name."""
        self._labels.append((control._label_control, "label", key))
        self._labels.append((control, "name", key))
        return control

    def _relabel(self):
        t = self._mw.i18n.t
        for control, kind, key in self._labels:
            if kind == "label":
                control.SetLabel(t(key))
            elif kind == "name":
                control.SetName(split_mnemonic(t(key))[0])
            else:
                for index, item in enumerate(key):
                    control.SetString(index, t(item))

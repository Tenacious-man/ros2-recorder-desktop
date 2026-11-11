"""Shared item view appearance and checkbox interaction."""
from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtWidgets import QAbstractItemView, QStyle, QStyledItemDelegate, QStyleOptionViewItem


class TopicItemDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        clean = QStyleOptionViewItem(option)
        clean.state &= ~QStyle.State_HasFocus
        super().paint(painter, clean, index)


class CheckableRowDelegate(TopicItemDelegate):
    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.MouseButtonRelease:
            if event.button() != Qt.LeftButton:
                return False
        elif event.type() != QEvent.KeyPress or event.key() not in (Qt.Key_Space, Qt.Key_Select):
            return False

        check = index.sibling(index.row(), 0)
        flags = check.flags()
        state = check.data(Qt.CheckStateRole)
        if not (flags & Qt.ItemIsEnabled and flags & Qt.ItemIsUserCheckable) or state is None:
            return False
        return model.setData(check, Qt.Unchecked if state == Qt.Checked else Qt.Checked, Qt.CheckStateRole)


def configure_checkable_rows(view):
    view.setSelectionMode(QAbstractItemView.NoSelection)
    view.setItemDelegate(CheckableRowDelegate(view))
    view.setStyleSheet(view.styleSheet() + '\nQAbstractItemView::indicator { width: 20px; height: 20px; }')

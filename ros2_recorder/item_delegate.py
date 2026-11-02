"""Shared topic-table painting: row highlight without a cell focus frame."""
from PyQt5.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem


class TopicItemDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        clean = QStyleOptionViewItem(option)
        clean.state &= ~QStyle.State_HasFocus
        super().paint(painter, clean, index)

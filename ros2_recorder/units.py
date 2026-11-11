"""Decimal capacity labels for values measured in bytes."""

KIB = 1024
MIB = 1024**2
GIB = 1024**3
DECIMAL_BYTES = {'KB': 1000, 'MB': 1000**2, 'GB': 1000**3}


def format_capacity(byte_count, unit):
    number = f'{byte_count / DECIMAL_BYTES[unit]:.2f}'.rstrip('0').rstrip('.')
    return f'{number} {unit}'


def format_size(byte_count, minimum='KB'):
    if byte_count >= DECIMAL_BYTES['GB']:
        return format_capacity(byte_count, 'GB')
    if byte_count >= DECIMAL_BYTES['MB'] or minimum == 'MB':
        return format_capacity(byte_count, 'MB')
    return format_capacity(byte_count, 'KB')

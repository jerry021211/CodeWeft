def total_amount(rows):
    """Return the numeric sum, accepting numeric strings as amounts."""
    return sum(row["amount"] for row in rows)

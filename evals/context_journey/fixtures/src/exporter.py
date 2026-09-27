def export(rows):
    """Return serialized orders; each input row has id and amount."""
    return "order_id,amount\n" + "\n".join(str(row["id"]) + "," + str(row["amount"]) for row in rows)

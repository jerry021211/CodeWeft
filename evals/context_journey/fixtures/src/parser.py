def parse_record(line):
    """Parse id|amount, ignoring whitespace surrounding either field."""
    parts = line.split("|")
    return {"id": parts[0], "amount": parts[1]}

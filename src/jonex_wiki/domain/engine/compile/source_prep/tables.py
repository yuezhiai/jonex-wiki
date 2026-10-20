"""HTML table grid expansion follows explicit spans; never guesses geometry.

迁移自原实现 ``parsing/tables.py``(verbatim)。
"""
from html.parser import HTMLParser

from jonex_wiki.domain.engine.compile.source_prep.chunking import PackingError


class _HTMLTable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.spans = (1, 1)
        self.header = False
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.depth += 1
            if self.depth > 1:
                raise PackingError('nested HTML tables require explicit normalization')
        if tag in {'td', 'th'}:
            attrs = dict(attrs)
            try:
                self.spans = (int(attrs.get('rowspan', '1')), int(attrs.get('colspan', '1')))
            except ValueError as exc:
                raise PackingError('invalid HTML table span') from exc
            if not all(0 < value <= 1000 for value in self.spans):
                raise PackingError('invalid or excessive HTML table span')
            self.cell, self.header = [], tag == 'th'
        elif tag == 'tr':
            self.row = []
        elif tag in {'br', 'p', 'div'} and self.cell:
            self.cell.append('<br>')

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == 'table':
            self.depth -= 1
        if tag in {'td', 'th'} and self.cell is not None and self.row is not None:
            self.row.append((''.join(self.cell).strip(), *self.spans, self.header))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def _expand_html_table(value: str):
    """Expand one HTML table into ``(grid, header_rows, regions)``.

    ``grid`` maps 0-based ``(row, column)`` to the text that owns that position,
    and ``regions`` lists every explicit ``rowspan``/``colspan`` box as a 1-based
    inclusive ``(row_start, column_start, row_end, column_end)``.

    A rectangle is lossy about spans: the positions a span covers are written as
    empty text, so they become indistinguishable from cells the source really
    left blank.  Callers that keep the geometry (``table_artifact`` stores it as
    cell merges) use ``regions`` for exactly that reason -- they never guess the
    geometry back from the blanks.
    """
    parser = _HTMLTable()
    parser.feed(value)
    grid, header_rows, regions = {}, 0, []
    for r, row in enumerate(parser.rows):
        if r == header_rows and row and all(cell[3] for cell in row):
            header_rows += 1
        col = 0
        for text, rowspan, colspan, _ in row:
            while (r, col) in grid:
                col += 1
            for y in range(r, r + rowspan):
                for x in range(col, col + colspan):
                    if (y, x) in grid:
                        raise PackingError('overlapping explicit HTML table spans')
                    grid[y, x] = text
            if rowspan > 1 or colspan > 1:
                regions.append((r + 1, col + 1, r + rowspan, col + colspan))
            col += colspan
    if not grid:
        raise PackingError('HTML table has no cells')
    height = max(r for r, c in grid) + 1
    width = max(c for r, c in grid) + 1
    if height != len(parser.rows) or height * width > 1_000_000:
        raise PackingError('HTML table span exceeds actual rows or grid safety limit')
    if any((r, c) not in grid for r in range(height) for c in range(width)):
        raise PackingError('HTML table has an incomplete grid')
    header_rows = max(1, header_rows)
    return grid, header_rows, regions


def html_table_regions(value: str) -> list[tuple[int, int, int, int]]:
    """Explicit spans of one HTML table as 1-based inclusive bounding boxes.

    Empty for a table without spans; a table whose geometry cannot be expanded
    raises ``PackingError`` exactly like ``html_table_to_markdown`` does, so a
    caller that already parsed the markdown never sees a different verdict.
    """
    _grid, _header_rows, regions = _expand_html_table(value)
    return regions


def html_table_to_markdown(value: str) -> str:
    grid, header_rows, _regions = _expand_html_table(value)
    width = max(c for r, c in grid) + 1
    height = max(r for r, c in grid) + 1
    headers = [' / '.join(dict.fromkeys(grid[r, c] for r in range(header_rows))) for c in range(width)]

    def render(row):
        return '| ' + ' | '.join(
            cell.replace('|', r'\|').replace('\n', '<br>').replace('\r', '') for cell in row) + ' |\n'
    return render(headers) + render(['---'] * width) + ''.join(
        render([grid[r, c] for c in range(width)]) for r in range(header_rows, height))
